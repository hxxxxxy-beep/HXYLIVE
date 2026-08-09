"""Discover ranking wire: viewers_desc multi_page_global pools.

- Chaturbate: original B4 path (is_b4_ranking_eligible)
- Bilibili + aggregate All: same pool/slice contract (is_global_ranking_eligible)
Budget: 10 upstream pages / 240 ranked rooms (see RankingPoolBudget defaults).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from .discover_ranking import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_POOL_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DiscoverRankingService,
    PageFetcher,
    RankingPoolBudget,
    RankingPoolError,
    RankingPoolExpired,
    RankingPoolFilterMismatch,
    RankingPoolNotFound,
    RankingPoolPageOutOfRange,
    RankingPoolSortMismatch,
    RankingPoolSourceMismatch,
)
from .discover_ranking_chaturbate import (
    SOURCE as CHATURBATE_SOURCE,
    ChaturbateRankingAdapterError,
    build_chaturbate_ranking_pool,
    continue_chaturbate_ranking_pool,
    make_chaturbate_page_fetcher,
    wrap_chaturbate_api_roomlist,
)
from .discover_ranking_providers import (
    RankingProviderAdapterError,
    make_aggregate_page_fetcher,
    make_provider_page_fetcher,
)
from .discover_ranking_types import (
    CONTRACT_VERSION,
    DiscoverSortRequest,
    RankingMode,
    annotate_model_viewer_fields,
    derive_response_viewer_count_reliable,
)

# Exact opt-in only — never use sorts_by_viewers().
_B4_SORT = DiscoverSortRequest.VIEWERS_DESC.value
_B4_SOURCE = CHATURBATE_SOURCE
_SINGLE_RANKING_SOURCES = frozenset({"chaturbate", "bilibili"})
_BATCH_CONTINUE_REASONS = frozenset({"max_pages", "pool_limit", "max_requests", "timeout"})

_GENDER_TO_CATEGORY = {
    "female": "female",
    "f": "female",
    "male": "male",
    "m": "male",
    "couple": "couple",
    "couples": "couple",
    "trans": "trans",
    "ts": "trans",
    "transgender": "trans",
}

_ranking_service: Optional[DiscoverRankingService] = None
_page_fetcher_override: Optional[PageFetcher] = None


def default_ranking_budget() -> RankingPoolBudget:
    return RankingPoolBudget(
        max_pages=DEFAULT_MAX_PAGES,
        max_requests=DEFAULT_MAX_REQUESTS,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        pool_limit=DEFAULT_POOL_LIMIT,
    )


def is_b4_ranking_eligible(source: Optional[str], sort: Optional[str]) -> bool:
    """True only for explicit source=chaturbate AND sort=viewers_desc.

    Omitting sort, default Query viewers, sort=viewers, source_default,
    and any non-chaturbate source must return False.
    """
    src = str(source or "").strip().lower()
    sort_token = str(sort or "").strip().lower()
    return src == _B4_SOURCE and sort_token == _B4_SORT


# Disabled: multi_page_global frozen pools (10 pages / ~240) made All/CB/Bili
# Discover page-1 noticeably slow. Revert to provider page_local path.
# Ranking modules remain for possible future re-enable.
_GLOBAL_VIEWER_RANKING_ENABLED = False


def is_global_ranking_eligible(
    source: Optional[str],
    sort: Optional[str],
    search: Optional[str] = None,
) -> bool:
    """Formerly: viewers_desc pools for Chaturbate, Bilibili, or aggregate All.

    Always False while ``_GLOBAL_VIEWER_RANKING_ENABLED`` is off — Discover uses
    the legacy page_local path (fast provider pages + local viewers sort).
    """
    if not _GLOBAL_VIEWER_RANKING_ENABLED:
        return False
    if str(search or "").strip():
        return False
    sort_token = str(sort or "").strip().lower()
    if sort_token != _B4_SORT:
        return False
    src = str(source or "").strip().lower()
    if not src or src == "all":
        return True
    if "," in src:
        return True
    return src in _SINGLE_RANKING_SOURCES


def ranking_pool_source_key(source: Optional[str]) -> str:
    src = str(source or "").strip().lower()
    if not src or src == "all":
        return "all"
    if "," in src:
        parts = sorted({p.strip() for p in src.split(",") if p.strip()})
        return ",".join(parts) if parts else "all"
    return src


def get_ranking_service() -> DiscoverRankingService:
    global _ranking_service
    if _ranking_service is None:
        _ranking_service = DiscoverRankingService()
    return _ranking_service


def reset_ranking_wire_for_tests(
    *,
    service: Optional[DiscoverRankingService] = None,
    page_fetcher: Optional[PageFetcher] = None,
    clear_override: bool = False,
) -> None:
    """Test hook: inject service / fetcher; never used by production paths."""
    global _ranking_service, _page_fetcher_override
    _ranking_service = service if service is not None else DiscoverRankingService()
    if clear_override:
        _page_fetcher_override = None
    if page_fetcher is not None:
        _page_fetcher_override = page_fetcher


def canonical_category_from_gender(gender: Optional[str]) -> str:
    token = str(gender or "").strip().lower()
    if not token or token == "all":
        return "all"
    return _GENDER_TO_CATEGORY.get(token, token)


def ranking_pool_http_status(error: RankingPoolError) -> int:
    if isinstance(error, RankingPoolExpired):
        return 410
    if isinstance(error, RankingPoolNotFound):
        return 404
    if isinstance(
        error,
        (
            RankingPoolSourceMismatch,
            RankingPoolSortMismatch,
            RankingPoolFilterMismatch,
        ),
    ):
        return 409
    if isinstance(error, RankingPoolPageOutOfRange):
        return 400
    return 400


def raise_ranking_pool_http(error: RankingPoolError) -> None:
    payload = error.to_dict()
    raise HTTPException(status_code=ranking_pool_http_status(error), detail=payload)


def _pool_id_required_http() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": "ranking_pool_id_required",
            "message": "pool_id is required for page>=2 on viewers_desc ranking.",
            "pool_id": None,
            "restart_from_page": 1,
            "retryable": False,
        },
    )


def _adapter_failure_http(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": "ranking_pool_upstream_failed",
            "message": str(exc) or "ranking pool upstream failed",
            "pool_id": None,
            "restart_from_page": 1,
            "retryable": True,
        },
    )


def _next_batch_meta(snapshot: Any, *, ranking_start_page: int) -> Dict[str, Any]:
    start_i = max(1, int(ranking_start_page or 1))
    pages_scanned = int(getattr(snapshot, "pages_scanned", 0) or 0)
    is_complete = bool(getattr(snapshot, "is_complete", True))
    partial_reason = getattr(snapshot, "partial_reason", None)
    next_start = None
    if (
        not is_complete
        and pages_scanned > 0
        and str(partial_reason or "") in _BATCH_CONTINUE_REASONS
    ):
        next_start = start_i + pages_scanned
    return {
        "ranking_start_page": start_i,
        "next_batch_start_page": next_start,
        "has_more_batches": next_start is not None,
    }


async def _resolve_chaturbate_fetch_page(
    *,
    gender: str,
    search: str,
    tag: str,
    chaturbate_api: Any,
    provider: Any,
) -> PageFetcher:
    if _page_fetcher_override is not None:
        return _page_fetcher_override
    if chaturbate_api is not None:
        return wrap_chaturbate_api_roomlist(
            chaturbate_api,
            gender=gender,
            search=search,
            tag=tag,
        )
    if provider is not None and hasattr(provider, "list_live_models"):

        async def roomlist_page(**kwargs: Any) -> Dict[str, Any]:
            page = int(kwargs.get("page") or 1)
            limit = int(kwargs.get("limit") or 24)
            data = await provider.list_live_models(
                page=page,
                limit=limit,
                gender=kwargs.get("gender") or gender or "",
                search=kwargs.get("search") or search or "",
                tag=kwargs.get("tag") or tag or "",
                tags=[tag] if tag else None,
            )
            if not isinstance(data, dict):
                raise ChaturbateRankingAdapterError("provider list_live_models returned non-object")
            return data

        return make_chaturbate_page_fetcher(
            roomlist_page,
            gender=gender,
            search=search,
            tag=tag,
        )
    raise ChaturbateRankingAdapterError("chaturbate api/provider unavailable for ranking pool")


def _serialize_ranking_meta(
    snapshot_or_slice: Dict[str, Any],
    *,
    snapshot: Any = None,
    ranking_start_page: int = 1,
) -> Dict[str, Any]:
    snap = snapshot
    batch = _next_batch_meta(snap or snapshot_or_slice, ranking_start_page=ranking_start_page)
    return {
        "requested_sort": _B4_SORT,
        "ranking_mode": RankingMode.MULTI_PAGE_GLOBAL.value,
        "pool_id": snapshot_or_slice.get("pool_id") or (getattr(snap, "pool_id", None) if snap else None),
        "filters_hash": snapshot_or_slice.get("filters_hash")
        or (getattr(snap, "filters_hash", None) if snap else None),
        "generated_at": getattr(snap, "generated_at", None) if snap else snapshot_or_slice.get("generated_at"),
        "expires_at": getattr(snap, "expires_at", None) if snap else snapshot_or_slice.get("expires_at"),
        "candidate_count": snapshot_or_slice.get("candidate_count")
        if snapshot_or_slice.get("candidate_count") is not None
        else (getattr(snap, "candidate_count", None) if snap else None),
        "pages_scanned": getattr(snap, "pages_scanned", None) if snap else snapshot_or_slice.get("pages_scanned"),
        "requests_used": getattr(snap, "requests_used", None) if snap else snapshot_or_slice.get("requests_used"),
        "is_complete": snapshot_or_slice.get("is_complete")
        if "is_complete" in snapshot_or_slice
        else (getattr(snap, "is_complete", None) if snap else None),
        "partial_reason": snapshot_or_slice.get("partial_reason")
        if "partial_reason" in snapshot_or_slice
        else (getattr(snap, "partial_reason", None) if snap else None),
        **batch,
    }


async def _attach_follow_flags(models: List[Dict[str, Any]], db: Any) -> None:
    if not db or not models:
        return
    try:
        tracked_models = await db.get_all_models()
        tracked_set = {
            (m["username"], m.get("source_type") or "chaturbate")
            for m in tracked_models
        }
        followed_models = await db.get_all_followed()
        followed_set = {
            (m["username"], m.get("source_type") or "chaturbate")
            for m in followed_models
        }
        for model in models:
            key = (model["username"], model.get("source_type") or "chaturbate")
            model["isTracked"] = key in tracked_set
            model["isFollowed"] = key in followed_set
    except Exception:
        return


def _finalize_models(
    models: Sequence[Dict[str, Any]],
    *,
    default_source: str = _B4_SOURCE,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in models:
        item = dict(raw)
        item["source_type"] = item.get("source_type") or default_source
        annotate_model_viewer_fields(item)
        out.append(item)
    return out


def build_b4_discover_payload(
    *,
    sliced: Dict[str, Any],
    snapshot: Any,
    page: int,
    limit: int,
    provider_statuses: Optional[List[Dict[str, Any]]] = None,
    default_source: str = _B4_SOURCE,
    ranking_start_page: int = 1,
    pool_has_more: Optional[bool] = None,
) -> Dict[str, Any]:
    models = _finalize_models(sliced.get("models") or [], default_source=default_source)
    candidate_count = int(sliced.get("candidate_count") or getattr(snapshot, "candidate_count", 0) or 0)
    total_pages = max(1, int(math.ceil(candidate_count / float(limit))) if limit else 1)
    ranking = _serialize_ranking_meta(
        sliced,
        snapshot=snapshot,
        ranking_start_page=ranking_start_page,
    )
    slice_has_more = bool(sliced.get("has_more"))
    has_more_batches = bool(ranking.get("has_more_batches"))
    if pool_has_more is not None:
        has_more = bool(pool_has_more)
    else:
        has_more = slice_has_more or has_more_batches
    payload: Dict[str, Any] = {
        "models": models,
        "total": candidate_count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "has_more": has_more,
        "pool_has_more": slice_has_more,
        "supported": True,
        "unsupported_reason": None,
        "provider_statuses": provider_statuses or [],
        "contract_version": CONTRACT_VERSION,
        "sort": _B4_SORT,
        "requested_sort": _B4_SORT,
        "ranking_mode": RankingMode.MULTI_PAGE_GLOBAL.value,
        "pool_id": ranking["pool_id"],
        "filters_hash": ranking["filters_hash"],
        "generated_at": ranking["generated_at"],
        "expires_at": ranking["expires_at"],
        "candidate_count": ranking["candidate_count"],
        "pages_scanned": ranking["pages_scanned"],
        "requests_used": ranking["requests_used"],
        "is_complete": ranking["is_complete"],
        "partial_reason": ranking["partial_reason"],
        "ranking_start_page": ranking["ranking_start_page"],
        "next_batch_start_page": ranking["next_batch_start_page"],
        "has_more_batches": has_more_batches,
        "ranking": ranking,
        "viewer_count_reliable": derive_response_viewer_count_reliable(models),
    }
    return payload


async def handle_b4_discover(
    *,
    page: int,
    limit: int,
    pool_id: Optional[str] = None,
    gender: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    language: str = "",
    sort: str = _B4_SORT,
    chaturbate_api: Any = None,
    provider: Any = None,
    db: Any = None,
    ranking_start_page: int = 1,
) -> Dict[str, Any]:
    """Execute Chaturbate multi_page_global discover (caller already gated eligibility)."""
    if not is_b4_ranking_eligible(_B4_SOURCE, sort):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ranking_pool_not_eligible",
                "message": "B4 ranking requires source=chaturbate and sort=viewers_desc.",
                "pool_id": pool_id,
                "restart_from_page": 1,
                "retryable": False,
            },
        )

    category = canonical_category_from_gender(gender)
    search_norm = str(search or "").strip().lower()
    tag_list = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    first_tag = tag_list[0] if tag_list else ""
    gender_for_api = "" if category == "all" else category
    service = get_ranking_service()
    page_i = max(1, int(page or 1))
    limit_i = max(1, min(100, int(limit or 24)))
    start_i = max(1, int(ranking_start_page or 1))

    try:
        if page_i >= 2:
            if not pool_id or not str(pool_id).strip():
                raise _pool_id_required_http()
            extra = {"ranking_start_page": start_i} if start_i > 1 else None
            sliced = continue_chaturbate_ranking_pool(
                service=service,
                pool_id=str(pool_id).strip(),
                page=page_i,
                limit=limit_i,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
                extra_filters=extra,
            )
            snapshot = service.require_snapshot(str(pool_id).strip())
        else:
            fetch_page = await _resolve_chaturbate_fetch_page(
                gender=gender_for_api,
                search=search_norm,
                tag=first_tag,
                chaturbate_api=chaturbate_api,
                provider=provider,
            )
            snapshot = await build_chaturbate_ranking_pool(
                service=service,
                fetch_page=fetch_page,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
                budget=default_ranking_budget(),
                page_size=limit_i,
                start_page=start_i,
            )
            sliced = continue_chaturbate_ranking_pool(
                service=service,
                pool_id=snapshot.pool_id,
                page=1,
                limit=limit_i,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
            )
    except HTTPException:
        raise
    except RankingPoolError as exc:
        raise_ranking_pool_http(exc)
    except ChaturbateRankingAdapterError as exc:
        raise _adapter_failure_http(exc) from exc
    except Exception as exc:
        raise _adapter_failure_http(exc) from exc

    statuses = [
        {
            "source_type": _B4_SOURCE,
            "display_name": "Chaturbate",
            "status": "ok",
            "detail": None,
            "count": len(sliced.get("models") or []),
            "total": int(sliced.get("candidate_count") or snapshot.candidate_count or 0),
        }
    ]
    payload = build_b4_discover_payload(
        sliced=sliced,
        snapshot=snapshot,
        page=page_i,
        limit=limit_i,
        provider_statuses=statuses,
        default_source=_B4_SOURCE,
        ranking_start_page=start_i,
    )
    await _attach_follow_flags(payload["models"], db)
    return payload


async def handle_global_ranking_discover(
    *,
    source: Optional[str],
    page: int,
    limit: int,
    pool_id: Optional[str] = None,
    gender: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    language: str = "",
    sort: str = _B4_SORT,
    chaturbate_api: Any = None,
    providers: Optional[Sequence[Any]] = None,
    db: Any = None,
    ranking_start_page: int = 1,
    game_id: Optional[str] = None,
    parent_area_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Multi_page_global discover for chaturbate / bilibili / aggregate All."""
    if not is_global_ranking_eligible(source, sort, search):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ranking_pool_not_eligible",
                "message": "Global ranking requires sort=viewers_desc for chaturbate, bilibili, or All (no search).",
                "pool_id": pool_id,
                "restart_from_page": 1,
                "retryable": False,
            },
        )

    pool_source = ranking_pool_source_key(source)
    if pool_source == "chaturbate":
        cb_provider = None
        for provider in providers or []:
            if getattr(provider, "source_type", "") == "chaturbate":
                cb_provider = provider
                break
        return await handle_b4_discover(
            page=page,
            limit=limit,
            pool_id=pool_id,
            gender=gender,
            search=search,
            tags=tags,
            language=language,
            sort=sort,
            chaturbate_api=chaturbate_api,
            provider=cb_provider,
            db=db,
            ranking_start_page=ranking_start_page,
        )

    category = canonical_category_from_gender(gender)
    # Aggregate All ignores site-native gender filters.
    if pool_source == "all" or "," in pool_source:
        category = "all"
        gender_for_api = ""
    else:
        gender_for_api = "" if category == "all" else category

    search_norm = str(search or "").strip().lower()
    tag_list = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    service = get_ranking_service()
    page_i = max(1, int(page or 1))
    limit_i = max(1, min(100, int(limit or 24)))
    start_i = max(1, int(ranking_start_page or 1))
    provider_list = list(providers or [])
    default_source = pool_source if pool_source in _SINGLE_RANKING_SOURCES else "chaturbate"

    try:
        if page_i >= 2:
            if not pool_id or not str(pool_id).strip():
                raise _pool_id_required_http()
            extra = {"ranking_start_page": start_i} if start_i > 1 else None
            sliced = service.slice_snapshot(
                str(pool_id).strip(),
                page=page_i,
                limit=limit_i,
                source=pool_source,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
                extra_filters=extra,
            )
            snapshot = service.require_snapshot(str(pool_id).strip())
        else:
            if _page_fetcher_override is not None:
                fetch_page = _page_fetcher_override
            elif pool_source == "bilibili":
                bili = next(
                    (p for p in provider_list if getattr(p, "source_type", "") == "bilibili"),
                    None,
                )
                if bili is None:
                    raise RankingProviderAdapterError("bilibili provider unavailable")
                fetch_page = make_provider_page_fetcher(
                    bili,
                    default_source="bilibili",
                    gender=gender_for_api,
                    search=search_norm,
                    tags=tag_list,
                    parent_area_id=parent_area_id,
                )
            else:
                if not provider_list:
                    raise RankingProviderAdapterError("no providers available for aggregate ranking")
                fetch_page = make_aggregate_page_fetcher(
                    provider_list,
                    gender=gender_for_api,
                    search=search_norm,
                    tags=tag_list,
                    game_id=game_id,
                    parent_area_id=parent_area_id,
                )
            snapshot = await service.build_pool(
                source=pool_source,
                fetch_page=fetch_page,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
                budget=default_ranking_budget(),
                ranking_mode=RankingMode.MULTI_PAGE_GLOBAL.value,
                page_size=limit_i,
                start_page=start_i,
            )
            sliced = service.slice_snapshot(
                snapshot.pool_id,
                page=1,
                limit=limit_i,
                source=pool_source,
                canonical_category=category,
                language=language,
                tags=tag_list,
                sort=_B4_SORT,
                search=search_norm,
                extra_filters={"ranking_start_page": start_i} if start_i > 1 else None,
            )
    except HTTPException:
        raise
    except RankingPoolError as exc:
        raise_ranking_pool_http(exc)
    except (RankingProviderAdapterError, ChaturbateRankingAdapterError) as exc:
        raise _adapter_failure_http(exc) from exc
    except Exception as exc:
        raise _adapter_failure_http(exc) from exc

    statuses = []
    for provider in provider_list:
        src = getattr(provider, "source_type", "") or "unknown"
        count = sum(
            1
            for m in (sliced.get("models") or [])
            if (m.get("source_type") or default_source) == src
        )
        statuses.append(
            {
                "source_type": src,
                "display_name": getattr(provider, "display_name", None) or src,
                "status": "ok",
                "detail": None,
                "count": count,
                "total": int(sliced.get("candidate_count") or snapshot.candidate_count or 0),
            }
        )
    if not statuses:
        statuses = [
            {
                "source_type": pool_source,
                "display_name": pool_source,
                "status": "ok",
                "detail": None,
                "count": len(sliced.get("models") or []),
                "total": int(sliced.get("candidate_count") or snapshot.candidate_count or 0),
            }
        ]

    payload = build_b4_discover_payload(
        sliced=sliced,
        snapshot=snapshot,
        page=page_i,
        limit=limit_i,
        provider_statuses=statuses,
        default_source=default_source,
        ranking_start_page=start_i,
    )
    await _attach_follow_flags(payload["models"], db)
    return payload

"""B3 Chaturbate multi-page ranking adapter (local / test bypass only).

NOT wired into /api/discover. Production Discover must not import this module.
Uses injected roomlist page sources (fixtures or optional ChaturbateAPI wrap).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from .discover_ranking import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_TIMEOUT_SECONDS,
    DiscoverRankingService,
    PageFetcher,
    RankingPoolBudget,
    RankingSnapshot,
)
from .discover_ranking_types import DiscoverSortRequest, RankingMode

SOURCE = "chaturbate"

# Discover viewers_desc pool budget (10 pages × 24 ≈ 240 ranked rooms).
B3_MAX_PAGES = 10
B3_MAX_REQUESTS = 10
B3_TIMEOUT_SECONDS = 60.0

RoomlistPageFn = Callable[..., Awaitable[Union[Sequence[Dict[str, Any]], Dict[str, Any]]]]


class ChaturbateRankingAdapterError(RuntimeError):
    """Controlled adapter failure (no silent inventing of pool completeness)."""


def attach_chaturbate_num_users_evidence(model: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure num_users exact evidence is present for B1.1 annotation."""
    out = dict(model)
    out["source_type"] = out.get("source_type") or SOURCE

    num_users = out.get("num_users")
    if num_users is None or num_users == "":
        # Roomlist path historically maps viewers ← num_users; restore only when
        # callers mark roomlist provenance or already supply an int viewers value
        # alongside an explicit roomlist source hint.
        if out.get("viewer_count_source") == "num_users" or out.get("_roomlist_num_users") is not None:
            num_users = out.get("_roomlist_num_users", out.get("viewers"))
        elif "num_users" in out:
            num_users = out.get("num_users")

    if num_users is not None and num_users != "":
        try:
            n = max(0, int(num_users))
        except (TypeError, ValueError) as exc:
            raise ChaturbateRankingAdapterError(
                f"invalid num_users for {out.get('username')!r}"
            ) from exc
        out["num_users"] = n
        out["viewers"] = n
        out["viewer_count_raw"] = n
        out["viewer_count_source"] = "num_users"
        out["viewer_count_precision_hint"] = "exact"
        out["viewer_count_present"] = True
    out.pop("_roomlist_num_users", None)
    return out


def model_from_roomlist_room(room: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a raw Chaturbate roomlist room dict, preserving num_users evidence."""
    if not isinstance(room, dict):
        return None
    username = str(room.get("username") or "").strip()
    if not username:
        return None

    if "num_users" in room and room.get("num_users") is not None and room.get("num_users") != "":
        raw_users = room.get("num_users")
    else:
        raw_users = room.get("viewers", room.get("num_viewers"))

    thumb = (
        room.get("img")
        or room.get("thumbnail")
        or room.get("thumbnail_url")
        or room.get("thumbnailUrl")
        or ""
    )
    if isinstance(thumb, str) and thumb.startswith("//"):
        thumb = "https:" + thumb

    model = {
        "username": username,
        "display_name": str(room.get("display_name") or username),
        "is_online": True,
        "thumbnail_url": thumb,
        "room_status": str(room.get("current_show") or room.get("room_status") or "public"),
        "tags": list(room.get("tags") or []),
        "subject": str(room.get("room_subject") or room.get("subject") or ""),
        "gender": room.get("gender") or "",
        "source_type": SOURCE,
        "_roomlist_num_users": raw_users,
        "viewer_count_source": "num_users",
    }
    if raw_users is not None and raw_users != "":
        try:
            model["num_users"] = max(0, int(raw_users))
        except (TypeError, ValueError):
            pass
    return attach_chaturbate_num_users_evidence(model)


def models_from_roomlist_payload(payload: Any) -> List[Dict[str, Any]]:
    """Extract models from a roomlist JSON object or a bare rooms list."""
    if payload is None:
        return []
    if isinstance(payload, list):
        rooms = payload
    elif isinstance(payload, dict):
        if "models" in payload and isinstance(payload.get("models"), list):
            # Already-normalized page result (e.g. ChaturbateAPI.get_live_models).
            out: List[Dict[str, Any]] = []
            for item in payload["models"]:
                if not isinstance(item, dict):
                    continue
                enriched = dict(item)
                enriched["source_type"] = enriched.get("source_type") or SOURCE
                # get_live_models drops num_users; restore from viewers when this
                # adapter is the only caller and roomlist mapping is assumed.
                if "num_users" not in enriched or enriched.get("num_users") in (None, ""):
                    if enriched.get("viewers") is not None:
                        enriched["num_users"] = enriched.get("viewers")
                        enriched["viewer_count_source"] = "num_users"
                out.append(attach_chaturbate_num_users_evidence(enriched))
            return out
        rooms = payload.get("rooms", [])
        if isinstance(rooms, dict):
            for key in ("rooms", "results", "items"):
                nested = rooms.get(key)
                if isinstance(nested, list):
                    rooms = nested
                    break
            else:
                rooms = list(rooms.values())
    else:
        raise ChaturbateRankingAdapterError("roomlist payload type not supported")

    if not isinstance(rooms, list):
        raise ChaturbateRankingAdapterError("roomlist rooms is not a list")

    models: List[Dict[str, Any]] = []
    for room in rooms:
        model = model_from_roomlist_room(room)
        if model is not None:
            models.append(model)
    return models


def make_chaturbate_page_fetcher(
    roomlist_page: RoomlistPageFn,
    *,
    gender: str = "",
    search: str = "",
    tag: str = "",
) -> PageFetcher:
    """Build a PageFetcher for DiscoverRankingService from a roomlist page source.

    ``roomlist_page`` may accept (page, limit) or keyword args including
    offset/gender/search/tag. Return value: rooms list, roomlist JSON, or
    ``{"models": [...]}``.
    """

    async def fetch_page(page: int, limit: int) -> Sequence[Dict[str, Any]]:
        offset = max(0, (int(page) - 1) * int(limit))
        try:
            try:
                raw = await roomlist_page(
                    page=int(page),
                    limit=int(limit),
                    offset=offset,
                    gender=gender,
                    search=search,
                    tag=tag,
                )
            except TypeError:
                # Positional (page, limit) fixtures.
                raw = await roomlist_page(int(page), int(limit))
        except ChaturbateRankingAdapterError:
            raise
        except Exception as exc:
            raise ChaturbateRankingAdapterError(
                f"chaturbate roomlist page fetch failed (page={page})"
            ) from exc
        try:
            return models_from_roomlist_payload(raw)
        except ChaturbateRankingAdapterError:
            raise
        except Exception as exc:
            raise ChaturbateRankingAdapterError(
                f"chaturbate roomlist page parse failed (page={page})"
            ) from exc

    return fetch_page


def wrap_chaturbate_api_roomlist(
    api: Any,
    *,
    gender: str = "",
    search: str = "",
    tag: str = "",
) -> PageFetcher:
    """Thin wrap of ``ChaturbateAPI.get_live_models`` (limit/offset via page).

    Re-attaches ``num_users`` evidence from the roomlist→viewers mapping used by
    the API parser. Intended for local pilot / tests with a fake API object —
    production Discover must not call this.
    """

    async def roomlist_page(**kwargs: Any) -> Dict[str, Any]:
        page = int(kwargs.get("page") or 1)
        limit = int(kwargs.get("limit") or 24)
        if api is None or not hasattr(api, "get_live_models"):
            raise ChaturbateRankingAdapterError("chaturbate api unavailable")
        try:
            data = await api.get_live_models(
                page=page,
                limit=limit,
                gender=kwargs.get("gender") or gender or "",
                search=kwargs.get("search") or search or "",
                tag=kwargs.get("tag") or tag or "",
            )
        except Exception as exc:
            raise ChaturbateRankingAdapterError(
                f"chaturbate get_live_models failed (page={page})"
            ) from exc
        if not isinstance(data, dict):
            raise ChaturbateRankingAdapterError("chaturbate get_live_models returned non-object")
        return data

    return make_chaturbate_page_fetcher(
        roomlist_page,
        gender=gender,
        search=search,
        tag=tag,
    )


def clamp_b3_budget(budget: Optional[RankingPoolBudget] = None) -> RankingPoolBudget:
    base = budget or RankingPoolBudget(
        max_pages=B3_MAX_PAGES,
        max_requests=B3_MAX_REQUESTS,
        timeout_seconds=B3_TIMEOUT_SECONDS,
    )
    return RankingPoolBudget(
        max_pages=min(max(1, int(base.max_pages)), B3_MAX_PAGES),
        max_requests=min(max(1, int(base.max_requests)), B3_MAX_REQUESTS),
        timeout_seconds=min(max(0.01, float(base.timeout_seconds)), B3_TIMEOUT_SECONDS),
        pool_limit=max(1, int(base.pool_limit)),
    )


async def build_chaturbate_ranking_pool(
    *,
    service: DiscoverRankingService,
    fetch_page: PageFetcher,
    canonical_category: str = "all",
    language: str = "",
    tags: Optional[Sequence[str]] = None,
    sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
    search: str = "",
    budget: Optional[RankingPoolBudget] = None,
    page_size: int = 24,
    start_page: int = 1,
) -> RankingSnapshot:
    """Build a Chaturbate multi_page_global snapshot via B2 service (bypass only)."""
    capped = clamp_b3_budget(budget)
    try:
        snapshot = await service.build_pool(
            source=SOURCE,
            fetch_page=fetch_page,
            canonical_category=canonical_category,
            language=language,
            tags=tags,
            sort=sort,
            search=search,
            budget=capped,
            ranking_mode=RankingMode.MULTI_PAGE_GLOBAL.value,
            page_size=page_size,
            start_page=start_page,
        )
    except ChaturbateRankingAdapterError:
        raise
    except Exception as exc:
        raise ChaturbateRankingAdapterError("chaturbate ranking pool build failed") from exc

    # Discipline: only built pools may claim multi_page_global.
    if snapshot.ranking_mode != RankingMode.MULTI_PAGE_GLOBAL.value:
        raise ChaturbateRankingAdapterError(
            f"unexpected ranking_mode={snapshot.ranking_mode!r} on built pool"
        )
    if not snapshot.pool_id:
        raise ChaturbateRankingAdapterError("built pool missing pool_id")
    return snapshot


def continue_chaturbate_ranking_pool(
    *,
    service: DiscoverRankingService,
    pool_id: str,
    page: int,
    limit: int = 24,
    canonical_category: str = "all",
    language: str = "",
    tags: Optional[Sequence[str]] = None,
    sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
    search: str = "",
    filters_hash: Optional[str] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """B3.1 page>=2 path: snapshot slice only (no roomlist fetch / rebuild)."""
    return service.slice_snapshot(
        pool_id,
        page=page,
        limit=limit,
        source=SOURCE,
        canonical_category=canonical_category,
        language=language,
        tags=tags,
        sort=sort,
        search=search,
        filters_hash=filters_hash,
        extra_filters=extra_filters,
    )


# Defaults re-export for tests / docs (avoid drifting past authorization).
assert B3_MAX_PAGES <= 10
assert B3_MAX_REQUESTS <= 10
assert B3_TIMEOUT_SECONDS <= 60.0
assert DEFAULT_MAX_PAGES >= 1
assert DEFAULT_MAX_REQUESTS >= 1
assert DEFAULT_TIMEOUT_SECONDS >= 1.0

"""API Router: Discover live models across registered providers."""

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..logger import logger
from ..core.config import CHATURBATE_REQUEST_TIMEOUT_SECONDS
from ..discover_category_catalog import (
    CONTRACT_VERSION as CATEGORIES_CONTRACT_VERSION,
    DEFAULT_BILIBILI_PARENT_AREA_ID,
    DEFAULT_TWITCH_GAME_ID,
    build_categories_payload,
)
from ..services.twitch_categories import (
    list_twitch_content_categories,
    normalize_twitch_game_id,
)
from ..services.bilibili_categories import (
    list_bilibili_parent_areas,
    normalize_bilibili_parent_area_id,
)
from ..discover_gender_capabilities import (
    filter_providers_for_gender,
    is_gender_supported,
    unsupported_message,
    unsupported_reason,
    uses_finite_local_filter,
)
from ..services.discover_ranking_types import (
    annotate_model_viewer_fields,
    b1_discover_response_extras,
    normalize_sort_param,
    preserves_encounter_order,
    sorts_by_newest,
    sorts_by_viewers,
)

router = APIRouter(prefix="/api", tags=["discover"])


def _unsupported_discover_payload(
    *,
    page: int,
    limit: int,
    source_type: str,
    display_name: str,
    reason_code: str,
    sort_mode: str = "viewers",
) -> Dict[str, Any]:
    detail = unsupported_message(reason_code)
    payload = {
        "models": [],
        "total": 0,
        "page": page,
        "limit": limit,
        "total_pages": 1,
        "has_more": False,
        "supported": False,
        "unsupported_reason": reason_code,
        "provider_statuses": [
            {
                "source_type": source_type,
                "display_name": display_name,
                "status": "unsupported",
                "detail": detail,
                "count": 0,
                "total": 0,
            }
        ],
    }
    payload.update(
        b1_discover_response_extras(sort_mode=sort_mode, models=[], supported=False)
    )
    return payload

_chaturbate_api = None
_db = None
_provider_registry = None
_pagination_total_cache: dict[tuple, tuple[float, int, int]] = {}
_PAGINATION_TOTAL_TTL_SECONDS = 300
_PROVIDER_DISABLED_SETTING = "disabled_providers"
_DISCOVER_AGGREGATE_PROVIDER_TIMEOUT_SECONDS = 6.0
# A P4: Cams Male/Trans delivery is handled provider-side via
# Cams.com used a classify-first short-TTL pool historically; removed with
# non-Twitch/Chaturbate sources.
# No discover.py pagination redesign required for Cams gender pages.
_GENDER_ALIASES = {
    "female": "female",
    "f": "female",
    "females": "female",
    "girl": "female",
    "girls": "female",
    "woman": "female",
    "women": "female",
    "male": "male",
    "m": "male",
    "males": "male",
    "man": "male",
    "men": "male",
    "guy": "male",
    "guys": "male",
    "couple": "couple",
    "couples": "couple",
    "cpl": "couple",
    "maleFemale": "couple",
    "malefemale": "couple",
    "male_female_group": "couple",
    "malefemalegroup": "couple",
    "trans": "trans",
    "transgender": "trans",
    "ts": "trans",
    "tranny": "trans",
    "femaleTranny": "trans",
    "femaletranny": "trans",
    "transsexual": "trans",
}


def init(chaturbate_api, db, provider_registry=None):
    global _chaturbate_api, _db, _provider_registry
    _chaturbate_api = chaturbate_api
    _db = db
    _provider_registry = provider_registry
    _pagination_total_cache.clear()


async def _disabled_provider_sources() -> set[str]:
    if _db is None:
        return set()
    try:
        if hasattr(_db, "get_disabled_providers"):
            return set(await _db.get_disabled_providers())
        raw_value = await _db.get_setting(_PROVIDER_DISABLED_SETTING)
    except Exception:
        return set()
    try:
        parsed = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(source_type or "").strip().lower()
        for source_type in parsed
        if str(source_type or "").strip()
    }


def _discover_providers(source: Optional[str], disabled_sources: Optional[set[str]] = None) -> list:
    if _provider_registry is None:
        return []
    disabled_sources = disabled_sources or set()
    requested = [
        item.strip().lower()
        for item in (source or "").split(",")
        if item.strip()
    ]
    if requested:
        providers = []
        for source_type in requested:
            if source_type in disabled_sources:
                continue
            if _provider_registry.has(source_type):
                providers.append(_provider_registry.get(source_type))
        return providers
    return [
        provider
        for provider in _provider_registry.all()
        if getattr(provider.capabilities, "can_discover", False)
        and provider.source_type not in disabled_sources
    ]


def _pagination_cache_key(
    source: Optional[str],
    gender: Optional[str],
    search: str,
    tags: List[str],
    sort_mode: str,
    limit: int,
    disabled_sources: set[str],
    game_id: Optional[str] = None,
    parent_area_id: Optional[str] = None,
) -> tuple:
    requested_sources = tuple(
        item.strip().lower()
        for item in (source or "").split(",")
        if item.strip()
    )
    return (
        requested_sources or ("__all__",),
        (gender or "").strip().lower(),
        search,
        tuple(tags),
        sort_mode,
        int(limit),
        tuple(sorted(disabled_sources)),
        (game_id or "").strip(),
        (parent_area_id or "").strip(),
    )


def _timeout_setting(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _stable_pagination_totals(
    cache_key: tuple,
    page: int,
    total: int,
    total_pages: int,
) -> tuple[int, int]:
    now = time.monotonic()
    cached = _pagination_total_cache.get(cache_key)
    if cached and now - cached[0] >= _PAGINATION_TOTAL_TTL_SECONDS:
        cached = None
        _pagination_total_cache.pop(cache_key, None)

    if page <= 1 or cached is None:
        stable = (max(0, int(total)), max(1, int(total_pages or 1)))
        _pagination_total_cache[cache_key] = (now, stable[0], stable[1])
        return stable

    return cached[1], cached[2]


def _canonical_gender(value: object) -> Optional[str]:
    token = str(value or "").strip()
    if not token:
        return None
    compact = token.replace("_", "").replace("-", "").replace(" ", "")
    lowered = token.lower().replace("_", "-").strip()
    return (
        _GENDER_ALIASES.get(token)
        or _GENDER_ALIASES.get(compact)
        or _GENDER_ALIASES.get(lowered)
        or _GENDER_ALIASES.get(lowered.replace("-", ""))
    )


def _gender_tokens(values: List[object]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        canonical = _canonical_gender(value)
        if canonical:
            tokens.add(canonical)
    return tokens


def _matches_gender_filter(
    item: Dict[str, Any],
    item_tags: List[str],
    requested_gender: Optional[str],
) -> bool:
    requested = _canonical_gender(requested_gender)
    if not requested:
        return True

    tag_tokens = _gender_tokens(item_tags)
    # Room-theme tags (e.g. Stripchat couples catalogue) must not be blocked by
    # a performer-sex primary field such as genderGroup=F → gender=f.
    if requested == "couple" and "couple" in tag_tokens:
        return True

    primary_tokens = _gender_tokens([
        item.get("gender"),
        item.get("gender_group"),
        item.get("genderGroup"),
        item.get("broadcastGender"),
        item.get("category"),
        item.get("main_category"),
    ])
    if primary_tokens:
        return requested in primary_tokens

    if "trans" in tag_tokens and requested != "trans":
        return False
    if "couple" in tag_tokens and requested != "couple":
        return False
    return requested in tag_tokens


async def _fetch_provider(
    provider,
    page: int,
    limit: int,
    gender: Optional[str],
    search: Optional[str],
    tags: Optional[List[str]],
    allow_browser: bool,
    exact_search_fallback: bool,
    aggregate_mode: bool,
    game_id: Optional[str] = None,
    parent_area_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    timeout = 25 if allow_browser else 14
    if aggregate_mode:
        timeout = _timeout_setting(
            "HXYLIVE_DISCOVER_AGGREGATE_PROVIDER_TIMEOUT",
            _DISCOVER_AGGREGATE_PROVIDER_TIMEOUT_SECONDS,
        )
    elif getattr(provider, "source_type", "") == "chaturbate":
        timeout = max(timeout, CHATURBATE_REQUEST_TIMEOUT_SECONDS + (45 if allow_browser else 5))
    try:
        call_kwargs: Dict[str, Any] = {
            "page": page,
            "limit": limit,
            "gender": gender or "",
            "search": search or "",
            "tags": tags or [],
            "allow_browser": allow_browser,
            "exact_search_fallback": exact_search_fallback,
        }
        # Pass native game_id only to Twitch; never as gender=.
        if game_id and getattr(provider, "source_type", "") == "twitch":
            call_kwargs["game_id"] = game_id
        # Bilibili: pass native parent_area_id only to Bilibili.
        if parent_area_id and getattr(provider, "source_type", "") == "bilibili":
            call_kwargs["parent_area_id"] = parent_area_id
        return await asyncio.wait_for(
            provider.list_live_models(**call_kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Discover provider timeout",
            source_type=getattr(provider, "source_type", "unknown"),
            timeout=timeout,
        )
        return {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "provider_status": "timeout",
            "provider_detail": f"Provider timed out after {timeout:g}s.",
        }
    except Exception as e:
        logger.warning(
            "Discover provider failure",
            source_type=getattr(provider, "source_type", "unknown"),
            error=str(e),
        )
        return None


@router.get("/discover/categories")
async def discover_categories(
    source: str = Query(..., description="Provider source_type"),
):
    """Read-only per-source category capability (ab-shared-v1).

    Twitch content categories come from Helix top-games (isolated TTL cache).
    Bilibili content categories come from Area/getList parent areas.
    Does not consume Twitch C2 unique-pool cursors.
    """
    source_key = (source or "").strip().lower()
    if not source_key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_source",
                "message": "Query parameter source is required.",
                "contract_version": CATEGORIES_CONTRACT_VERSION,
            },
        )
    twitch_games = None
    bilibili_areas = None
    if source_key == "twitch":
        twitch_games = await list_twitch_content_categories()
    elif source_key == "bilibili":
        bilibili_areas = await list_bilibili_parent_areas()
    payload = build_categories_payload(
        source_key,
        twitch_games=twitch_games,
        bilibili_areas=bilibili_areas,
    )
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_source",
                "source": source_key,
                "message": "Source is not a known discover provider.",
                "contract_version": CATEGORIES_CONTRACT_VERSION,
                "categories": [],
                "ranking_hints": {
                    "supports_viewer_count": False,
                    "viewer_count_reliable": False,
                    "viewer_count_precision_default": "unverified",
                    "supported_sort_modes": [],
                    "ranking_modes": [],
                    "ranking_modes_available": [],
                    "evidence_source": None,
                    "implementation_status": "unsupported",
                },
            },
        )
    return payload


@router.get("/discover/profile-images")
async def discover_profile_images(
    source: str = Query(..., description="Provider source_type"),
    usernames: str = Query("", description="Comma-separated usernames"),
):
    """Resolve durable circular avatars for Discover cards (Chaturbate summaries).

    Roomlist covers stay on live ``riw`` frames; Media/Watch already use
    chatvideocontext ``summary_card_image``. Discover loads those asynchronously
    so the grid can paint without waiting on rate-limited lookups.
    """
    source_key = str(source or "").strip().lower()
    names = [
        part.strip().strip("/").lower()
        for part in str(usernames or "").split(",")
        if part.strip()
    ][:24]
    if not names:
        return {"source": source_key, "images": {}}
    if source_key != "chaturbate":
        return {"source": source_key, "images": {}}
    if _chaturbate_api is None or not hasattr(_chaturbate_api, "resolve_profile_images"):
        return {"source": source_key, "images": {}}
    try:
        images = await _chaturbate_api.resolve_profile_images(names)
    except Exception as exc:
        logger.warning("Discover profile-image resolve failed", error=str(exc))
        images = {}
    return {"source": source_key, "images": images or {}}


@router.get("/discover")
async def discover_models(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    source: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    game_id: Optional[str] = Query(
        None,
        description="Twitch Helix game_id content filter; never a gender alias",
    ),
    parent_area_id: Optional[str] = Query(
        None,
        description="Bilibili live parent_area_id content filter; never a gender alias",
    ),
    search: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    sort: Optional[str] = Query("viewers"),
):
    """Return public live rooms from one provider or an aggregate provider group."""
    included_tags: List[str] = []
    if tags:
        included_tags = [t.strip().lower() for t in tags.split(",") if t.strip()]

    search_lower = (search or "").strip().lower()
    effective_game_id = normalize_twitch_game_id(game_id)
    effective_parent_area_id = normalize_bilibili_parent_area_id(parent_area_id)
    source_key = (source or "").strip().lower()
    # Twitch/Bilibili no longer expose All — default to the primary partition.
    if source_key == "twitch" and not effective_game_id:
        effective_game_id = normalize_twitch_game_id(
            os.getenv("TWITCH_GAME_ID") or DEFAULT_TWITCH_GAME_ID
        )
    if source_key == "bilibili" and not effective_parent_area_id:
        effective_parent_area_id = DEFAULT_BILIBILI_PARENT_AREA_ID
    # Accept source_default / viewers_desc; viewers|newest keep prior meaning.
    # Default query remains sort=viewers → page-local viewers ordering.
    sort_mode = normalize_sort_param(sort)

    # Provider availability / disabled_providers first (shared gate; no second copy).
    disabled_sources = await _disabled_provider_sources()
    providers = _discover_providers(source, disabled_sources)
    if not providers:
        empty = {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "has_more": False,
            "supported": True,
            "unsupported_reason": None,
        }
        empty.update(b1_discover_response_extras(sort_mode=sort_mode, models=[], supported=True))
        return empty
    aggregate_source = False
    explicit_source = bool((source or "").strip()) and not aggregate_source

    # Explicit single-source unsupported gender: do not fetch or pretend empty inventory.
    if explicit_source and len(providers) == 1:
        only = providers[0]
        reason_code = unsupported_reason(only.source_type, gender)
        if reason_code:
            return _unsupported_discover_payload(
                page=page,
                limit=limit,
                source_type=only.source_type,
                display_name=only.display_name,
                reason_code=reason_code,
                sort_mode=sort_mode,
            )

    # Aggregate / multi-source: skip providers that cannot serve this gender.
    providers = filter_providers_for_gender(providers, gender)
    if not providers:
        # No provider can serve this gender for the requested source set.
        empty = {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "has_more": False,
            "supported": False,
            "unsupported_reason": unsupported_reason(
                (source or "").strip().lower() or "unknown", gender
            )
            or "no_reliable_gender_signal",
            "provider_statuses": [],
        }
        empty.update(b1_discover_response_extras(sort_mode=sort_mode, models=[], supported=False))
        return empty

    # Aggregate views fetch a stable amount from each provider before local
    # sorting and pagination so page totals remain consistent.
    per_source_limit = limit if explicit_source else 100
    provider_page = page if explicit_source else 1
    allow_browser = explicit_source
    aggregate_mode = not explicit_source

    results = await asyncio.gather(*[
        _fetch_provider(
            provider,
            provider_page,
            per_source_limit,
            gender,
            search,
            included_tags or None,
            allow_browser=allow_browser,
            exact_search_fallback=explicit_source,
            aggregate_mode=aggregate_mode,
            game_id=effective_game_id,
            parent_area_id=effective_parent_area_id,
        )
        for provider in providers
    ])

    blacklisted_tags: List[str] = []
    if _db:
        try:
            blacklisted_tags = await _db.get_blacklisted_tags()
        except Exception:
            blacklisted_tags = []
    blacklisted_set = {t.lower() for t in blacklisted_tags}

    capabilities = {
        provider.source_type: provider.capabilities
        for provider in providers
    }

    def _fallback_tags(item: Dict[str, Any]) -> List[str]:
        values = [
            item.get("gender"),
            item.get("room_status") or "public",
        ]
        age = item.get("age")
        try:
            age_value = int(age or 0)
        except (TypeError, ValueError):
            age_value = 0
        if 18 <= age_value <= 29:
            values.append("18-29")
        elif 30 <= age_value <= 39:
            values.append("30-39")
        elif 40 <= age_value <= 49:
            values.append("40-49")
        elif age_value >= 50:
            values.append("50+")
        seen = set()
        tags_out: List[str] = []
        for value in values:
            tag = str(value or "").strip().lower()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            tags_out.append(tag)
        return tags_out

    def _filter_list(result: Optional[Dict[str, Any]], source_type: str) -> List[Dict[str, Any]]:
        if result is None:
            return []
        out: List[Dict[str, Any]] = []
        for item in result.get("models", []):
            rs = (item.get("room_status") or "public").lower()
            username_lower = str(item.get("username") or "").strip().lower()
            # Search hit = needle appears as a contiguous substring in id/name
            # (e.g. xuanshen → xuanshen1, mazzanti → mazzanti_). Keep those
            # cards even when private / password / offline.
            offline_search_hit = False
            if search_lower:
                dname = str(item.get("display_name") or "").strip().lower()
                uid = str(item.get("user_id") or "").strip().lower()
                room_id = str(item.get("room_id") or "").strip().lower()
                short_id = str(item.get("short_id") or "").strip().lower()
                if source_type in {"twitch", "chaturbate", "stripchat"}:
                    if search_lower in username_lower or search_lower in dname:
                        offline_search_hit = True
                elif source_type == "bilibili" and (
                    search_lower in dname
                    or search_lower in username_lower
                    or search_lower == uid
                    or search_lower == room_id
                    or search_lower == short_id
                ):
                    offline_search_hit = True
            if not offline_search_hit and (rs != "public" or not item.get("is_online", True)):
                continue
            item_tags = [str(tag).strip().lower() for tag in (item.get("tags") or []) if str(tag).strip()]
            if not item_tags:
                item_tags = _fallback_tags(item)
            item_tags_lower = [t.lower() for t in item_tags]
            if not _matches_gender_filter(item, item_tags_lower, gender):
                continue
            if blacklisted_set and any(bt in item_tags_lower for bt in blacklisted_set):
                continue
            # Every requested tag must be present.
            if included_tags and not all(t in item_tags_lower for t in included_tags):
                continue
            # Search must match the username or display name.
            if search_lower:
                uname = (item.get("username") or "").lower()
                dname = (item.get("display_name") or "").lower()
                uid = str(item.get("user_id") or "").lower()
                room_id = str(item.get("room_id") or "").lower()
                short_id = str(item.get("short_id") or "").lower()
                if (
                    search_lower not in uname
                    and search_lower not in dname
                    and search_lower != uid
                    and search_lower != room_id
                    and search_lower != short_id
                ):
                    continue
            item = dict(item)
            item["tags"] = item_tags
            item["source_type"] = item.get("source_type") or source_type
            # Additive viewer_count*; keep viewers as non-negative int (missing → 0).
            annotate_model_viewer_fields(item)
            item_caps = capabilities.get(item["source_type"])
            stream_available = bool(getattr(item_caps, "can_stream", True))
            record_available = bool(getattr(item_caps, "can_record", stream_available))
            if not item.get("is_online", True):
                # Offline search hits are followable/discoverable, not currently playable.
                stream_available = False
            if rs in {
                "private",
                "p2p",
                "group",
                "ticket",
                "password_protected",
                "hidden",
                "offline",
            }:
                stream_available = False
            if (
                not explicit_source
                and not stream_available
                and item.get("is_online", True)
                and not offline_search_hit
            ):
                continue
            if not explicit_source and not item.get("is_online", True) and not offline_search_hit:
                continue
            item["stream_available"] = stream_available
            item["record_available"] = record_available
            item["can_follow"] = bool(
                getattr(item_caps, "can_follow", False) or stream_available or record_available
            )
            out.append(item)
        return out

    grouped_items: List[List[Dict[str, Any]]] = []
    for provider, result in zip(providers, results):
        grouped_items.append(_filter_list(result, provider.source_type))

    provider_statuses: List[Dict[str, Any]] = []
    for provider, result, items in zip(providers, results, grouped_items):
        if result is None:
            provider_statuses.append({
                "source_type": provider.source_type,
                "display_name": provider.display_name,
                "status": "error",
                "detail": "Provider did not return a Discover response.",
                "count": 0,
                "total": 0,
            })
            continue
        provider_can_stream = bool(getattr(provider.capabilities, "can_stream", True))
        provider_status = str(result.get("provider_status") or ("ok" if items else "empty"))
        provider_detail = str(result.get("provider_detail") or "")
        if not provider_can_stream and result.get("models"):
            provider_status = "discover_only"
            provider_detail = (
                provider_detail
                or "Discover is available, but this provider did not expose a public FFmpeg-readable stream."
            )
        provider_statuses.append({
            "source_type": provider.source_type,
            "display_name": provider.display_name,
            "status": provider_status,
            "detail": provider_detail,
            "count": len(items),
            "total": int(result.get("total") or 0),
        })

    plugin_totals = []
    plugin_total_pages = []
    for r in results:
        if r is None:
            continue
        plugin_totals.append(int(r.get("total") or 0))
        plugin_total_pages.append(int(r.get("total_pages") or 1))

    # Cursor-backed providers such as Twitch only know whether another page
    # exists. Their reported total_pages grows as each cursor is visited, so a
    # page-one total must not be treated as the final pagination boundary.
    provider_has_more = any(
        int(result.get("total_pages") or 1) > provider_page
        for result in results
        if result is not None
    )

    # viewers / viewers_desc keep this path; source_default preserves encounter
    # order (opt-in only — default query sort=viewers is unchanged).
    ranked_items = [item for group in grouped_items for item in group]
    # Browse-only: drop zero-viewer padding when any live room exists.
    # Search must keep exact offline hits (0 viewers) alongside live matches.
    if not explicit_source and sorts_by_viewers(sort_mode) and not search_lower:
        positive_items = [item for item in ranked_items if int(item.get("viewers") or 0) > 0]
        if positive_items:
            ranked_items = [
                item for item in ranked_items
                if int(item.get("viewers") or 0) > 0
            ]
    if sorts_by_newest(sort_mode):
        ranked_items.sort(key=lambda m: (m.get("age") or 99))
    elif preserves_encounter_order(sort_mode):
        pass
    elif search_lower:
        # Prefer exact → compact prefix (ti→tiffy) → other prefix → other contains.
        needle = search_lower
        max_compact = len(needle) + 4

        def _search_rank_key(m: Dict[str, Any]) -> tuple:
            uname = str(m.get("username") or "").strip().lower()
            dname = str(m.get("display_name") or "").strip().lower()

            def _compact(value: str) -> bool:
                return bool(value) and value.startswith(needle) and len(value) <= max_compact

            if uname == needle or dname == needle:
                tier = 4
            elif _compact(uname) or _compact(dname):
                tier = 3
            elif uname.startswith(needle) or dname.startswith(needle):
                tier = 2
            else:
                tier = 1
            online = 1 if m.get("is_online", True) else 0
            viewers = int(m.get("viewers") or 0)
            brevity = -min(len(uname) or 99, len(dname) or 99)
            return (tier, online, viewers, brevity)

        ranked_items.sort(key=_search_rank_key, reverse=True)
    else:
        # sort=viewers, sort=viewers_desc, and any unknown → viewers descending.
        ranked_items.sort(key=lambda m: int(m.get("viewers") or 0), reverse=True)

    if explicit_source:
        combined = ranked_items[:limit]
        has_more = provider_has_more
        # Finite homepage scrapes slice locally before discover gender-filters.
        # An empty filtered page must not keep advertising has_more (LiveJasmin Couple).
        if (
            gender
            and _canonical_gender(gender)
            and not combined
            and any(uses_finite_local_filter(p.source_type) for p in providers)
        ):
            has_more = False
        # Keyword/exact search miss: do not keep infinite-scrolling empty pages
        # when providers still advertise the unfiltered inventory total.
        if search_lower and not combined:
            has_more = False
    else:
        start = (page - 1) * limit
        combined = ranked_items[start:start + limit]
        has_more = start + len(combined) < len(ranked_items)

    total_combined = sum(plugin_totals)
    if explicit_source:
        total_pages = max(plugin_total_pages) if plugin_total_pages else 1
        if (
            gender
            and _canonical_gender(gender)
            and not combined
            and any(uses_finite_local_filter(p.source_type) for p in providers)
        ):
            # Keep schema-compatible stop: at least current page, no further pages.
            total_pages = max(1, int(page))
        if search_lower and not combined:
            total_pages = max(1, int(page))
            total_combined = 0
    else:
        total_combined = len(ranked_items)
        total_pages = max(1, (total_combined + limit - 1) // limit)

    # Scheme A (C2): Twitch/Bilibili unique pools grow with the pool.
    # Freezing page-1 estimates makes Discover page/has_more/total_pages
    # contradictory. Skip only for explicit Twitch/Bilibili (no gender);
    # leave other sources unchanged.
    growing_pool_skip_stable = (
        explicit_source
        and len(providers) == 1
        and getattr(providers[0], "source_type", "") in {"twitch", "bilibili"}
        and not _canonical_gender(gender)
    )
    if not growing_pool_skip_stable:
        total_combined, total_pages = _stable_pagination_totals(
            _pagination_cache_key(
                source,
                gender,
                search_lower,
                included_tags,
                sort_mode,
                limit,
                disabled_sources,
                effective_game_id,
                effective_parent_area_id,
            ),
            page,
            total_combined,
            total_pages,
        )

    if _db:
        try:
            tracked_models = await _db.get_all_models()
            tracked_set = {
                (m["username"], m.get("source_type") or "chaturbate")
                for m in tracked_models
            }
            followed_models = await _db.get_all_followed()
            followed_set = {
                (m["username"], m.get("source_type") or "chaturbate")
                for m in followed_models
            }
            for model in combined:
                key = (model["username"], model.get("source_type") or "chaturbate")
                model["isTracked"] = key in tracked_set
                model["isFollowed"] = key in followed_set
        except Exception:
            pass

    payload = {
        "models": combined,
        "total": total_combined,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "has_more": has_more,
        "supported": True,
        "unsupported_reason": None,
        "provider_statuses": provider_statuses,
    }
    payload.update(
        b1_discover_response_extras(
            sort_mode=sort_mode,
            models=combined,
            supported=True,
        )
    )
    return payload

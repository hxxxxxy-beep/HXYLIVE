"""Discover category taxonomy, contextual synonym maps, and categories API payloads.

Does not implement ranking pools or mutate /api/discover list semantics.
Classification must never use title, username, avatar, or pixels.

Field meanings:
- source_signal_present: upstream exposes a recognizable category signal
- reliability: quality of that signal only (not delivery acceptance)
- readiness: verified | experimental | not_ready | unsupported
- available: HXYLIVE implemented + accepted for formal frontend use
- request_param / request_value / filter_scope: how to query /api/discover
Formal ``categories`` lists only available=true items.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .discover_gender_capabilities import (
    CANONICAL_GENDERS,
    unsupported_reason,
)

CONTRACT_VERSION = "ab-shared-v1"
# Readiness plus request mapping (canonical_key / request_param / filter_scope).
SCHEMA_VERSION = "categories-request-v1"

FILTER_SCOPE_PRIMARY = "primary"
FILTER_SCOPE_SECONDARY = "secondary"

# Discover list API: gender (+ implicit all), Twitch game_id, Bilibili parent_area_id.
# Other request_param values must not be auto-sent by the frontend.
SUPPORTED_DISCOVER_REQUEST_PARAMS = frozenset({"gender", "game_id", "parent_area_id"})
CATEGORY_TYPE_CONTENT = "content"
FILTER_SCOPE_CONTENT = "content"

READINESS_VERIFIED = "verified"
READINESS_EXPERIMENTAL = "experimental"
READINESS_NOT_READY = "not_ready"
READINESS_UNSUPPORTED = "unsupported"
READINESS_VALUES = frozenset({
    READINESS_VERIFIED,
    READINESS_EXPERIMENTAL,
    READINESS_NOT_READY,
    READINESS_UNSUPPORTED,
})

# Canonical taxonomy keys used by the categories API (gender + all).
CANONICAL_CATEGORY_ALL = "all"
CANONICAL_TAXONOMY_GENDER = CANONICAL_GENDERS  # female, male, trans, couple

CATEGORY_TYPE_ALL = "all"
CATEGORY_TYPE_GENDER = "gender"

SOURCE_MODE_NATIVE = "native_categories"
SOURCE_MODE_STRUCTURED = "structured_filters"
SOURCE_MODE_UNSUPPORTED = "unsupported"

RELIABILITY_HIGH = "high"
RELIABILITY_MEDIUM = "medium"
RELIABILITY_NONE = "none"

# Known discover sources (must stay aligned with provider registry).
KNOWN_SOURCES = frozenset({
    "twitch",
    "chaturbate",
    "bilibili",
    "stripchat",
})

# Twitch / Bilibili expose native content partitions — no synthetic All pill.
# Chaturbate / Stripchat keep All + gender.
SOURCES_WITHOUT_ALL_CATEGORY = frozenset({"twitch", "bilibili"})

# Discover defaults when the client omits a content filter.
DEFAULT_TWITCH_GAME_ID = "509659"  # ASMR
DEFAULT_BILIBILI_PARENT_AREA_ID = "9"  # Bilibili "Virtual streamers" parent
DEFAULT_BILIBILI_PARENT_AREA_NAME = "Virtual streamers"

DISPLAY_NAMES = {
    "twitch": "Twitch",
    "chaturbate": "Chaturbate",
    "bilibili": "Bilibili",
    "stripchat": "Stripchat",
}

GENDER_LABELS = {
    "female": "Female",
    "male": "Male",
    "trans": "Trans",
    "couple": "Couple",
}


def _norm_raw(raw: object) -> str:
    return str(raw or "").strip().lower().replace("-", "").replace("_", "")


# Contextual synonym tables: (sources, field_name, data_locus) → raw→canonical.
# Every mapping is scoped by source set + field + locus. No title/username paths.
_CONTEXTUAL_SYNONYM_TABLES: Tuple[Dict[str, Any], ...] = (
    {
        "sources": frozenset({"chaturbate", "stripchat"}),
        "field_name": "gender",
        "data_locus": "structured_alias",
        "map": {
            "female": "female",
            "f": "female",
            "females": "female",
            "woman": "female",
            "women": "female",
            "girl": "female",
            "girls": "female",
            "male": "male",
            "m": "male",
            "males": "male",
            "man": "male",
            "men": "male",
            "guy": "male",
            "guys": "male",
            "trans": "trans",
            "transgender": "trans",
            "ts": "trans",
            "tranny": "trans",
            "transsexual": "trans",
            "couple": "couple",
            "couples": "couple",
            "duo": "couple",
            "cpl": "couple",
            "malefemale": "couple",
            "c": "couple",
        },
    },
    {
        "sources": frozenset({"stripchat"}),
        "field_name": "primaryTag",
        "data_locus": "stripchat_api",
        "map": {
            "girls": "female",
            "men": "male",
            "trans": "trans",
            "couples": "couple",
        },
    },
    {
        "sources": frozenset({"chaturbate"}),
        "field_name": "genders",
        "data_locus": "chaturbate_roomlist_api",
        "map": {
            "f": "female",
            "m": "male",
            "t": "trans",
            "c": "couple",
            "s": "trans",
        },
    },
)


def canonicalize_category_value(
    source: str,
    field_name: str,
    data_locus: str,
    raw_value: object,
) -> Optional[str]:
    """Map a source-scoped raw value to a canonical category key.

    Returns None when unmapped. Never inspects title/username/avatar.
    """
    source_key = (source or "").strip().lower()
    field = (field_name or "").strip()
    locus = (data_locus or "").strip()
    raw = _norm_raw(raw_value)
    if not source_key or not field or not locus or not raw:
        return None
    if raw in {"all", ""}:
        return CANONICAL_CATEGORY_ALL

    for table in _CONTEXTUAL_SYNONYM_TABLES:
        if source_key not in table["sources"]:
            continue
        if table["field_name"] != field:
            continue
        if table["data_locus"] != locus:
            continue
        hit = table["map"].get(raw)
        if hit:
            return hit
    return None


# Per-source gender specs. ``available`` gates the formal categories list.
# Signal presence and readiness are independent of frontend delivery.
_SOURCE_GENDER_SPECS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "chaturbate": {
        "female": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["f"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "genders",
            "data_locus": "chaturbate_roomlist_api",
        },
        "male": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["m"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "genders",
            "data_locus": "chaturbate_roomlist_api",
        },
        "trans": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["t", "s"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "genders",
            "data_locus": "chaturbate_roomlist_api",
        },
        "couple": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["c"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "genders",
            "data_locus": "chaturbate_roomlist_api",
        },
    },
    "stripchat": {
        "female": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["girls"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "primaryTag",
            "data_locus": "stripchat_api",
            "upstream_parameter": {"primaryTag": "girls"},
        },
        "male": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["men"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "primaryTag",
            "data_locus": "stripchat_api",
            "upstream_parameter": {"primaryTag": "men"},
        },
        "trans": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["trans"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "primaryTag",
            "data_locus": "stripchat_api",
            "upstream_parameter": {"primaryTag": "trans"},
        },
        "couple": {
            "source_mode": SOURCE_MODE_NATIVE,
            "source_values": ["couples"],
            "reliability": RELIABILITY_HIGH,
            "source_signal_present": True,
            "available": True,
            "readiness": READINESS_VERIFIED,
            "field_name": "primaryTag",
            "data_locus": "stripchat_api",
            "upstream_parameter": {"primaryTag": "couples"},
        },
    },
}


def _conservative_ranking_hints(source: str) -> Dict[str, Any]:
    """Default B-line projection: no invented viewer-count / global-rank support."""
    _ = (source or "").strip().lower()
    return {
        "supports_viewer_count": False,
        "viewer_count_reliable": False,
        "viewer_count_precision_default": "unverified",
        "supported_sort_modes": ["source_default"],
        "ranking_modes": [],
        "ranking_modes_available": [],
        "evidence_source": None,
        "implementation_status": "unsupported",
    }


def _chaturbate_ranking_hints() -> Dict[str, Any]:
    """Chaturbate Discover uses page_local only (global pool disabled)."""
    modes = ["page_local"]
    return {
        "supports_viewer_count": True,
        "viewer_count_reliable": True,
        "viewer_count_precision_default": "exact",
        "supported_sort_modes": ["source_default"],
        # Contract alias pair: ranking_modes + ranking_modes_available.
        "ranking_modes": list(modes),
        "ranking_modes_available": list(modes),
        "evidence_source": "num_users",
        "implementation_status": "verified",
        "evidence_note": (
            "Chaturbate roomlist num_users exact evidence; "
            "Discover stays page_local."
        ),
    }


def _bilibili_ranking_hints() -> Dict[str, Any]:
    modes = ["page_local"]
    return {
        "supports_viewer_count": True,
        "viewer_count_reliable": True,
        "viewer_count_precision_default": "exact",
        "supported_sort_modes": ["source_default"],
        "ranking_modes": list(modes),
        "ranking_modes_available": list(modes),
        "evidence_source": "audience_count",
        "implementation_status": "verified",
        "evidence_note": (
            "Bilibili room-audience evidence; Discover stays page_local "
            "(multi_page_global pool disabled)."
        ),
    }


# Published B-line ranking projections only. A must not invent entries here.
_RANKING_HINTS_BY_SOURCE: Dict[str, Dict[str, Any]] = {
    "chaturbate": _chaturbate_ranking_hints(),
    "bilibili": _bilibili_ranking_hints(),
}


def ranking_hints_for_source(source: str) -> Dict[str, Any]:
    """Return B capability projection for categories API (read-only)."""
    key = (source or "").strip().lower()
    published = _RANKING_HINTS_BY_SOURCE.get(key)
    if published is not None:
        return dict(published)
    return _conservative_ranking_hints(key)


def _request_mapping_for(category_type: str, canonical: str) -> Dict[str, Any]:
    """Declare how a category maps onto /api/discover query params."""
    ctype = (category_type or "").strip().lower()
    key = (canonical or "").strip().lower() or CANONICAL_CATEGORY_ALL
    if ctype == CATEGORY_TYPE_ALL or key == CANONICAL_CATEGORY_ALL:
        return {
            "request_param": None,
            "request_value": None,
            "filter_scope": FILTER_SCOPE_PRIMARY,
        }
    if ctype == CATEGORY_TYPE_GENDER:
        return {
            "request_param": "gender",
            "request_value": key,
            "filter_scope": FILTER_SCOPE_PRIMARY,
        }
    # Content / language / tag / region: declare mapping, never gender.
    if ctype == CATEGORY_TYPE_CONTENT or ctype == "content":
        # Twitch uses game_id; Bilibili uses parent_area_id; else category=.
        if key.startswith("game:"):
            game_id = key.split(":", 1)[1].strip()
            return {
                "request_param": "game_id",
                "request_value": game_id,
                "filter_scope": FILTER_SCOPE_CONTENT,
            }
        if key.startswith("parent_area:"):
            area_id = key.split(":", 1)[1].strip()
            return {
                "request_param": "parent_area_id",
                "request_value": area_id,
                "filter_scope": FILTER_SCOPE_CONTENT,
            }
        return {
            "request_param": "category",
            "request_value": key,
            "filter_scope": FILTER_SCOPE_PRIMARY,
        }
    if ctype == "language":
        return {
            "request_param": "language",
            "request_value": key,
            "filter_scope": FILTER_SCOPE_SECONDARY,
        }
    if ctype == "tag":
        return {
            "request_param": "tags",
            "request_value": key,
            "filter_scope": FILTER_SCOPE_SECONDARY,
        }
    if ctype == "region":
        return {
            "request_param": "region",
            "request_value": key,
            "filter_scope": FILTER_SCOPE_SECONDARY,
        }
    return {
        "request_param": None,
        "request_value": key if key != CANONICAL_CATEGORY_ALL else None,
        "filter_scope": FILTER_SCOPE_PRIMARY,
    }


def _category_item(
    *,
    canonical: str,
    category_type: str,
    source_mode: str,
    source_values: List[Any],
    reliability: str,
    source_signal_present: bool,
    available: bool,
    readiness: str,
    unsupported_reason_code: Optional[str] = None,
    field_name: Optional[str] = None,
    data_locus: Optional[str] = None,
    upstream_parameter: Optional[Dict[str, Any]] = None,
    evidence_note: Optional[str] = None,
    request_param: Any = "__auto__",
    request_value: Any = "__auto__",
    filter_scope: Optional[str] = None,
    display_label: Optional[str] = None,
) -> Dict[str, Any]:
    if readiness not in READINESS_VALUES:
        raise ValueError(f"invalid readiness: {readiness}")
    # Formal delivery requires verified (or experimental only when explicitly available).
    if available and readiness in {READINESS_NOT_READY, READINESS_UNSUPPORTED}:
        available = False
    if display_label is not None and str(display_label).strip():
        label = str(display_label).strip()
    else:
        label = (
            "All"
            if canonical == CANONICAL_CATEGORY_ALL
            else GENDER_LABELS.get(canonical, canonical.title())
        )
    mapping = _request_mapping_for(category_type, canonical)
    if request_param != "__auto__":
        mapping["request_param"] = request_param
    if request_value != "__auto__":
        mapping["request_value"] = request_value
    if filter_scope is not None:
        mapping["filter_scope"] = filter_scope

    item: Dict[str, Any] = {
        # canonical_category plus canonical_key alias
        "canonical_category": canonical,
        "canonical_key": canonical,
        "display_label": label,
        "category_type": category_type,
        "request_param": mapping["request_param"],
        "request_value": mapping["request_value"],
        "filter_scope": mapping["filter_scope"],
        "source_mode": source_mode,
        "source_values": list(source_values or []),
        "reliability": reliability,
        "source_signal_present": bool(source_signal_present),
        "available": bool(available),
        "readiness": readiness,
        "unsupported_reason": unsupported_reason_code,
    }
    if field_name:
        item["field_name"] = field_name
    if data_locus:
        item["data_locus"] = data_locus
    if upstream_parameter:
        item["upstream_parameter"] = dict(upstream_parameter)
    if evidence_note:
        item["evidence_note"] = evidence_note
    # Contract alias used by site-wide P5 docs / clients.
    item["label"] = item["display_label"]
    return item


def _all_category_item() -> Dict[str, Any]:
    return _category_item(
        canonical=CANONICAL_CATEGORY_ALL,
        category_type=CATEGORY_TYPE_ALL,
        source_mode=SOURCE_MODE_NATIVE,
        source_values=[],
        reliability=RELIABILITY_HIGH,
        source_signal_present=True,
        available=True,
        readiness=READINESS_VERIFIED,
    )


def _gender_item_from_spec(canonical: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return _category_item(
        canonical=canonical,
        category_type=CATEGORY_TYPE_GENDER,
        source_mode=spec.get("source_mode") or SOURCE_MODE_STRUCTURED,
        source_values=list(spec.get("source_values") or []),
        reliability=spec.get("reliability") or RELIABILITY_MEDIUM,
        source_signal_present=bool(spec.get("source_signal_present")),
        available=bool(spec.get("available")),
        readiness=str(spec.get("readiness") or READINESS_NOT_READY),
        unsupported_reason_code=spec.get("unsupported_reason"),
        field_name=spec.get("field_name"),
        data_locus=spec.get("data_locus"),
        upstream_parameter=spec.get("upstream_parameter"),
        evidence_note=spec.get("evidence_note"),
    )


def _unsupported_gender_item(canonical: str, reason_code: str) -> Dict[str, Any]:
    return _category_item(
        canonical=canonical,
        category_type=CATEGORY_TYPE_GENDER,
        source_mode=SOURCE_MODE_UNSUPPORTED,
        source_values=[],
        reliability=RELIABILITY_NONE,
        source_signal_present=False,
        available=False,
        readiness=READINESS_UNSUPPORTED,
        unsupported_reason_code=reason_code,
        evidence_note=f"Capability layer marks unsupported: {reason_code}",
    )


def twitch_content_category_item(game_id: str, name: str) -> Dict[str, Any]:
    """Formal Twitch native game/content category."""
    gid = str(game_id or "").strip()
    label = str(name or "").strip()
    return _category_item(
        canonical=f"game:{gid}",
        category_type=CATEGORY_TYPE_CONTENT,
        source_mode=SOURCE_MODE_NATIVE,
        source_values=[gid],
        reliability=RELIABILITY_HIGH,
        source_signal_present=True,
        available=True,
        readiness=READINESS_VERIFIED,
        field_name="game_id",
        data_locus="helix_games_top",
        upstream_parameter={"helix": "GET /helix/games/top", "streams_filter": "game_id"},
        evidence_note="Twitch Helix Get Top Games native content category.",
        request_param="game_id",
        request_value=gid,
        filter_scope=FILTER_SCOPE_CONTENT,
        display_label=label,
    )


def bilibili_content_category_item(parent_area_id: str, name: str) -> Dict[str, Any]:
    """Formal Bilibili native parent-area category."""
    aid = str(parent_area_id or "").strip()
    label = str(name or "").strip()
    return _category_item(
        canonical=f"parent_area:{aid}",
        category_type=CATEGORY_TYPE_CONTENT,
        source_mode=SOURCE_MODE_NATIVE,
        source_values=[aid],
        reliability=RELIABILITY_HIGH,
        source_signal_present=True,
        available=True,
        readiness=READINESS_VERIFIED,
        field_name="parent_area_id",
        data_locus="bilibili_area_get_list",
        upstream_parameter={
            "area_list": "GET /room/v1/Area/getList",
            "room_list_filter": "parent_area_id",
        },
        evidence_note="Bilibili live parent area from Area/getList.",
        request_param="parent_area_id",
        request_value=aid,
        filter_scope=FILTER_SCOPE_CONTENT,
        display_label=label,
    )


def is_executable_request_mapping(item: Dict[str, Any]) -> bool:
    """True when /api/discover can execute this category's request mapping."""
    if not isinstance(item, dict):
        return False
    ctype = str(item.get("category_type") or "").strip().lower()
    key = str(item.get("canonical_key") or item.get("canonical_category") or "").strip()
    if ctype == CATEGORY_TYPE_ALL or key == CANONICAL_CATEGORY_ALL:
        return True
    param = item.get("request_param")
    value = item.get("request_value")
    if param is None or value is None:
        return False
    param_s = str(param).strip()
    value_s = str(value).strip()
    if not param_s or not value_s:
        return False
    if param_s not in SUPPORTED_DISCOVER_REQUEST_PARAMS:
        return False
    if param_s == "game_id" and not value_s.isdigit():
        return False
    if param_s == "parent_area_id" and not value_s.isdigit():
        return False
    return True


def is_formal_deliverable_category(item: Dict[str, Any]) -> bool:
    """Site-wide gate for categories[] (never unavailable/unsupported/experimental)."""
    if not isinstance(item, dict):
        return False
    if item.get("available") is not True:
        return False
    if str(item.get("readiness") or "") != READINESS_VERIFIED:
        return False
    key = str(item.get("canonical_key") or item.get("canonical_category") or "").strip()
    label = str(item.get("display_label") or item.get("label") or "").strip()
    if not key or not label:
        return False
    return is_executable_request_mapping(item)


def build_categories_payload(
    source: str,
    *,
    twitch_games: Optional[Sequence[Dict[str, Any]]] = None,
    bilibili_areas: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return categories payload for a known source, or None if unknown.

    ``categories`` contains only available=true items for formal frontend use.
    Signal-present-but-not-ready items go to ``unavailable_categories``.
    Twitch content categories are injected via ``twitch_games`` (Helix top games);
    Bilibili parent areas via ``bilibili_areas``. Twitch/Bilibili omit All;
    Chaturbate/Stripchat keep All + gender.
    """
    source_key = (source or "").strip().lower()
    if not source_key or source_key not in KNOWN_SOURCES:
        return None

    formal: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []

    if source_key not in SOURCES_WITHOUT_ALL_CATEGORY:
        all_item = _all_category_item()
        all_item["source_type"] = source_key
        formal.append(all_item)
        evidence.append({
            "canonical_category": CANONICAL_CATEGORY_ALL,
            "source_signal_present": True,
            "readiness": READINESS_VERIFIED,
            "available": True,
            "note": "Default catalogue path.",
        })

    # Twitch native content categories before gender diagnostics.
    if source_key == "twitch":
        seen_game_ids: set[str] = set()
        for row in list(twitch_games or []):
            if not isinstance(row, dict):
                continue
            gid = str(row.get("game_id") or row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            if not gid or not gid.isdigit() or not name or gid in seen_game_ids:
                continue
            seen_game_ids.add(gid)
            item = twitch_content_category_item(gid, name)
            item["source_type"] = source_key
            if is_formal_deliverable_category(item):
                formal.append(item)
            evidence.append({
                "canonical_category": item["canonical_category"],
                "source_signal_present": True,
                "readiness": READINESS_VERIFIED,
                "available": True,
                "reliability": RELIABILITY_HIGH,
                "field_name": "game_id",
                "data_locus": "helix_games_top",
                "note": item.get("evidence_note"),
            })

    if source_key == "bilibili":
        seen_area_ids: set[str] = set()
        area_items: List[Dict[str, Any]] = []
        for row in list(bilibili_areas or []):
            if not isinstance(row, dict):
                continue
            aid = str(row.get("parent_area_id") or row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            if not aid or not aid.isdigit() or not name or aid in seen_area_ids:
                continue
            seen_area_ids.add(aid)
            item = bilibili_content_category_item(aid, name)
            item["source_type"] = source_key
            area_items.append(item)
            evidence.append({
                "canonical_category": item["canonical_category"],
                "source_signal_present": True,
                "readiness": READINESS_VERIFIED,
                "available": True,
                "reliability": RELIABILITY_HIGH,
                "field_name": "parent_area_id",
                "data_locus": "bilibili_area_get_list",
                "note": item.get("evidence_note"),
            })
        # Always expose Virtual streamers (parent_area_id=9) first as Discover default.
        if DEFAULT_BILIBILI_PARENT_AREA_ID not in seen_area_ids:
            vtuber = bilibili_content_category_item(
                DEFAULT_BILIBILI_PARENT_AREA_ID,
                DEFAULT_BILIBILI_PARENT_AREA_NAME,
            )
            vtuber["source_type"] = source_key
            area_items.insert(0, vtuber)
            seen_area_ids.add(DEFAULT_BILIBILI_PARENT_AREA_ID)
            evidence.append({
                "canonical_category": vtuber["canonical_category"],
                "source_signal_present": True,
                "readiness": READINESS_VERIFIED,
                "available": True,
                "reliability": RELIABILITY_HIGH,
                "field_name": "parent_area_id",
                "data_locus": "bilibili_area_get_list",
                "note": "Pinned Discover default partition.",
            })
        else:
            area_items.sort(
                key=lambda item: (
                    0
                    if str(item.get("request_value") or "") == DEFAULT_BILIBILI_PARENT_AREA_ID
                    else 1,
                    str(item.get("display_label") or "").lower(),
                )
            )
        for item in area_items:
            if is_formal_deliverable_category(item):
                formal.append(item)

    gender_specs = _SOURCE_GENDER_SPECS.get(source_key) or {}

    for gender in CANONICAL_TAXONOMY_GENDER:
        reason = unsupported_reason(source_key, gender)
        spec = gender_specs.get(gender)
        if spec is not None:
            item = _gender_item_from_spec(gender, spec)
        elif reason:
            item = _unsupported_gender_item(gender, reason)
        else:
            # No positive evidence and not in unsupported matrix → not advertised.
            continue

        item["source_type"] = source_key
        evidence.append({
            "canonical_category": item["canonical_category"],
            "source_signal_present": item["source_signal_present"],
            "readiness": item["readiness"],
            "available": item["available"],
            "reliability": item["reliability"],
            "field_name": item.get("field_name"),
            "data_locus": item.get("data_locus"),
            "note": item.get("evidence_note"),
        })

        # Site-wide: formal categories[] only verified + executable mappings.
        # experimental / not_ready / unsupported / invalid mapping → diagnostics only.
        if is_formal_deliverable_category(item):
            formal.append(item)
        else:
            unavailable.append(item)

    # Stamp source_type on All as well.
    for row in formal:
        row.setdefault("source_type", source_key)

    diagnostics = {
        "schema_note": (
            "categories lists only available=true + readiness=verified + executable "
            "request mapping for frontend buttons. "
            "unavailable_categories / diagnostics / unsupported are backend-only and "
            "must never be rendered as pills (including grey/disabled)."
        ),
        "readiness_values": sorted(READINESS_VALUES),
        "supported_discover_request_params": sorted(SUPPORTED_DISCOVER_REQUEST_PARAMS),
        "formal_category_count": len(formal),
        "unavailable_category_count": len(unavailable),
        "twitch_content_source": (
            "helix_games_top" if source_key == "twitch" else None
        ),
        "bilibili_content_source": (
            "area_get_list" if source_key == "bilibili" else None
        ),
    }

    return {
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source": source_key,
        "display_name": DISPLAY_NAMES.get(source_key, source_key),
        "default_category": CANONICAL_CATEGORY_ALL,
        "categories": formal,
        "unavailable_categories": unavailable,
        "diagnostics": diagnostics,
        "capability_evidence": evidence,
        "secondary_filters": {
            "supports_language": False,
            "supports_tags": False,
            "supports_region": False,
            "supported_filter_keys": [],
        },
        "ranking_hints": ranking_hints_for_source(source_key),
    }


def known_source_list() -> List[str]:
    return sorted(KNOWN_SOURCES)

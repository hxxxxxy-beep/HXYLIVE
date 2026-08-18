"""Discover viewer-count fields and sort helpers.

viewer_count_precision is evidence-based — never exact from a source
whitelist or from “viewers is an int” alone.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple


CONTRACT_VERSION = "ab-shared-v1"

# Query values still accepted by /api/discover.
LEGACY_SORT_VIEWERS = "viewers"
LEGACY_SORT_NEWEST = "newest"

# Stale only when an update timestamp is present and older than this.
VIEWER_COUNT_STALE_AFTER_SECONDS = 300

# Proven exact upstream field names (must be present on the model as evidence).
_EXACT_RAW_FIELD_NAMES = (
    "num_users",       # Chaturbate roomlist
    "viewersCount",    # Stripchat API
    "viewer_count",    # Twitch Helix / Cams mapping (only if not coerced default)
)

# Sources that indicate abbreviation / HTML parse paths → approximate.
_APPROXIMATE_SOURCES = frozenset({
    "html_abbrev",
    "html_parse_count",
    "parse_count_km",
    "k_m_suffix",
})

_ABBREV_COUNT_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*[kKmM]\s*$"
)


class DiscoverSortRequest(str, Enum):
    """User/request sort values (ab-shared-v1)."""

    SOURCE_DEFAULT = "source_default"
    VIEWERS_DESC = "viewers_desc"


class RankingMode(str, Enum):
    """Backend-executed ranking mode (response metadata only)."""

    PAGE_LOCAL = "page_local"
    PROVIDER_NATIVE = "provider_native"
    CACHED_POOL = "cached_pool"
    MULTI_PAGE_GLOBAL = "multi_page_global"
    UNAVAILABLE = "unavailable"


class ViewerCountPrecision(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    MISSING = "missing"
    STALE = "stale"
    UNVERIFIED = "unverified"


def normalize_sort_param(raw: Optional[str]) -> str:
    value = (raw or LEGACY_SORT_VIEWERS).strip().lower()
    return value or LEGACY_SORT_VIEWERS


def sorts_by_viewers(sort_mode: str) -> bool:
    return sort_mode in {
        LEGACY_SORT_VIEWERS,
        DiscoverSortRequest.VIEWERS_DESC.value,
    }


def sorts_by_newest(sort_mode: str) -> bool:
    return sort_mode == LEGACY_SORT_NEWEST


def preserves_encounter_order(sort_mode: str) -> bool:
    return sort_mode == DiscoverSortRequest.SOURCE_DEFAULT.value


def derive_viewer_count_reliable(precision: Optional[str]) -> bool:
    """Derived compatibility flag — never used to infer precision."""
    return (precision or "") == ViewerCountPrecision.EXACT.value


def derive_response_viewer_count_reliable(models: list) -> bool:
    if not models:
        return False
    precisions = [str(m.get("viewer_count_precision") or "") for m in models]
    return bool(precisions) and all(p == ViewerCountPrecision.EXACT.value for p in precisions)


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return None
    if _ABBREV_COUNT_RE.match(text):
        return None  # caller handles abbrev separately
    try:
        return max(0, int(float(text.replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _parse_abbrev_count(raw: Any) -> Optional[int]:
    """Parse 1.2k / 3.4M style strings into approximate integers."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("\xa0", " ").replace(" ", "")
    match = re.match(r"^(\d+(?:[.,]\d+)?)([km])$", text)
    if not match:
        return None
    number = match.group(1).replace(",", ".")
    try:
        parsed = float(number)
    except ValueError:
        return None
    mult = 1000 if match.group(2) == "k" else 1_000_000
    return max(0, int(parsed * mult))


def viewers_value_is_missing(item: Dict[str, Any]) -> bool:
    if item.get("viewer_count_present") is False:
        return True
    if "viewers" not in item and item.get("viewer_count_raw") is None:
        # No viewers key and no raw evidence.
        if not any(name in item for name in _EXACT_RAW_FIELD_NAMES):
            return True
    if "viewers" in item:
        raw = item.get("viewers")
        if raw is None:
            return True
        if isinstance(raw, str) and not raw.strip():
            return True
    return False


def _first_exact_raw_field(item: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    for name in _EXACT_RAW_FIELD_NAMES:
        if name in item and item.get(name) is not None and item.get(name) != "":
            return name, item.get(name)
    return None, None


def _resolve_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
    """Collect additive evidence without requiring provider rewrites."""
    hint = str(item.get("viewer_count_precision_hint") or "").strip().lower() or None
    source = str(item.get("viewer_count_source") or "").strip().lower() or None
    raw = item.get("viewer_count_raw")
    present_flag = item.get("viewer_count_present")

    exact_field, exact_raw = _first_exact_raw_field(item)
    if exact_field is not None:
        if raw is None:
            raw = exact_raw
        if source is None:
            source = exact_field
        if hint is None:
            hint = ViewerCountPrecision.EXACT.value
        if present_flag is None:
            present_flag = True

    if raw is None and "viewers" in item and item.get("viewers") is not None:
        raw = item.get("viewers")

    return {
        "viewer_count_raw": raw,
        "viewer_count_source": source,
        "viewer_count_precision_hint": hint,
        "viewer_count_present": present_flag,
        "viewer_count_updated_at": item.get("viewer_count_updated_at"),
    }


def _precision_from_evidence(
    *,
    evidence: Dict[str, Any],
    parsed_count: Optional[int],
    is_abbrev: bool,
    field_missing: bool,
) -> str:
    hint = evidence.get("viewer_count_precision_hint")
    source = (evidence.get("viewer_count_source") or "") or ""
    updated_at = evidence.get("viewer_count_updated_at")

    if field_missing:
        return ViewerCountPrecision.MISSING.value

    if is_abbrev or source in _APPROXIMATE_SOURCES or hint == ViewerCountPrecision.APPROXIMATE.value:
        return ViewerCountPrecision.APPROXIMATE.value

    if updated_at is not None:
        try:
            age = time.time() - float(updated_at)
            if age > VIEWER_COUNT_STALE_AFTER_SECONDS:
                return ViewerCountPrecision.STALE.value
        except (TypeError, ValueError):
            pass

    # Exact only with explicit evidence — never from source whitelist alone.
    exact_ok = (
        hint == ViewerCountPrecision.EXACT.value
        or source in {n.lower() for n in _EXACT_RAW_FIELD_NAMES}
        or source in {"num_users", "viewerscount", "viewer_count", "helix_viewer_count"}
    )
    if exact_ok and evidence.get("viewer_count_present") is not False and parsed_count is not None:
        return ViewerCountPrecision.EXACT.value

    # Provider-padded zero without proof of a real observation → missing.
    if parsed_count == 0 and not exact_ok:
        return ViewerCountPrecision.MISSING.value

    # Numeric value present but precision unproven.
    return ViewerCountPrecision.UNVERIFIED.value


def annotate_model_viewer_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """Add additive viewer_count* fields; keep legacy viewers semantics.

    Evidence-based precision (B1.1):
    - missing: no observation (compat viewers=0, viewer_count=null)
    - approximate: k/M or html abbrev parse
    - exact: only with proven raw field / hint
    - unverified: number present, provenance unproven
    - stale: updated_at older than TTL
    """
    out = item
    evidence = _resolve_evidence(out)
    raw = evidence["viewer_count_raw"]

    # Persist additive evidence fields (old clients ignore them).
    out["viewer_count_raw"] = raw
    if evidence["viewer_count_source"] is not None:
        out["viewer_count_source"] = evidence["viewer_count_source"]
    if evidence["viewer_count_precision_hint"] is not None:
        out["viewer_count_precision_hint"] = evidence["viewer_count_precision_hint"]

    abbrev_parsed = _parse_abbrev_count(raw)
    is_abbrev = abbrev_parsed is not None
    field_missing = viewers_value_is_missing(out) and abbrev_parsed is None and _first_exact_raw_field(out)[0] is None

    if field_missing and evidence.get("viewer_count_present") is not True:
        out["viewers"] = 0
        out["viewer_count"] = None
        out["viewer_count_present"] = False
        out["viewer_count_precision"] = ViewerCountPrecision.MISSING.value
        out["viewer_count_reliable"] = False
        return out

    if is_abbrev:
        parsed = abbrev_parsed
    else:
        # Prefer exact raw field value when present.
        _, exact_raw = _first_exact_raw_field(out)
        parsed = _coerce_non_negative_int(exact_raw if exact_raw is not None else raw)

    if parsed is None:
        out["viewers"] = 0
        out["viewer_count"] = None
        out["viewer_count_present"] = False
        out["viewer_count_precision"] = ViewerCountPrecision.MISSING.value
        out["viewer_count_reliable"] = False
        return out

    precision = _precision_from_evidence(
        evidence=evidence,
        parsed_count=parsed,
        is_abbrev=is_abbrev,
        field_missing=False,
    )

    # Compat viewers always non-negative int.
    out["viewers"] = int(parsed)

    if precision == ViewerCountPrecision.MISSING.value:
        # Padded/unproven zero: do not claim a real viewer_count observation.
        out["viewer_count"] = None
        out["viewer_count_present"] = False
    else:
        out["viewer_count"] = int(parsed)
        out["viewer_count_present"] = True

    out["viewer_count_precision"] = precision
    out["viewer_count_reliable"] = derive_viewer_count_reliable(precision)
    return out


def b1_default_ranking_mode(*, supported: bool = True) -> str:
    """Page-local browse never claims multi_page_global or unproven provider_native."""
    if not supported:
        return RankingMode.UNAVAILABLE.value
    return RankingMode.PAGE_LOCAL.value


def b1_discover_response_extras(
    *,
    sort_mode: str,
    models: list,
    supported: bool = True,
) -> Dict[str, Any]:
    ranking_mode = b1_default_ranking_mode(supported=supported)
    return {
        "contract_version": CONTRACT_VERSION,
        "sort": sort_mode,
        "ranking_mode": ranking_mode,
        "pool_id": None,
        "viewer_count_reliable": (
            False
            if ranking_mode == RankingMode.UNAVAILABLE.value
            else derive_response_viewer_count_reliable(models)
        ),
    }

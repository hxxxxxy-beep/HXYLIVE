"""Central discover gender capability map for HXYLIVE integrations.

"unsupported" means the current HXYLIVE integration has no reliable category
signal for that source×gender — not a claim that the upstream site never
supports the category.
"""

from __future__ import annotations

from typing import Optional

# Genders the UI can request besides the implicit All (empty / None).
CANONICAL_GENDERS = ("female", "male", "trans", "couple")

REASON_NO_RELIABLE_SIGNAL = "no_reliable_gender_signal"
REASON_NO_CATEGORY_URL = "no_upstream_category_url"
REASON_GENDER_NOT_SUPPORTED = "gender_not_supported_by_provider"
REASON_PARSER_HARDCODED = "parser_gender_hardcoded_other"

# Human-facing detail for API provider_statuses / frontend.
REASON_MESSAGES = {
    REASON_NO_RELIABLE_SIGNAL: (
        "This HXYLIVE integration has no reliable gender/category signal for "
        "the selected filter."
    ),
    REASON_NO_CATEGORY_URL: (
        "This HXYLIVE integration has no proven upstream category URL for the "
        "selected filter."
    ),
    REASON_GENDER_NOT_SUPPORTED: (
        "This provider does not expose a gender dimension in the current "
        "HXYLIVE discover integration."
    ),
    REASON_PARSER_HARDCODED: (
        "This HXYLIVE parser does not expose reliable non-default gender "
        "labels for the selected filter."
    ),
}

# Explicit unsupported (source, gender) → reason code.
# All (no gender) is supported unless the provider itself is unavailable.
_UNSUPPORTED_GENDERS: dict[str, dict[str, str]] = {
    "twitch": {
        "female": REASON_GENDER_NOT_SUPPORTED,
        "male": REASON_GENDER_NOT_SUPPORTED,
        "trans": REASON_GENDER_NOT_SUPPORTED,
        "couple": REASON_GENDER_NOT_SUPPORTED,
    },
    "bilibili": {
        "female": REASON_GENDER_NOT_SUPPORTED,
        "male": REASON_GENDER_NOT_SUPPORTED,
        "trans": REASON_GENDER_NOT_SUPPORTED,
        "couple": REASON_GENDER_NOT_SUPPORTED,
    },
}

# Providers that scrape a finite homepage/catalogue once and slice locally.
# After a gender filter, an empty page must not advertise has_more=true.
# (No remaining sources use this pattern; Twitch/Chaturbate paginate upstream.)
FINITE_LOCAL_FILTER_SOURCES = frozenset()


def normalize_gender(gender: Optional[str]) -> Optional[str]:
    token = (gender or "").strip().lower()
    if not token or token == "all":
        return None
    return token


def unsupported_reason(source_type: str, gender: Optional[str]) -> Optional[str]:
    """Return reason code if source×gender is unsupported; else None."""
    source = (source_type or "").strip().lower()
    requested = normalize_gender(gender)
    if not source or not requested:
        return None
    return (_UNSUPPORTED_GENDERS.get(source) or {}).get(requested)


def is_gender_supported(source_type: str, gender: Optional[str]) -> bool:
    return unsupported_reason(source_type, gender) is None


def unsupported_message(reason_code: Optional[str]) -> str:
    if not reason_code:
        return REASON_MESSAGES[REASON_NO_RELIABLE_SIGNAL]
    return REASON_MESSAGES.get(reason_code, REASON_MESSAGES[REASON_NO_RELIABLE_SIGNAL])


def uses_finite_local_filter(source_type: str) -> bool:
    return (source_type or "").strip().lower() in FINITE_LOCAL_FILTER_SOURCES


def all_unsupported_combos() -> list[tuple[str, str, str]]:
    """List (source, gender, reason_code) for tests and audits."""
    rows: list[tuple[str, str, str]] = []
    for source, genders in sorted(_UNSUPPORTED_GENDERS.items()):
        for gender, reason in sorted(genders.items()):
            rows.append((source, gender, reason))
    return rows


def filter_providers_for_gender(providers: list, gender: Optional[str]) -> list:
    """Drop providers that cannot serve the requested gender (aggregate views)."""
    requested = normalize_gender(gender)
    if not requested:
        return list(providers)
    return [
        provider
        for provider in providers
        if is_gender_supported(getattr(provider, "source_type", ""), requested)
    ]

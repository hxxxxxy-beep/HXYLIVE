import re
from typing import Optional, Tuple


FILENAME_FORMAT_TIMESTAMP = "timestamp"
FILENAME_FORMAT_USERNAME_TIMESTAMP = "username_timestamp"
ALLOWED_FILENAME_FORMATS = {
    FILENAME_FORMAT_TIMESTAMP,
    FILENAME_FORMAT_USERNAME_TIMESTAMP,
}

# On-disk streamer / file markers: username(source). Keep in sync with providers.
KNOWN_SOURCE_MARKERS = frozenset({
    "chaturbate",
    "stripchat",
    "twitch",
    "bilibili",
    "youtube",
})

_SOURCE_MARKER_PATTERN = "|".join(sorted(KNOWN_SOURCE_MARKERS, key=len, reverse=True))
# Matches ``name(source)`` as a whole token, or ``name(source)_…`` / ``name(source) …``.
_IDENTITY_WITH_SOURCE_RE = re.compile(
    rf"^(?P<name>.+?)\((?P<source>{_SOURCE_MARKER_PATTERN})\)(?:$|(?=[_\s.-]))",
    re.IGNORECASE,
)


def normalize_filename_format(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_FILENAME_FORMATS:
        return normalized
    return FILENAME_FORMAT_TIMESTAMP


def normalize_source_marker(value: object) -> Optional[str]:
    """Return a known source_type marker, or None when unrecognized."""
    source = str(value or "").strip().lower()
    if not source:
        return None
    aliases = {
        "yt": "youtube",
        "youtube.com": "youtube",
        "cb": "chaturbate",
        "sc": "stripchat",
    }
    source = aliases.get(source, source)
    if source in KNOWN_SOURCE_MARKERS:
        return source
    return None


def safe_filename_part(value: object, fallback: str = "session") -> str:
    """Sanitize one path segment while keeping Unicode letters (e.g. CJK display names)."""
    text = str(value or "").strip()
    if not text:
        return fallback
    # Drop path separators / Windows-illegal chars; keep CJK and other letters.
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text)
    # Parentheses would break ``name(source)`` stamps.
    cleaned = cleaned.replace("(", "_").replace(")", "_")
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip(".-")
    return cleaned or fallback


def format_identity_with_source(username: object, source_type: object = None) -> str:
    """Stamp ``username(source)`` for folders and recording names."""
    user = safe_filename_part(username, "unknown")
    source = normalize_source_marker(source_type)
    if not source:
        return user
    return f"{user}({source})"


def parse_identity_with_source(value: object) -> Tuple[str, Optional[str]]:
    """Parse ``username(source)`` (folder or filename stem). Legacy bare names keep source=None."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return "", None
    parts = [part for part in raw.split("/") if part]
    candidates: list[str] = []
    if len(parts) > 1:
        candidates.append(parts[0])
    if parts:
        candidates.append(parts[-1])

    def _parse_one(token: str) -> Tuple[str, Optional[str]]:
        text = str(token or "").strip()
        if not text:
            return "", None
        # Drop a single trailing extension so stems like ``user(chaturbate).mp4`` parse.
        if "." in text and not text.endswith(")"):
            stem, ext = text.rsplit(".", 1)
            if ext and len(ext) <= 5 and ext.isalnum():
                text = stem
        match = _IDENTITY_WITH_SOURCE_RE.match(text)
        if not match:
            return text, None
        name = str(match.group("name") or "").strip()
        source = normalize_source_marker(match.group("source"))
        if not name or not source:
            return text, None
        return name, source

    for candidate in candidates:
        name, source = _parse_one(candidate)
        if source:
            return name, source
    if len(parts) > 1:
        return _parse_one(parts[0])
    if parts:
        return _parse_one(parts[-1])
    return "", None


def recording_base_name(
    person: str,
    start_timestamp: str,
    session_id: str,
    filename_format: object,
    source_type: object = None,
) -> str:
    """Build the recording stem. New files embed ``(source)`` when source_type is known."""
    source = normalize_source_marker(source_type)
    identity = format_identity_with_source(person, source)
    if normalize_filename_format(filename_format) == FILENAME_FORMAT_USERNAME_TIMESTAMP:
        return f"{identity}_{start_timestamp.replace('_', '-')}"
    base = f"{start_timestamp}_{session_id[:6]}"
    if source:
        return f"{base}({source})"
    return base

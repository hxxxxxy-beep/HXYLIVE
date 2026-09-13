"""Monthly per-streamer recording traffic quota helpers.

Quota measures captured recording bytes (inbound stream size), not converted
MP4 size. Cycles start on a configurable calendar day (default the 13th).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

GB_BYTES = 1024 ** 3
DEFAULT_MONTHLY_QUOTA_GB = 100
DEFAULT_QUOTA_CYCLE_DAY = 13
MAX_MONTHLY_QUOTA_GB = 10_000


def resolve_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return year, month


def _clamp_cycle_day(year: int, month: int, cycle_day: int) -> int:
    last_day = monthrange(year, month)[1]
    return max(1, min(int(cycle_day), last_day))


def normalize_quota_cycle_day(value, default: int = DEFAULT_QUOTA_CYCLE_DAY) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError):
        day = default
    if day < 1 or day > 28:
        raise ValueError("quota_cycle_day must be between 1 and 28")
    return day


def normalize_monthly_quota_gb(
    value,
    *,
    default: Optional[int] = DEFAULT_MONTHLY_QUOTA_GB,
    allow_none: bool = False,
) -> Optional[int]:
    """Normalize a quota in GB.

    None means inherit the global default when allow_none is True.
    0 means unlimited.
    """
    if value is None or value == "":
        if allow_none:
            return None
        return default
    try:
        quota = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("monthly quota must be an integer number of GB") from exc
    if quota < 0:
        raise ValueError("monthly quota must be 0 or greater")
    if quota > MAX_MONTHLY_QUOTA_GB:
        raise ValueError(f"monthly quota must be <= {MAX_MONTHLY_QUOTA_GB} GB")
    return quota


def gb_to_bytes(quota_gb: Optional[int]) -> int:
    """Return byte limit. 0 / None with unlimited semantics returns 0."""
    if not quota_gb:
        return 0
    return int(quota_gb) * GB_BYTES


def effective_monthly_quota_gb(
    stored: Optional[int],
    default_gb: int = DEFAULT_MONTHLY_QUOTA_GB,
) -> int:
    if stored is None:
        return int(default_gb)
    return int(stored)


def quota_cycle_bounds(
    now: Optional[datetime] = None,
    *,
    cycle_day: int = DEFAULT_QUOTA_CYCLE_DAY,
    tz_name: str = "UTC",
) -> tuple[datetime, datetime]:
    """Return [start, end) datetimes for the active quota cycle."""
    tz = resolve_tz(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    cycle_day = normalize_quota_cycle_day(cycle_day)
    day = _clamp_cycle_day(now.year, now.month, cycle_day)
    if now.day >= day:
        start_year, start_month = now.year, now.month
    else:
        start_year, start_month = _shift_month(now.year, now.month, -1)
        day = _clamp_cycle_day(start_year, start_month, cycle_day)

    start = datetime(start_year, start_month, day, 0, 0, 0, tzinfo=tz)
    end_year, end_month = _shift_month(start_year, start_month, 1)
    end_day = _clamp_cycle_day(end_year, end_month, cycle_day)
    end = datetime(end_year, end_month, end_day, 0, 0, 0, tzinfo=tz)
    return start, end


def quota_cycle_unix(
    now: Optional[datetime] = None,
    *,
    cycle_day: int = DEFAULT_QUOTA_CYCLE_DAY,
    tz_name: str = "UTC",
) -> tuple[int, int]:
    start, end = quota_cycle_bounds(now, cycle_day=cycle_day, tz_name=tz_name)
    return int(start.timestamp()), int(end.timestamp())


def recording_bytes_for_quota(row: dict) -> int:
    """Prefer original capture size (TS) over converted playable size."""
    for key in ("file_size", "mp4_size", "playable_size"):
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def is_quota_exceeded(used_bytes: int, limit_bytes: int) -> bool:
    """Unlimited when limit_bytes <= 0."""
    if limit_bytes <= 0:
        return False
    return int(used_bytes or 0) >= int(limit_bytes)


def blocked_reason(
    *,
    recording_enabled: bool,
    quota_exceeded: bool,
) -> Optional[str]:
    if not recording_enabled:
        return "global_pause"
    if quota_exceeded:
        return "quota_exceeded"
    return None


def seconds_until(dt: datetime, now: Optional[datetime] = None) -> int:
    tz = dt.tzinfo or timezone.utc
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    return max(0, int((dt - now).total_seconds()))


def cycle_label(start: datetime, end: datetime) -> str:
    return f"{start.date().isoformat()} → {end.date().isoformat()}"


def add_months_safe(dt: datetime, months: int) -> datetime:
    year, month = _shift_month(dt.year, dt.month, months)
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day) + timedelta(0)

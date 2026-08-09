"""
Background task: continuous model monitoring
Checks online status, generates thumbnails, and updates SQLite
"""
from __future__ import annotations

import asyncio
import aiohttp
import json
import re
import os
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from datetime import datetime, timezone

if TYPE_CHECKING:
    from ..ffmpeg_runner import FFmpegManager
    from ..core.database import Database

from ..logger import logger
from ..subprocess_utils import communicate_with_timeout, wait_with_timeout
from ..core.config import (
    AUTO_RECORD_INTERVAL,
    CHATURBATE_REQUEST_TIMEOUT_SECONDS,
    MIN_RECORDING_BYTES,
    MIN_RECORDING_SECONDS,
    OUTPUT_DIR,
)
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs

# Check interval (in seconds)
CHECK_INTERVAL_SETTING_KEY = "check_interval_seconds"
MIN_CHECK_INTERVAL_SECONDS = 30
MAX_CHECK_INTERVAL_SECONDS = 3600
MONITOR_INTERVAL = AUTO_RECORD_INTERVAL
THUMBNAIL_UPDATE_INTERVAL = 300  # Offline thumbnail: every 5 minutes
THUMBNAIL_UPDATE_INTERVAL_LIVE = 60  # Live thumbnail: every 60s to reflect activity
SHORT_RECORDING_PROBE_BYTES = max(MIN_RECORDING_BYTES, 64 * 1024 * 1024)


def normalize_check_interval_seconds(value, default: int = MONITOR_INTERVAL) -> int:
    try:
        interval = int(value)
    except (ValueError, TypeError):
        interval = default

    if interval < MIN_CHECK_INTERVAL_SECONDS:
        raise ValueError(f"check interval must be at least {MIN_CHECK_INTERVAL_SECONDS} seconds")
    if interval > MAX_CHECK_INTERVAL_SECONDS:
        raise ValueError(f"check interval must be at most {MAX_CHECK_INTERVAL_SECONDS} seconds")
    return interval


async def get_check_interval_seconds(db: 'Database') -> int:
    raw = await db.get_setting(CHECK_INTERVAL_SETTING_KEY)
    try:
        return normalize_check_interval_seconds(
            raw if raw is not None else AUTO_RECORD_INTERVAL
        )
    except ValueError as exc:
        logger.warning(
            "Invalid monitoring interval, falling back to env",
            task="monitor",
            value=raw,
            error=str(exc),
        )
        return normalize_check_interval_seconds(AUTO_RECORD_INTERVAL)

async def _check_live_via_cdn(session: aiohttp.ClientSession, username: str) -> bool:
    """Check if a model is live using the Chaturbate thumbnail CDN.

    This CDN endpoint is not behind Cloudflare and does not require cookies.
    A 200 response with a non-trivial body means the model is currently streaming.
    """
    url = f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://chaturbate.com/",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
            ssl=False,
            **aiohttp_request_kwargs(),
        ) as resp:
            if resp.status == 200:
                content = await resp.read()
                return len(content) > 1000
    except Exception as e:
        logger.debug("Live CDN fallback error", username=username, error=str(e))
    return False


async def check_model_status(
    session: aiohttp.ClientSession,
    username: str,
    csrftoken: str = None,
    auth_cookies: dict | None = None,
) -> dict:
    """Check a model's status via the Chaturbate API.

    Cookies priority:
    1. ``auth_cookies`` (authenticated session stored in DB, injected by the
       ChaturbateAuthService via the builtin plugin or monitor task)
    2. ``CHATURBATE_*`` environment variables (legacy fallback)

    Without auth cookies Chaturbate redirects ``/api/chatvideocontext/`` to the
    login page and the check silently fails (see GH #11).

    When the chatvideocontext API is blocked by Cloudflare TLS fingerprinting
    (connection reset, error code 0), the CDN thumbnail endpoint is used as a
    reliable fallback to detect liveness without any Cloudflare dependency.
    """
    try:
        url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://chaturbate.com/",
            "Origin": "https://chaturbate.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        cookies: dict[str, str] = {}

        if auth_cookies:
            cookies.update(auth_cookies)

        if csrftoken and "csrftoken" not in cookies:
            cookies["csrftoken"] = csrftoken

        # Legacy env-var fallback: only fill slots the authenticated session did
        # not already provide.
        affkey_env = os.getenv("CHATURBATE_AFFKEY")
        sessionid_env = os.getenv("CHATURBATE_SESSIONID")
        if affkey_env and "affkey" not in cookies:
            cookies["affkey"] = affkey_env
        if sessionid_env and "sessionid" not in cookies:
            cookies["sessionid"] = sessionid_env

        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
            ssl=False,
            **aiohttp_request_kwargs(),
        ) as response:
            if response.status == 200:
                data = await response.json()

                # Log API data for debugging
                logger.debug("Chaturbate API response",
                           username=username,
                           room_status=data.get("room_status"),
                           has_hls=bool(data.get("hls_source")),
                           num_users=data.get("num_users", 0))

                # Improved online status detection
                room_status = data.get("room_status", "")
                hls_source = data.get("hls_source")

                # A model is online if:
                # 1. It has an available HLS stream OR
                # 2. Le room_status est "public" OU
                # 3. room_status is "away" (temporarily away but still online)
                is_online = (
                    bool(hls_source) or
                    room_status in ["public", "away"]
                )

                viewers = data.get("num_users", 0)

                return {
                    "is_online": is_online,
                    "viewers": viewers,
                    "hls_source": hls_source,
                    "room_status": room_status or None,
                    "tags": data.get("tags") or data.get("room_tags") or [],
                }
            # Cloudflare / rate-limit responses must not fall through to the CDN
            # thumbnail heuristic (offline promo images are often >1KB).
            if response.status in {403, 429, 503}:
                logger.debug(
                    "chatvideocontext blocked; skip CDN online guess",
                    username=username,
                    status=response.status,
                )
                return {
                    "is_online": False,
                    "viewers": 0,
                    "hls_source": None,
                    "room_status": None,
                    "tags": [],
                }
    except Exception as e:
        logger.debug("Error checking model status", username=username, error=str(e))

    # Fallback: chatvideocontext is blocked by Cloudflare TLS fingerprinting.
    # Use the thumbnail CDN which has no CF protection to detect liveness.
    try:
        is_live = await _check_live_via_cdn(session, username)
        if is_live:
            logger.debug("Status detected via CDN (CF fallback)", username=username, is_online=True)
        return {
            "is_online": is_live,
            "viewers": 0,
            "hls_source": None,
            "room_status": "public" if is_live else None,
            "tags": [],
        }
    except Exception as e:
        logger.debug("CDN fallback error", username=username, error=str(e))

    return {
        "is_online": False,
        "viewers": 0,
        "hls_source": None,
        "room_status": None,
        "tags": [],
    }

async def generate_thumbnail_from_stream(
    username: str,
    session_id: str,
    output_dir: Path,
    ffmpeg_path: str = "ffmpeg"
) -> str | None:
    """Generate a thumbnail from the current HLS stream"""
    try:
        session_dir = output_dir / "sessions" / session_id
        m3u8_file = session_dir / "stream.m3u8"

        if not m3u8_file.exists():
            return None

        # Directory for live thumbnails
        live_thumbs_dir = output_dir / "thumbnails" / "live"
        live_thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = live_thumbs_dir / f"{username}.jpg"

        # Generate the thumbnail
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-i", str(m3u8_file),
            "-vframes", "1",
            "-vf", "scale=280:-1",
            "-y",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        await wait_with_timeout(process, 10)

        if thumb_path.exists():
            return str(thumb_path)

    except Exception as e:
        logger.debug("Error generating stream thumbnail", username=username, error=str(e))

    return None

async def generate_thumbnail_from_recording(
    username: str,
    output_dir: Path,
    ffmpeg_path: str = "ffmpeg"
) -> str | None:
    """Generate a thumbnail from the latest recording"""
    try:
        records_dir = output_dir / "records" / username

        if not records_dir.exists():
            return None

        # Find the latest recording
        ts_files = sorted(records_dir.rglob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)

        if not ts_files:
            return None

        latest_recording = ts_files[0]

        # Directory for offline thumbnails
        offline_thumbs_dir = output_dir / "thumbnails" / "offline"
        offline_thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = offline_thumbs_dir / f"{username}.jpg"

        # Regenerate only if the thumbnail is missing or older than the recording
        if thumb_path.exists() and thumb_path.stat().st_mtime > latest_recording.stat().st_mtime:
            return str(thumb_path)

        # Extract a frame from the middle of the video
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-ss", "00:00:30",
            "-i", str(latest_recording),
            "-vframes", "1",
            "-vf", "scale=280:-1",
            "-y",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        await wait_with_timeout(process, 15)

        if thumb_path.exists():
            return str(thumb_path)

    except Exception as e:
        logger.debug("Error generating offline thumbnail", username=username, error=str(e))

    return None

async def download_thumbnail_from_chaturbate(
    session: aiohttp.ClientSession,
    username: str,
    output_dir: Path
) -> str | None:
    """Download the thumbnail from Chaturbate"""
    try:
        img_urls = [
            f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg",
            f"https://cbjpeg.stream.highwebmedia.com/stream?room={username}&f=.jpg",
        ]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://chaturbate.com/",
        }

        for img_url in img_urls:
            try:
                async with session.get(
                    img_url,
                    headers=headers,
                    timeout=5,
                    **aiohttp_request_kwargs(),
                ) as response:
                    if response.status == 200:
                        content = await response.read()

                        if len(content) > 1000:
                            # Save the thumbnail
                            cb_thumbs_dir = output_dir / "thumbnails" / "chaturbate"
                            cb_thumbs_dir.mkdir(parents=True, exist_ok=True)
                            thumb_path = cb_thumbs_dir / f"{username}.jpg"

                            with open(thumb_path, 'wb') as f:
                                f.write(content)

                            return str(thumb_path)
            except Exception:
                continue

    except Exception as e:
        logger.debug("Error downloading Chaturbate thumbnail", username=username, error=str(e))

    return None

async def get_video_duration(file_path: Path, ffmpeg_path: str = "ffmpeg") -> int:
    """Get video duration with ffprobe"""
    try:
        # Use ffprobe to get the duration
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")

        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await communicate_with_timeout(process, 10)

        if process.returncode == 0 and stdout:
            duration_str = stdout.decode().strip()
            if duration_str:
                return int(float(duration_str))

    except Exception as e:
        logger.debug("Error fetching video duration", file_path=str(file_path), error=str(e))

    return 0


_RECORDED_AT_TAG_KEYS = (
    "creation_time",
    "com.apple.quicktime.creationdate",
    "date_utc",
    "date-utc",
    "encoded_date",
    "tagged_date",
    "creation_date",
    "creationdate",
    "date",
)

_REFERENCE_MONTHS = {
    "jan": 1,
    "january": 1,
    "janvier": 1,
    "feb": 2,
    "february": 2,
    "fev": 2,
    "fevr": 2,
    "fevrier": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "avr": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juin": 6,
    "jul": 7,
    "july": 7,
    "juillet": 7,
    "aug": 8,
    "august": 8,
    "aout": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "septembre": 9,
    "oct": 10,
    "october": 10,
    "octobre": 10,
    "nov": 11,
    "november": 11,
    "novembre": 11,
    "dec": 12,
    "december": 12,
    "decembre": 12,
}

_REFERENCE_MONTH_PATTERN = "|".join(sorted(_REFERENCE_MONTHS, key=len, reverse=True))
_REFERENCE_TIME_SUFFIX = (
    r"(?:"
    r"(?:[\s_t,.;@-]+|[\s_]*a[\s_]+|[\s_]*at[\s_]+|[\s_]*vers[\s_]+)"
    r"([01]?\d|2[0-3])"
    r"(?:[:h._-]?([0-5]\d))?"
    r"(?:[:h._-]?([0-5]\d))?"
    r"\s*(am|pm)?"
    r")?"
)


def _normalize_metadata_key(key: object) -> str:
    return re.sub(r"[\s_-]+", "", str(key or "").strip().lower())


def _parse_metadata_timestamp(value: object) -> int | None:
    raw = str(value or "").strip().strip("\x00")
    if not raw or raw.upper() in {"N/A", "NONE", "NULL"}:
        return None

    candidate = raw
    if candidate.upper().startswith("UTC "):
        candidate = candidate[4:].strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", candidate):
        candidate = f"{candidate[:-5]}{candidate[-5:-2]}:{candidate[-2:]}"
    if re.match(r"^\d{4}:\d{2}:\d{2}", candidate):
        candidate = candidate.replace(":", "-", 2)

    formats = (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    )

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_reference_text(value: object) -> str:
    raw = str(value or "").strip().strip("\x00")
    normalized = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _reference_timestamp_from_parts(
    year: object,
    month: object,
    day: object,
    hour: object = None,
    minute: object = None,
    second: object = None,
    meridiem: object = None,
) -> int | None:
    try:
        hour_value = int(hour) if hour not in (None, "") else 0
        minute_value = int(minute) if minute not in (None, "") else 0
        second_value = int(second) if second not in (None, "") else 0
        marker = str(meridiem or "").lower()
        if marker == "pm" and hour_value < 12:
            hour_value += 12
        elif marker == "am" and hour_value == 12:
            hour_value = 0

        parsed = datetime(
            int(year),
            int(month),
            int(day),
            hour_value,
            minute_value,
            second_value,
        )
    except (TypeError, ValueError, OverflowError):
        return None

    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def reference_timestamp_from_text(text: object) -> int | None:
    """Extract a local timestamp from a content title or user-supplied filename."""
    candidate = _normalize_reference_text(text)
    if not candidate:
        return None

    compact_datetime = re.search(
        r"(?<!\d)(\d{4})(\d{2})(\d{2})([01]\d|2[0-3])([0-5]\d)([0-5]\d)?(?!\d)",
        candidate,
    )
    if compact_datetime:
        parsed = _reference_timestamp_from_parts(
            compact_datetime.group(1),
            compact_datetime.group(2),
            compact_datetime.group(3),
            compact_datetime.group(4),
            compact_datetime.group(5),
            compact_datetime.group(6),
        )
        if parsed is not None:
            return parsed

    compact_date = re.search(
        r"(?<!\d)(\d{4})(\d{2})(\d{2})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if compact_date:
        parsed = _reference_timestamp_from_parts(
            compact_date.group(1),
            compact_date.group(2),
            compact_date.group(3),
            compact_date.group(4),
            compact_date.group(5),
            compact_date.group(6),
            compact_date.group(7),
        )
        if parsed is not None:
            return parsed

    year_first = re.search(
        r"(?<!\d)(\d{4})[-_. /](\d{1,2})[-_. /](\d{1,2})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if year_first:
        parsed = _reference_timestamp_from_parts(
            year_first.group(1),
            year_first.group(2),
            year_first.group(3),
            year_first.group(4),
            year_first.group(5),
            year_first.group(6),
            year_first.group(7),
        )
        if parsed is not None:
            return parsed

    day_first = re.search(
        r"(?<!\d)(\d{1,2})[-_. /](\d{1,2})[-_. /](\d{4})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if day_first:
        parsed = _reference_timestamp_from_parts(
            day_first.group(3),
            day_first.group(2),
            day_first.group(1),
            day_first.group(4),
            day_first.group(5),
            day_first.group(6),
            day_first.group(7),
        )
        if parsed is not None:
            return parsed

    day_month_name = re.search(
        rf"(?<!\w)(\d{{1,2}})(?:st|nd|rd|th)?[\s._-]+({_REFERENCE_MONTH_PATTERN})"
        rf"[\s._,-]+(\d{{4}})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if day_month_name:
        parsed = _reference_timestamp_from_parts(
            day_month_name.group(3),
            _REFERENCE_MONTHS.get(day_month_name.group(2)),
            day_month_name.group(1),
            day_month_name.group(4),
            day_month_name.group(5),
            day_month_name.group(6),
            day_month_name.group(7),
        )
        if parsed is not None:
            return parsed

    month_name_first = re.search(
        rf"(?<!\w)({_REFERENCE_MONTH_PATTERN})[\s._-]+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"[,]?[\s._-]+(\d{{4}})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if month_name_first:
        parsed = _reference_timestamp_from_parts(
            month_name_first.group(3),
            _REFERENCE_MONTHS.get(month_name_first.group(1)),
            month_name_first.group(2),
            month_name_first.group(4),
            month_name_first.group(5),
            month_name_first.group(6),
            month_name_first.group(7),
        )
        if parsed is not None:
            return parsed

    return None


def _parse_video_recorded_at(probe_data: dict[str, Any]) -> int | None:
    tag_maps: list[dict[str, Any]] = []
    format_tags = (probe_data.get("format") or {}).get("tags")
    if isinstance(format_tags, dict):
        tag_maps.append(format_tags)
    for stream in probe_data.get("streams") or []:
        if isinstance(stream, dict) and isinstance(stream.get("tags"), dict):
            tag_maps.append(stream["tags"])

    wanted_keys = [_normalize_metadata_key(key) for key in _RECORDED_AT_TAG_KEYS]
    for wanted_key in wanted_keys:
        for tags in tag_maps:
            for key, value in tags.items():
                if _normalize_metadata_key(key) == wanted_key:
                    parsed = _parse_metadata_timestamp(value)
                    if parsed is not None:
                        return parsed

    for tags in tag_maps:
        for key, value in tags.items():
            normalized = _normalize_metadata_key(key)
            if "date" in normalized or ("creation" in normalized and "time" in normalized):
                parsed = _parse_metadata_timestamp(value)
                if parsed is not None:
                    return parsed
    return None


async def get_video_recorded_at(file_path: Path, ffmpeg_path: str = "ffmpeg") -> int | None:
    """Read the media/container recorded date from ffprobe metadata tags."""
    try:
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")

        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _stderr = await communicate_with_timeout(process, 10)
        if process.returncode == 0 and stdout:
            return _parse_video_recorded_at(json.loads(stdout.decode("utf-8", errors="replace")))
    except Exception as e:
        logger.debug("Error fetching video metadata date", file_path=str(file_path), error=str(e))

    return None


def recording_timestamp_from_filename(filename: str) -> int | None:
    path = Path(filename)
    for value in (path.stem, path.name):
        parsed = reference_timestamp_from_text(value)
        if parsed is not None:
            return parsed
    return None


def _iter_reference_texts(file_path: Path, reference_texts: Iterable[object] | None = None):
    seen: set[str] = set()
    for value in (*tuple(reference_texts or ()), file_path.stem, file_path.name):
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized


async def get_media_created_at(
    file_path: Path,
    ffmpeg_path: str = "ffmpeg",
    fallback_timestamp: int | None = None,
    reference_texts: Iterable[object] | None = None,
) -> int:
    for value in _iter_reference_texts(file_path, reference_texts):
        reference_at = reference_timestamp_from_text(value)
        if reference_at is not None:
            return reference_at

    recorded_at = await get_video_recorded_at(file_path, ffmpeg_path)
    if recorded_at is not None:
        return recorded_at
    if fallback_timestamp is not None:
        return int(fallback_timestamp)
    try:
        return int(file_path.stat().st_mtime)
    except OSError:
        return int(datetime.now(tz=timezone.utc).timestamp())


def _thumbnail_looks_empty(path: Path, min_bytes: int = 2500) -> bool:
    """Tiny JPEGs are usually solid-color covers / failed extracts."""
    try:
        return (not path.is_file()) or path.stat().st_size < min_bytes
    except OSError:
        return True


async def generate_recording_thumbnail(
    ts_file: Path,
    output_dir: Path,
    username: str,
    ffmpeg_path: str = "ffmpeg",
    *,
    replace_existing: bool = False,
) -> str | None:
    """Extract a cover frame; try several seeks so static intros do not win."""
    try:
        thumbs_dir = output_dir / "thumbnails" / username
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumbs_dir / f"{ts_file.stem}.jpg"

        if thumb_path.exists() and not replace_existing and not _thumbnail_looks_empty(thumb_path):
            return str(thumb_path)
        if thumb_path.exists() and (replace_existing or _thumbnail_looks_empty(thumb_path)):
            thumb_path.unlink(missing_ok=True)

        temporary = thumb_path.with_suffix(".tmp.jpg")
        temporary.unlink(missing_ok=True)
        # Prefer later / mid seeks first — Bilibili rooms often keep a static
        # cover or brown keyframe for the first tens of seconds.
        seeks = ("00:01:00", "00:00:30", "00:00:10", "00:00:03", "00:00:00")
        best_tmp = thumb_path.with_suffix(".best.tmp.jpg")
        best_tmp.unlink(missing_ok=True)
        best_size = 0
        for seek in seeks:
            process = await asyncio.create_subprocess_exec(
                ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                seek,
                "-i",
                str(ts_file),
                "-vframes",
                "1",
                "-vf",
                "scale=320:-1",
                "-y",
                str(temporary),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await wait_with_timeout(process, 20)
            if not temporary.exists():
                continue
            try:
                size = temporary.stat().st_size
            except OSError:
                temporary.unlink(missing_ok=True)
                continue
            if size <= 0:
                temporary.unlink(missing_ok=True)
                continue
            if size > best_size:
                best_tmp.unlink(missing_ok=True)
                temporary.replace(best_tmp)
                best_size = size
            else:
                temporary.unlink(missing_ok=True)
            # Detailed frames compress larger than solid-color covers.
            if best_size >= 2500:
                best_tmp.replace(thumb_path)
                return str(thumb_path)

        if best_tmp.exists() and best_size > 0:
            best_tmp.replace(thumb_path)
            return str(thumb_path)
        temporary.unlink(missing_ok=True)
        best_tmp.unlink(missing_ok=True)

    except Exception as e:
        logger.debug("Error generating recording thumbnail",
                    username=username,
                    filename=ts_file.name,
                    error=str(e))

    return None


async def _record_dirs_for_username(db: 'Database', username: str, output_dir: Path) -> list[Path]:
    records_root = output_dir / "records"
    candidates: list[Path] = []
    try:
        model = await db.get_model(username)
    except Exception:
        model = None

    record_path = (model or {}).get("record_path")
    if record_path:
        try:
            relative = Path(str(record_path))
            if not relative.is_absolute() and not any(part in {"", ".", ".."} for part in relative.parts):
                candidates.append(records_root / relative)
        except Exception:
            pass
    else:
        candidates.append(records_root / username)

    candidates.extend([
        records_root / username / "videos" / "record",
        records_root / username,
    ])

    unique: list[Path] = []
    seen: set[str] = set()
    root = records_root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            key = str(resolved)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


async def update_recordings_cache(db: 'Database', username: str, output_dir: Path, ffmpeg_path: str = "ffmpeg"):
    """Update the recordings cache in SQLite"""
    try:
        import time
        records_root = output_dir / "records"
        records_dirs = await _record_dirs_for_username(db, username, output_dir)

        # Purge orphan DB rows: a TS and MP4 file that no longer
        # exist on disk must be removed from the DB, otherwise the recording
        # stays shown on /recordings with no way to delete it from
        # the UI (e.g. manual file deletion, reset volume).
        # Safety: purge ONLY if the parent /records folder exists (otherwise
        # unmounted volume, we must not delete anything).
        if records_root.exists():
            existing_recs = await db.get_recordings(username)
            for rec in existing_recs:
                if rec.get('media_kind') == 'import':
                    continue
                ts_path_str = rec.get('file_path')
                mp4_path_str = rec.get('mp4_path')
                ts_exists = bool(ts_path_str) and Path(ts_path_str).exists()
                mp4_exists = bool(mp4_path_str) and Path(mp4_path_str).exists()
                if not ts_exists and not mp4_exists:
                    await db.delete_recording(username, rec['filename'])
                    logger.info(
                        "Orphan recording row deleted",
                        username=username,
                        filename=rec['filename'],
                        task="monitor",
                    )

        records_dirs = [records_dir for records_dir in records_dirs if records_dir.exists()]
        if not records_dirs:
            return

        async def cleanup_short_recording(ts_file: Path, existing_rec: dict | None, seconds_since_modification: float) -> bool:
            stat = ts_file.stat()
            cached_duration = int((existing_rec or {}).get('duration_seconds') or 0)
            duration_seconds = cached_duration

            if seconds_since_modification < 120:
                return False

            should_probe_duration = (
                stat.st_size == 0
                or stat.st_size <= SHORT_RECORDING_PROBE_BYTES
                or (0 < duration_seconds < MIN_RECORDING_SECONDS)
            )

            if should_probe_duration and duration_seconds == 0 and stat.st_size > 0:
                duration_seconds = await get_video_duration(ts_file, ffmpeg_path)

            too_short = (
                stat.st_size == 0
                or (duration_seconds == 0 and stat.st_size < MIN_RECORDING_BYTES)
                or (0 < duration_seconds < MIN_RECORDING_SECONDS)
            )

            if not too_short:
                return False

            mp4_path = ts_file.with_suffix('.mp4')
            thumb_path = output_dir / "thumbnails" / username / f"{ts_file.stem}.jpg"
            deleted = []
            for path, label in ((ts_file, "TS"), (mp4_path, "MP4"), (thumb_path, "thumbnail")):
                if path.exists():
                    try:
                        path.unlink()
                        deleted.append(label)
                    except Exception as e:
                        logger.error(
                            "Error deleting fragment recording",
                            username=username,
                            filename=path.name,
                            error=str(e),
                        )

            await db.delete_recording(username, ts_file.name)
            logger.warning(
                "Recording too short, deleted",
                username=username,
                filename=ts_file.name,
                duration_seconds=duration_seconds,
                file_size=stat.st_size,
                min_seconds=MIN_RECORDING_SECONDS,
                min_bytes=MIN_RECORDING_BYTES,
                deleted=deleted,
            )
            return True

        for ts_file in (ts_file for records_dir in records_dirs for ts_file in records_dir.glob("*.ts")):
            stat = ts_file.stat()

            # Fetch the current duration from the DB
            existing_recordings = await db.get_recordings(username)
            existing_rec = next((r for r in existing_recordings if r['filename'] == ts_file.name), None)
            seconds_since_modification = time.time() - stat.st_mtime

            if await cleanup_short_recording(ts_file, existing_rec, seconds_since_modification):
                continue

            # Compute duration only if it is not already cached or is 0
            duration_seconds = 0
            if existing_rec:
                duration_seconds = existing_rec.get('duration_seconds', 0)

            if duration_seconds == 0:
                # Ensure the file is stable (not modified for 120s)
                # to avoid computing duration on a file still being written
                if seconds_since_modification >= 120:
                    # Compute duration with ffprobe
                    duration_seconds = await get_video_duration(ts_file, ffmpeg_path)
                    logger.debug("Duration computed", username=username, filename=ts_file.name, duration=duration_seconds)
                else:
                    logger.debug("File not stable yet, skip duration calculation",
                               username=username,
                               filename=ts_file.name,
                               seconds_since_modification=int(seconds_since_modification))

            # Generate the thumbnail if it does not exist
            thumbnail_path = None
            if existing_rec:
                thumbnail_path = existing_rec.get('thumbnail_path')

            thumb_file = Path(thumbnail_path) if thumbnail_path else None
            if (
                not thumb_file
                or not thumb_file.exists()
                or _thumbnail_looks_empty(thumb_file)
            ):
                thumbnail_path = await generate_recording_thumbnail(
                    ts_file,
                    output_dir,
                    username,
                    ffmpeg_path,
                    replace_existing=bool(thumb_file and thumb_file.exists()),
                )
                if thumbnail_path:
                    logger.debug("Thumbnail generated", username=username, filename=ts_file.name, thumb=thumbnail_path)

            # Generate recording_id if this is a new recording
            recording_id = None
            if existing_rec:
                recording_id = existing_rec.get('recording_id')

            if not recording_id:
                # Extract the timestamp from the filename (format: YYYYMMDD_HHMMSS_xxx.ts)
                # Otherwise generate a new recording_id
                recording_id = f"{username}_{ts_file.stem}"

            existing_created_at = int((existing_rec or {}).get('created_at') or 0)
            created_at = existing_created_at
            filename_created_at = recording_timestamp_from_filename(ts_file.name)
            if (
                created_at in {0, int(stat.st_mtime)}
                or (filename_created_at is not None and created_at != filename_created_at)
            ):
                created_at = await get_media_created_at(
                    ts_file,
                    ffmpeg_path,
                    fallback_timestamp=int(stat.st_mtime),
                )

            await db.add_or_update_recording(
                username=username,
                filename=ts_file.name,
                file_path=str(ts_file),
                file_size=stat.st_size,
                recording_id=recording_id,
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path,
                created_at=created_at,
            )
            if duration_seconds:
                try:
                    start = int(created_at or 0)
                    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
                    seen_at = (start + int(duration_seconds)) if start > 0 else now_ts
                    await db.note_last_seen_online(username, seen_at=seen_at)
                except Exception:
                    pass

    except Exception as e:
        logger.debug("Error updating recordings cache", username=username, error=str(e))

async def monitor_models_task(
    db: 'Database',
    manager: 'FFmpegManager',
    ffmpeg_path: str = "ffmpeg",
    chaturbate_auth=None,
    provider_registry=None,
):
    """
    Background monitoring task.

    For each tracked model, check status via the provider registry
    when available, with the legacy Chaturbate path as fallback.

    ``chaturbate_auth`` provides authenticated cookies for check_model_status
    Chaturbate (avoids the login redirect, GH #11).
    """
    logger.background_task("monitor", "Starting continuous monitoring")

    csrftoken = os.getenv("CHATURBATE_CSRFTOKEN")
    if csrftoken:
        logger.info("CSRF token detected", has_token=True)

    await db.initialize()

    async def sleep_until_next_check():
        try:
            interval = await get_check_interval_seconds(db)
        except Exception as exc:
            logger.warning(
                "Unable to read the monitoring interval",
                task="monitor",
                error=str(exc),
            )
            interval = MONITOR_INTERVAL
        await asyncio.sleep(interval)

    async with aiohttp_client_session() as session:
        while True:
            try:
                models = await db.get_all_models()

                if not models:
                    await sleep_until_next_check()
                    continue

                logger.debug("Checking models", count=len(models))

                active_sessions = manager.list_status()

                for model in models:
                    username = model['username']
                    source_type = model.get('source_type') or 'chaturbate'

                    try:
                        if provider_registry is not None and provider_registry.has(source_type):
                            try:
                                status_obj = await provider_registry.get(source_type).check_status(username)
                                status = status_obj.as_dict() if hasattr(status_obj, "as_dict") else dict(status_obj)
                            except Exception as e:
                                logger.debug(
                                    "Provider check_status error",
                                    source_type=source_type,
                                    username=username,
                                    error=str(e),
                                )
                                status = {'is_online': False, 'viewers': 0, 'hls_source': None, 'room_status': None, 'tags': []}
                        else:
                            # Chaturbate: direct check with authenticated cookies
                            auth_cookies = (
                                chaturbate_auth.get_cookies()
                                if chaturbate_auth is not None
                                else None
                            )
                            status = await check_model_status(
                                session,
                                username,
                                csrftoken,
                                auth_cookies=auth_cookies,
                            )

                        # Check whether currently recording
                        active_session = next(
                            (
                                s
                                for s in active_sessions
                                if s.get('running')
                                and (s.get('source_type') or 'chaturbate') == source_type
                                and (s.get('target') or s.get('person')) == username
                            ),
                            None
                        )
                        is_recording = active_session is not None

                        # Generate/update the thumbnail. Live models
                        # are refreshed more often (60s) to reflect
                        # activity on the Discover / Following pages.
                        thumbnail_path = None
                        last_thumbnail_update = model.get('thumbnail_updated_at') or 0
                        thumb_interval = (
                            THUMBNAIL_UPDATE_INTERVAL_LIVE if status['is_online']
                            else THUMBNAIL_UPDATE_INTERVAL
                        )
                        needs_thumbnail_update = (
                            datetime.now().timestamp() - last_thumbnail_update > thumb_interval
                        )

                        if needs_thumbnail_update:
                            # 1) HTTP download from provider (zero CPU). Only for Chaturbate;
                            #    CAM4 has its own flow via download in followed/model APIs.
                            if status['is_online'] and source_type == 'chaturbate':
                                thumbnail_path = await download_thumbnail_from_chaturbate(
                                    session,
                                    username,
                                    OUTPUT_DIR
                                )

                            # 2) Fallback: ffmpeg extract 1 frame from our local HLS playlist
                            if not thumbnail_path and is_recording and active_session:
                                thumbnail_path = await generate_thumbnail_from_stream(
                                    username,
                                    active_session['id'],
                                    OUTPUT_DIR,
                                    ffmpeg_path
                                )

                            # 3) Offline fallback: extract from latest recording
                            if not thumbnail_path:
                                thumbnail_path = await generate_thumbnail_from_recording(
                                    username,
                                    OUTPUT_DIR,
                                    ffmpeg_path
                                )

                        # Update status in the DB
                        await db.update_model_status(
                            username=username,
                            is_online=status['is_online'],
                            viewers=status['viewers'],
                            is_recording=is_recording,
                            thumbnail_path=thumbnail_path,
                            room_status=status.get('room_status'),
                            source_type=source_type,
                        )

                        # Update the recordings cache
                        await update_recordings_cache(db, username, OUTPUT_DIR, ffmpeg_path)

                        logger.debug("Model updated",
                                   username=username,
                                   is_online=status['is_online'],
                                   is_recording=is_recording,
                                   viewers=status['viewers'])

                    except Exception as e:
                        logger.error("Error monitoring model",
                                   username=username,
                                   error=str(e),
                                   exc_info=True)
                        continue

                # Wait before the next check
                await sleep_until_next_check()

            except Exception as e:
                logger.error("Error in monitor task",
                           error=str(e),
                           exc_info=True)
                await sleep_until_next_check()

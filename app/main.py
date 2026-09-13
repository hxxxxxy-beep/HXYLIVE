import re
import base64
import mimetypes
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
import os
import asyncio
import aiohttp
import json
import shutil
import time
from datetime import datetime
import secrets
import threading
import hashlib
import http.client
import socket as raw_socket
import ipaddress
from html import unescape

from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .ffmpeg_runner import FFmpegManager
from .logger import logger
from .core.database import Database
from .core.config import MIN_RECORDING_BYTES, MIN_RECORDING_SECONDS
from .core.utils import format_bytes
from .services import recording_audit
from .services import recording_quota
from .services.recording_quota import (
    DEFAULT_MONTHLY_QUOTA_GB,
    DEFAULT_QUOTA_CYCLE_DAY,
    blocked_reason as _quota_blocked_reason,
    effective_monthly_quota_gb,
    gb_to_bytes,
    is_quota_exceeded,
    normalize_monthly_quota_gb,
    normalize_quota_cycle_day,
    quota_cycle_unix,
)
from .following_sync import (
    classify_follow_reconcile,
    ensure_streamer_card,
    ensure_streamer_card_for_follow,
    filter_following_items,
    store_provider_following,
    sync_items_authoritative,
    sync_items_skipped_reason,
    sync_items_trusted,
)
from .core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from .tasks.monitor import (
    CHECK_INTERVAL_SETTING_KEY,
    get_check_interval_seconds,
    get_media_created_at,
    get_video_duration,
    monitor_models_task,
    normalize_check_interval_seconds,
)
from .tasks.convert import auto_convert_recordings_task
from .tasks.retention import cleanup_retention_job
from .tasks.disk_pressure import (
    DEFAULT_LIBRARY_STOP_GB,
    DEFAULT_STAGING_STOP_GB,
    GB as DISK_GB,
    STORAGE_POLL_SECONDS,
    STORAGE_POLL_STRESSED_SECONDS,
    disk_free_bytes,
    normalize_library_stop_gb,
    normalize_staging_stop_gb,
    promote_closed_staging_files,
    staging_has_recording_files,
    storage_gate_decision,
)
from .tasks.media_imports import (
    DEFAULT_MIN_AGE_SECONDS,
    DIRECT_PLAYABLE_EXTENSIONS,
    MediaImportManager,
    create_playable_mp4_copy,
    generate_import_thumbnail,
    media_imports_task,
    mp4_needs_faststart_repair,
    remove_import_record,
    stable_import_recording_id,
    SUPPORTED_VIDEO_EXTENSIONS,
    title_from_filename,
)
from .recording_names import (
    ALLOWED_FILENAME_FORMATS,
    FILENAME_FORMAT_TIMESTAMP,
    format_identity_with_source,
    normalize_filename_format,
    normalize_source_marker,
    parse_identity_with_source,
    safe_filename_part,
)
from .services.flaresolverr import DEFAULT_FLARE_SERVICE_URL, FlareSolverrClient
from .services.chaturbate_auth import ChaturbateAuthService
from .services.chaturbate_api import ChaturbateAPI
from .providers import (
    ProviderAuthError,
    ProviderError,
    ProviderInteractionRequired,
    ProviderOfflineError,
    ProviderPrivateError,
    ProviderStatus,
    ResolvedStream,
    create_provider_registry,
)
from .api import auth as auth_router
from .api import discover as discover_router
from .api import following as following_router

# Environment
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "data")))
# Hot TS staging (SSD). Defaults under OUTPUT_DIR for single-volume tests/dev;
# production mounts a separate SSD at STAGING_DIR (e.g. /staging).
_STAGING_DIR_ENV = (os.getenv("STAGING_DIR") or "").strip()
STAGING_DIR = Path(_STAGING_DIR_ENV) if _STAGING_DIR_ENV else (OUTPUT_DIR / "staging")
APP_TZ = os.getenv("TZ", "UTC")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
HLS_TIME = int(os.getenv("HLS_TIME", "4"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))
CB_RESOLVER_ENABLED = os.getenv("CB_RESOLVER_ENABLED", "false").lower() in {"1", "true", "yes"}
PASSWORD = os.getenv("PASSWORD", "")  # Mot de passe optionnel
CHATURBATE_USERNAME = os.getenv("CHATURBATE_USERNAME", "")
CHATURBATE_PASSWORD = os.getenv("CHATURBATE_PASSWORD", "")
RECORDING_RANGE_CHUNK_SIZE = int(os.getenv("RECORDING_RANGE_CHUNK_SIZE", str(8 * 1024 * 1024)))
RECORDING_INITIAL_METADATA_MAX_BYTES = int(
    os.getenv("RECORDING_INITIAL_METADATA_MAX_BYTES", str(128 * 1024 * 1024))
)
MEDIA_IMPORTS_ENABLED = os.getenv("HXYLIVE_MEDIA_IMPORTS", "false").lower() in {"1", "true", "yes"}
MEDIA_LIBRARY_METADATA_MIN_AGE_SECONDS = DEFAULT_MIN_AGE_SECONDS
MEDIA_LIBRARY_VIDEO_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS
MEDIA_LIBRARY_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
MEDIA_LIBRARY_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}
MEDIA_LIBRARY_EXTENSIONS = (
    MEDIA_LIBRARY_VIDEO_EXTENSIONS
    | MEDIA_LIBRARY_IMAGE_EXTENSIONS
    | MEDIA_LIBRARY_AUDIO_EXTENSIONS
)
MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
PROFILE_IMAGES_DIR = OUTPUT_DIR / "profile-images"
PROFILE_IMAGE_MAX_BYTES = int(os.getenv("HXYLIVE_PROFILE_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
MAC_DOWNLOAD_LINK_TTL_SECONDS = max(
    300,
    int(os.getenv("HXYLIVE_DOWNLOAD_LINK_TTL_SECONDS", "21600")),
)
MAC_HELPER_SNAPSHOT_TTL_SECONDS = max(
    15,
    int(os.getenv("HXYLIVE_MAC_HELPER_SNAPSHOT_TTL_SECONDS", "45")),
)
MAC_HELPER_COMMAND_TTL_SECONDS = max(
    15,
    int(os.getenv("HXYLIVE_MAC_HELPER_COMMAND_TTL_SECONDS", "60")),
)
MAC_HELPER_COMMAND_MAX_QUEUED = 20
MAC_HELPER_IMPORT_TTL_SECONDS = max(
    60,
    int(os.getenv("HXYLIVE_MAC_HELPER_IMPORT_TTL_SECONDS", "180")),
)
MAC_HELPER_CHROME_IMPORT_SOURCES = {"bilibili", "twitch", "chaturbate", "stripchat", "youtube"}
MAC_HELPER_THUMB_COMMAND_MAX_QUEUED = 40
MAC_HELPER_THUMB_TTL_SECONDS = max(
    300,
    int(os.getenv("HXYLIVE_MAC_HELPER_THUMB_TTL_SECONDS", "21600")),
)
MAC_HELPER_THUMB_WAIT_SECONDS = max(
    0.0,
    float(os.getenv("HXYLIVE_MAC_HELPER_THUMB_WAIT_SECONDS", "8")),
)
MAC_HELPER_THUMB_MAX_BYTES = max(
    32 * 1024,
    int(os.getenv("HXYLIVE_MAC_HELPER_THUMB_MAX_BYTES", str(1_500_000))),
)
BABEPEDIA_BASE_URL = "https://www.babepedia.com"
BABEPEDIA_USER_AGENT = os.getenv(
    "HXYLIVE_PROFILE_IMAGE_USER_AGENT",
    "HXYLIVE/0.1",
)
MEDIA_LIBRARY_TEMP_SUFFIXES = (
    ".tmp",
    ".part",
    ".partial",
    ".download",
    ".crdownload",
)
IVS_PLAYER_ASSET_BASE_URL = "https://player.live-video.net/1.4.1"
IVS_PLAYER_ASSETS = {
    "amazon-ivs-player.min.js": "application/javascript",
    "amazon-ivs-worker.min.js": "application/javascript",
    "amazon-ivs-wasmworker.min.js": "application/javascript",
    "amazon-ivs-wasmworker.min.wasm": "application/wasm",
}
_IVS_PLAYER_ASSET_CACHE: dict[str, dict[str, object]] = {}

# Docker constants
DOCKER_SOCKET = '/var/run/docker.sock'
DOCKER_UPDATE_ENABLED = os.getenv("HXYLIVE_ALLOW_DOCKER_UPDATE", "false").lower() in {"1", "true", "yes"}
DOCKER_IMAGE = os.getenv("HXYLIVE_DOCKER_IMAGE", "").strip()
GITHUB_REPOSITORY = os.getenv("HXYLIVE_GITHUB_REPOSITORY", "").strip()


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection over Unix domain socket (for Docker API)."""
    def __init__(self, socket_path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = raw_socket.socket(raw_socket.AF_UNIX, raw_socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _docker_api(method, path, body=None, timeout=30):
    """Send a request to the Docker Engine API via Unix socket."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    headers = {}
    body_bytes = None
    if body is not None:
        body_bytes = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    conn.request(method, path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _get_container_id():
    """Detect the current Docker container ID."""
    hostname = raw_socket.gethostname()
    if hostname and len(hostname) >= 12:
        try:
            int(hostname[:12], 16)
            return hostname[:12]
        except ValueError:
            pass
    for path in ('/proc/self/cgroup', '/proc/self/mountinfo'):
        try:
            with open(path, 'r') as f:
                for line in f:
                    if '/docker/' in line:
                        for part in reversed(line.strip().split('/')):
                            if len(part) >= 12:
                                try:
                                    int(part[:12], 16)
                                    return part[:12]
                                except ValueError:
                                    continue
        except FileNotFoundError:
                continue
    return None


def _normalize_version(version: Optional[str]) -> str:
    return (version or "").strip().lstrip("v").strip()


def _version_parts(version: str) -> Optional[Tuple[int, ...]]:
    match = re.search(r"\d+(?:\.\d+)*", _normalize_version(version))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _is_update_available(current_version: str, latest_version: str) -> bool:
    current = _normalize_version(current_version)
    latest = _normalize_version(latest_version)
    if not current or current.lower() in {"dev", "local"} or not latest:
        return False
    if current == latest:
        return False

    current_parts = _version_parts(current)
    latest_parts = _version_parts(latest)
    if current_parts and latest_parts:
        width = max(len(current_parts), len(latest_parts))
        current_parts = current_parts + (0,) * (width - len(current_parts))
        latest_parts = latest_parts + (0,) * (width - len(latest_parts))
        return latest_parts > current_parts

    return current != latest


# Ensure dirs
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "records").mkdir(parents=True, exist_ok=True)
(STAGING_DIR / "records").mkdir(parents=True, exist_ok=True)

recording_audit.configure(OUTPUT_DIR)
logger.info("Output directory (library)", path=str(OUTPUT_DIR))
logger.info("Staging directory (hot TS)", path=str(STAGING_DIR))
logger.info("FFmpeg path", path=FFMPEG_PATH)
logger.info("HLS Configuration", hls_time=HLS_TIME, hls_list_size=HLS_LIST_SIZE)
logger.info("Chaturbate Resolver", enabled=CB_RESOLVER_ENABLED)
logger.info("Local media imports", enabled=MEDIA_IMPORTS_ENABLED)
if PASSWORD:
    logger.info("Authentication enabled", protected=True)
else:
    logger.info("Authentication disabled", protected=False)

app = FastAPI(title="HXYLIVE", version=os.environ.get("APP_VERSION", "dev"))

# Simple in-memory session manager
active_sessions = set()

def generate_session_token() -> str:
    """Generate a secure session token"""
    return secrets.token_urlsafe(32)

def verify_password(provided_password: str) -> bool:
    """Check whether the provided password matches"""
    return secrets.compare_digest(str(provided_password), str(PASSWORD))

def is_authenticated(session_token: Optional[str]) -> bool:
    """Check whether the session is valid"""
    if not PASSWORD:
        return True  # No authentication required
    return session_token in active_sessions

# Authentication middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Public routes (no authentication needed)
    public_paths = [
        "/login",
        "/api/login",
        "/favicon.ico",
        "/api/mac/helper/heartbeat",
        "/api/mac/helper/commands",
    ]
    public_prefixes = [
        "/static/",
        "/api/proxy/hls/",
        # Mac helper claims jobs with localSessionId; Chrome uses unguessable tokens.
        # POST /api/mac/download-jobs stays protected (no trailing slash on create).
        "/api/mac/download-jobs/",
        "/api/mac/download/",
    ]

    if request.url.path in public_paths or any(
        request.url.path.startswith(p) for p in public_prefixes
    ):
        return await call_next(request)
    # Helper uploads covers with localSessionId; the browser GET stays authenticated.
    if request.url.path == "/api/mac/helper/thumb" and request.method == "POST":
        return await call_next(request)
    if request.url.path == "/api/mac/helper/provider-session" and request.method == "POST":
        return await call_next(request)
    if request.url.path == "/api/mac/helper/provider-follow-result" and request.method == "POST":
        return await call_next(request)

    # If no password is configured, allow through
    if not PASSWORD:
        return await call_next(request)

    # Validate the session token
    session_token = request.cookies.get("session_token")

    if not is_authenticated(session_token):
        # Redirect to the login page
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        return RedirectResponse(url="/login", status_code=303)

    return await call_next(request)

# Middleware to log all requests
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

    # Log request
    logger.api_request(request.method, request.url.path)

    # Process request
    response = await call_next(request)

    # Log response
    duration_ms = (time.time() - start_time) * 1000
    logger.api_response(response.status_code, request.url.path, duration_ms)

    return response

# Permissive CORS configuration for Docker/self-hosted deployments
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=False,  # No credentials with wildcard origin
    allow_methods=["*"],  # Allow all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allow all headers
)

# Register API routers
app.include_router(auth_router.router)
app.include_router(discover_router.router)
app.include_router(following_router.router)

# Static mounts — disable cache so JS/CSS reloads are picked
# up immediately in dev (volume mount). Bandwidth overhead is
# negligible for local files.
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    no_cache_paths = {"/", "/discover", "/recordings", "/media", "/settings"}
    path = request.url.path
    # Discover JSON must never be served from browser/CDN cache — a stale
    # Chaturbate "all" page looks live until the user switches category.
    if (
        path.startswith("/static/")
        or path in no_cache_paths
        or path.startswith("/api/discover")
    ):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def _recording_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mkv":
        return "video/x-matroska"
    if ext == ".avi":
        return "video/x-msvideo"
    return "video/mp2t"


def _recording_headers(filename: str, file_size: int) -> dict:
    return {
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "public, max-age=3600",
    }


def _mp4_faststart_metadata_end(file_path: Path, file_size: int) -> Optional[int]:
    """Return the byte offset after an initial MP4 moov box, when it is before media data."""
    max_metadata_bytes = max(RECORDING_INITIAL_METADATA_MAX_BYTES, 0)
    if file_size <= 0 or max_metadata_bytes <= 0:
        return None

    probe_limit = min(file_size, max_metadata_bytes)
    offset = 0
    try:
        with open(file_path, "rb") as f:
            while offset + 8 <= probe_limit:
                f.seek(offset)
                header = f.read(16)
                if len(header) < 8:
                    return None

                box_size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    if len(header) < 16:
                        return None
                    box_size = int.from_bytes(header[8:16], "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset

                if box_size < header_size:
                    return None

                box_end = offset + box_size
                if box_end > file_size:
                    return None
                if box_type == b"moov":
                    return box_end if box_end <= max_metadata_bytes else None
                if box_type in {b"mdat", b"moof"}:
                    return None

                offset = box_end
    except OSError:
        return None

    return None


def _initial_open_range_chunk_size(file_path: Path, filename: str, file_size: int, range_header: str) -> Optional[int]:
    range_spec = range_header[len("bytes="):].strip() if range_header.startswith("bytes=") else ""
    if range_spec != "0-":
        return None
    if Path(filename).suffix.lower() not in {".mp4", ".m4v", ".mov"}:
        return None

    metadata_end = _mp4_faststart_metadata_end(file_path, file_size)
    if not metadata_end:
        return None
    return max(RECORDING_RANGE_CHUNK_SIZE, metadata_end)


def _parse_byte_range(
    range_header: str,
    file_size: int,
    open_ended_chunk_size: Optional[int] = None,
) -> Optional[tuple[int, int]]:
    if file_size <= 0 or not range_header.startswith("bytes="):
        return None

    range_spec = range_header[len("bytes="):].strip()
    if "," in range_spec or "-" not in range_spec:
        return None

    start_text, end_text = range_spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            if end_text:
                end = int(end_text)
            else:
                # Browsers commonly request "bytes=N-" for video. Returning a
                # bounded chunk keeps very long replays responsive and avoids a
                # single multi-GB response through Docker/proxy layers.
                chunk_size = (
                    open_ended_chunk_size
                    if open_ended_chunk_size is not None
                    else RECORDING_RANGE_CHUNK_SIZE
                )
                end = start + max(chunk_size, 1) - 1
            end = min(end, file_size - 1)
    except ValueError:
        return None

    if start < 0 or start >= file_size or end < start:
        return None
    return start, end


async def _serve_video_file_with_ranges(request: Request, file_path: Path, filename: str):
    """Serve a local video path with browser-friendly Range/HEAD support."""
    from fastapi.responses import StreamingResponse

    if not file_path.exists() or not file_path.is_file():
        logger.error("Video file not found", path=str(file_path))
        raise HTTPException(status_code=404, detail="Media not found")

    file_size = file_path.stat().st_size
    logger.file_operation("Read", str(file_path), size=file_size)

    media_type = _recording_media_type(filename)
    base_headers = _recording_headers(filename, file_size)
    range_header = request.headers.get("range")

    if range_header:
        open_ended_chunk_size = _initial_open_range_chunk_size(
            file_path,
            filename,
            file_size,
            range_header,
        )
        byte_range = _parse_byte_range(
            range_header,
            file_size,
            open_ended_chunk_size=open_ended_chunk_size,
        )
        if not byte_range:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                }
            )

        start, end = byte_range
        chunk_size = end - start + 1

        async def range_file_stream():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    read_size = min(remaining, 64 * 1024)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(chunk_size),
        }

        if request.method == "HEAD":
            return Response(status_code=206, media_type=media_type, headers=headers)

        return StreamingResponse(
            range_file_stream(),
            status_code=206,
            media_type=media_type,
            headers=headers
        )

    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=base_headers)

    return FileResponse(str(file_path), media_type=media_type, headers=base_headers)


def _media_library_kind(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return "image"
    if ext in MEDIA_LIBRARY_VIDEO_EXTENSIONS:
        return "video"
    if ext in MEDIA_LIBRARY_AUDIO_EXTENSIONS:
        return "audio"
    return None


def _is_media_library_file(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name
    if name.startswith(".") or any(name.endswith(suffix) for suffix in MEDIA_LIBRARY_TEMP_SUFFIXES):
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    return _media_library_kind(path) is not None


def _quote_url_path(path_value: str) -> str:
    return "/".join(quote(part, safe="") for part in Path(path_value).parts)


def _content_disposition(filename: str, disposition: str = "inline") -> str:
    fallback = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip() or "media"
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'


def _resolve_library_media_path(username: str, file_path: str) -> Path:
    if (
        not username
        or ".." in username
        or "/" in username
        or "\\" in username
        or "\x00" in username
        or "\x00" in file_path
        or "\\" in file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid media path")

    relative = Path(file_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid media path")

    records_root = (OUTPUT_DIR / "records").resolve()
    profile_root = (records_root / username).resolve()
    candidate = (profile_root / relative).resolve()

    if not candidate.is_relative_to(profile_root) or not candidate.is_relative_to(records_root):
        raise HTTPException(status_code=403, detail="Media path is not allowed")
    if _media_library_kind(candidate) is None:
        raise HTTPException(status_code=400, detail="Unsupported media format")
    return candidate


def _validate_media_profile_username(username: str) -> str:
    username = (username or "").strip()
    if (
        not username
        or username in {".", ".."}
        or "/" in username
        or "\\" in username
        or "\x00" in username
    ):
        raise HTTPException(status_code=400, detail="Invalid media profile")
    return username


def _media_profile_dir(username: str) -> Path:
    username = _validate_media_profile_username(username)
    records_root = (OUTPUT_DIR / "records").resolve()
    profile_dir = (records_root / username).resolve()
    if not profile_dir.is_relative_to(records_root):
        raise HTTPException(status_code=403, detail="Media profile is not allowed")
    return profile_dir


def _list_media_profile_folders() -> set[str]:
    records_root = OUTPUT_DIR / "records"
    if not records_root.exists():
        return set()

    profiles: set[str] = set()
    for child in records_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            _validate_media_profile_username(child.name)
        except HTTPException:
            continue
        profiles.add(child.name)
    return profiles


async def _repair_truncated_media_profile_usernames() -> int:
    try:
        profiles = await db.get_all_media_profiles()
        profile_names = {
            profile["username"]
            for profile in profiles
            if profile.get("username")
        }
        if not profile_names:
            return 0
        model_names = {
            model["username"]
            for model in await db.get_all_models()
            if model.get("username")
        }
        folder_names = _list_media_profile_folders()
        candidate_names = model_names | folder_names
        repaired = 0
        for profile_name in sorted(profile_names):
            candidates = [
                candidate
                for candidate in candidate_names
                if candidate != profile_name
                and candidate not in profile_names
                and (candidate.startswith("_") or candidate.endswith("_"))
                and candidate.strip("_") == profile_name
            ]
            if len(candidates) != 1:
                continue
            if await db.rename_media_profile(profile_name, candidates[0]):
                repaired += 1
                profile_names.discard(profile_name)
                profile_names.add(candidates[0])
                logger.info(
                    "Media profile repaired after underscore truncation",
                    old_username=profile_name,
                    new_username=candidates[0],
                )
        return repaired
    except Exception as exc:
        logger.debug("Truncated media profile repair ignored", error=str(exc))
        return 0


def _media_library_url(username: str, relative_path: str, download: bool = False) -> str:
    url = f"/streams/library/{quote(username, safe='')}/{_quote_url_path(relative_path)}"
    if download:
        return f"{url}?download=1"
    return url


def _model_for_media_profile(username: str, models: list[dict]) -> Optional[dict]:
    selected = None
    for model in models:
        if model.get("username") != username:
            continue
        if selected is None:
            selected = model
        if (model.get("source_type") or model.get("sourceType")) == "chaturbate":
            return model
    return selected


def _coerce_unix_timestamp(value: object) -> Optional[int]:
    try:
        stamp = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return stamp if stamp > 0 else None


def _media_profile_last_seen_online_at(
    *,
    profile_username: str,
    channel_username: str,
    source_type: str,
    followed_by_key: dict[tuple[str, str], dict],
    media_profile: Optional[dict] = None,
    model: Optional[dict] = None,
) -> Optional[int]:
    """Best-effort last-online stamp for Media cards."""
    candidates: list[int] = []
    stamp = _coerce_unix_timestamp((media_profile or {}).get("last_seen_online_at"))
    if stamp:
        candidates.append(stamp)
    source = _normalize_source_type(source_type) or "chaturbate"
    for candidate in (channel_username, profile_username):
        key = (str(candidate or "").strip().lower(), source)
        if not key[0]:
            continue
        row = followed_by_key.get(key)
        if not row:
            continue
        stamp = _coerce_unix_timestamp(row.get("last_seen_online_at"))
        if stamp:
            candidates.append(stamp)
    stamp = _coerce_unix_timestamp((model or {}).get("last_seen_online_at"))
    if stamp:
        candidates.append(stamp)
    return max(candidates) if candidates else None


def _media_profile_last_live_at(
    *,
    last_seen_online_at: Optional[int],
    profile: dict,
) -> Optional[int]:
    """Offline card timestamp: newest of last-seen and latest recording/media."""
    candidates: list[int] = []
    for value in (
        last_seen_online_at,
        profile.get("latestVideoAt"),
        profile.get("latestAt"),
    ):
        stamp = _coerce_unix_timestamp(value)
        if stamp:
            candidates.append(stamp)
    return max(candidates) if candidates else None


def _normalize_profile_image_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Profile image URL must be an HTTP URL")
    return raw


def _is_stripchat_snapshot_url(value: object) -> bool:
    raw = str(value or "").strip().lower()
    return "doppiocdn." in raw and "/snapshot/" in raw


def _is_chaturbate_riw_url(value: object) -> bool:
    raw = str(value or "").strip().lower()
    return "thumb.live.mmcdn.com/riw/" in raw


def _is_chaturbate_room_poster_url(value: object) -> bool:
    """Chaturbate /ri/ room preview — looks like a live screenshot, not a face."""
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if "roomimg.stream.highwebmedia.com/ri/" in raw:
        return True
    if "thumb.live.mmcdn.com/ri/" in raw and "/riw/" not in raw:
        return True
    return False


def _is_live_cover_profile_image(value: object) -> bool:
    """True for live webcam/room covers that must never be circular avatars."""
    if (
        _is_chaturbate_riw_url(value)
        or _is_chaturbate_room_poster_url(value)
        or _is_stripchat_snapshot_url(value)
    ):
        return True
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if "/previews/" in raw and ("doppiocdn." in raw or "static-proxy.strpst.com" in raw):
        return True
    # Twitch Helix stream preview (live frame) — not the profile face photo.
    if "previews-ttv" in raw or "live_user_" in raw:
        return True
    if "i.ytimg.com/" in raw or "ytimg.com/vi/" in raw:
        return True
    return False


def _rewrite_chaturbate_poster_url(value: object) -> str:
    """Rewrite expired roomimg posters to the current mmcdn /ri/ host."""
    raw = str(value or "").strip()
    match = re.match(
        r"^https?://roomimg\.stream\.highwebmedia\.com/ri/([A-Za-z0-9_]+)\.jpg(?:\?.*)?$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"https://thumb.live.mmcdn.com/ri/{match.group(1).lower()}.jpg"
    return raw


def _media_live_profile_image_url(
    item: dict[str, Any],
    *,
    source_type: str,
    is_online: bool,
) -> str:
    """Pick a Media-card avatar from provider live/search metadata.

    Stripchat: only real ``/avatars/`` photos — never live snapshots or room
    previews (UI shows a letter avatar when empty). Chaturbate live ``riw``
    frames are webcam covers — prefer a real summary/profile photo when the
    payload still carries one.
    """
    avatar = _rewrite_chaturbate_poster_url(
        item.get("profile_image_url") or item.get("profileImageUrl") or ""
    )
    thumb = _rewrite_chaturbate_poster_url(item.get("thumbnail") or "")
    source = str(source_type or "").strip().lower()
    if source in {"chaturbate", "stripchat"}:
        # Live covers are never circular avatars — empty → letter avatar in UI.
        if avatar and not _is_live_cover_profile_image(avatar):
            return avatar
        return ""
    if avatar and not _is_live_cover_profile_image(avatar):
        return avatar
    if thumb and not _is_live_cover_profile_image(thumb):
        return thumb
    return ""


def _validate_public_http_url(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid remote image URL")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        raise HTTPException(status_code=400, detail="Remote image URL is not allowed")
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if not address.is_global:
            raise HTTPException(status_code=400, detail="Remote image URL is not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Invalid remote image URL")
    return raw


class _PublicAddressResolver:
    """Reject non-public DNS answers at the point aiohttp opens a socket."""

    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=raw_socket.AF_INET):
        results = await self._resolver.resolve(host, port, family)
        if not results:
            raise OSError("Remote image host did not resolve")
        for result in results:
            raw_address = str(result.get("host") or "").split("%", 1)[0]
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise OSError("Remote image host returned an invalid address") from exc
            mapped = getattr(address, "ipv4_mapped", None)
            if mapped is not None:
                address = mapped
            if not address.is_global:
                raise OSError("Remote image host resolved to a non-public address")
        return results

    async def close(self):
        await self._resolver.close()


async def _fetch_public_http_bytes(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    max_bytes: int,
) -> dict:
    current_url = _validate_public_http_url(url)
    resolver = _PublicAddressResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=True)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            connector=connector,
            trust_env=False,
        ) as session:
            for redirect_count in range(6):
                current_url = _validate_public_http_url(current_url)
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_count >= 5:
                            raise HTTPException(status_code=400, detail="Remote image redirect is not allowed")
                        current_url = urljoin(current_url, location)
                        continue

                    content_length = response.headers.get("Content-Length")
                    try:
                        if content_length is not None and int(content_length) > max_bytes:
                            raise HTTPException(status_code=413, detail="Remote image response is too large")
                    except ValueError:
                        pass

                    body = bytearray()
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        if not chunk:
                            continue
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise HTTPException(status_code=413, detail="Remote image response is too large")
                    return {
                        "status": response.status,
                        "body": bytes(body),
                        "contentType": (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower(),
                        "url": str(response.url),
                    }
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.warning("Remote profile image request blocked or failed", error=str(exc))
        raise HTTPException(status_code=400, detail="Remote image host is not reachable or allowed")

    raise HTTPException(status_code=400, detail="Remote image redirect is not allowed")


def _normalize_profile_source_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Profile image source URL must be an HTTP URL")
    return raw


def _is_local_profile_image_url(value: object) -> bool:
    raw = str(value or "").strip()
    return raw.startswith("/api/media-profiles/") and "/profile-image" in raw


def _profile_image_local_url(username: str, image_path: object) -> str:
    raw = str(image_path or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        if not path.exists() or not path.is_file() or not _path_is_inside_output(path):
            return ""
        version = int(path.stat().st_mtime)
    except OSError:
        return ""
    return f"/api/media-profiles/{quote(username, safe='')}/profile-image?v={version}"


def _media_profile_image_url(profile: dict) -> str:
    username = profile.get("username") or ""
    local_url = _profile_image_local_url(username, profile.get("profile_image_path"))
    if local_url:
        return local_url
    remote_url = str(profile.get("profile_image_url") or "").strip()
    if remote_url.startswith("http://") or remote_url.startswith("https://"):
        return remote_url
    return ""


def _is_babepedia_image_ref(*values: object) -> bool:
    """True when a stored avatar URL clearly came from Babepedia."""
    for value in values:
        raw = str(value or "").strip().lower()
        if "babepedia." in raw:
            return True
    return False


def _pick_media_card_avatar(
    *,
    media_profile: Optional[dict] = None,
    live_image: object = "",
    model_image: object = "",
) -> str:
    """Choose the circular Media-card face.

    Prefer a platform-native live face over Babepedia. Babepedia slug pages
    often surface the wrong person's gallery thumb (e.g. Lillyfee for Tiffy),
    and local Mac cards hydrate via ``/api/model/.../status`` which used to
    prefer that stored file and flash the wrong avatar before the web card
    arrived with the Twitch Helix face.
    """
    live = _rewrite_chaturbate_poster_url(live_image or "")
    if live and not _is_live_cover_profile_image(live):
        return live

    profile = media_profile or {}
    stored = _rewrite_chaturbate_poster_url(_media_profile_image_url(profile))
    stored_is_babepedia = _is_babepedia_image_ref(
        profile.get("profile_image_source_url"),
        profile.get("profile_image_url"),
    )
    model = _rewrite_chaturbate_poster_url(model_image or "")
    if model and not _is_live_cover_profile_image(model):
        if stored_is_babepedia or not stored:
            return model

    if stored and not _is_live_cover_profile_image(stored):
        return stored
    if model and not _is_live_cover_profile_image(model):
        return model
    return ""


def _media_profile_formatted(profile: Optional[dict]) -> dict:
    profile = profile or {}
    birth_date = profile.get("birth_date") or ""
    profile_image_url = _media_profile_image_url(profile)
    profile_image_source_url = profile.get("profile_image_source_url") or ""
    return {
        "displayName": profile.get("display_name") or "",
        "firstName": profile.get("first_name") or "",
        "lastName": profile.get("last_name") or "",
        "age": profile.get("age"),
        "birthDate": birth_date,
        "birth_date": birth_date,
        "profileImageUrl": profile_image_url,
        "profile_image_url": profile_image_url,
        "profileImageSourceUrl": profile_image_source_url,
        "profile_image_source_url": profile_image_source_url,
        "address": profile.get("address") or "",
        "city": profile.get("city") or "",
        "region": profile.get("region") or "",
        "postalCode": profile.get("postal_code") or "",
        "country": profile.get("country") or "",
        "aliases": profile.get("aliases") or "",
        "tags": profile.get("tags") or "",
        "notes": profile.get("notes") or "",
        "socialUrls": profile.get("social_urls") or [],
        "streamUrls": profile.get("stream_urls") or [],
        "profileUrls": profile.get("profile_urls") or [],
    }


def _recording_source_session_key(profile_username: str, source_type: str, channel_username: str) -> str:
    return ":".join([
        _validate_media_profile_username(profile_username),
        (source_type or "chaturbate").strip().lower(),
        _validate_media_profile_username(channel_username),
    ])


def _channel_username_from_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname.endswith("youtube.com") or hostname in {"youtu.be", "www.youtu.be"}:
        from .providers.youtube import youtube_username_from_url
        return youtube_username_from_url(raw)
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if not parts:
        return None
    ignored = {"b", "chat", "en", "fr", "room", "rooms", "videochat"}
    for part in parts:
        if part.lower() not in ignored:
            return part.lstrip("@")
    return parts[-1].lstrip("@")


def _canonical_stream_url(source_type: str, channel_username: str, channel_url: Optional[str] = None) -> str:
    raw_url = str(channel_url or "").strip()
    normalized_source = (source_type or "chaturbate").strip().lower()
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            # Keep matching / unrecognized URLs. Rebuild when the host belongs
            # to a different provider (e.g. leftover chaturbate.com on stripchat).
            url_source = _source_type_from_url(raw_url)
            if not url_source or url_source == normalized_source:
                return raw_url
    channel = quote((channel_username or "").strip().lstrip("@"), safe="")
    if normalized_source == "chaturbate":
        return f"https://chaturbate.com/{channel}/"
    try:
        if provider_registry.has(normalized_source):
            return provider_registry.get(normalized_source).canonical_url(channel_username)
    except Exception:
        pass
    return raw_url


def _normalize_live_channel_username(value: object, channel_url: object = None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = _channel_username_from_url(raw) or ""
    if not raw:
        raw = _channel_username_from_url(channel_url) or ""
    raw = raw.strip().lstrip("@")
    return _validate_media_profile_username(raw)


def _profile_source_response(source: dict) -> dict:
    source_type = (source.get("source_type") or source.get("sourceType") or "chaturbate").strip().lower()
    channel_username = source.get("channel_username") or source.get("channelUsername") or source.get("username") or ""
    profile_username = source.get("profile_username") or source.get("profileUsername") or ""
    record_path = source.get("record_path") or _default_record_path(profile_username or channel_username)
    retention_value = source.get("retention_days")
    if retention_value is None:
        retention_value = source.get("retentionDays", 30)
    try:
        retention_days = int(retention_value if retention_value is not None else 30)
    except (TypeError, ValueError):
        retention_days = 30
    raw_quota = source.get("monthly_quota_gb")
    if raw_quota is None:
        raw_quota = source.get("monthlyQuotaGb")
    if raw_quota is None or raw_quota == "":
        monthly_quota_gb = None
    else:
        try:
            monthly_quota_gb = int(raw_quota)
        except (TypeError, ValueError):
            monthly_quota_gb = None
    return {
        "id": source.get("id"),
        "profileUsername": profile_username,
        "profile_username": profile_username,
        "sourceType": source_type,
        "source_type": source_type,
        "channelUsername": channel_username,
        "channel_username": channel_username,
        "channelUrl": _canonical_stream_url(
            source_type,
            channel_username,
            source.get("channel_url") or source.get("channelUrl"),
        ),
        "channel_url": _canonical_stream_url(
            source_type,
            channel_username,
            source.get("channel_url") or source.get("channelUrl"),
        ),
        "autoRecord": bool(source.get("auto_record", source.get("autoRecord", False))),
        "auto_record": bool(source.get("auto_record", source.get("autoRecord", False))),
        "recordQuality": source.get("record_quality") or source.get("recordQuality") or "best",
        "record_quality": source.get("record_quality") or source.get("recordQuality") or "best",
        "monthlyQuotaGb": monthly_quota_gb,
        "monthly_quota_gb": monthly_quota_gb,
        "retentionDays": retention_days,
        "retention_days": retention_days,
        "recordPath": record_path,
        "record_path": record_path,
        "sessionKey": _recording_source_session_key(profile_username or channel_username, source_type, channel_username) if channel_username else "",
    }


async def _media_profile_stream_sources(username: str) -> list[dict]:
    username = _validate_media_profile_username(username)
    sources = await db.get_media_profile_sources(username)
    if sources:
        return [_profile_source_response(source) for source in sources]

    model = await db.get_model(username)
    if not model:
        return []
    source_type = await _infer_source_type(username, model)
    return [_profile_source_response({
        "profile_username": username,
        "source_type": source_type,
        "channel_username": username,
        "channel_url": _canonical_stream_url(source_type, username),
        "auto_record": bool(model.get("auto_record", False)),
        "record_quality": model.get("record_quality") or await _get_default_record_quality(),
        "monthly_quota_gb": model.get("monthly_quota_gb"),
        "retention_days": model.get("retention_days", await _get_default_retention_days()),
        "record_path": _record_path_from_model(model, username),
    })]


async def _normalize_profile_source_payload(
    profile_username: str,
    raw_source: dict,
    default_auto_record: bool = False,
) -> dict:
    raw_source = raw_source or {}
    channel_url = raw_source.get("channelUrl") or raw_source.get("channel_url") or raw_source.get("url") or raw_source.get("sourceUrl")
    source_from_url = _source_type_from_url(str(channel_url or ""))
    requested_source = _normalize_source_type(
        raw_source.get("sourceType")
        or raw_source.get("source_type")
    )
    if source_from_url and (not requested_source or requested_source == "chaturbate"):
        source_type = source_from_url
    else:
        source_type = requested_source or source_from_url or "chaturbate"
    if source_type not in _available_source_types():
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is not available")

    channel_username = _normalize_live_channel_username(
        raw_source.get("channelUsername")
        or raw_source.get("channel_username")
        or raw_source.get("modelUsername")
        or raw_source.get("model_username")
        or raw_source.get("target")
        or raw_source.get("username"),
        channel_url,
    )
    retention_days = _normalize_retention_days(
        raw_source.get("retentionDays") if "retentionDays" in raw_source else raw_source.get("retention_days"),
        await _get_default_retention_days(),
    )
    record_quality = (
        raw_source.get("recordQuality")
        or raw_source.get("record_quality")
        or await _get_default_record_quality()
    )
    if "monthlyQuotaGb" in raw_source or "monthly_quota_gb" in raw_source:
        try:
            monthly_quota_gb = recording_quota.normalize_monthly_quota_gb(
                raw_source.get("monthlyQuotaGb")
                if "monthlyQuotaGb" in raw_source
                else raw_source.get("monthly_quota_gb"),
                allow_none=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        monthly_quota_gb = None
    auto_record = bool(raw_source.get("autoRecord", raw_source.get("auto_record", default_auto_record)))
    record_path = _normalize_record_path(
        raw_source.get("recordPath") or raw_source.get("record_path") or _default_record_path(profile_username),
        profile_username,
    )
    return {
        "profile_username": profile_username,
        "source_type": source_type,
        "channel_username": channel_username,
        "channel_url": _canonical_stream_url(source_type, channel_username, channel_url),
        "auto_record": auto_record,
        "record_quality": record_quality,
        "monthly_quota_gb": monthly_quota_gb,
        "retention_days": retention_days,
        "record_path": record_path,
    }


def _normalize_birth_date(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Birth date must use YYYY-MM-DD")
    return parsed.strftime("%Y-%m-%d")


def _babepedia_slug(value: str) -> str:
    raw = re.sub(r"\s+", "_", str(value or "").strip())
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip(".-")
    return raw


def _babepedia_candidate_urls(username: str, profile: dict, body: dict) -> list[str]:
    candidates: list[str] = []

    def add_url(value: object):
        raw = str(value or "").strip()
        if not raw:
            return
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc and raw not in candidates:
            candidates.append(raw)

    def add_query(value: object):
        raw = str(value or "").strip()
        if not raw:
            return
        slug = _babepedia_slug(raw)
        if slug:
            add_url(f"{BABEPEDIA_BASE_URL}/babe/{quote(slug, safe='')}")

    add_url(body.get("sourceUrl") or body.get("source_url"))
    for url in body.get("profileUrls") or body.get("profile_urls") or profile.get("profile_urls") or []:
        if "babepedia." in str(url).lower():
            add_url(url)

    add_query(body.get("query"))
    add_query(profile.get("display_name"))
    first_last = " ".join(
        part for part in [profile.get("first_name"), profile.get("last_name")] if part
    )
    add_query(first_last)
    for alias in re.split(r"[,;\n]+", str(profile.get("aliases") or "")):
        add_query(alias)
    add_query(username)
    return candidates


def _direct_image_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if Path(parsed.path).suffix.lower() in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return raw
    return None


def _html_meta_content(html: str, key: str) -> Optional[str]:
    for tag in re.findall(r"<meta\b[^>]*>", html or "", flags=re.IGNORECASE):
        if re.search(
            r"(?:property|name)\s*=\s*['\"]" + re.escape(key) + r"['\"]",
            tag,
            flags=re.IGNORECASE,
        ):
            match = re.search(r"content\s*=\s*['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
            if match:
                return unescape(match.group(1).strip())
    return None


def _extract_babepedia_image_url(html: str, page_url: str) -> Optional[str]:
    for key in ("og:image", "twitter:image", "twitter:image:src"):
        image_url = _html_meta_content(html, key)
        if image_url:
            return urljoin(page_url, image_url)

    focused = re.search(
        r"<img\b[^>]*(?:class|id)\s*=\s*['\"][^'\"]*(?:profile|babe|thumb|photo)[^'\"]*['\"][^>]*>",
        html or "",
        flags=re.IGNORECASE,
    )
    if focused:
        match = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", focused.group(0), flags=re.IGNORECASE)
        if match:
            return urljoin(page_url, unescape(match.group(1).strip()))

    return None


async def _fetch_text_url(url: str) -> str:
    headers = {"User-Agent": BABEPEDIA_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    response = await _fetch_public_http_bytes(
        url,
        headers=headers,
        timeout_seconds=20,
        max_bytes=2 * 1024 * 1024,
    )
    if response["status"] >= 400:
        raise HTTPException(status_code=404, detail="Profile image source not found")
    return response["body"].decode("utf-8", errors="ignore")


def _profile_image_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return ".jpg" if guessed == ".jpeg" else guessed
    return ".jpg"


async def _download_profile_image(username: str, image_url: str) -> dict:
    headers = {"User-Agent": BABEPEDIA_USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    response = await _fetch_public_http_bytes(
        image_url,
        headers=headers,
        timeout_seconds=30,
        max_bytes=PROFILE_IMAGE_MAX_BYTES,
    )
    if response["status"] >= 400:
        raise HTTPException(status_code=404, detail="Profile image could not be downloaded")

    content_type = response["contentType"]
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Profile image source did not return an image")

    extension = _profile_image_extension(response["url"], content_type)
    PROFILE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    destination = PROFILE_IMAGES_DIR / f"{username}{extension}"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(response["body"])
    temporary.replace(destination)
    size = len(response["body"])

    return {
        "path": str(destination),
        "size": size,
        "contentType": content_type or mimetypes.guess_type(destination.name)[0] or "image/jpeg",
    }


async def _resolve_profile_image_from_babepedia(username: str, profile: dict, body: dict) -> dict:
    direct_url = _direct_image_url(body.get("profileImageUrl") or body.get("profile_image_url"))
    if direct_url:
        return {"imageUrl": direct_url, "sourceUrl": body.get("sourceUrl") or body.get("source_url") or direct_url}

    candidates = _babepedia_candidate_urls(username, profile, body)
    if not candidates:
        raise HTTPException(status_code=404, detail="No Babepedia profile image candidate found")

    last_error: Optional[Exception] = None
    for candidate in candidates:
        direct = _direct_image_url(candidate)
        if direct:
            return {"imageUrl": direct, "sourceUrl": direct}

        try:
            html = await _fetch_text_url(candidate)
            image_url = _extract_babepedia_image_url(html, candidate)
            if image_url:
                return {"imageUrl": image_url, "sourceUrl": candidate}
        except HTTPException as e:
            last_error = e
        except Exception as e:
            last_error = e
            logger.debug("Babepedia profile image resolve failed", url=candidate, error=str(e))

    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(status_code=404, detail="No Babepedia profile image found")


def _profile_image_update_from_body(body: dict, existing: dict) -> dict:
    has_image_url = "profileImageUrl" in body or "profile_image_url" in body
    raw_image_url = body.get("profileImageUrl") if "profileImageUrl" in body else body.get("profile_image_url")
    has_source_url = "profileImageSourceUrl" in body or "profile_image_source_url" in body
    raw_source_url = (
        body.get("profileImageSourceUrl")
        if "profileImageSourceUrl" in body
        else body.get("profile_image_source_url")
    )

    image_path = existing.get("profile_image_path")
    image_url = existing.get("profile_image_url")
    source_url = existing.get("profile_image_source_url")

    if has_image_url:
        if _is_local_profile_image_url(raw_image_url):
            image_url = existing.get("profile_image_url")
            image_path = existing.get("profile_image_path")
        elif str(raw_image_url or "").strip():
            image_url = _normalize_profile_image_url(raw_image_url)
            image_path = None
        else:
            image_url = None
            image_path = None

    if has_source_url:
        source_url = _normalize_profile_source_url(raw_source_url) if str(raw_source_url or "").strip() else None

    return {
        "profile_image_url": image_url,
        "profile_image_source_url": source_url,
        "profile_image_path": image_path,
    }


def _pick_primary_profile_source(stream_sources: list[dict]) -> Optional[dict]:
    """Choose the source that should label the Media streamer card.

    Prefer an auto-record source. When several exist (or none are recording),
    avoid alphabetical order which puts chaturbate ahead of twitch.
    """
    if not stream_sources:
        return None
    priority = {
        "twitch": 1,
        "youtube": 2,
        "bilibili": 3,
        "stripchat": 4,
        "chaturbate": 5,
    }

    def sort_key(source: dict) -> tuple:
        source_type = _normalize_source_type(
            source.get("sourceType") or source.get("source_type")
        ) or "chaturbate"
        auto = bool(
            source.get("autoRecord")
            if source.get("autoRecord") is not None
            else source.get("auto_record", False)
        )
        channel = str(
            source.get("channelUsername") or source.get("channel_username") or ""
        ).lower()
        return (0 if auto else 1, priority.get(source_type, 9), source_type, channel)

    return sorted(stream_sources, key=sort_key)[0]


async def _media_profile_payload(username: str) -> dict:
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    default_quality = await _get_default_record_quality()
    default_retention = await _get_default_retention_days()
    stream_sources = await _media_profile_stream_sources(username)
    primary_source = _pick_primary_profile_source(stream_sources)
    source_type = (primary_source or {}).get("sourceType") or (await _infer_source_type(username, model) if model else "chaturbate")
    auto_record = bool((primary_source or {}).get("autoRecord", False)) if primary_source else (bool(model.get("auto_record", False)) if model else False)
    record_quality = (primary_source or {}).get("recordQuality") or (model or {}).get("record_quality") or default_quality
    retention_days = (primary_source or {}).get("retentionDays")
    if retention_days is None:
        retention_days = (model or {}).get("retention_days", default_retention)
    stored_quota = (primary_source or {}).get("monthlyQuotaGb")
    if stored_quota is None:
        stored_quota = (model or {}).get("monthly_quota_gb")
    quota_fields = await _profile_quota_snapshot(username, stored_quota_gb=stored_quota)

    gap_raw = await db.get_setting(f"gap_buffer_minutes:{username}")
    from app.services.recording_projects import (
        DEFAULT_GAP_BUFFER_MINUTES,
        normalize_gap_buffer_minutes,
    )

    gap_buffer_minutes = None
    if gap_raw is not None and str(gap_raw).strip() != "":
        gap_buffer_minutes = normalize_gap_buffer_minutes(gap_raw, DEFAULT_GAP_BUFFER_MINUTES)

    return {
        "username": username,
        "folderExists": profile_dir.exists() and profile_dir.is_dir(),
        "deleteUrl": f"/api/media-profiles/{quote(username, safe='')}",
        **_media_profile_formatted(profile),
        "sourceType": source_type,
        "source_type": source_type,
        "autoRecord": auto_record,
        "recordQuality": record_quality,
        "retentionDays": retention_days,
        "streamSources": stream_sources,
        "stream_sources": stream_sources,
        "gapBufferMinutes": gap_buffer_minutes,
        "gap_buffer_minutes": gap_buffer_minutes,
        **quota_fields,
        **_record_path_fields(username, model),
    }


def _media_library_placeholder_title(filename: str) -> str:
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return re.sub(r"\s+", " ", title) or filename


def _recording_thumb_url(username: str, rec: Optional[dict], path: Path) -> Optional[str]:
    thumb_value = (rec or {}).get("thumbnail_path")
    if thumb_value:
        thumb_path = Path(thumb_value)
        if thumb_path.exists():
            return f"/api/recording-thumbnail/{quote(username, safe='')}/{quote(thumb_path.name, safe='')}"

    generated = OUTPUT_DIR / "thumbnails" / username / f"{path.stem}.jpg"
    if generated.exists():
        return f"/api/recording-thumbnail/{quote(username, safe='')}/{quote(generated.name, safe='')}"
    return None


_MEDIA_VIDEO_STREAM_META_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def _parse_ffprobe_frame_rate(raw: str) -> Optional[float]:
    text = str(raw or "").strip()
    if not text or text in {"0/0", "N/A", "nan"}:
        return None
    try:
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            num = float(num_s)
            den = float(den_s)
            if den == 0:
                return None
            value = num / den
        else:
            value = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if value <= 0 or value > 240:
        return None
    return round(value, 3)


def _estimate_bitrate_bps(size_bytes: int, duration_seconds: float) -> Optional[int]:
    try:
        size = int(size_bytes)
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return None
    if size <= 0 or duration <= 0:
        return None
    return max(1, int(round(size * 8.0 / duration)))


async def _media_video_stream_meta(
    media_path: Path,
    stat: os.stat_result,
    *,
    duration_seconds: int = 0,
) -> dict[str, Any]:
    """Probe WxH / fps / bitrate once per unchanged file (ffprobe)."""
    empty: dict[str, Any] = {"resolution": "", "fps": None, "bitrate": None}
    try:
        cache_key = str(media_path.resolve())
        signature = (int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))), int(stat.st_size))
    except (OSError, OverflowError, ValueError, TypeError):
        return empty
    cached = _MEDIA_VIDEO_STREAM_META_CACHE.get(cache_key)
    if cached and cached[:2] == signature:
        return dict(cached[2])

    meta = dict(empty)
    ffprobe_path = str(Path(FFMPEG_PATH).with_name("ffprobe"))
    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,bit_rate:format=bit_rate,duration",
            "-of", "json",
            str(media_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        payload = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
        streams = payload.get("streams") if isinstance(payload, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else {}
        fmt = payload.get("format") if isinstance(payload, dict) else {}
        if not isinstance(stream, dict):
            stream = {}
        if not isinstance(fmt, dict):
            fmt = {}
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        if width > 0 and height > 0:
            meta["resolution"] = f"{width}x{height}"
        fps = _parse_ffprobe_frame_rate(str(stream.get("avg_frame_rate") or ""))
        if fps is None:
            fps = _parse_ffprobe_frame_rate(str(stream.get("r_frame_rate") or ""))
        meta["fps"] = fps
        bitrate = None
        for raw in (stream.get("bit_rate"), fmt.get("bit_rate")):
            try:
                candidate = int(raw)
            except (TypeError, ValueError):
                candidate = 0
            if candidate > 0:
                bitrate = candidate
                break
        if bitrate is None:
            probe_duration = duration_seconds
            if probe_duration <= 0:
                try:
                    probe_duration = float(fmt.get("duration") or 0)
                except (TypeError, ValueError):
                    probe_duration = 0
            bitrate = _estimate_bitrate_bps(int(stat.st_size), probe_duration)
        meta["bitrate"] = bitrate
    except (OSError, asyncio.TimeoutError, IndexError, json.JSONDecodeError, TypeError, ValueError):
        pass

    if meta.get("bitrate") is None and duration_seconds > 0:
        meta["bitrate"] = _estimate_bitrate_bps(int(stat.st_size), duration_seconds)

    _MEDIA_VIDEO_STREAM_META_CACHE[cache_key] = (*signature, dict(meta))
    return meta


async def _media_video_resolution(media_path: Path, stat: os.stat_result) -> str:
    meta = await _media_video_stream_meta(media_path, stat)
    return str(meta.get("resolution") or "")


async def _ensure_media_library_video_metadata(
    username: str,
    relative_path: str,
    media_path: Path,
    rec: Optional[dict],
    stat: os.stat_result,
) -> Optional[dict]:
    if _media_library_kind(media_path) != "video":
        return rec
    if time.time() - stat.st_mtime < MEDIA_LIBRARY_METADATA_MIN_AGE_SECONDS:
        return rec

    current = dict(rec or {})
    duration_seconds = int(current.get("duration_seconds") or 0)
    resolution = str(current.get("resolution") or "").strip()
    if resolution and not re.fullmatch(r"\d+x\d+", resolution):
        resolution = ""
    try:
        fps = float(current.get("fps")) if current.get("fps") is not None else None
        if fps is not None and fps <= 0:
            fps = None
    except (TypeError, ValueError):
        fps = None
    try:
        bitrate = int(current.get("bitrate")) if current.get("bitrate") is not None else None
        if bitrate is not None and bitrate <= 0:
            bitrate = None
    except (TypeError, ValueError):
        bitrate = None
    thumbnail_url = _recording_thumb_url(username, current, media_path)
    source_mtime = int(stat.st_mtime)
    current_created_at = int(current.get("created_at") or 0)
    recording_id = current.get("recording_id") or stable_import_recording_id(username, relative_path)
    media_kind = (current.get("media_kind") or "import").strip().lower() or "import"
    title = current.get("title") or title_from_filename(media_path.name)
    needs_faststart_repair = media_kind == "import" and mp4_needs_faststart_repair(media_path)
    playable_points_to_source = _resolved_path_key(current.get("playable_path")) == _resolved_path_key(str(media_path))
    source_changed = (
        int(current.get("source_mtime") or 0) != source_mtime
        or int(current.get("file_size") or 0) != stat.st_size
    )
    needs_playable_copy = (
        media_kind == "import"
        and (
            (
                media_path.suffix.lower() not in DIRECT_PLAYABLE_EXTENSIONS
                and (not current.get("playable_path") or source_changed)
            )
            or (
                needs_faststart_repair
                and (
                    not current.get("playable_path")
                    or playable_points_to_source
                    or source_changed
                )
            )
        )
    )
    created_at = current_created_at or source_mtime
    if current_created_at in {0, source_mtime}:
        created_at = await get_media_created_at(
            media_path,
            FFMPEG_PATH,
            fallback_timestamp=source_mtime,
            reference_texts=[title],
        )
    if (
        duration_seconds > 0
        and resolution
        and fps is not None
        and bitrate is not None
        and thumbnail_url
        and current_created_at == created_at
        and not needs_playable_copy
    ):
        return rec

    if duration_seconds <= 0:
        duration_seconds = await get_video_duration(media_path, FFMPEG_PATH)

    if not resolution or fps is None or bitrate is None:
        stream_meta = await _media_video_stream_meta(
            media_path,
            stat,
            duration_seconds=duration_seconds,
        )
        if not resolution:
            resolution = str(stream_meta.get("resolution") or "")
        if fps is None:
            try:
                fps = float(stream_meta.get("fps")) if stream_meta.get("fps") is not None else None
                if fps is not None and fps <= 0:
                    fps = None
            except (TypeError, ValueError):
                fps = None
        if bitrate is None:
            try:
                bitrate = int(stream_meta.get("bitrate")) if stream_meta.get("bitrate") is not None else None
                if bitrate is not None and bitrate <= 0:
                    bitrate = None
            except (TypeError, ValueError):
                bitrate = None
        if bitrate is None and duration_seconds > 0:
            bitrate = _estimate_bitrate_bps(int(stat.st_size), duration_seconds)

    # Stream-only backfill: keep existing playable/thumbnail rows untouched.
    if (
        duration_seconds > 0
        and resolution
        and thumbnail_url
        and current_created_at == created_at
        and not needs_playable_copy
        and (fps is not None or bitrate is not None)
    ):
        if fps is not None or bitrate is not None or resolution:
            try:
                await db.set_recording_stream_meta(
                    username,
                    media_path.name,
                    resolution=resolution or None,
                    fps=fps,
                    bitrate=bitrate,
                )
            except Exception:
                pass
        current.update({
            "resolution": resolution,
            "fps": fps,
            "bitrate": bitrate,
        })
        return current

    thumbnail_path = current.get("thumbnail_path") if thumbnail_url else None
    if not thumbnail_path:
        thumbnail_path = await generate_import_thumbnail(
            media_path,
            OUTPUT_DIR,
            username,
            recording_id,
            FFMPEG_PATH,
        )

    playable_path = current.get("playable_path")
    playable_size = current.get("playable_size")
    mp4_path = current.get("mp4_path")
    mp4_size = current.get("mp4_size")
    import_status = current.get("import_status") or ("ready" if media_kind == "import" else None)
    import_error = current.get("import_error")
    if media_kind == "import" and source_changed:
        playable_path = None
        playable_size = None
        mp4_path = None
        mp4_size = None

    if needs_faststart_repair and media_kind == "import":
        cached_path = OUTPUT_DIR / "media_imports" / username / f"{recording_id}.mp4"
        if cached_path.exists() and not source_changed:
            playable_path = str(cached_path)
            playable_size = cached_path.stat().st_size
            mp4_path = str(cached_path)
            mp4_size = playable_size
            import_status = "ready"
            import_error = None
        else:
            playable_path = None

    if not playable_path and media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS and not needs_faststart_repair:
        playable_path = str(media_path)
        playable_size = stat.st_size
    elif not playable_path and media_kind == "import":
        ok, converted_path, error = await create_playable_mp4_copy(
            media_path,
            OUTPUT_DIR,
            username,
            recording_id,
            FFMPEG_PATH,
        )
        if ok and converted_path:
            playable_path = str(converted_path)
            playable_size = converted_path.stat().st_size
            if converted_path != media_path:
                mp4_path = str(converted_path)
                mp4_size = playable_size
            import_status = "ready"
            import_error = None
        else:
            import_status = "failed"
            import_error = error or "Conversion failed"

    protected_from_retention = bool(current.get("protected_from_retention"))
    if media_kind == "import":
        protected_from_retention = True

    is_converted = bool(playable_path) if media_kind == "import" and source_changed else bool(
        current.get("is_converted") or playable_path
    )

    await db.add_or_update_recording(
        username=username,
        filename=media_path.name,
        file_path=str(media_path),
        file_size=stat.st_size,
        recording_id=recording_id,
        duration_seconds=duration_seconds,
        resolution=resolution or None,
        fps=fps,
        bitrate=bitrate,
        thumbnail_path=thumbnail_path,
        mp4_path=mp4_path,
        mp4_size=mp4_size,
        is_converted=is_converted,
        media_kind=media_kind,
        title=title,
        import_status=import_status,
        import_error=import_error,
        source_mtime=source_mtime,
        playable_path=playable_path,
        playable_size=playable_size,
        protected_from_retention=protected_from_retention,
        created_at=created_at,
        replace_media_paths=bool(media_kind == "import" and source_changed),
    )

    current.update({
        "username": username,
        "filename": media_path.name,
        "file_path": str(media_path),
        "file_size": stat.st_size,
        "recording_id": recording_id,
        "duration_seconds": duration_seconds,
        "resolution": resolution,
        "fps": fps,
        "bitrate": bitrate,
        "thumbnail_path": thumbnail_path or current.get("thumbnail_path"),
        "mp4_path": mp4_path,
        "mp4_size": mp4_size,
        "is_converted": is_converted,
        "media_kind": media_kind,
        "title": title,
        "import_status": import_status,
        "import_error": import_error,
        "source_mtime": source_mtime,
        "playable_path": playable_path,
        "playable_size": playable_size,
        "protected_from_retention": protected_from_retention,
        "created_at": created_at,
    })
    return current


def _media_library_stats(items: list[dict]) -> dict:
    stats = {
        "total": len(items),
        "videos": 0,
        "images": 0,
        "audio": 0,
        "totalSize": 0,
    }
    for item in items:
        stats["totalSize"] += int(item.get("size") or 0)
        if item.get("type") == "video":
            stats["videos"] += 1
        elif item.get("type") == "image":
            stats["images"] += 1
        elif item.get("type") == "audio":
            stats["audio"] += 1
    return stats


def _is_processing_media_file(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name.lower()
    if path.suffix.lower() == ".ts":
        return True
    if name.startswith(".") and name.endswith(".tmp.mp4"):
        return True
    return any(name.endswith(suffix) for suffix in MEDIA_LIBRARY_TEMP_SUFFIXES)


def _processing_file_username(path: Path) -> str:
    try:
        relative = path.relative_to(_records_root())
    except ValueError:
        return ""
    return relative.parts[0] if relative.parts else ""


def _resolve_processing_file_path(relative_path: str) -> Path:
    relative = Path(str(relative_path or "").strip().replace("\\", "/"))
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid file path")

    records_root = _records_root()
    candidate = (records_root / relative).resolve()
    if not candidate.is_relative_to(records_root):
        raise HTTPException(status_code=403, detail="File path is not allowed")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_processing_media_file(candidate):
        raise HTTPException(status_code=400, detail="File is not a processing or temporary file")
    return candidate


def _collect_processing_files() -> list[dict]:
    records_root = _records_root()
    if not records_root.exists():
        return []

    files: list[dict] = []
    for path in records_root.rglob("*"):
        try:
            if path.is_symlink() or not _is_processing_media_file(path):
                continue
            stat = path.stat()
            relative = path.relative_to(records_root).as_posix()
            username = _processing_file_username(path)
            files.append({
                "path": relative,
                "filename": path.name,
                "username": username,
                "size": int(stat.st_size),
                "sizeFormatted": format_bytes(stat.st_size),
                "modifiedAt": int(stat.st_mtime),
            })
        except OSError:
            continue

    files.sort(key=lambda item: (-int(item.get("modifiedAt") or 0), str(item.get("path") or "").lower()))
    return files


def _media_storage_stats(items: list[dict]) -> dict:
    """Summarize library (HDD) and staging (SSD) filesystem usage."""
    records_root = _records_root()
    staging_root = _staging_records_root()
    folder_bytes = 0
    processing_bytes = 0
    staging_bytes = 0
    if records_root.exists():
        for path in records_root.rglob("*"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            folder_bytes += size
            if _is_processing_media_file(path):
                processing_bytes += size
    if staging_root.exists():
        for path in staging_root.rglob("*"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                staging_bytes += path.stat().st_size
            except OSError:
                continue

    visible_bytes = sum(int(item.get("size") or 0) for item in items)
    untracked_bytes = max(0, folder_bytes - visible_bytes - processing_bytes)
    # Library disk = durable volume (HDD). Staging disk = hot volume (SSD).
    library_disk = shutil.disk_usage(OUTPUT_DIR)
    staging_disk = shutil.disk_usage(STAGING_DIR)
    disk_used = max(0, int(library_disk.used))
    disk_free = max(0, int(library_disk.free))
    disk_usable = max(0, disk_used + disk_free)
    disk_used_percent = round((disk_used / disk_usable) * 100, 1) if disk_usable else 0
    disk_free_percent = round((disk_free / disk_usable) * 100, 1) if disk_usable else 0
    staging_used = max(0, int(staging_disk.used))
    staging_free = max(0, int(staging_disk.free))
    staging_usable = max(0, staging_used + staging_free)

    return {
        "recordingFolderBytes": folder_bytes,
        "visibleMediaBytes": visible_bytes,
        "visibleMediaFormatted": format_bytes(visible_bytes),
        "processingBytes": processing_bytes,
        "processingFormatted": format_bytes(processing_bytes),
        "untrackedBytes": untracked_bytes,
        "untrackedFormatted": format_bytes(untracked_bytes),
        "diskUsableBytes": disk_usable,
        "diskUsedBytes": disk_used,
        "diskFreeBytes": disk_free,
        "diskFreeFormatted": format_bytes(disk_free),
        "diskUsedPercent": disk_used_percent,
        "diskFreePercent": disk_free_percent,
        "stagingFolderBytes": staging_bytes,
        "stagingFolderFormatted": format_bytes(staging_bytes),
        "stagingUsableBytes": staging_usable,
        "stagingUsedBytes": staging_used,
        "stagingFreeBytes": staging_free,
        "stagingFreeFormatted": format_bytes(staging_free),
        "libraryPath": str(OUTPUT_DIR),
        "stagingPath": str(STAGING_DIR),
    }


def _normalize_watched_threshold(value, default: int = 90) -> int:
    try:
        threshold = int(value)
    except (ValueError, TypeError):
        threshold = default
    return max(0, min(100, threshold))


async def _get_watched_threshold() -> int:
    raw = await db.get_setting("auto_delete_threshold")
    return _normalize_watched_threshold(raw if raw is not None else 90)


def _playback_progress(position_seconds: float, duration_seconds: float) -> int:
    try:
        position = float(position_seconds)
        duration = float(duration_seconds)
    except (ValueError, TypeError):
        return 0
    if duration <= 0 or position <= 0:
        return 0
    return max(0, min(100, round((position / duration) * 100)))


def _is_playback_watched(
    position_seconds: float,
    duration_seconds: float,
    watched_threshold: int,
) -> bool:
    try:
        position = float(position_seconds)
        duration = float(duration_seconds)
    except (ValueError, TypeError):
        return False
    if duration <= 0 or position <= 0:
        return False
    return _playback_progress(position, duration) >= watched_threshold or position >= max(0, duration - 2)


def _attach_media_playback_state(
    item: dict,
    playback_position: Optional[dict],
    watched_threshold: int,
) -> None:
    if item.get("type") != "video":
        item.update({
            "playbackPosition": 0,
            "playbackDuration": 0,
            "playbackProgress": 0,
            "watchedThreshold": watched_threshold,
            "isWatched": False,
            "watchedAt": None,
        })
        return

    position = float((playback_position or {}).get("position_seconds") or 0)
    duration = float(
        (playback_position or {}).get("duration_seconds")
        or item.get("duration")
        or 0
    )
    progress = _playback_progress(position, duration)
    stored_watched_at = (playback_position or {}).get("watched_at")
    is_watched = bool(stored_watched_at) or _is_playback_watched(position, duration, watched_threshold)

    item.update({
        "playbackPosition": position,
        "playbackDuration": duration,
        "playbackProgress": progress,
        "watchedThreshold": watched_threshold,
        "isWatched": is_watched,
        "watchedAt": stored_watched_at or ((playback_position or {}).get("updated_at") if is_watched else None),
    })


def _media_library_lazy_record(
    username: str,
    relative_path: str,
    media_path: Path,
    stat: os.stat_result,
) -> dict:
    """Return enough recording-like metadata for listing without probing media."""
    return {
        "username": username,
        "filename": media_path.name,
        "file_path": str(media_path),
        "file_size": stat.st_size,
        "recording_id": stable_import_recording_id(username, relative_path),
        "duration_seconds": 0,
        "media_kind": "library",
        "title": title_from_filename(media_path.name),
        "source_mtime": int(stat.st_mtime),
        "playable_path": str(media_path)
        if media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS
        else None,
        "playable_size": stat.st_size
        if media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS
        else None,
        "created_at": int(stat.st_mtime),
    }


async def _scan_media_library_items(
    profile_username: Optional[str] = None,
    refresh_metadata: bool = True,
) -> list[dict]:

    records_root = OUTPUT_DIR / "records"
    if not records_root.exists():
        return []

    if profile_username:
        selected_username = _validate_media_profile_username(profile_username)
        selected_dir = records_root / selected_username
        profile_dirs: list[Path] = []
        if selected_dir.is_dir():
            profile_dirs = [selected_dir]
        elif records_root.exists():
            for child in sorted(records_root.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                parsed_name, _parsed_source = parse_identity_with_source(child.name)
                if parsed_name == selected_username or child.name == selected_username:
                    profile_dirs.append(child)
    else:
        profile_dirs = [
            profile_dir
            for profile_dir in sorted(records_root.iterdir(), key=lambda p: p.name.lower())
            if profile_dir.is_dir() and not profile_dir.name.startswith(".")
        ]
    recording_lookup_names: list[str] = []
    for profile_dir in profile_dirs:
        recording_lookup_names.append(profile_dir.name)
        parsed_name, _ = parse_identity_with_source(profile_dir.name)
        if parsed_name and parsed_name not in recording_lookup_names:
            recording_lookup_names.append(parsed_name)
    recordings_by_username = await db.get_recordings_for_usernames(recording_lookup_names)

    items: list[dict] = []
    for profile_dir in profile_dirs:
        parsed_username, parsed_source = parse_identity_with_source(profile_dir.name)
        username = parsed_username or profile_dir.name
        source_type = parsed_source or ""
        profile_root = profile_dir.resolve()
        recordings: list[dict] = []
        seen_rec_ids: set[str] = set()
        for key in (profile_dir.name, username):
            for rec in recordings_by_username.get(key, []) or []:
                rec_id = str(rec.get("recording_id") or rec.get("id") or id(rec))
                if rec_id in seen_rec_ids:
                    continue
                seen_rec_ids.add(rec_id)
                recordings.append(rec)
        recordings_by_path: dict[str, dict] = {}
        recordings_by_filename: dict[str, dict] = {}

        for rec in recordings:
            filename = rec.get("filename")
            if filename:
                recordings_by_filename.setdefault(filename, rec)
            for key in ("file_path", "mp4_path", "playable_path"):
                value = rec.get(key)
                if not value:
                    continue
                try:
                    recordings_by_path.setdefault(str(Path(value).resolve()), rec)
                except Exception:
                    continue

        for media_path in profile_dir.rglob("*"):
            if not _is_media_library_file(media_path):
                continue
            try:
                resolved = media_path.resolve()
                if not resolved.is_relative_to(profile_root):
                    continue
                stat = media_path.stat()
                relative_path = media_path.relative_to(profile_dir).as_posix()
            except Exception:
                continue

            kind = _media_library_kind(media_path)
            if not kind:
                continue

            rec = recordings_by_path.get(str(resolved))
            if rec is None and len(Path(relative_path).parts) == 1:
                rec = recordings_by_filename.get(media_path.name)
            if kind == "video" and refresh_metadata:
                rec = await _ensure_media_library_video_metadata(
                    username,
                    relative_path,
                    media_path,
                    rec,
                    stat,
                )
            elif kind == "video" and rec is None:
                rec = _media_library_lazy_record(username, relative_path, media_path, stat)

            imported_recording_id = (rec or {}).get("recording_id")
            media_kind = (rec or {}).get("media_kind")
            is_imported = bool(rec and media_kind == "import")
            if kind == "video" and is_imported and imported_recording_id:
                url = f"/streams/media/{quote(imported_recording_id, safe='')}"
                download_url = f"{url}?download=1"
                browser_playable = bool((rec or {}).get("playable_path")) or media_path.suffix.lower() in MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS
            else:
                # Keep URLs rooted at the on-disk folder name (may include source stamp).
                url = _media_library_url(profile_dir.name, relative_path)
                download_url = _media_library_url(profile_dir.name, relative_path, download=True)
                browser_playable = kind != "video" or media_path.suffix.lower() in MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS
            thumbnail = url if kind == "image" else _recording_thumb_url(username, rec, media_path)
            created_at = int((rec or {}).get("created_at") or stat.st_mtime)
            duration_seconds = int((rec or {}).get("duration_seconds") or 0)
            resolution = str((rec or {}).get("resolution") or "").strip()
            try:
                fps = float((rec or {}).get("fps")) if (rec or {}).get("fps") is not None else None
                if fps is not None and fps <= 0:
                    fps = None
            except (TypeError, ValueError):
                fps = None
            try:
                bitrate = int((rec or {}).get("bitrate")) if (rec or {}).get("bitrate") is not None else None
                if bitrate is not None and bitrate <= 0:
                    bitrate = None
            except (TypeError, ValueError):
                bitrate = None
            if kind == "video" and (
                not re.fullmatch(r"\d+x\d+", resolution)
                or fps is None
                or bitrate is None
            ):
                # Probe even in lazy mode when DB lacks stream meta; result is cached + persisted.
                stream_meta = await _media_video_stream_meta(
                    media_path,
                    stat,
                    duration_seconds=duration_seconds,
                )
                if not re.fullmatch(r"\d+x\d+", resolution):
                    resolution = str(stream_meta.get("resolution") or "")
                if fps is None:
                    try:
                        fps = float(stream_meta.get("fps")) if stream_meta.get("fps") is not None else None
                        if fps is not None and fps <= 0:
                            fps = None
                    except (TypeError, ValueError):
                        fps = None
                if bitrate is None:
                    try:
                        bitrate = int(stream_meta.get("bitrate")) if stream_meta.get("bitrate") is not None else None
                        if bitrate is not None and bitrate <= 0:
                            bitrate = None
                    except (TypeError, ValueError):
                        bitrate = None
                if bitrate is None and duration_seconds > 0:
                    bitrate = _estimate_bitrate_bps(int(stat.st_size), duration_seconds)
                if (resolution or fps is not None or bitrate is not None) and rec is not None:
                    try:
                        await db.set_recording_stream_meta(
                            username,
                            media_path.name,
                            resolution=resolution or None,
                            fps=fps,
                            bitrate=bitrate,
                        )
                        if resolution:
                            rec["resolution"] = resolution
                        if fps is not None:
                            rec["fps"] = fps
                        if bitrate is not None:
                            rec["bitrate"] = bitrate
                    except Exception:
                        pass
            if kind == "video" and bitrate is None and duration_seconds > 0:
                bitrate = _estimate_bitrate_bps(int(stat.st_size), duration_seconds)
            recording_id = imported_recording_id
            item_id_seed = f"{profile_dir.name}\0{relative_path}"
            item_id = hashlib.sha256(item_id_seed.encode("utf-8")).hexdigest()[:16]
            try:
                modified_iso = datetime.fromtimestamp(stat.st_mtime).isoformat()
            except (OverflowError, OSError, ValueError):
                modified_iso = ""

            items.append({
                "id": item_id,
                "recordingId": recording_id,
                "username": username,
                "folderName": profile_dir.name,
                "sourceType": source_type,
                "source_type": source_type,
                "filename": media_path.name,
                "relativePath": relative_path,
                "title": (rec or {}).get("title") or _media_library_placeholder_title(media_path.name),
                "type": kind,
                "extension": media_path.suffix.lower().lstrip("."),
                "mimeType": mimetypes.guess_type(media_path.name)[0] or (
                    _recording_media_type(media_path.name) if kind == "video" else "application/octet-stream"
                ),
                "size": stat.st_size,
                "sizeFormatted": format_bytes(stat.st_size),
                "createdAt": created_at,
                "modifiedAt": int(stat.st_mtime),
                "modified": modified_iso,
                "duration": duration_seconds,
                "durationStr": _format_duration_label(duration_seconds) if duration_seconds else "",
                "resolution": resolution if kind == "video" else "",
                "fps": fps if kind == "video" else None,
                "bitrate": bitrate if kind == "video" else None,
                "url": url,
                "downloadUrl": download_url,
                "deleteUrl": f"/api/media-library/{quote(profile_dir.name, safe='')}/{_quote_url_path(relative_path)}",
                "thumbnail": thumbnail,
                "browserPlayable": browser_playable,
                "isImported": is_imported,
                "isRecording": bool(rec and (media_kind or "recording") == "recording"),
            })

    return items


# Protected route for recordings
def _recording_candidate_paths(rec: dict) -> list[Path]:
    candidates: list[Path] = []
    if rec.get("is_converted") and rec.get("mp4_path"):
        candidates.append(Path(rec["mp4_path"]))
    for key in ("playable_path", "file_path", "mp4_path"):
        value = rec.get(key)
        if value:
            path = Path(value)
            if path not in candidates:
                candidates.append(path)
    return candidates


def _select_recording_path(rec: dict, requested_filename: Optional[str] = None) -> Optional[Path]:
    candidates = _recording_candidate_paths(rec)
    if requested_filename:
        for path in candidates:
            if path.name == requested_filename and path.exists() and path.is_file():
                return path
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _assert_recording_path_is_safe(path: Path):
    if not _path_is_inside_output(path):
        logger.warning("Recording path outside volume refused", path=str(path))
        raise HTTPException(status_code=403, detail="Recording path is not allowed")


def _assert_recording_not_active(username: str, path: Path):
    target_key = _resolved_path_key(str(path))
    for session in _all_recording_statuses():
        if not (session.get("person") == username and session.get("running")):
            continue
        active_key = _resolved_path_key(session.get("record_path") or "")
        if target_key and active_key == target_key:
            logger.warning("Blocked access to in-progress recording", username=username, path=str(path))
            raise HTTPException(
                status_code=403,
                detail="This recording is in progress. Watch the live stream instead.",
            )


async def _serve_recording_from_record(
    request: Request,
    rec: dict,
    requested_filename: Optional[str] = None,
):
    username = rec.get("username") or ""
    path = _select_recording_path(rec, requested_filename)
    if not path:
        raise HTTPException(status_code=404, detail="Recording not found")
    _assert_recording_path_is_safe(path)
    _assert_recording_not_active(username, path)
    return await _serve_video_file_with_ranges(request, path, path.name)


@app.api_route("/streams/recordings/{recording_id}", methods=["GET", "HEAD"])
async def serve_recording_by_id(request: Request, recording_id: str):
    """Serve a recording by ID, even if the file is in a subdirectory."""
    if ".." in recording_id or "/" in recording_id:
        raise HTTPException(status_code=400, detail="Invalid recording ID")
    rec = await db.get_recording_by_id(recording_id)
    if not rec or rec.get("media_kind") == "import":
        raise HTTPException(status_code=404, detail="Recording not found")
    return await _serve_recording_from_record(request, rec)


@app.api_route("/streams/records/{username}/{filename}", methods=["GET", "HEAD"])
async def serve_recording_protected(request: Request, username: str, filename: str):
    """Serve a recording (TS or MP4) with HTTP Range support for large files"""
    logger.api_request(request.method, f"/streams/records/{username}/{filename}")

    # Security: validate the filename
    if (
        ".." in username
        or "/" in username
        or ".." in filename
        or "/" in filename
        or not filename.lower().endswith((".ts", ".mp4", ".webm"))
    ):
        logger.warning("Invalid file access attempt", username=username, filename=filename)
        raise HTTPException(status_code=400, detail="Invalid filename")

    for rec in await db.get_recordings(username):
        if rec.get("media_kind") == "import":
            continue
        if (
            rec.get("filename") == filename
            or Path(rec.get("file_path") or "").name == filename
            or Path(rec.get("mp4_path") or "").name == filename
            or Path(rec.get("playable_path") or "").name == filename
        ):
            return await _serve_recording_from_record(request, rec, filename)

    # Serve the file
    file_path = OUTPUT_DIR / "records" / username / filename
    _assert_recording_not_active(username, file_path)
    return await _serve_video_file_with_ranges(request, file_path, filename)


@app.api_route("/streams/media/{recording_id}", methods=["GET", "HEAD"])
async def serve_imported_media(request: Request, recording_id: str, download: bool = False):
    """Serve an imported media item by its stable ID without exposing a disk path."""
    if ".." in recording_id or "/" in recording_id:
        raise HTTPException(status_code=400, detail="Invalid media ID")

    rec = await db.get_recording_by_id(recording_id)
    if not rec or rec.get("media_kind") != "import":
        raise HTTPException(status_code=404, detail="Media not found")

    path_value = rec.get("file_path") if download else (rec.get("playable_path") or rec.get("file_path"))
    if not path_value:
        raise HTTPException(status_code=404, detail="Media file not found")

    file_path = Path(path_value)
    if file_path.suffix.lower() == ".ts":
        raise HTTPException(status_code=400, detail="TS files are not supported in Media")
    output_root = OUTPUT_DIR.resolve()
    try:
        resolved = file_path.resolve()
        if not resolved.is_relative_to(output_root):
            raise HTTPException(status_code=403, detail="Media path is not allowed")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media path")

    return await _serve_video_file_with_ranges(request, file_path, file_path.name)


@app.api_route("/streams/library/{username}/{file_path:path}", methods=["GET", "HEAD"])
async def serve_library_media(request: Request, username: str, file_path: str, download: bool = False):
    """Serve a media file under /records/<profile>/ with path validation."""
    media_path = _resolve_library_media_path(username, file_path)
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    kind = _media_library_kind(media_path)
    media_type = mimetypes.guess_type(media_path.name)[0] or (
        _recording_media_type(media_path.name) if kind == "video" else "application/octet-stream"
    )

    if download:
        return FileResponse(
            str(media_path),
            media_type=media_type,
            headers={
                "Content-Disposition": _content_disposition(media_path.name, "attachment"),
                "Cache-Control": "private, max-age=0",
            },
        )

    if kind == "video":
        return await _serve_video_file_with_ranges(request, media_path, media_path.name)

    return FileResponse(
        str(media_path),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(media_path.name),
            "Cache-Control": "public, max-age=3600",
        },
    )

# Mount live HLS sessions only
app.mount("/streams/sessions", StaticFiles(directory=str(OUTPUT_DIR / "sessions")), name="streams_sessions")
app.mount("/streams/thumbnails", StaticFiles(directory=str(OUTPUT_DIR / "thumbnails")), name="streams_thumbnails")

manager = FFmpegManager(str(OUTPUT_DIR), ffmpeg_path=FFMPEG_PATH, hls_time=HLS_TIME, hls_list_size=HLS_LIST_SIZE)

# Database SQLite
DB_FILE = OUTPUT_DIR / "hxylive.db"
_legacy_db = OUTPUT_DIR / "streamrec.db"
if not DB_FILE.exists() and _legacy_db.exists():
    try:
        _legacy_db.rename(DB_FILE)
        logger.info("Migrated legacy SQLite database", from_path=str(_legacy_db), to_path=str(DB_FILE))
    except OSError as exc:
        logger.warning(
            "Failed to migrate legacy SQLite database; using new path",
            from_path=str(_legacy_db),
            to_path=str(DB_FILE),
            error=str(exc),
        )
db = Database(DB_FILE)
media_import_manager: Optional[MediaImportManager] = None

# Chaturbate API (initialized at startup)
chaturbate_api: Optional[ChaturbateAPI] = None
flaresolverr_client: Optional[FlareSolverrClient] = None

SOURCE_TYPES = {"chaturbate", "twitch"}
provider_registry = create_provider_registry(db, output_dir=OUTPUT_DIR)
SOURCE_TYPES = provider_registry.source_types()
_HLS_PROXY_CACHE: dict[str, dict] = {}
_HLS_PROXY_REVERSE: dict[tuple, str] = {}
_HLS_PROXY_TTL_SECONDS = int(os.getenv("HXYLIVE_HLS_PROXY_TTL_SECONDS", "900"))
_HLS_MOUFLON_PSCH_RE = re.compile(r"^#EXT-X-MOUFLON:PSCH:([^:]+):(.+)$", re.IGNORECASE)
_HLS_MOUFLON_URI_RE = re.compile(r"^#EXT-X-MOUFLON:URI:(https?://.+)$", re.IGNORECASE)


def _parse_auto_record_users_env(raw_value: Optional[str]) -> Tuple[list[str], int]:
    """Parse AUTO_RECORD_USERS, preserving order and dropping duplicates."""
    users: list[str] = []
    seen: set[str] = set()
    skipped = 0

    if not raw_value:
        return users, skipped

    for item in (raw_value or "").split(","):
        username = item.strip()
        if not username:
            skipped += 1
            continue
        dedupe_key = username.lower()
        if dedupe_key in seen:
            skipped += 1
            continue
        seen.add(dedupe_key)
        users.append(username)

    return users, skipped


async def _import_auto_record_users_from_env() -> dict[str, int]:
    """Import AUTO_RECORD_USERS into SQLite so the auto-record loop can see it."""
    users, skipped = _parse_auto_record_users_env(os.getenv("AUTO_RECORD_USERS", ""))
    imported = 0
    updated = 0
    created = 0

    for username in users:
        existing = await db.get_model(username)
        if existing:
            retention_days = existing.get("retention_days")
            await db.add_or_update_model(
                username=username,
                display_name=existing.get("display_name"),
                auto_record=True,
                record_quality=existing.get("record_quality") or "best",
                retention_days=retention_days if retention_days is not None else 30,
                source_type=existing.get("source_type") or "chaturbate",
            )
            updated += 1
        else:
            await db.add_or_update_model(
                username=username,
                display_name=username,
                auto_record=True,
                record_quality=await _get_default_record_quality(),
                retention_days=await _get_default_retention_days(),
                source_type="chaturbate",
            )
            created += 1
        imported += 1

    if imported or skipped:
        logger.info(
            "AUTO_RECORD_USERS imported",
            imported=imported,
            created=created,
            updated=updated,
            skipped=skipped,
        )

    return {
        "imported": imported,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


_SOURCE_TYPE_ALIASES = {
    "yt": "youtube",
    "youtube.com": "youtube",
}


def _normalize_source_type(source_type: Optional[str]) -> Optional[str]:
    value = (source_type or "").strip().lower()
    if not value or value == "auto":
        return None
    return _SOURCE_TYPE_ALIASES.get(value, value)


def _available_source_types() -> set[str]:
    types = provider_registry.source_types()
    return types or set(SOURCE_TYPES)


def _source_type_from_url(target: str) -> Optional[str]:
    try:
        hostname = (urlparse(target).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if not hostname:
        return None
    for provider in provider_registry.all():
        for domain in provider.domains:
            clean = domain.lower().rstrip(".")
            if hostname == clean or hostname.endswith(f".{clean}"):
                return provider.source_type
    return None


async def _infer_source_type(
    username: Optional[str],
    model: Optional[dict] = None,
) -> str:
    """Pick the platform without overriding an explicit provider identity."""
    model_source = _normalize_source_type(model.get("source_type") if model else None)
    if model_source:
        return model_source

    if username:
        try:
            followed = await db.get_followed_model(username)
            followed_source = _normalize_source_type(
                followed.get("source_type") if followed else None
            )
            if followed_source:
                return followed_source
        except Exception:
            pass

    return model_source or "chaturbate"


def _provider_for(source_type: str):
    if provider_registry.has(source_type):
        return provider_registry.get(source_type)
    raise ValueError(f"unknown source_type: {source_type}")


def _supports_browser_capture(source_type: str) -> bool:
    return False


def _provider_error_detail(source_type: str, target: str, exc: Exception) -> str:
    if isinstance(exc, ProviderInteractionRequired):
        return f"{source_type}/{target}: manual interaction required (CAPTCHA/2FA/challenge)"
    if isinstance(exc, ProviderPrivateError):
        return f"{source_type}/{target}: private or paid stream not supported"
    if isinstance(exc, ProviderAuthError):
        return f"{source_type}/{target}: login required"
    if isinstance(exc, ProviderOfflineError):
        return f"{source_type}/{target}: model offline or public stream not found"
    return f"{source_type}/{target}: {exc}"


async def _resolve_stream(
    source_type: str,
    target: str,
    max_height: Optional[int],
    *,
    pin_variant: bool = False,
) -> ResolvedStream:
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_stream", True):
        raise ProviderError(
            f"{provider.display_name} is available in Discover only: "
            "no public FFmpeg-readable stream was validated."
        )
    stream = await provider.resolve_stream(
        target,
        max_height=max_height,
        pin_variant=pin_variant,
    )
    if not stream.source_type:
        stream.source_type = source_type
    return stream


async def _resolve_m3u8(source_type: str, target: str, max_height: Optional[int]) -> Optional[str]:
    """Backward-compatible URL-only resolver."""
    stream = await _resolve_stream(source_type, target, max_height)
    return stream.url


def _prune_hls_proxy_cache() -> None:
    now = time.time()
    expired = [
        token for token, entry in _HLS_PROXY_CACHE.items()
        if entry.get("expires_at", 0) <= now
    ]
    for token in expired:
        entry = _HLS_PROXY_CACHE.pop(token, None)
        cache_key = entry.get("cache_key") if entry else None
        if cache_key and _HLS_PROXY_REVERSE.get(cache_key) == token:
            _HLS_PROXY_REVERSE.pop(cache_key, None)


def _hls_proxy_cache_key(
    url: str,
    headers: Optional[dict[str, str]],
    suffix: str,
) -> tuple:
    header_items = tuple(
        sorted((str(key), str(value)) for key, value in (headers or {}).items())
    )
    return (url, header_items, suffix)


def _hls_proxy_path_suffix(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path
    if re.search(r"(?:^|[&;])flags=segment(?:$|[&;])", parsed.query or "", re.IGNORECASE):
        return ".mp4"
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".pts":
        return ".ts"
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        return suffix
    return ""


def _url_with_missing_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url or "")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    existing = {key for key, _ in pairs}
    for key, value in params.items():
        if key not in existing and value:
            pairs.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _register_hls_proxy_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    suffix: Optional[str] = None,
) -> str:
    _prune_hls_proxy_cache()
    resolved_suffix = suffix if suffix is not None else _hls_proxy_path_suffix(url)
    cache_key = _hls_proxy_cache_key(url, headers, resolved_suffix)
    now = time.time()
    existing_token = _HLS_PROXY_REVERSE.get(cache_key)
    existing = _HLS_PROXY_CACHE.get(existing_token or "")
    if existing and existing.get("expires_at", 0) > now:
        existing["expires_at"] = now + max(60, _HLS_PROXY_TTL_SECONDS)
        return f"/api/proxy/hls/{existing_token}{resolved_suffix}"

    token = secrets.token_urlsafe(24)
    _HLS_PROXY_CACHE[token] = {
        "url": url,
        "headers": dict(headers or {}),
        "cache_key": cache_key,
        "created_at": now,
        "expires_at": now + max(60, _HLS_PROXY_TTL_SECONDS),
    }
    _HLS_PROXY_REVERSE[cache_key] = token
    return f"/api/proxy/hls/{token}{resolved_suffix}"


def _register_cached_hls_body(
    url: str,
    body: bytes,
    content_type: str = "",
    suffix: str = ".mp4",
) -> str:
    _prune_hls_proxy_cache()
    token = secrets.token_urlsafe(24)
    now = time.time()
    _HLS_PROXY_CACHE[token] = {
        "url": url,
        "headers": {},
        "body": body,
        "content_type": content_type,
        "created_at": now,
        "expires_at": now + max(60, _HLS_PROXY_TTL_SECONDS),
    }
    return f"/api/proxy/hls/{token}{suffix}"


def _hls_segment_header_variants(headers: Optional[dict[str, str]]) -> list[dict[str, str]]:
    primary = dict(headers or {})
    variants = [primary]
    without_cookie = {
        key: value
        for key, value in primary.items()
        if key.lower() != "cookie"
    }
    if without_cookie != primary:
        variants.append(without_cookie)
    return variants


def _single_segment_hls_info(text: str) -> Optional[tuple[str, str, str, str]]:
    target_duration = "1"
    version = "3"
    duration = "1.0"
    pending_duration: Optional[str] = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = stripped.split(":", 1)[1].strip() or target_duration
        elif stripped.startswith("#EXT-X-VERSION:"):
            version = stripped.split(":", 1)[1].strip() or version
        elif stripped.startswith("#EXTINF:"):
            pending_duration = stripped.split(":", 1)[1].split(",", 1)[0].strip() or duration
        elif not stripped.startswith("#"):
            return stripped, pending_duration or duration, target_duration, version
    return None


async def _rewrite_prefetched_single_segment_playlist(
    text: str,
    base_url: str,
    headers: Optional[dict[str, str]],
    entry: dict,
    session,
) -> str:
    segment_info = _single_segment_hls_info(text)
    if not segment_info:
        return _rewrite_hls_playlist(text, base_url, headers=headers, live_sequence=0)

    raw_uri, duration, target_duration, version = segment_info
    raw_url = urljoin(base_url, raw_uri)
    proxy_url = None
    for attempt_headers in _hls_segment_header_variants(headers):
        try:
            async with session.get(
                raw_url,
                headers=attempt_headers,
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
                body = await resp.read()
                if resp.status < 400 and body:
                    proxy_url = _register_cached_hls_body(
                        str(resp.url),
                        body,
                        resp.headers.get("Content-Type", "video/mp4"),
                    )
                    break
        except Exception:
            continue

    if not proxy_url:
        proxy_url = _register_hls_proxy_url(raw_url, headers=headers, suffix=".mp4")

    seq = int(entry.get("next_media_sequence") or 0)
    history = list(entry.get("live_segments") or [])
    history.append({"seq": seq, "duration": duration, "url": proxy_url})
    history = history[-6:]
    entry["live_segments"] = history
    entry["next_media_sequence"] = seq + 1

    lines = [
        "#EXTM3U",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-VERSION:{version}",
        f"#EXT-X-MEDIA-SEQUENCE:{history[0]['seq']}",
    ]
    for segment in history:
        lines.append(f"#EXTINF:{segment['duration']},")
        lines.append(segment["url"])
    return "\n".join(lines) + "\n"


def _rewrite_hls_playlist(
    text: str,
    base_url: str,
    headers: Optional[dict[str, str]] = None,
    live_sequence: Optional[int] = None,
    *,
    absolute_upstream: bool = False,
) -> str:
    def proxy_url(raw_uri: str) -> str:
        absolute = urljoin(base_url, raw_uri)
        if absolute_upstream:
            return absolute
        return _register_hls_proxy_url(absolute, headers=headers)

    def proxy_absolute_url(url: str) -> str:
        if absolute_upstream:
            return url
        return _register_hls_proxy_url(url, headers=headers)

    media_sequence_written = False
    pending_program_date_time: Optional[str] = None
    pending_mouflon_uri: Optional[str] = None
    pending_variant = False
    skip_mouflon_full_segment = False
    variant_index = 0
    mouflon_keys: list[tuple[str, str]] = []
    has_mouflon_parts = "#EXT-X-MOUFLON:URI:" in (text or "") and "#EXT-X-PART:" in (text or "")
    rewritten = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
            continue
        if stripped.startswith("#"):
            upper = stripped.upper()
            psch_match = _HLS_MOUFLON_PSCH_RE.match(stripped)
            if psch_match:
                mouflon_keys.append((psch_match.group(1).strip(), psch_match.group(2).strip()))
                rewritten.append(line)
                continue
            uri_match = _HLS_MOUFLON_URI_RE.match(stripped)
            if uri_match:
                pending_mouflon_uri = uri_match.group(1).strip()
                continue
            # Hls.js can chase generic LL-HLS parts/preload hints faster than
            # a server-side proxy can refresh provider tokens. Stripchat's
            # Mouflon playlists are different: the full media.mp4 placeholders
            # 404, while the part URLs in the preceding Mouflon tags are valid.
            if (
                upper.startswith("#EXT-X-SERVER-CONTROL:")
                or upper.startswith("#EXT-X-PART-INF:")
                or upper.startswith("#EXT-X-RENDITION-REPORT:")
            ):
                continue
            if upper.startswith("#EXT-X-PART:"):
                if has_mouflon_parts and pending_mouflon_uri:
                    duration_match = re.search(r"DURATION=([0-9.]+)", stripped, re.IGNORECASE)
                    rewritten.append(f"#EXTINF:{duration_match.group(1) if duration_match else '0.5'},")
                    rewritten.append(proxy_absolute_url(pending_mouflon_uri))
                pending_mouflon_uri = None
                continue
            if upper.startswith("#EXT-X-PRELOAD-HINT:"):
                pending_mouflon_uri = None
                continue
            if upper.startswith("#EXT-X-MOUFLON:EXT-REF:"):
                continue
            if upper.startswith("#EXT-X-STREAM-INF:"):
                pending_variant = True
                rewritten.append(line)
                continue
            if upper.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_program_date_time = line
                continue
            if upper.startswith("#EXTINF:"):
                if has_mouflon_parts:
                    skip_mouflon_full_segment = True
                    pending_program_date_time = None
                    continue
                if pending_program_date_time:
                    rewritten.append(pending_program_date_time)
                    pending_program_date_time = None
                rewritten.append(line)
                continue
            if live_sequence is not None:
                if upper.startswith("#EXT-X-PLAYLIST-TYPE:") or upper == "#EXT-X-ENDLIST":
                    continue
                if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    rewritten.append(f"#EXT-X-MEDIA-SEQUENCE:{live_sequence}")
                    media_sequence_written = True
                    continue
            if "URI=\"" in stripped:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda match: f'URI="{proxy_url(match.group(1))}"',
                    line,
                )
            rewritten.append(line)
            continue
        raw_uri = pending_mouflon_uri or stripped
        pending_mouflon_uri = None
        if skip_mouflon_full_segment:
            skip_mouflon_full_segment = False
            continue
        absolute_url = urljoin(base_url, raw_uri)
        if pending_variant:
            query_keys = {key for key, _ in parse_qsl(urlparse(absolute_url).query, keep_blank_values=True)}
            if mouflon_keys and "pkey" not in query_keys:
                version, pkey = mouflon_keys[min(variant_index, len(mouflon_keys) - 1)]
                absolute_url = _url_with_missing_query_params(
                    absolute_url,
                    {"playlistType": "lowLatency", "psch": version, "pkey": pkey},
                )
            variant_index += 1
            pending_variant = False
        rewritten.append(proxy_absolute_url(absolute_url))
    if live_sequence is not None and not media_sequence_written:
        insert_at = 1 if rewritten and rewritten[0].strip() == "#EXTM3U" else 0
        rewritten.insert(insert_at, f"#EXT-X-MEDIA-SEQUENCE:{live_sequence}")
    return "\n".join(rewritten) + ("\n" if text.endswith("\n") else "")


def _proxied_stream_url(stream: ResolvedStream) -> str:
    lower = (stream.url or "").lower()
    if stream.hls_playlist_text and ".m3u8" in lower:
        # Chaturbate LL-HLS master tokens can reject an immediate second fetch.
        # Reuse the resolver-fetched master and proxy its rewritten variants.
        rewritten = _rewrite_hls_playlist(
            stream.hls_playlist_text,
            stream.hls_playlist_base_url or stream.url,
            headers=stream.headers,
        )
        return _register_cached_hls_body(
            stream.url,
            rewritten.encode("utf-8"),
            stream.hls_playlist_content_type or "application/vnd.apple.mpegurl",
            suffix=".m3u8",
        )
    if ".m3u8" in lower or ".mpd" in lower:
        return _register_hls_proxy_url(stream.url, headers=stream.headers)
    return stream.url


def _watch_stream_payload(stream: ResolvedStream) -> dict:
    return {"streamUrl": _proxied_stream_url(stream)}


def _local_proxy_url_for_ffmpeg(url: str) -> str:
    """Legacy helper kept for tests/callers; prefer _ffmpeg_stream_input."""
    if (url or "").startswith("/api/proxy/hls/"):
        port = os.getenv("PORT", "8080")
        return f"http://127.0.0.1:{port}{url}"
    return url


def _write_ffmpeg_hls_playlist(text: str) -> str:
    """Persist a rewritten master playlist for ffmpeg without HTTP loopback."""
    playlist_dir = OUTPUT_DIR / "sessions" / "_ffmpeg_playlists"
    playlist_dir.mkdir(parents=True, exist_ok=True)
    path = playlist_dir / f"{secrets.token_urlsafe(12)}.m3u8"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ffmpeg_stream_input(stream: ResolvedStream) -> tuple[str, Optional[dict[str, str]], str]:
    """Build ffmpeg input for recording.

    Prefetched masters (Chaturbate LL-HLS) are served from the in-process HLS
    proxy cache so ffmpeg does not re-fetch the master token, and so variant /
    segment requests keep provider headers. FFmpeg 7 rejects ``-headers`` /
    ``-user_agent`` on local ``file:`` inputs (``Option not found``), so writing
    an on-disk playlist with absolute CDN URLs cannot authenticate mmcdn.
    """
    headers = dict(stream.headers) if stream.headers else None
    lower = (stream.url or "").lower()
    if stream.hls_playlist_text and ".m3u8" in lower:
        rewritten = _rewrite_hls_playlist(
            stream.hls_playlist_text,
            stream.hls_playlist_base_url or stream.url,
            headers=stream.headers,
        )
        proxied = _register_cached_hls_body(
            stream.url,
            rewritten.encode("utf-8"),
            stream.hls_playlist_content_type or "application/vnd.apple.mpegurl",
            suffix=".m3u8",
        )
        # Headers are applied by the HLS proxy; do not pass them to ffmpeg.
        return _local_proxy_url_for_ffmpeg(proxied), None, stream.url
    return stream.url, headers, stream.url


def _all_recording_statuses() -> list[dict]:
    return manager.list_status()


async def _note_recording_last_seen(
    *,
    username: str,
    created_at: Optional[int],
    duration_seconds: Optional[int],
    source_type: Optional[str] = None,
) -> None:
    """Refresh last-seen from a finished/indexed recording (no Media page needed)."""
    channel = str(username or "").strip()
    if not channel:
        return
    start = _coerce_unix_timestamp(created_at) or 0
    duration = max(0, int(duration_seconds or 0))
    # Prefer session end (start + duration); fall back to "now" if metadata is thin.
    seen_at = (start + duration) if start and duration else int(time.time())
    if seen_at <= 0:
        seen_at = int(time.time())
    try:
        await db.note_last_seen_online(
            channel,
            seen_at=seen_at,
            source_type=source_type,
        )
    except Exception:
        pass


async def _index_ffmpeg_recording(session) -> None:
    paths = []
    try:
        paths = [Path(path) for path in session._recording_paths_for_cleanup()]
    except Exception:
        record_path = getattr(session, "record_path", None)
        if record_path:
            paths = [Path(record_path)]

    if not paths:
        return

    try:
        from app.tasks.monitor import generate_recording_thumbnail, get_media_created_at, get_video_duration

        for record_path in paths:
            if not record_path.exists() or not record_path.is_file():
                continue
            file_size = record_path.stat().st_size
            duration_seconds = await get_video_duration(record_path, FFMPEG_PATH)
            if not duration_seconds:
                duration_seconds = max(0, int(time.time() - getattr(session, "start_time", time.time())))
            if duration_seconds < MIN_RECORDING_SECONDS and file_size < MIN_RECORDING_BYTES:
                logger.info(
                    "Recording too short, indexing skipped",
                    session_id=session.id,
                    person=session.person,
                    file=record_path.name,
                    duration_seconds=duration_seconds,
                    file_size=file_size,
                )
                continue
            fallback_created_at = int(getattr(session, "start_time", None) or record_path.stat().st_mtime)
            thumbnail_path = await generate_recording_thumbnail(
                record_path,
                OUTPUT_DIR,
                session.person,
                FFMPEG_PATH,
            )
            created_at = await get_media_created_at(
                record_path,
                FFMPEG_PATH,
                fallback_timestamp=fallback_created_at,
            )
            await db.add_or_update_recording(
                username=session.person,
                filename=record_path.name,
                file_path=str(record_path),
                file_size=file_size,
                recording_id=f"{session.person}_{record_path.stem}",
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path,
                is_converted=False,
                created_at=created_at,
            )
            await _note_recording_last_seen(
                username=session.person,
                created_at=created_at,
                duration_seconds=duration_seconds,
                source_type=getattr(session, "source_type", None),
            )
            logger.info(
                "FFmpeg recording indexed",
                session_id=session.id,
                person=session.person,
                file=record_path.name,
                duration_seconds=duration_seconds,
                file_size=file_size,
            )
    except Exception as exc:
        logger.warning(
            "FFmpeg recording indexing failed",
            session_id=getattr(session, "id", None),
            person=getattr(session, "person", None),
            error=str(exc),
        )


async def _provider_status(source_type: str, username: str) -> ProviderStatus:
    provider = _provider_for(source_type)
    status = await provider.check_status(username)
    if not status.source_type:
        status.source_type = source_type
    return status


# Model list backup file (server-side)
MODELS_FILE = OUTPUT_DIR / "models.json"


class StartBody(BaseModel):
    target: str  # Either an m3u8 URL or a username (if resolver enabled)
    source_type: Optional[str] = None  # "m3u8", provider source_type, or None/"auto"
    name: Optional[str] = None  # display name
    person: Optional[str] = None  # recording bucket (per person)
    auto_start: Optional[bool] = False  # True for automatic start
    record_quality: Optional[str] = None  # best, 1080p, 720p, 480p, 360p
    recordQuality: Optional[str] = None  # camelCase frontend alias
    session_key: Optional[str] = None
    sessionKey: Optional[str] = None


class ProviderLoginBody(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


class ProviderSessionBody(BaseModel):
    username: Optional[str] = None
    cookies: Optional[Any] = None
    cookieHeader: Optional[str] = None
    localStorage: Optional[Any] = None
    origins: Optional[Any] = None
    storageState: Optional[dict[str, Any]] = None
    userAgent: Optional[str] = None
    user_agent: Optional[str] = None
    xBc: Optional[str] = None
    xbc: Optional[str] = None


class ProviderEnabledBody(BaseModel):
    enabled: bool


class FollowingSyncBody(BaseModel):
    usernames: Optional[list[str]] = None


class ProviderFollowBody(BaseModel):
    """Optional body for follow/unfollow.

    remoteCompleted=True means Mac Helper (or the browser) already performed
    the remote mutation; the VPS only updates the local follow cache.
    """
    remoteCompleted: bool = False


class ModelVolumeBody(BaseModel):
    volume: float


class MacLocalFileBody(BaseModel):
    recordingId: Optional[str] = None
    filename: str
    size: int
    durationSeconds: Optional[int] = None
    resolution: Optional[str] = None
    fps: Optional[float] = None
    bitrate: Optional[int] = None


class MacSyncSnapshotBody(BaseModel):
    localSessionId: str
    files: list[MacLocalFileBody]
    # Helper --origin; used to build Mac download URLs without a browser Request.
    origin: Optional[str] = None
    # Item/recording ids whose Motrix tasks failed permanently; drop from inflight jobs
    # so catch-up can enqueue a fresh download with a new URL.
    releaseItemIds: Optional[list[str]] = None
    # Motrix progress rows: {itemId|recordingId, completedLength, totalLength, downloadSpeed, status}
    downloadProgress: Optional[list[dict[str, Any]]] = None
    # Concat task reports: {projectId|taskId, status, error?, step?}
    concatReports: Optional[list[dict[str, Any]]] = None
    # Helper: all download tasks paused and no pending relocate (for lite ack)
    downloadsPaused: Optional[bool] = None


class MacDownloadJobBody(BaseModel):
    localSessionId: str
    itemIds: list[str]
    method: str = "chrome"


class TaskQueueMutateBody(BaseModel):
    taskIds: Optional[list[str]] = None
    projectIds: Optional[list[str]] = None


class ConcatStatusBody(BaseModel):
    localSessionId: str
    projectId: Optional[str] = None
    taskId: Optional[str] = None
    status: str
    error: Optional[str] = None
    step: Optional[int] = None
    finalRelativePath: Optional[str] = None


class MediaDeletePreviewBody(BaseModel):
    itemIds: list[str]


class MacHelperOpenBody(BaseModel):
    localSessionId: str
    relativePath: Optional[str] = None
    recordingId: Optional[str] = None
    reveal: bool = False


class MacHelperDeleteItem(BaseModel):
    relativePath: Optional[str] = None
    recordingId: Optional[str] = None


class MacHelperDeleteBody(BaseModel):
    localSessionId: str
    items: list[MacHelperDeleteItem]


class MacHelperThumbBody(BaseModel):
    localSessionId: str
    relativePath: Optional[str] = None
    recordingId: Optional[str] = None
    contentType: Optional[str] = None
    imageBase64: str


class MacHelperImportSessionBody(BaseModel):
    localSessionId: str
    sourceType: str


class MacHelperProviderFollowBody(BaseModel):
    localSessionId: str
    sourceType: str
    username: str
    follow: bool = True


class MacHelperProviderFollowResultBody(BaseModel):
    localSessionId: str
    commandId: str
    sourceType: str
    username: str
    follow: bool = True
    success: bool = False
    error: Optional[str] = None
    detail: Optional[str] = None
    action: Optional[str] = None
    userId: Optional[str] = None


class MacHelperProviderSessionBody(BaseModel):
    localSessionId: str
    commandId: str
    sourceType: str
    cookieHeader: Optional[str] = None
    cookies: Optional[Any] = None
    userAgent: Optional[str] = None
    username: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    loginUrl: Optional[str] = None


class MediaBatchDeleteBody(BaseModel):
    itemIds: list[str]


class ProcessingFilesDeleteBody(BaseModel):
    paths: list[str]


_mac_download_jobs: dict[str, dict[str, Any]] = {}
_mac_download_tokens: dict[str, dict[str, Any]] = {}
_mac_helper_snapshot: dict[str, Any] = {}
_mac_helper_commands: dict[str, dict[str, Any]] = {}
_mac_helper_thumbs: dict[str, dict[str, Any]] = {}
_mac_helper_import_results: dict[str, dict[str, Any]] = {}
_mac_helper_follow_results: dict[str, dict[str, Any]] = {}
_mac_state_lock = threading.Lock()
_mac_download_public_base: str = ""
_library_space_blocked: bool = False
_staging_blocked: bool = False
_disk_convert_paused: bool = False
_media_profile_live_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "session"


def _records_root() -> Path:
    return (OUTPUT_DIR / "records").resolve()


def _staging_records_root() -> Path:
    return (STAGING_DIR / "records").resolve()


def _default_record_path(username: str) -> str:
    return f"{_clean_record_path_part(username)}/videos/record"


def _legacy_record_path(username: str) -> str:
    return _clean_record_path_part(username)


def _clean_record_path_part(part: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(part or "").strip())
    cleaned = cleaned.strip(".-")
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid record path")
    return cleaned


def _normalize_record_path(value: object, username: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = _default_record_path(username)

    raw = raw.replace("\\", "/")
    root = _records_root()

    if raw.startswith("/"):
        try:
            absolute = Path(raw).resolve()
            relative = absolute.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail="Record path must stay under /data/records")
        parts = relative.parts
    else:
        raw = raw.strip("/")
        if raw == "records":
            raw = ""
        elif raw.startswith("records/"):
            raw = raw[len("records/"):]
        parts = Path(raw).parts

    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="Invalid record path")

    cleaned_parts = [_clean_record_path_part(part) for part in parts]
    if not cleaned_parts:
        raise HTTPException(status_code=400, detail="Invalid record path")
    model_folder = _clean_record_path_part(username)
    if cleaned_parts[0].lower() != model_folder.lower():
        raise HTTPException(status_code=400, detail="Record path must stay inside this model folder")
    cleaned_parts[0] = model_folder

    relative_path = Path(*cleaned_parts)
    absolute_path = (root / relative_path).resolve()
    if not absolute_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Record path must stay under /data/records")
    return relative_path.as_posix()


def _record_path_from_model(model: Optional[dict], username: str) -> str:
    try:
        if model is not None and not (model.get("record_path") or "").strip():
            return _legacy_record_path(username)
        return _normalize_record_path((model or {}).get("record_path"), username)
    except HTTPException:
        logger.warning(
            "Invalid model recording path, falling back to default",
            username=username,
            record_path=(model or {}).get("record_path"),
        )
        return _default_record_path(username)


def _record_dir_from_path(record_path: str) -> Path:
    return (_records_root() / record_path).resolve()


def _model_record_dir(model: Optional[dict], username: str) -> Path:
    return _record_dir_from_path(_record_path_from_model(model, username))


def _model_staging_record_dir(model: Optional[dict], username: str) -> Path:
    """Directory on the SSD staging volume where live TS is written."""
    return (_staging_records_root() / _record_path_from_model(model, username)).resolve()


def _record_path_fields(username: str, model: Optional[dict] = None) -> dict:
    record_path = _record_path_from_model(model, username)
    return {
        "recordPath": record_path,
        "record_path": record_path,
        "recordPathDefault": _default_record_path(username),
        "recordPathLegacy": _legacy_record_path(username),
        "recordPathDisplay": str(_record_dir_from_path(record_path)),
    }


def _record_dirs_for_model(username: str, model: Optional[dict] = None) -> list[Path]:
    candidates = [
        _model_record_dir(model, username),
        _record_dir_from_path(_default_record_path(username)),
        _records_root() / username,
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


@app.get("/")
async def index():
    """Root serves the Media library."""
    return FileResponse(str(STATIC_DIR / "media.html"))


@app.get("/discover")
async def discover_page():
    """Discover page - browse live models"""
    return FileResponse(str(STATIC_DIR / "discover.html"))


@app.get("/following")
async def following_page():
    """Legacy Following page; Media now owns follow/recording cards."""
    return RedirectResponse(url="/media", status_code=307)

@app.get("/recordings")
async def recordings_page():
    """Legacy recordings page; Media is now the library and profile surface."""
    return RedirectResponse(url="/media", status_code=307)


@app.get("/media")
async def media_page():
    """Media page - file library for recordings folders"""
    return FileResponse(str(STATIC_DIR / "media.html"))


@app.get("/settings")
async def settings_page():
    """Settings page"""
    return FileResponse(str(STATIC_DIR / "settings.html"))


@app.get("/watch/{username}")
async def watch_page(username: str):
    """Watch page - view live stream or recording for a model"""
    return FileResponse(str(STATIC_DIR / "watch.html"))


@app.get("/login")
async def login_page():
    """Login page"""
    return FileResponse(str(STATIC_DIR / "login.html"))


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def api_login(body: LoginBody, response: Response):
    """Login endpoint."""
    if not PASSWORD:
        raise HTTPException(status_code=400, detail="Authentication is not configured")

    if not verify_password(body.password):
        logger.warning("Login attempt failed")
        raise HTTPException(status_code=401, detail="Incorrect password")

    session_token = generate_session_token()
    active_sessions.add(session_token)

    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        max_age=86400 * 30,  # 30 days
        samesite="lax"
    )

    logger.info("Login successful")
    return {"success": True, "message": "Signed in"}


@app.post("/api/logout")
async def api_logout(response: Response, session_token: Optional[str] = Cookie(None)):
    """Logout endpoint."""
    if session_token and session_token in active_sessions:
        active_sessions.remove(session_token)

    response.delete_cookie(key="session_token")
    logger.info("Logout successful")
    return {"success": True, "message": "Signed out"}


@app.get("/favicon.ico")
async def favicon():
    """Return a simple SVG favicon"""
    from fastapi.responses import Response
    svg_favicon = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="45" fill="#6366f1"/>
        <circle cx="50" cy="35" r="8" fill="white"/>
        <rect x="35" y="45" width="30" height="35" rx="5" fill="white"/>
        <rect x="42" y="52" width="16" height="20" fill="#6366f1"/>
    </svg>'''
    return Response(content=svg_favicon, media_type="image/svg+xml")


@app.get("/api/version")
async def get_version():
    """Return version and configuration information"""
    version = os.environ.get("APP_VERSION", "dev")
    check_interval = await get_check_interval_seconds(db)
    return {
        "version": version,
        "output_dir": str(OUTPUT_DIR),
        "ffmpeg_path": FFMPEG_PATH,
        "authentication_required": bool(PASSWORD),
        "check_interval": check_interval,
        "check_interval_seconds": check_interval,
    }


# ============================================
# Logs Endpoints
# ============================================

@app.get("/api/logs")
async def get_logs(level: Optional[str] = None, limit: int = 200, offset: int = 0):
    """Return application logs from memory"""
    logs = logger.memory_handler.get_logs(level=level, limit=limit, offset=offset)
    total = logger.memory_handler.get_total(level=level)
    return {"logs": logs, "total": total}


@app.get("/model.html")
async def model_page(request: Request):
    """Redirect the retired legacy player to the maintained Watch page."""
    username = str(request.query_params.get("username") or "").strip()
    if not username:
        return RedirectResponse("/", status_code=308)
    target = f"/watch/{quote(username, safe='')}"
    remaining_query = urlencode(
        [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "username"
        ]
    )
    if remaining_query:
        target = f"{target}?{remaining_query}"
    return RedirectResponse(target, status_code=308)


@app.api_route("/vendor/{asset_name}", methods=["GET", "HEAD"])
async def amazon_ivs_player_asset(asset_name: str, request: Request):
    media_type = IVS_PLAYER_ASSETS.get(asset_name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Unknown vendor asset")

    now = time.time()
    cached = _IVS_PLAYER_ASSET_CACHE.get(asset_name) or {}
    cached_body = cached.get("body")
    cached_at = float(cached.get("cached_at") or 0)
    cache_headers = {"Cache-Control": "public, max-age=86400"}
    if isinstance(cached_body, bytes) and now - cached_at < 86400:
        if request.method == "HEAD":
            return Response(status_code=200, media_type=media_type, headers=cache_headers)
        return Response(content=cached_body, media_type=media_type, headers=cache_headers)

    try:
        async with aiohttp_client_session(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(
                f"{IVS_PLAYER_ASSET_BASE_URL}/{asset_name}",
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status >= 400:
                    raise HTTPException(status_code=502, detail="IVS player asset unavailable")
                body = await resp.read()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error loading IVS asset", asset=asset_name, error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail="IVS player asset unavailable")

    _IVS_PLAYER_ASSET_CACHE[asset_name] = {"body": body, "cached_at": now}
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=cache_headers)
    return Response(content=body, media_type=media_type, headers=cache_headers)


@app.api_route("/api/proxy/hls/{token_path:path}", methods=["GET", "HEAD"])
async def hls_proxy(token_path: str, request: Request):
    token = (token_path or "").split("/", 1)[0].split(".", 1)[0]
    entry = _HLS_PROXY_CACHE.get(token)
    if not entry or entry.get("expires_at", 0) <= time.time():
        _HLS_PROXY_CACHE.pop(token, None)
        raise HTTPException(status_code=404, detail="Proxy stream expired")

    url = entry["url"]
    headers = dict(entry.get("headers") or {})
    entry["expires_at"] = time.time() + max(60, _HLS_PROXY_TTL_SECONDS)
    if "body" in entry:
        content_type = entry.get("content_type") or "application/octet-stream"
        if request.method == "HEAD":
            return Response(status_code=200, media_type=content_type)
        return Response(
            content=entry.get("body") or b"",
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )
    try:
        async with aiohttp_client_session(timeout=aiohttp.ClientTimeout(total=30)) as session:
            last_status = None
            for attempt_headers in _hls_segment_header_variants(headers):
                async with session.get(
                    url,
                    headers=attempt_headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    last_status = resp.status
                    if resp.status in {401, 403} and "Cookie" in attempt_headers:
                        logger.debug(
                            "Proxy HLS upstream rejected cookies, retrying without them",
                            url=url,
                            status=resp.status,
                        )
                        continue
                    if resp.status >= 400:
                        raise HTTPException(status_code=resp.status, detail=f"Provider HTTP {resp.status}")
                    content_type = resp.headers.get("Content-Type", "")
                    if request.method == "HEAD":
                        return Response(status_code=200, media_type=content_type or None)
                    body = await resp.read()
                    lower_url = str(resp.url).lower()
                    is_playlist = (
                        ".m3u8" in lower_url
                        or "mpegurl" in content_type.lower()
                        or body.startswith(b"#EXTM3U")
                    )
                    if is_playlist:
                        text = body.decode("utf-8", errors="replace")
                        if "flags=segment" in text and "#EXT-X-ENDLIST" in text:
                            rewritten = await _rewrite_prefetched_single_segment_playlist(
                                text,
                                str(resp.url),
                                headers=attempt_headers,
                                entry=entry,
                                session=session,
                            )
                        else:
                            rewritten = _rewrite_hls_playlist(text, str(resp.url), headers=attempt_headers)
                        return Response(
                            content=rewritten,
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"},
                        )
                    return Response(
                        content=body,
                        media_type=content_type or "application/octet-stream",
                        headers={"Cache-Control": "no-store"},
                    )
            raise HTTPException(status_code=last_status or 502, detail=f"Provider HTTP {last_status or 502}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("HLS proxy error", url=url, error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail="HLS proxy error")


async def _provider_login_state(source_type: str, *, verify: bool = False) -> dict:
    try:
        provider = _provider_for(source_type)
    except Exception:
        return {"isLoggedIn": False, "username": None, "lastError": "Unknown provider"}

    if not getattr(getattr(provider, "capabilities", None), "can_login", False):
        return {
            "isLoggedIn": False,
            "username": None,
            "lastError": None,
            "lastLoginAt": None,
            "hasCookies": False,
            "hasLocalStorage": False,
            "hasSession": False,
            "hasSavedSessionData": False,
            "hasSavedCredentials": False,
            "accountDisabled": True,
        }

    status: dict
    used_auth_status = False
    auth = getattr(provider, "auth", None)
    if auth is not None:
        try:
            status = auth.get_status()
            row = await db.get_provider_session(source_type)
            has_saved_credentials = bool(row and row.get("credential_username") and row.get("credential_password"))
            has_local_storage = _stored_json_has_items(row.get("local_storage") if row else None)
            has_saved_session_data = bool(status.get("hasCookies") or has_local_storage)
            is_logged_in = bool(status.get("isLoggedIn"))
            status["hasSavedCredentials"] = has_saved_credentials
            status["credentialsUpdatedAt"] = row.get("credentials_updated_at") if has_saved_credentials else None
            status["hasLocalStorage"] = has_local_storage
            status["hasSavedSessionData"] = has_saved_session_data
            status["hasSession"] = bool(is_logged_in and (status.get("hasCookies") or has_local_storage or is_logged_in))
            if not is_logged_in:
                status["lastLoginAt"] = None
            if not status.get("username") and row:
                status["username"] = row.get("credential_username")
            used_auth_status = True
        except Exception:
            status = {}
            auth = None
    else:
        status = {}

    if not status:
        row = await db.get_provider_session(source_type)
        if not row:
            return {
                "isLoggedIn": False,
                "username": None,
                "lastError": None,
                "hasCookies": False,
                "hasLocalStorage": False,
                "hasSession": False,
                "hasSavedSessionData": False,
                "hasSavedCredentials": False,
            }
        has_saved_credentials = bool(row.get("credential_username") and row.get("credential_password"))
        has_cookies = _stored_json_has_items(row.get("session_cookies"))
        has_local_storage = _stored_json_has_items(row.get("local_storage"))
        has_saved_session_data = bool(has_cookies or has_local_storage)
        is_logged_in = bool(row.get("is_logged_in")) and bool(has_cookies or has_local_storage)
        username = row.get("username") or row.get("credential_username")
        last_error = row.get("last_error")
        status = {
            "isLoggedIn": is_logged_in,
            "username": username,
            "lastError": last_error,
            "lastLoginAt": row.get("last_login_at") if is_logged_in else None,
            "hasCookies": has_cookies,
            "hasLocalStorage": has_local_storage,
            "hasSession": is_logged_in,
            "hasSavedSessionData": has_saved_session_data,
            "hasSavedCredentials": has_saved_credentials,
            "credentialsUpdatedAt": row.get("credentials_updated_at") if has_saved_credentials else None,
        }

    should_verify = False
    if hasattr(provider, "verify_logged_in"):
        if verify:
            # Settings: live-check any provider that has session material.
            should_verify = bool(
                status.get("isLoggedIn")
                or status.get("hasSavedSessionData")
                or status.get("hasCookies")
            )
        elif status.get("isLoggedIn") and not used_auth_status:
            # Discover/other: light re-check for cookie-session providers only.
            # Chaturbate auth memory is validated on Settings verify=1 / sync.
            should_verify = True

    if should_verify:
        timeout_s = 15.0 if verify else 8.0
        try:
            verified = await asyncio.wait_for(provider.verify_logged_in(), timeout=timeout_s)
        except Exception:
            verified = None
        if verified is False:
            status["isLoggedIn"] = False
            status["hasSession"] = False
            status["lastLoginAt"] = None
            status["lastError"] = status.get("lastError") or "Saved session is not logged in"
            if getattr(provider, "session_store", None):
                try:
                    state = await provider.session_store.get(source_type)
                    await provider.session_store.save(
                        source_type=source_type,
                        username=status.get("username"),
                        is_logged_in=False,
                        cookies=state.get("cookies") or [],
                        local_storage=state.get("localStorage") or [],
                        last_error=status.get("lastError"),
                    )
                except Exception:
                    pass
        elif verified is True:
            status["isLoggedIn"] = True
            status["hasSession"] = True
            status["lastError"] = None
            if getattr(provider, "session_store", None):
                try:
                    state = await provider.session_store.get(source_type)
                    if state and not state.get("is_logged_in"):
                        await provider.session_store.save(
                            source_type=source_type,
                            username=status.get("username") or state.get("username"),
                            is_logged_in=True,
                            cookies=state.get("cookies") or [],
                            local_storage=state.get("localStorage") or [],
                            last_error=None,
                        )
                except Exception:
                    pass
    return status


async def _invalidate_provider_login(source_type: str, error: str) -> None:
    """Mark a provider session as logged out after a live auth failure."""
    message = (error or "").strip() or "Saved session is not logged in"
    try:
        provider = _provider_for(source_type)
    except Exception:
        return
    auth = getattr(provider, "auth", None)
    if auth is not None:
        try:
            auth._is_logged_in = False
            auth._last_error = message
            cookies = auth.get_cookies() if hasattr(auth, "get_cookies") else {}
            row = await auth.db.get_auth_state() if getattr(auth, "db", None) else None
            if getattr(auth, "db", None):
                await auth.db.save_auth_state(
                    username=getattr(auth, "_username", None) or (row or {}).get("username") or "",
                    password_hash=(row or {}).get("password_hash") or "",
                    is_logged_in=False,
                    session_cookies=json.dumps(cookies or {}),
                    cf_clearance=(cookies or {}).get("cf_clearance"),
                    csrf_token=(cookies or {}).get("csrftoken"),
                    last_login_at=(row or {}).get("last_login_at"),
                    last_error=message,
                )
        except Exception:
            pass
    if getattr(provider, "session_store", None):
        try:
            state = await provider.session_store.get(source_type)
            await provider.session_store.save(
                source_type=source_type,
                username=(state or {}).get("username"),
                is_logged_in=False,
                cookies=(state or {}).get("cookies") or [],
                local_storage=(state or {}).get("localStorage") or [],
                last_error=message,
            )
        except Exception:
            pass


def _stored_json_has_items(raw_value: object) -> bool:
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        return False
    if isinstance(parsed, list):
        return any(bool(item) for item in parsed)
    if isinstance(parsed, dict):
        return bool(parsed)
    return False

async def _saved_provider_credentials(source_type: str) -> Optional[tuple[str, str]]:
    row = await db.get_provider_session(source_type)
    if not row:
        return None
    username = (row.get("credential_username") or "").strip()
    password = row.get("credential_password") or ""
    if not username or not password:
        return None
    return username, password


async def _login_with_saved_provider_credentials(source_type: str) -> bool:
    credentials = await _saved_provider_credentials(source_type)
    if not credentials:
        return False
    username, password = credentials
    result = await _provider_for(source_type).login(username, password)
    return bool(result.get("success"))


async def _ensure_saved_provider_login(provider, source_type: str) -> None:
    try:
        logged_in = await _login_with_saved_provider_credentials(source_type)
    except ProviderInteractionRequired:
        raise HTTPException(
            status_code=409,
            detail=f"{provider.display_name}: verified browser session required",
        )
    except ProviderAuthError:
        raise HTTPException(status_code=401, detail=f"{provider.display_name}: login required")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not logged_in:
        raise HTTPException(status_code=401, detail=f"{provider.display_name}: login required")


async def _disabled_provider_sources() -> set[str]:
    try:
        return set(await db.get_disabled_providers())
    except Exception:
        return set()


@app.get("/api/providers")
async def list_providers(verify: bool = False):
    """List providers. Pass verify=1 from Settings to live-check each account session."""
    disabled_sources = await _disabled_provider_sources()

    async def _one(meta: dict) -> dict:
        source_type = meta["sourceType"]
        item = dict(meta)
        item["enabled"] = source_type not in disabled_sources
        item["status"] = await _provider_login_state(source_type, verify=bool(verify))
        return item

    providers = list(await asyncio.gather(*[_one(meta) for meta in provider_registry.metadata()]))
    return {"providers": providers, "sourceTypes": sorted(_available_source_types())}


@app.put("/api/providers/{source_type}/enabled")
async def provider_set_enabled(source_type: str, body: ProviderEnabledBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")

    disabled_sources = await _disabled_provider_sources()
    if body.enabled:
        disabled_sources.discard(source_type)
    else:
        disabled_sources.add(source_type)
    await db.set_disabled_providers(sorted(disabled_sources))
    return {
        "sourceType": source_type,
        "enabled": source_type not in disabled_sources,
        "disabledProviders": sorted(disabled_sources),
    }


@app.get("/api/providers/{source_type}/status")
async def provider_status(source_type: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    return {
        **provider.metadata(),
        **await _provider_login_state(source_type),
    }


def _schedule_following_sync(source_type: str) -> None:
    """No-op. Import/login does not pull remote follows.

    The user must click Sync follows so each site replaces only that
    provider's HXYLIVE list.
    """
    return


async def _persist_local_follow(provider, source_type: str, username: str) -> None:
    status = ProviderStatus(False, source_type=source_type)
    try:
        status = await asyncio.wait_for(_provider_status(source_type, username), timeout=2.5)
    except Exception:
        pass
    display_name = username
    thumbnail_url = status.thumbnail
    if source_type in {"bilibili", "youtube"} and hasattr(provider, "resolve_watch_meta"):
        try:
            meta = await asyncio.wait_for(provider.resolve_watch_meta(username), timeout=4.0)
            if isinstance(meta, dict) and meta:
                display_name = str(meta.get("displayName") or username).strip() or username
                thumbnail_url = (
                    meta.get("thumbnail")
                    or meta.get("profileImageUrl")
                    or thumbnail_url
                )
                if meta.get("isOnline") is not None:
                    status = ProviderStatus(
                        is_online=bool(meta.get("isOnline")),
                        viewers=int(meta.get("viewers") or 0),
                        room_status="public" if meta.get("isOnline") else "offline",
                        thumbnail=thumbnail_url,
                        source_type=source_type,
                    )
        except Exception:
            pass
    await db.upsert_followed_model(
        username=username,
        display_name=display_name,
        is_online=bool(status.is_online),
        viewers=int(status.viewers or 0),
        thumbnail_url=thumbnail_url,
        source_type=source_type,
        room_status=status.room_status,
    )
    try:
        await db.reconcile_model_sources_from_followed()
    except Exception:
        pass


async def _apply_unfollow_keep_card(source_type: str, username: str) -> list[str]:
    """Remove remote-follow cache row. Streamer card and recording stay unchanged."""
    await db.delete_followed_model(username, source_type=source_type)
    return []


async def _apply_follow_change(
    provider,
    source_type: str,
    username: str,
    *,
    follow: bool,
    remote_completed: bool = False,
) -> dict:
    """Remote site follow/unfollow only. Does not create or delete local cards.

    When ``remote_completed`` is True, the browser/Mac Helper already performed
    the site mutation (needed for Twitch integrity). Only refresh the local
    follow cache.
    """
    can_remote = bool(getattr(provider.capabilities, "can_sync_following", False))
    if not can_remote:
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name}: remote follow unsupported",
        )
    try:
        login_state = await _provider_login_state(source_type)
    except Exception:
        login_state = {}
    if not bool(login_state.get("isLoggedIn")):
        raise HTTPException(
            status_code=401,
            detail=f"{provider.display_name}: log in under Settings before remote follow",
        )
    action_name = "follow" if follow else "unfollow"
    if remote_completed:
        remote = {
            "success": True,
            "remote": True,
            "provider": source_type,
            "username": str(username or "").strip(),
            "via": "mac-helper",
            "action": action_name,
        }
    else:
        try:
            remote_call = provider.follow if follow else provider.unfollow
            remote = await asyncio.wait_for(remote_call(username), timeout=20)
        except ProviderInteractionRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProviderAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"{provider.display_name}: {action_name} timeout",
            )
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not remote.get("success", True):
            raise HTTPException(
                status_code=400,
                detail=remote.get("error") or remote.get("detail") or f"Remote {action_name} failed",
            )
    if follow:
        await _persist_local_follow(provider, source_type, username)
        return {
            **remote,
            "success": True,
            "localOnly": False,
            "action": remote.get("action") or action_name,
            "remoteCompleted": bool(remote_completed),
        }
    stopped = await _apply_unfollow_keep_card(source_type, username)
    return {
        **remote,
        "success": True,
        "localOnly": False,
        "action": remote.get("action") or action_name,
        "cardKept": True,
        "stoppedSessionIds": stopped,
        "remoteCompleted": bool(remote_completed),
    }


@app.post("/api/providers/{source_type}/login")
async def provider_login(source_type: str, body: ProviderLoginBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_login", False):
        raise HTTPException(status_code=400, detail=f"{provider.display_name}: account connection unsupported")
    username = (body.username or "").strip()
    password = body.password or ""
    saved_credentials = False
    if password:
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        await db.save_provider_credentials(source_type, username, password)
        saved_credentials = True
    else:
        credentials = await _saved_provider_credentials(source_type)
        if not credentials:
            raise HTTPException(status_code=400, detail="Username and password are required")
        username, password = credentials
        saved_credentials = True
    try:
        result = await provider.login(username, password)
    except ProviderInteractionRequired as exc:
        return JSONResponse(
            status_code=409,
            content={"success": False, "savedCredentials": saved_credentials, "detail": str(exc)},
        )
    except ProviderError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "savedCredentials": saved_credentials, "detail": str(exc)},
        )
    if not result.get("success"):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "savedCredentials": saved_credentials,
                "detail": result.get("error", "Login failed"),
            },
        )
    result["savedCredentials"] = saved_credentials
    return result


def _provider_session_payload(body: ProviderSessionBody) -> tuple[Optional[list[dict[str, Any]]], list[dict[str, Any]]]:
    storage_state = body.storageState if isinstance(body.storageState, dict) else {}
    cookies = body.cookies if body.cookies is not None else storage_state.get("cookies")
    local_storage = (
        body.localStorage
        if body.localStorage is not None
        else body.origins
        if body.origins is not None
        else storage_state.get("origins")
    )

    if cookies is not None and not isinstance(cookies, list):
        raise HTTPException(status_code=400, detail="Session cookies must be a JSON array or cookie header")
    normalized_cookies = [
        cookie
        for cookie in (cookies or [])
        if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value") is not None
    ]
    if local_storage is None:
        local_storage = []
    if not isinstance(local_storage, list):
        raise HTTPException(status_code=400, detail="localStorage/origins must be a JSON array")
    normalized_storage = [entry for entry in local_storage if isinstance(entry, dict)]
    return normalized_cookies or None, normalized_storage


@app.post("/api/providers/{source_type}/session")
async def provider_import_session(source_type: str, body: ProviderSessionBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_login", False):
        raise HTTPException(status_code=400, detail=f"{provider.display_name}: session import unsupported")
    cookies, local_storage = _provider_session_payload(body)
    cookie_header = (body.cookieHeader or "").strip()
    if not cookies and not cookie_header and not local_storage:
        raise HTTPException(status_code=400, detail="No valid session data provided")
    username = (body.username or "").strip() or None
    try:
        result = await provider.import_session(
            username=username,
            cookie_header=cookie_header or None,
            cookies=cookies,
            local_storage=local_storage,
            user_agent=(body.userAgent or body.user_agent or None),
            x_bc=(body.xBc or body.xbc or None),
        )
    except ProviderError as exc:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(exc)})
    if not result.get("success"):
        return JSONResponse(
            status_code=401,
            content={"success": False, "detail": result.get("error", "Session import failed")},
        )
    return result


@app.post("/api/providers/{source_type}/logout")
async def provider_logout(source_type: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    result = await _provider_for(source_type).logout()
    return result


def _public_following_sync_item(item: dict) -> dict:
    row = item if isinstance(item, dict) else {}
    username = str(row.get("username") or "").strip()
    profile_image = str(
        row.get("profile_image_url")
        or row.get("profileImageUrl")
        or row.get("avatar_url")
        or row.get("avatarUrl")
        or ""
    ).strip()
    thumbnail = str(row.get("thumbnail_url") or row.get("thumbnail") or "").strip()
    return {
        "username": username,
        "displayName": str(row.get("display_name") or row.get("displayName") or username).strip() or username,
        "isOnline": bool(row.get("is_online", row.get("isOnline", False))),
        "viewers": int(row.get("viewers") or 0),
        "profileImageUrl": profile_image or None,
        "thumbnailUrl": thumbnail or None,
    }


async def _fetch_provider_following_items(provider, source_type: str):
    try:
        return await asyncio.wait_for(provider.sync_following(), timeout=60)
    except ProviderAuthError as first_exc:
        try:
            await _ensure_saved_provider_login(provider, source_type)
            return await asyncio.wait_for(provider.sync_following(), timeout=60)
        except ProviderAuthError as retry_exc:
            await _invalidate_provider_login(source_type, str(retry_exc) or str(first_exc))
            raise
        except Exception:
            await _invalidate_provider_login(source_type, str(first_exc))
            raise first_exc


@app.get("/api/providers/{source_type}/following/remote")
async def provider_list_remote_following(source_type: str):
    """Preview remote follows without writing the HXYLIVE follow list."""
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_sync_following", False):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name}: remote following sync unsupported; local follows only",
        )
    try:
        items = await _fetch_provider_following_items(provider, source_type)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{provider.display_name}: following sync timeout")
    except ProviderInteractionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProviderAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    trusted = sync_items_trusted(items)
    public_items = [_public_following_sync_item(item) for item in (items or []) if isinstance(item, dict)]
    public_items = [item for item in public_items if item.get("username")]
    local_rows = await db.get_all_followed()
    local_usernames = sorted(
        {
            str(row.get("username") or "").strip()
            for row in (local_rows or [])
            if str(row.get("source_type") or "").strip().lower() == source_type
            and str(row.get("username") or "").strip()
        },
        key=str.lower,
    )
    return {
        "sourceType": source_type,
        "trusted": trusted,
        "authoritative": sync_items_authoritative(items),
        "skippedReason": sync_items_skipped_reason(items),
        "count": len(public_items),
        "items": public_items,
        "localUsernames": local_usernames,
        "selectDefault": "all",
    }


@app.post("/api/providers/{source_type}/following/sync")
async def provider_sync_following(source_type: str, body: Optional[FollowingSyncBody] = None):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_sync_following", False):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name}: remote following sync unsupported; local follows only",
        )
    selected = body.usernames if body is not None else None
    if selected is not None and not [
        str(name or "").strip() for name in selected if str(name or "").strip()
    ]:
        raise HTTPException(status_code=400, detail="Select at least one channel to sync")

    try:
        items = await _fetch_provider_following_items(provider, source_type)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{provider.display_name}: following sync timeout")
    except ProviderInteractionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProviderAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if selected is not None:
        items = filter_following_items(items, selected)
        if sync_items_trusted(items) and not list(items or []):
            raise HTTPException(
                status_code=400,
                detail="None of the selected channels were found in the remote follow list",
            )

    stored = await store_provider_following(db, source_type, items)
    repaired_sources = await db.reconcile_model_sources_from_followed()
    return {
        "synced": stored["synced"],
        "sourceType": source_type,
        "trusted": stored["trusted"],
        "authoritative": stored["authoritative"],
        "skippedReason": stored["skippedReason"],
        "repairedSources": repaired_sources,
        "message": (
            f"{provider.display_name}: remote follow cache refreshed ({stored['synced']})"
            if stored["trusted"]
            else f"{provider.display_name}: following sync skipped"
        ),
    }


@app.get("/api/providers/{source_type}/following/reconcile")
async def provider_reconcile_following(source_type: str):
    """Compare local cards for a site with remote follows (read + cache refresh).

    Does not create or delete cards. Returns three buckets for the Media sync UI.
    """
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_sync_following", False):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name}: remote following sync unsupported",
        )
    try:
        items = await _fetch_provider_following_items(provider, source_type)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{provider.display_name}: following sync timeout")
    except ProviderInteractionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProviderAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    trusted = sync_items_trusted(items)
    if trusted:
        await store_provider_following(db, source_type, items)
        try:
            await db.reconcile_model_sources_from_followed()
        except Exception:
            pass

    all_sources = await db.get_all_media_profile_sources()
    all_profiles = await db.get_all_media_profiles()
    profiles_by_name = {
        str(row.get("username") or "").strip().lower(): row
        for row in (all_profiles or [])
        if str(row.get("username") or "").strip()
    }
    local_cards = []
    for source in all_sources or []:
        st = _normalize_source_type(source.get("source_type")) or ""
        if st != source_type:
            continue
        channel = str(source.get("channel_username") or "").strip()
        profile_username = str(source.get("profile_username") or channel).strip()
        if not channel:
            continue
        profile = profiles_by_name.get(profile_username.lower()) or {}
        local_cards.append({
            "username": channel,
            "profileUsername": profile_username,
            "displayName": profile.get("display_name") or channel,
            "profileImageUrl": profile.get("profile_image_url"),
            "channelUrl": source.get("channel_url") or _canonical_stream_url(source_type, channel),
            "autoRecord": bool(source.get("auto_record")),
            "sourceType": source_type,
        })

    buckets = classify_follow_reconcile(
        source_type=source_type,
        remote_items=list(items or []),
        local_cards=local_cards,
    )
    return {
        "sourceType": source_type,
        "trusted": trusted,
        "authoritative": sync_items_authoritative(items),
        "skippedReason": sync_items_skipped_reason(items),
        "cardOnly": buckets["cardOnly"],
        "remoteOnly": buckets["remoteOnly"],
        "both": buckets["both"],
        "counts": {
            "cardOnly": len(buckets["cardOnly"]),
            "remoteOnly": len(buckets["remoteOnly"]),
            "both": len(buckets["both"]),
        },
    }


class EnsureCardBody(BaseModel):
    username: str
    sourceType: Optional[str] = None
    source_type: Optional[str] = None
    displayName: Optional[str] = None
    display_name: Optional[str] = None
    profileImageUrl: Optional[str] = None
    profile_image_url: Optional[str] = None
    channelUrl: Optional[str] = None
    channel_url: Optional[str] = None
    channelUsername: Optional[str] = None
    channel_username: Optional[str] = None
    autoRecord: bool = False
    auto_record: Optional[bool] = None


@app.post("/api/media-profiles/ensure-card")
async def ensure_media_profile_card(body: EnsureCardBody):
    """Create or keep a local streamer card. Optional autoRecord. Never touches remote follows."""
    username = _validate_media_profile_username(
        str(body.channelUsername or body.channel_username or body.username or "").strip()
    )
    source_type = _normalize_source_type(body.sourceType or body.source_type) or "chaturbate"
    if source_type not in _available_source_types():
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is not available")
    display_name = str(body.displayName or body.display_name or username).strip() or username
    profile_image = body.profileImageUrl or body.profile_image_url
    channel_url = body.channelUrl or body.channel_url or _canonical_stream_url(source_type, username)
    want_record = bool(body.autoRecord if body.auto_record is None else body.auto_record)

    created = await ensure_streamer_card(
        db,
        username,
        source_type,
        display_name=display_name,
        profile_image_url=profile_image,
        channel_url=channel_url,
        auto_record=want_record,
    )
    if want_record:
        await db.set_media_profile_auto_record(username, True, source_type=source_type)
        await db.add_or_update_model(
            username=username,
            display_name=display_name,
            auto_record=True,
            record_quality="best",
            retention_days=30,
            source_type=source_type,
        )
    profile = await _media_profile_payload(created or username)
    return {
        "success": True,
        "username": created or username,
        "autoRecord": want_record,
        "profile": profile,
    }


@app.get("/api/providers/{source_type}/is-following/{username}")
async def provider_is_following(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    local = await db.get_followed_model(username, source_type=source_type)
    local_match = bool(local and (local.get("source_type") or "chaturbate") == source_type)
    remote_match = False
    remote_checked = False
    if getattr(provider.capabilities, "can_sync_following", False):
        try:
            remote_match = bool(await provider.is_following(username))
            remote_checked = True
        except ProviderAuthError:
            try:
                await _ensure_saved_provider_login(provider, source_type)
                remote_match = bool(await provider.is_following(username))
                remote_checked = True
            except HTTPException:
                remote_checked = False
        except ProviderError:
            remote_checked = False
    return {
        "isFollowing": bool(remote_match or local_match),
        "localOnly": not getattr(provider.capabilities, "can_sync_following", False),
        "localFollowing": local_match,
        "remoteFollowing": remote_match if remote_checked else None,
    }


@app.post("/api/providers/{source_type}/follow/{username}")
async def provider_follow(source_type: str, username: str, request: Request):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    remote_completed = False
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            remote_completed = bool(payload.get("remoteCompleted"))
    except Exception:
        remote_completed = False
    return await _apply_follow_change(
        provider,
        source_type,
        username,
        follow=True,
        remote_completed=remote_completed,
    )


@app.post("/api/providers/{source_type}/unfollow/{username}")
async def provider_unfollow(source_type: str, username: str, request: Request):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' unavailable")
    provider = _provider_for(source_type)
    remote_completed = False
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            remote_completed = bool(payload.get("remoteCompleted"))
    except Exception:
        remote_completed = False
    return await _apply_follow_change(
        provider,
        source_type,
        username,
        follow=False,
        remote_completed=remote_completed,
    )


@app.post("/api/start")
async def api_start(body: StartBody):
    start_time = time.time()
    logger.section("API /api/start - Start recording")
    logger.debug("Request received",
                target=body.target,
                source_type=body.source_type,
                person=body.person,
                name=body.name,
                auto_start=body.auto_start)

    if not await _recording_enabled():
        logger.warning(
            "Recording refused: global Auto Record is off",
            target=body.target,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Recording is paused globally. Turn on Auto Record in the header "
                "to resume; per-streamer recording settings are unchanged."
            ),
        )

    if _disk_blocks_new_recordings():
        reason = _recording_block_detail()
        logger.warning(
            "Recording refused: storage gate",
            target=body.target,
            detail=reason,
        )
        raise HTTPException(status_code=503, detail=reason)

    target = (body.target or "").strip()
    if not target:
        logger.error("Empty 'target' field in request")
        raise HTTPException(status_code=400, detail="Missing required 'target' field")

    requested_source = _normalize_source_type(body.source_type)
    model_settings = None
    if not target.startswith("http://") and not target.startswith("https://"):
        model_lookup_usernames = []
        if (body.person or "").strip():
            model_lookup_usernames.append((body.person or "").strip())
        if target not in model_lookup_usernames:
            model_lookup_usernames.append(target)
        for model_lookup_username in model_lookup_usernames:
            try:
                model_settings = await db.get_model(model_lookup_username, source_type=requested_source)
                if model_settings:
                    break
            except Exception:
                model_settings = None

    # For auto-start, verify auto_record is enabled in the DB
    if body.auto_start:
        username = body.person or target
        model = model_settings or await db.get_model(username, source_type=requested_source)
        if model:
            auto_record = bool(model.get('auto_record', True))
            if not auto_record:
                logger.warning("Auto-record disabled for this model", username=username)
                raise HTTPException(status_code=403, detail=f"Auto-record disabled for {username}")
        else:
            logger.warning("Model not found in DB, auto-start refused", username=username)
            raise HTTPException(status_code=404, detail=f"Model {username} not found")

    quota_person = (body.person or "").strip()
    if not quota_person and not target.startswith("http://") and not target.startswith("https://"):
        quota_person = target
    if quota_person:
        stored_quota = None
        if model_settings is not None:
            stored_quota = model_settings.get("monthly_quota_gb")
        else:
            try:
                sources = await db.get_media_profile_sources(quota_person)
                if sources:
                    primary = _pick_primary_profile_source(
                        [_profile_source_response(source) for source in sources]
                    )
                    if primary:
                        stored_quota = primary.get("monthlyQuotaGb")
            except Exception:
                stored_quota = None
        await _assert_recording_allowed_for_person(
            quota_person,
            stored_quota_gb=stored_quota,
        )

    logger.info("Parameters validated", target=target, source_type=body.source_type)

    m3u8_url: Optional[str] = None
    stream_headers: Optional[dict[str, str]] = None
    source_url: Optional[str] = None
    ffmpeg_video_stream_index: Optional[int] = None
    person: Optional[str] = (body.person or "").strip() or None
    record_quality = body.record_quality or body.recordQuality
    session_key = body.session_key or body.sessionKey
    if not record_quality and model_settings:
        record_quality = model_settings.get("record_quality")
    max_height = await _get_recording_height_for_quality(record_quality)
    filename_format = await _get_recording_filename_format()

    # Determine source type
    stype = requested_source or await _infer_source_type(person or target, model_settings)
    if target.startswith("http://") or target.startswith("https://"):
        url_source = _source_type_from_url(target)
        if not requested_source and url_source:
            stype = url_source
    logger.debug("Determining source type", source_type=stype or 'auto', target=target)

    direct_media_url = (
        target.startswith("http://")
        or target.startswith("https://")
    ) and (".m3u8" in target.lower() or ".mpd" in target.lower())

    if stype == "m3u8" or direct_media_url:
        logger.info("Direct M3U8 URL detected", url=target[:80])
        m3u8_url = target
        source_url = target
    else:
        effective_source = stype
        available_sources = _available_source_types()
        if effective_source not in available_sources:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown source_type '{effective_source}'. "
                    f"Available sources: {', '.join(sorted(available_sources))}"
                ),
            )
        logger.subsection(f"Resolve via source '{effective_source}'")
        try:
            resolved = await _resolve_stream(
                effective_source,
                target,
                max_height,
                pin_variant=True,
            )
            ffmpeg_video_stream_index = resolved.ffmpeg_video_stream_index
            m3u8_url, stream_headers, source_url = _ffmpeg_stream_input(resolved)
            if not m3u8_url:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unable to find stream for {target}",
                )
            logger.success("M3U8 resolved", username=target, url=m3u8_url[:80])
            if not person:
                person = target
                logger.debug("Person set from target", person=person)
        except HTTPException:
            raise
        except Exception as e:
            error_detail = f"Resolve failed {effective_source}: {_provider_error_detail(effective_source, target, e)}"
            logger.error(error_detail, exc_info=True, username=target)
            raise HTTPException(status_code=400, detail=error_detail)

    # If person still not set (direct m3u8), infer from URL
    if not person:
        try:
            pu = urlparse(m3u8_url)
            # try last non-empty path part without extension
            parts = [p for p in pu.path.split('/') if p]
            base = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else pu.hostname or "session")
            base = base.split('.')[0]
            person = base or (pu.hostname or "session")
        except Exception:
            person = "session"

    person = slugify(person)
    logger.info("Slugified identifier", person=person, display_name=body.name)
    source_url = source_url or m3u8_url
    records_dir_for_person = _model_staging_record_dir(model_settings, person)

    segment_duration_seconds, segment_size_bytes = await _get_recording_segment_limits()
    logger.subsection("Starting FFmpeg session")
    try:
        sess = await asyncio.to_thread(
            manager.start_session,
            m3u8_url,
            person=person,
            display_name=body.name,
            max_height=max_height,
            segment_duration_seconds=segment_duration_seconds,
            segment_size_bytes=segment_size_bytes,
            input_headers=stream_headers,
            source_url=source_url,
            ffmpeg_video_stream_index=ffmpeg_video_stream_index,
            filename_format=filename_format,
            records_dir_for_person=str(records_dir_for_person),
            source_type=stype,
            target=target,
            session_key=session_key,
        )
        duration_ms = (time.time() - start_time) * 1000
        logger.success("Session created successfully",
                      session_id=sess.id,
                      person=person,
                      duration_ms=f"{duration_ms:.2f}")
    except RuntimeError as e:
        logger.error("Session already running", person=person, error=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.critical("Session creation error", exc_info=True, person=person, error=str(e))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

    return {
        "id": sess.id,
        "person": person,
        "name": sess.name,
        "playback_url": sess.playback_url,
        "record_path": sess.record_path_today(),
        "created_at": sess.created_at,
        "running": True,
        "source_type": stype,
        "target": target,
        "session_key": session_key or person,
    }


@app.get("/api/status")
async def api_status():
    return _all_recording_statuses()


@app.get("/api/ops/recording-history")
async def api_ops_recording_history(username: Optional[str] = None, limit: int = 50):
    """Tombstones for purged/deleted recordings (audit trail)."""
    rows = await db.get_recording_history(username, limit=limit)
    return {"history": rows, "count": len(rows)}


@app.get("/api/ops/recording-lifecycle")
async def api_ops_recording_lifecycle(
    days: float = 1.0,
    username: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Grouped recording lifecycle sessions from durable JSONL audit."""
    return await asyncio.to_thread(
        recording_audit.list_lifecycle_sessions,
        days=days,
        username=username,
        limit=limit,
        offset=offset,
    )


@app.post("/api/stop/{session_id}")
async def api_stop(session_id: str):
    ffmpeg_session = manager.get_session(session_id)
    ok = await asyncio.to_thread(manager.stop_session, session_id, "api_stop")
    if ok and ffmpeg_session:
        await _index_ffmpeg_recording(ffmpeg_session)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"stopped": True, "id": session_id}


# ============================================
# FFmpeg process inspection endpoints
# ============================================

def _proc_state_letter(status: str) -> str:
    """psutil status string -> short Linux state letter."""
    mapping = {
        "running": "R",
        "sleeping": "S",
        "disk-sleep": "D",
        "stopped": "T",
        "tracing-stop": "t",
        "zombie": "Z",
        "dead": "X",
        "wake-kill": "K",
        "waking": "W",
        "idle": "I",
        "parked": "P",
    }
    return mapping.get(status, status[:1].upper() if status else "?")


def _safe_dir_size(path: str) -> Tuple[int, int]:
    """Return (segment_count, bytes) for files under `path`. Best effort."""
    try:
        total = 0
        count = 0
        for entry in os.scandir(path):
            if entry.is_file() and entry.name.endswith(".ts"):
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
        return count, total
    except OSError:
        return 0, 0


async def _build_process_snapshot(sess_status: dict) -> dict:
    """Augment a manager.list_status() entry with /proc + psutil + disk stats."""
    import psutil

    out = {
        "session_id": sess_status.get("id"),
        "person": sess_status.get("person"),
        "name": sess_status.get("name"),
        "input_url": sess_status.get("input_url"),
        "record_path": sess_status.get("record_path"),
        "playback_url": sess_status.get("playback_url"),
        "running": bool(sess_status.get("running")),
        "started_at": sess_status.get("created_at"),
        "start_date": sess_status.get("start_date"),
        # filled below
        "pid": None,
        "uptime_seconds": None,
        "cpu_percent": None,
        "rss_bytes": None,
        "vsz_bytes": None,
        "num_threads": None,
        "num_fds": None,
        "status": None,
        "nice": None,
        "io_read_bytes": None,
        "io_write_bytes": None,
        "record_size_bytes": None,
        "segment_count": None,
        "quality": None,
        "bytes_written": sess_status.get("bytes_written"),
        "seconds_since_progress": sess_status.get("seconds_since_progress"),
    }

    # Resolve the underlying FFmpegSession to grab the live process
    sess = manager._sessions.get(sess_status.get("id")) if hasattr(manager, "_sessions") else None
    proc = getattr(sess, "process", None) if sess else None
    pid = proc.pid if proc and proc.poll() is None else None
    out["pid"] = pid

    if pid:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                out["cpu_percent"] = round(p.cpu_percent(interval=0.0), 1)
                mem = p.memory_info()
                out["rss_bytes"] = mem.rss
                out["vsz_bytes"] = mem.vms
                out["num_threads"] = p.num_threads()
                try:
                    out["num_fds"] = p.num_fds()
                except (psutil.AccessDenied, AttributeError):
                    out["num_fds"] = None
                out["status"] = _proc_state_letter(p.status())
                try:
                    out["nice"] = p.nice()
                except psutil.AccessDenied:
                    out["nice"] = None
                try:
                    io = p.io_counters()
                    out["io_read_bytes"] = io.read_bytes
                    out["io_write_bytes"] = io.write_bytes
                except (psutil.AccessDenied, AttributeError):
                    pass
                out["uptime_seconds"] = max(0, int(time.time() - p.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if out["uptime_seconds"] is None and out["started_at"]:
        try:
            out["uptime_seconds"] = max(0, int(time.time() - float(out["started_at"])))
        except (TypeError, ValueError):
            pass

    # Recorded TS file size (output)
    rp = sess_status.get("record_path")
    if rp:
        try:
            out["record_size_bytes"] = os.path.getsize(rp)
        except OSError:
            out["record_size_bytes"] = None

    # HLS segment dir stats
    if sess and getattr(sess, "sessions_dir", None):
        seg_count, seg_size = _safe_dir_size(sess.sessions_dir)
        out["segment_count"] = seg_count
        out["segment_bytes"] = seg_size

    # Effective quality from the model's current setting (plus global cap)
    person = sess_status.get("person")
    if person:
        try:
            model = await db.get_model(person)
            if model:
                rq = model.get("record_quality") or "best"
                eff = await _get_recording_height_for_quality(rq)
                out["quality"] = f"{eff}p" if eff else "best"
                out["record_quality"] = rq
        except Exception:
            pass

    return out


@app.get("/api/processes")
async def api_processes():
    """List ffmpeg recording processes with detailed metrics."""
    import psutil
    statuses = manager.list_status()
    procs = []
    total_cpu = 0.0
    total_rss = 0
    for s in statuses:
        snap = await _build_process_snapshot(s)
        procs.append(snap)
        if snap.get("cpu_percent") is not None:
            total_cpu += snap["cpu_percent"]
        if snap.get("rss_bytes") is not None:
            total_rss += snap["rss_bytes"]
    active = sum(1 for p in procs if p.get("running"))
    try:
        cores = psutil.cpu_count(logical=True) or 1
    except Exception:
        cores = 1
    return {
        "processes": procs,
        "totals": {
            "active": active,
            "total": len(procs),
            "cpu_percent_sum": round(total_cpu, 1),
            "rss_bytes_sum": total_rss,
            "host_cores": cores,
        },
    }


@app.get("/api/processes/{session_id}/log")
async def api_process_log(session_id: str, lines: int = 30):
    """Return the last `lines` lines of the session's ffmpeg.log."""
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    log_path = getattr(sess, "log_path", None)
    if not log_path or not os.path.exists(log_path):
        return {"session_id": session_id, "lines": [], "path": log_path}
    lines = max(1, min(int(lines or 30), 500))
    try:
        # Tail without loading the whole file
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
        tail = text.splitlines()[-lines:]
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Log read error: {e}")
    return {"session_id": session_id, "lines": tail, "path": log_path}


@app.post("/api/processes/{session_id}/stop")
async def api_process_stop(session_id: str):
    """Graceful stop (SIGTERM, then SIGKILL on timeout). Same as /api/stop."""
    ok = await asyncio.to_thread(manager.stop_session, session_id, "api_stop")
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"stopped": True, "session_id": session_id}


@app.post("/api/processes/{session_id}/kill")
async def api_process_kill(session_id: str):
    """Force SIGKILL on the ffmpeg process. Use when graceful stop hangs."""
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    proc = getattr(sess, "process", None)
    if not proc or proc.poll() is not None:
        # Already gone — fall back to graceful stop to clean up bookkeeping
        await asyncio.to_thread(manager.stop_session, session_id, "api_kill")
        return {"killed": False, "reason": "already-exited", "session_id": session_id}
    try:
        proc.kill()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kill error: {e}")
    # Run the manager's stop to release locks / threads
    await asyncio.to_thread(manager.stop_session, session_id, "api_kill")
    return {"killed": True, "session_id": session_id}


@app.post("/api/processes/{session_id}/restart")
async def api_process_restart(session_id: str):
    """Stop the session — the auto-monitor will re-spawn it on its next tick.

    Restart is implemented as a clean stop on purpose: Chaturbate URLs are
    token-bearing and may have expired, so respawning with the cached URL
    can fail. The monitor task re-resolves a fresh URL when it picks the
    model back up (typically within a few seconds).
    """
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    person = sess.person
    ok = await asyncio.to_thread(manager.stop_session, session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Stop failed")
    return {
        "restarted": True,
        "session_id": session_id,
        "person": person,
        "note": "monitor will re-spawn within a few seconds",
    }


@app.get("/api/model/{username}/status")
async def get_model_status(username: str, source: Optional[str] = None):
    """Fetch a model's status via the provider registry."""
    source_type = _normalize_source_type(source)
    model = await db.get_model(username, source_type=source_type)
    if not model or not model.get('source_type'):
        followed = await db.get_followed_model(username, source_type=source_type)
        if followed and followed.get('source_type'):
            if not model:
                model = followed
            else:
                model = {**model, 'source_type': followed['source_type']}

    if not source_type:
        source_type = await _infer_source_type(username, model)

    media_profile = await db.get_media_profile(username)
    live_card_meta = await _media_profile_live_card_meta(source_type, username)
    live_viewers = live_card_meta.get("viewers")
    followers = live_card_meta.get("followers")
    if followers is None and model:
        followers = model.get("followers", model.get("num_followers"))
    if source_type == "stripchat":
        followers = None
    started_at = (
        live_card_meta.get("startedAt")
        or (model or {}).get("started_at")
        or (model or {}).get("startedAt")
        or ""
    )
    started_at = str(started_at or "").strip() or None
    profile_image = _pick_media_card_avatar(
        media_profile=media_profile,
        live_image=live_card_meta.get("profileImageUrl"),
        model_image=(model or {}).get("profile_image_url"),
    )
    if not profile_image and source_type not in {"stripchat", "chaturbate"}:
        thumb = _rewrite_chaturbate_poster_url((model or {}).get("thumbnail") or "")
        if thumb and not _is_live_cover_profile_image(thumb):
            profile_image = thumb
    watch_meta = {
        "followers": int(followers) if followers is not None else None,
        "channelUrl": (
            live_card_meta.get("channelUrl")
            or _canonical_stream_url(source_type, username)
        ),
        "profileImageUrl": profile_image or "",
        "displayName": (
            live_card_meta.get("displayName")
            or (model or {}).get("display_name")
            or username
        ),
        "roomId": live_card_meta.get("roomId") or (model or {}).get("room_id"),
        "userId": live_card_meta.get("userId") or (model or {}).get("user_id"),
        "startedAt": started_at,
    }

    if source_type not in _available_source_types():
        return {
            "username": username,
            "isOnline": False,
            "thumbnail": f"/api/thumbnail/{username}",
            "viewers": int(live_viewers or 0),
            "tags": [],
            "roomStatus": "unsupported",
            "sourceType": source_type,
            **watch_meta,
        }

    if model and model.get('is_online'):
        # Twitch uptime comes from Helix started_at; refresh when card meta missed it.
        if source_type == "twitch" and not watch_meta.get("startedAt"):
            try:
                status = await _provider_status(source_type, username)
                if getattr(status, "started_at", None):
                    watch_meta["startedAt"] = str(status.started_at or "").strip() or None
                if live_viewers is None and status.viewers:
                    live_viewers = int(status.viewers or 0)
            except Exception:
                pass
        # Stripchat cam/profile usually omits viewersCount. If live meta only has
        # zero, backfill from catalogue without resolving HLS. Skip private/p2p —
        # those rooms are absent from the public catalogue.
        room_status = (
            live_card_meta.get("roomStatus")
            or model.get("room_status")
            or model.get("roomStatus")
        )
        if source_type == "stripchat" and int(live_viewers or 0) <= 0:
            from .providers.stripchat import _stripchat_is_private_status
            if not _stripchat_is_private_status(room_status):
                try:
                    provider = _provider_for(source_type)
                    catalog_viewers = await provider._stripchat_catalog_viewers(username)
                    if catalog_viewers:
                        live_viewers = int(catalog_viewers)
                except Exception:
                    pass
        # Chaturbate: live-card meta can pin viewers=0 from chatvideocontext;
        # check_status roomlist-backfills the real cams count.
        if source_type == "chaturbate" and int(live_viewers or 0) <= 0:
            try:
                status = await _provider_status(source_type, username)
                if int(getattr(status, "viewers", 0) or 0) > 0:
                    live_viewers = int(status.viewers or 0)
                if not room_status and getattr(status, "room_status", None):
                    room_status = status.room_status
            except Exception:
                pass
        return {
            "username": username,
            "isOnline": True,
            "thumbnail": f"/api/thumbnail/{username}",
            "viewers": int(live_viewers) if live_viewers is not None else int(model.get('viewers', 0) or 0),
            "tags": list(model.get('tags') or []),
            "roomStatus": room_status,
            "sourceType": source_type,
            **watch_meta,
        }

    try:
        status = await _provider_status(source_type, username)
        # Stream thumbnails (CB riw / SC snapshot / Twitch previews-ttv) are
        # live covers — never promote them into the circular face avatar.
        if (
            not watch_meta.get("profileImageUrl")
            and status.thumbnail
            and not _is_live_cover_profile_image(status.thumbnail)
        ):
            watch_meta["profileImageUrl"] = status.thumbnail
        if not watch_meta.get("startedAt") and getattr(status, "started_at", None):
            watch_meta["startedAt"] = str(status.started_at or "").strip() or None
        status_viewers = int(status.viewers or 0)
        if live_viewers is not None and int(live_viewers or 0) > 0:
            viewers_out = int(live_viewers)
        else:
            # Do not let a zero live-card meta hide check_status roomlist backfill.
            viewers_out = status_viewers if status_viewers > 0 else int(live_viewers or 0)
        return {
            "username": username,
            "isOnline": bool(status.is_online),
            "thumbnail": status.thumbnail or f"/api/thumbnail/{username}",
            "viewers": viewers_out,
            "tags": list(status.tags or []),
            "roomStatus": status.room_status,
            "sourceType": source_type,
            **watch_meta,
        }
    except Exception as e:
        logger.debug(
            "Provider check_status failed",
            username=username,
            source_type=source_type,
            error=str(e),
        )

    return {
        "username": username,
        "isOnline": model.get('is_online', False) if model else False,
        "thumbnail": f"/api/thumbnail/{username}",
        "viewers": int(live_viewers) if live_viewers is not None else int(model.get('viewers', 0) or 0) if model else 0,
        "tags": list(model.get('tags') or []) if model else [],
        "roomStatus": model.get('room_status') if model else None,
        "sourceType": source_type,
        **watch_meta,
    }


@app.get("/api/model/{username}/stream")
async def get_model_stream(
    username: str,
    source: Optional[str] = None,
    quality: Optional[int] = None,
):
    """Fetch the live stream URL for a model.

    source_type is resolved by priority: query param `source`
    (from Discover), then SQLite cache, then default Chaturbate.
    """
    try:
        model = None
        try:
            requested_source = _normalize_source_type(source)
            model = await db.get_model(username, source_type=requested_source)
        except Exception:
            pass
        source_type = _normalize_source_type(source)
        if not source_type:
            source_type = await _infer_source_type(username, model)

        if source_type not in _available_source_types():
            raise HTTPException(
                status_code=404,
                detail=f"Source '{source_type}' unavailable",
            )
        if quality is not None and quality not in {360, 480, 720, 1080}:
            raise HTTPException(
                status_code=400,
                detail="Quality must be one of 360, 480, 720, or 1080",
            )

        try:
            # Watching quality is independent from the recording-quality cap.
            # When the browser asks for a fixed height, providers that expose
            # only one rendition resolve the best stream at or below it.
            max_height = quality
            resolved = await _resolve_stream(source_type, username, max_height)
        except Exception as e:
            raise HTTPException(status_code=404, detail=_provider_error_detail(source_type, username, e))

        if not resolved.url:
            raise HTTPException(status_code=404, detail=f"Unable to find stream for {username}")

        return {
            "username": username,
            **_watch_stream_payload(resolved),
            "isOnline": True,
            "sourceType": source_type,
            "selectedQuality": quality,
            "qualityOptions": [1080, 720, 480],
            "roomStatus": resolved.room_status,
            "viewers": int(resolved.viewers or 0),
            "tags": list(resolved.tags or []),
            "thumbnail": resolved.thumbnail,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Stream fetch error", username=username, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/thumbnail/{username}")
async def get_thumbnail(username: str):
    """Serve the thumbnail from cache (generated by the monitor task)"""
    from fastapi.responses import FileResponse, Response

    # Get the thumbnail path from SQLite
    model = await db.get_model(username)

    if model and model.get('thumbnail_path'):
        thumb_path = Path(model['thumbnail_path'])

        if thumb_path.exists():
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60"}
            )

    # Search folders manually if not in cache
    # Preference order: live > chaturbate > offline
    for subdir in ["live", "chaturbate", "offline"]:
        thumb_path = OUTPUT_DIR / "thumbnails" / subdir / f"{username}.jpg"
        if thumb_path.exists():
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60"}
            )

    # SVG placeholder when no thumbnail is found
    svg_placeholder = f'''<svg xmlns="http://www.w3.org/2000/svg" width="280" height="200">
        <defs>
            <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#6366f1;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#a855f7;stop-opacity:1" />
            </linearGradient>
        </defs>
        <rect fill="url(#grad)" width="280" height="200"/>
        <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="white" font-family="system-ui" font-size="18" font-weight="600">{username}</text>
        <text x="50%" y="70%" dominant-baseline="middle" text-anchor="middle" fill="white" font-family="system-ui" font-size="12" opacity="0.8">📷 Loading...</text>
    </svg>'''

    return Response(
        content=svg_placeholder,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=10"}
    )


def _format_duration_label(duration_seconds: int) -> str:
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s"


def _format_size_display(file_size: int) -> str:
    if file_size >= 1000 * 1024 * 1024:
        return f"{file_size / 1024 / 1024 / 1024:.2f} GB"
    return f"{file_size / 1024 / 1024:.0f} MB"


def _format_import_recording(rec: dict, username: str) -> Optional[dict]:

    source_path = Path(rec.get("file_path") or "")
    if not source_path.exists():
        return None

    recording_id = rec.get("recording_id") or source_path.stem
    playable_raw = rec.get("playable_path")
    playable_path = Path(playable_raw) if playable_raw else None
    playable = bool(playable_path and playable_path.exists())
    file_size = (
        rec.get("playable_size")
        if playable and rec.get("playable_size")
        else rec.get("file_size") or source_path.stat().st_size
    )
    created_at = rec.get("created_at") or int(source_path.stat().st_mtime)
    duration_seconds = int(rec.get("duration_seconds") or 0)
    thumb_path = Path(rec.get("thumbnail_path") or "")
    thumb_url = (
        f"/api/recording-thumbnail/{username}/{thumb_path.name}"
        if thumb_path.exists()
        else None
    )

    return {
        "recordingId": recording_id,
        "filename": source_path.name,
        "title": rec.get("title") or source_path.stem,
        "date": source_path.stem,
        "size": file_size,
        "size_formatted": format_bytes(file_size),
        "size_mb": round(file_size / 1024 / 1024, 2),
        "size_display": _format_size_display(file_size),
        "modified": datetime.fromtimestamp(source_path.stat().st_mtime).isoformat(),
        "url": f"/streams/media/{recording_id}" if playable else None,
        "downloadUrl": f"/streams/media/{recording_id}?download=1",
        "thumbnail": thumb_url,
        "duration": duration_seconds,
        "duration_str": _format_duration_label(duration_seconds),
        "isConverted": playable,
        "isImported": True,
        "mediaKind": "import",
        "importStatus": rec.get("import_status") or ("ready" if playable else "failed"),
        "importError": rec.get("import_error"),
        "playable": playable,
        "createdAt": created_at,
        "mp4": None,
    }


@app.get("/api/recordings/{username}")
async def list_recordings(username: str, show_ts: bool = False):
    """List recordings (converted MP4 or raw TS)"""
    from datetime import datetime

    # Fetch from SQLite
    recordings_db = await db.get_recordings(username)

    recordings = []
    thumbnails_dir = OUTPUT_DIR / "thumbnails" / username

    for rec in recordings_db:
        if rec.get("media_kind") == "import":
            formatted_import = _format_import_recording(rec, username)
            if formatted_import:
                recordings.append(formatted_import)
            continue

        # Determine the playable file: prefer MP4, fall back to TS
        is_converted = bool(rec.get('is_converted'))
        mp4_raw = rec.get('mp4_path')
        ts_raw = rec.get('file_path')

        if is_converted and mp4_raw and Path(mp4_raw).exists():
            serve_path = Path(mp4_raw)
            file_size = rec.get('mp4_size') or serve_path.stat().st_size
        elif ts_raw and Path(ts_raw).exists():
            serve_path = Path(ts_raw)
            # Only raw transport streams are opt-in. Browser captures are
            # written as WebM/MP4 and remain directly playable by default.
            if serve_path.suffix.lower() == ".ts" and not show_ts:
                continue
            file_size = rec.get('file_size') or serve_path.stat().st_size
        else:
            continue

        stat = serve_path.stat()

        # Thumbnail
        thumb_path = thumbnails_dir / f"{serve_path.stem}.jpg"
        thumb_url = f"/api/recording-thumbnail/{username}/{serve_path.stem}.jpg"

        # Format the duration
        duration_seconds = rec.get('duration_seconds', 0)
        if (
            (duration_seconds and duration_seconds < MIN_RECORDING_SECONDS)
            or (not duration_seconds and file_size < MIN_RECORDING_BYTES)
        ):
            continue

        duration_str = _format_duration_label(duration_seconds)
        size_display = _format_size_display(file_size)

        # Use created_at from DB, fallback to file mtime
        created_at = rec.get('created_at')
        if not created_at:
            created_at = int(stat.st_mtime)

        recording_id = rec.get('recording_id', serve_path.stem)
        recording_url = f"/streams/recordings/{quote(recording_id, safe='')}"

        recordings.append({
            "recordingId": recording_id,
            "filename": serve_path.name,
            "date": serve_path.stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "size_mb": round(file_size / 1024 / 1024, 2),
            "size_display": size_display,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "url": recording_url,
            "thumbnail": thumb_url if thumb_path.exists() else None,
            "duration": duration_seconds,
            "duration_str": duration_str,
            "isConverted": is_converted,
            "isImported": False,
            "mediaKind": "recording",
            "importStatus": None,
            "importError": None,
            "playable": True,
            "downloadUrl": recording_url,
            "conversionAttempts": rec.get('conversion_attempts') or 0,
            "conversionError": rec.get('conversion_error'),
            "createdAt": created_at,
            "mp4": {
                "filename": Path(mp4_raw).name,
                "size": rec.get('mp4_size', 0),
                "size_formatted": format_bytes(rec.get('mp4_size', 0)),
                "url": recording_url
            } if is_converted and mp4_raw else None
        })

    return {"recordings": recordings}


@app.post("/api/recordings/{recording_id}/retry-conversion")
async def retry_conversion(recording_id: str):
    """Reset the failure counter to force another conversion attempt."""
    rec = await db.get_recording_by_id(recording_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    if rec.get('is_converted'):
        return {"success": True, "message": "Already converted", "alreadyConverted": True}
    ts_path = Path(rec.get('file_path', ''))
    if not ts_path.exists():
        raise HTTPException(status_code=404, detail="Source TS file not found")
    reset = await db.reset_conversion_failure(recording_id)
    return {
        "success": reset,
        "message": "Conversion scheduled on next scan (~30s)",
        "recordingId": recording_id
    }


def _get_media_import_manager() -> MediaImportManager:
    global media_import_manager
    if media_import_manager is None:
        media_import_manager = MediaImportManager(db, OUTPUT_DIR, FFMPEG_PATH)
    return media_import_manager


@app.get("/api/media-imports/status")
async def get_media_imports_status():
    """Return local media import feature status."""
    if not MEDIA_IMPORTS_ENABLED:
        return {
            "enabled": False,
            "running": False,
            "lastScanAt": None,
            "lastResult": None,
        }

    manager_ref = _get_media_import_manager()
    return {
        "enabled": True,
        "running": manager_ref.running,
        "lastScanAt": manager_ref.last_scan_at,
        "lastResult": manager_ref.last_result,
    }


@app.post("/api/media-imports/rescan")
async def rescan_media_imports():
    """Trigger an immediate local media import scan."""
    if not MEDIA_IMPORTS_ENABLED:
        return {
            "success": False,
            "enabled": False,
            "message": "Local media imports are disabled",
        }

    result = await _get_media_import_manager().scan()
    return {
        "enabled": True,
        **result,
    }


async def _media_profile_live_card_meta(
    source_type: str,
    username: str,
    force_refresh: bool = False,
) -> dict[str, Any]:
    cache_key = f"{source_type}:{username}".lower()
    cached = _media_profile_live_cache.get(cache_key)
    if not force_refresh and cached and time.monotonic() < cached[0]:
        return cached[1]
    result: dict[str, Any] = {}
    try:
        provider = _provider_for(source_type)
        # Exact identity APIs — never Discover keyword search.
        # CB roomlist returns live `riw` frames; Twitch search is partition-
        # scoped and page-sliced (short logins like tiffy often miss page 1).
        if source_type in {"bilibili", "chaturbate", "stripchat", "youtube", "twitch"} and hasattr(
            provider, "resolve_watch_meta"
        ):
            result = await asyncio.wait_for(
                provider.resolve_watch_meta(username),
                timeout=20,
            ) or {}
            if result and source_type == "stripchat":
                from .providers.stripchat import (
                    _stripchat_is_offline_status,
                    _stripchat_is_private_status,
                )
                room_status = str(result.get("roomStatus") or "").strip()
                is_online = bool(result.get("isOnline"))
                viewers = int(result.get("viewers") or 0)
                if _stripchat_is_private_status(room_status):
                    # Private/p2p/group must never collapse to Offline or Live · 0.
                    is_online = True
                    viewers = 0
                elif _stripchat_is_offline_status(room_status):
                    is_online = False
                    viewers = 0
                    room_status = "offline"
                result["isOnline"] = is_online
                result["viewers"] = viewers if is_online else 0
                result["roomStatus"] = room_status or result.get("roomStatus")
                result["profileImageUrl"] = _media_live_profile_image_url(
                    {
                        "profile_image_url": result.get("profileImageUrl"),
                        "thumbnail": result.get("thumbnail"),
                    },
                    source_type=source_type,
                    is_online=is_online,
                )
                # Catalogue miss / race: do not pin viewers=0 for two minutes.
                if is_online and viewers <= 0:
                    ttl = 60 if _stripchat_is_private_status(room_status) else 15
                    _media_profile_live_cache[cache_key] = (time.monotonic() + ttl, result)
                    return result
            elif result and source_type == "twitch":
                # Never let Helix stream previews into the circular avatar.
                is_online = bool(result.get("isOnline"))
                result["profileImageUrl"] = _media_live_profile_image_url(
                    {
                        "profile_image_url": result.get("profileImageUrl"),
                        "thumbnail": result.get("thumbnail"),
                    },
                    source_type=source_type,
                    is_online=is_online,
                )
        else:
            payload = await asyncio.wait_for(
                provider.list_live_models(page=1, limit=12, search=username),
                timeout=15,
            )
            needle = username.strip().lower()
            item = next(
                (
                    entry
                    for entry in (payload.get("models") or [])
                    if str(entry.get("username") or "").strip().lower() == needle
                ),
                None,
            )
            if item:
                followers = item.get("followers")
                if followers is None:
                    followers = item.get("num_followers")
                started_at = str(item.get("started_at") or item.get("startedAt") or "").strip()
                viewers = int(item.get("viewers") or 0)
                is_online = bool(item.get("is_online", True))
                room_status = str(item.get("room_status") or item.get("roomStatus") or "").strip()
                if source_type == "stripchat":
                    from .providers.stripchat import _stripchat_is_offline_status
                    if _stripchat_is_offline_status(room_status):
                        is_online = False
                        viewers = 0
                        room_status = "offline"
                profile_image = _media_live_profile_image_url(
                    item,
                    source_type=source_type,
                    is_online=is_online,
                )
                result = {
                    "isOnline": is_online,
                    "viewers": viewers,
                    "followers": int(followers) if followers is not None else None,
                    "channelUrl": item.get("channel_url") or item.get("channelUrl"),
                    "profileImageUrl": profile_image,
                    "displayName": item.get("display_name") or item.get("displayName"),
                    "username": item.get("username"),
                    "startedAt": started_at or None,
                    "roomStatus": room_status or None,
                }
                # Stripchat cam search can briefly resolve online with viewers=0
                # before catalogue backfill; avoid caching that miss for 2 minutes.
                # Private rooms never appear in the public catalogue — cache longer.
                if source_type == "stripchat" and is_online and viewers <= 0:
                    from .providers.stripchat import _stripchat_is_private_status
                    ttl = 60 if _stripchat_is_private_status(room_status) else 15
                    _media_profile_live_cache[cache_key] = (time.monotonic() + ttl, result)
                    return result
    except Exception:
        result = {}
    # Empty misses are often FlareSolverr/timeout blips — retry soon instead of
    # caching a blank avatar for two minutes.
    ttl = 15 if not result else 120
    _media_profile_live_cache[cache_key] = (time.monotonic() + ttl, result)
    return result


@app.get("/api/media-timeline")
async def get_media_timeline(
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    usernames: str = "",
    days: int = 7,
):
    """Live vs recording coverage for Media timeline chart.

    ``usernames`` is a comma-separated list of Media card usernames. Empty means
    all cards that have at least one live session or recording in range.
    """
    now = int(time.time())
    try:
        day_span = max(1, min(int(days or 7), 90))
    except (TypeError, ValueError):
        day_span = 7
    end = int(to_ts) if to_ts and int(to_ts) > 0 else now
    start = int(from_ts) if from_ts and int(from_ts) > 0 else (end - day_span * 86400)
    if end < start:
        start, end = end, start
    if end - start > 90 * 86400:
        start = end - 90 * 86400

    requested = [
        part.strip()
        for part in str(usernames or "").split(",")
        if part and part.strip()
    ]

    live_rows = await db.get_live_sessions_in_range(
        from_ts=start,
        to_ts=end,
        profile_usernames=requested or None,
    )
    recording_rows = await db.get_recording_intervals_in_range(
        from_ts=start,
        to_ts=end,
        usernames=requested or None,
    )

    mac_ids: set[str] = set()
    try:
        snap = dict(_mac_helper_snapshot or {})
        for key in ("matched", "macOnly", "mac_only", "items"):
            for item in snap.get(key) or []:
                if key == "items" and not (item.get("onMac") or item.get("on_mac")):
                    continue
                rid = str(item.get("recordingId") or "").strip()
                if rid:
                    mac_ids.add(rid)
    except Exception:
        mac_ids = set()

    for row in recording_rows:
        rid = str(row.get("recordingId") or "").strip()
        locations = {str(row.get("location") or "vps")}
        if rid and rid in mac_ids:
            locations.add("mac")
        row["locations"] = sorted(locations)

    name_set: set[str] = set(requested)
    for row in live_rows:
        name = str(row.get("profile_username") or row.get("channel_username") or "").strip()
        if name:
            name_set.add(name)
    for row in recording_rows:
        name = str(row.get("username") or "").strip()
        if name:
            name_set.add(name)

    profiles_by_name: dict[str, dict] = {}
    try:
        for profile in await db.get_all_media_profiles():
            key = str(profile.get("username") or "").strip()
            if key:
                profiles_by_name[key.lower()] = profile
    except Exception:
        profiles_by_name = {}

    palette = [
        "#22d3ee", "#f472b6", "#a3e635", "#fb923c", "#38bdf8",
        "#e879f9", "#facc15", "#4ade80", "#f87171", "#c084fc",
        "#2dd4bf", "#fda4af",
    ]
    streamers = []
    for index, name in enumerate(sorted(name_set, key=lambda value: value.lower())):
        profile = profiles_by_name.get(name.lower()) or {}
        display = str(profile.get("display_name") or profile.get("displayName") or name)
        streamers.append({
            "username": name,
            "displayName": display,
            "color": palette[index % len(palette)],
            "colorIndex": index % len(palette),
        })

    live_out = []
    for row in live_rows:
        live_end = row.get("ended_at")
        if live_end is None:
            live_end = now
        live_out.append({
            "username": row.get("profile_username") or row.get("channel_username"),
            "channelUsername": row.get("channel_username"),
            "sourceType": row.get("source_type") or "chaturbate",
            "start": int(row.get("started_at") or 0),
            "end": int(live_end or now),
            "open": row.get("ended_at") is None,
            "detection": row.get("detection") or "monitor",
        })

    return {
        "from": start,
        "to": end,
        "generatedAt": now,
        "streamers": streamers,
        "live": live_out,
        "recordings": recording_rows,
        "note": (
            "Live bars come from monitor polls (default every 2 minutes; "
            "Settings → check interval). Short offline gaps under 5 minutes "
            "stay one session. Recording bars are known capture coverage: "
            "current VPS files, history tombstones after delete, and Mac "
            "presence from the latest helper scan — not a live disk walk."
        ),
    }


@app.get("/api/recording-projects")
async def list_recording_projects_api(
    username: Optional[str] = None,
    usernames: str = "",
):
    """List recording projects for one or more streamers (Media project view)."""
    from app.services import recording_projects as recording_projects_svc

    await recording_projects_svc.finalize_ready_projects(db)
    try:
        library_items = await _scan_media_library_items(refresh_metadata=False)
        await recording_projects_svc.backfill_library_fragments(db, library_items)
    except Exception as exc:
        logger.warning("Recording project backfill failed", error=str(exc))

    names: list[str] = []
    if username:
        names.append(str(username).strip())
    for part in str(usernames or "").split(","):
        part = part.strip()
        if part and part not in names:
            names.append(part)
    projects = await db.list_recording_projects(usernames=names or None)
    with _mac_state_lock:
        statuses = dict((_mac_helper_snapshot.get("statuses") or {}))
    out = []
    for project in projects:
        members = await db.list_project_members(project["project_id"])
        enriched = []
        for member in members:
            row = dict(member)
            item_id = str(row.get("item_id") or "")
            sync = statuses.get(item_id) or ""
            stored_loc = str(row.get("location") or "vps").strip().lower()
            row["syncStatus"] = sync or ("synced" if stored_loc == "mac" else "")
            if sync == "synced" or stored_loc == "mac":
                row["location"] = "mac"
            elif item_id:
                row["location"] = "vps"
            else:
                row["location"] = stored_loc or "vps"
            row["partLabel"] = row.get("part_label")
            row["itemId"] = item_id
            row["recordingId"] = row.get("recording_id")
            row["startedAt"] = row.get("started_at")
            row["endedAt"] = row.get("ended_at")
            row["durationSeconds"] = row.get("duration_seconds")
            row["sizeBytes"] = row.get("size_bytes")
            enriched.append(row)
        display = recording_projects_svc.project_display_dict(
            project,
            enriched,
            sync_statuses=statuses,
        )
        if display.get("localComplete") and str(project.get("status")) not in {
            "concatenated",
            "concat_failed",
        }:
            if str(project.get("status")) != "local_complete":
                await db.update_recording_project(
                    project["project_id"],
                    status="local_complete",
                    updated_at=int(time.time()),
                )
                display["status"] = "local_complete"
        out.append(display)
    return {"projects": out, "count": len(out)}


@app.post("/api/recording-projects/finalize")
async def finalize_recording_projects_api():
    from app.services import recording_projects as recording_projects_svc

    ready = await recording_projects_svc.finalize_ready_projects(db)
    return {"ready": ready, "count": len(ready)}


@app.post("/api/recording-projects/concat")
async def queue_recording_project_concat(body: dict):
    """Queue concat for local-complete projects (helper picks up concat_status=queued)."""
    from app.services import recording_projects as recording_projects_svc
    from app.services.concat_queue import concat_queue

    raw_ids = body.get("projectIds")
    if raw_ids is None:
        raw_ids = body.get("project_ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="projectIds must be a list")

    with _mac_state_lock:
        statuses = dict((_mac_helper_snapshot.get("statuses") or {}))

    queued: list[str] = []
    skipped: list[dict] = []
    enqueue_rows: list[dict] = []
    now_ts = int(time.time())
    for raw in raw_ids:
        project_id = str(raw or "").strip()
        if not project_id:
            continue
        project = await db.get_recording_project(project_id)
        if not project:
            skipped.append({"projectId": project_id, "reason": "not_found"})
            continue
        members = await db.list_project_members(project_id)
        display = recording_projects_svc.project_display_dict(
            project,
            members,
            sync_statuses=statuses,
        )
        if not display.get("localComplete"):
            skipped.append({"projectId": project_id, "reason": "not_local_complete"})
            continue
        username = str(project.get("username") or "")
        source_type = await _primary_source_type_for_profile(username)
        display_name = await _mac_download_display_name(username)
        folder_label = _mac_download_folder_label(username, display_name)
        from app.recording_names import format_identity_with_source, normalize_source_marker

        source = normalize_source_marker(source_type) or "chaturbate"
        safe_folder = format_identity_with_source(
            _mac_download_safe_segment(folder_label or username, "unknown"),
            source,
        )
        project_dir = recording_projects_svc.mac_project_relative_dir(safe_folder, project_id)
        member_paths = []
        used_paths: set = set()
        ordered_members = sorted(
            members,
            key=lambda row: (
                int(row.get("started_at") or 0),
                int(row.get("sort_index") or 0),
                str(row.get("filename") or ""),
            ),
        )
        for member in ordered_members:
            fname = str(member.get("filename") or "")
            rel = _mac_download_relative_path(
                folder_label or username,
                fname,
                int(member.get("started_at") or 0),
                used_paths,
                source_type=source_type,
                project_id=project_id,
            )
            member_paths.append(rel)
        final_path = recording_projects_svc.mac_final_relative_path(
            safe_folder,
            int(project.get("started_at") or 0),
            int(project.get("ended_at") or project.get("started_at") or 0),
        )
        await db.update_recording_project(
            project_id,
            concat_status="queued",
            updated_at=now_ts,
        )
        enqueue_rows.append({
            "projectId": project_id,
            "username": username,
            "title": display.get("title") or "",
            "memberCount": len(members),
            "memberPaths": member_paths,
            "finalRelativePath": final_path,
            "projectDir": project_dir,
            "startedAt": int(project.get("started_at") or 0),
            "endedAt": int(project.get("ended_at") or 0),
        })
        queued.append(project_id)
    if enqueue_rows:
        concat_queue.enqueue(enqueue_rows)
    return {"queued": queued, "skipped": skipped, "count": len(queued)}


@app.get("/api/tasks/download")
async def get_download_tasks():
    from app.services.download_queue import download_queue

    project_n, file_n = await _download_concurrency_settings()
    data = download_queue.list_grouped()
    data["projectConcurrency"] = project_n
    data["fileConcurrency"] = file_n
    return data


@app.post("/api/tasks/download/reorder")
async def reorder_download_tasks(body: TaskQueueMutateBody):
    from app.services.download_queue import download_queue

    return download_queue.reorder(task_ids=body.taskIds, project_ids=body.projectIds)


@app.post("/api/tasks/download/pause")
async def pause_download_tasks(body: TaskQueueMutateBody):
    from app.services.download_queue import download_queue

    return download_queue.pause(task_ids=body.taskIds, project_ids=body.projectIds)


@app.post("/api/tasks/download/resume")
async def resume_download_tasks(body: TaskQueueMutateBody):
    from app.services.download_queue import download_queue

    return download_queue.resume(task_ids=body.taskIds, project_ids=body.projectIds)


@app.post("/api/tasks/download/delete")
async def delete_download_tasks(body: TaskQueueMutateBody):
    from app.services.download_queue import download_queue

    return download_queue.delete(task_ids=body.taskIds, project_ids=body.projectIds)


@app.get("/api/tasks/concat")
async def get_concat_tasks():
    from app.services.concat_queue import concat_queue

    return concat_queue.list_tasks()


@app.post("/api/tasks/concat/reorder")
async def reorder_concat_tasks(body: TaskQueueMutateBody):
    from app.services.concat_queue import concat_queue

    return concat_queue.reorder(task_ids=body.taskIds, project_ids=body.projectIds)


@app.post("/api/tasks/concat/pause")
async def pause_concat_tasks(body: TaskQueueMutateBody):
    from app.services.concat_queue import concat_queue

    result = concat_queue.pause(task_ids=body.taskIds, project_ids=body.projectIds)
    for task in (result.get("queue") or {}).get("tasks") or []:
        if str(task.get("status")) == "paused":
            try:
                await db.update_recording_project(
                    str(task.get("projectId")),
                    concat_status="queued",
                    updated_at=int(time.time()),
                )
            except Exception:
                pass
    return result


@app.post("/api/tasks/concat/resume")
async def resume_concat_tasks(body: TaskQueueMutateBody):
    from app.services.concat_queue import concat_queue

    result = concat_queue.resume(task_ids=body.taskIds, project_ids=body.projectIds)
    for pid in body.projectIds or []:
        try:
            await db.update_recording_project(
                str(pid),
                concat_status="queued",
                updated_at=int(time.time()),
            )
        except Exception:
            pass
    return result


@app.post("/api/tasks/concat/delete")
async def delete_concat_tasks(body: TaskQueueMutateBody):
    from app.services.concat_queue import concat_queue

    targets = body.projectIds or []
    result = concat_queue.delete(task_ids=body.taskIds, project_ids=body.projectIds)
    for pid in targets:
        try:
            await db.update_recording_project(
                str(pid),
                concat_status="none",
                updated_at=int(time.time()),
            )
        except Exception:
            pass
    return result


@app.post("/api/mac/helper/concat-status")
async def mac_helper_concat_status(body: ConcatStatusBody):
    """Helper reports concat-project outcome."""
    from app.services.concat_queue import concat_queue

    _require_live_mac_helper(body.localSessionId)
    result = concat_queue.report(
        project_id=str(body.projectId or ""),
        task_id=str(body.taskId or ""),
        status=str(body.status or ""),
        error=str(body.error or ""),
        step=body.step,
    )
    project_id = str((result or {}).get("projectId") or body.projectId or "")
    status = str((result or {}).get("status") or body.status or "").lower()
    if project_id:
        fields: dict[str, Any] = {"updated_at": int(time.time())}
        if status in {"done", "success", "concatenated"}:
            fields["concat_status"] = "concatenated"
            fields["status"] = "concatenated"
        elif status in {"failed", "error"}:
            fields["concat_status"] = "concat_failed"
        elif status in {"running", "concatenating"}:
            fields["concat_status"] = "concatenating"
        await db.update_recording_project(project_id, **fields)
    return {"success": True, "task": result}


@app.get("/api/tasks/convert")
async def get_convert_tasks():
    """Read-only convert/processing monitor (no mutate)."""
    converting = []
    try:
        converting = recording_audit.converting_items()
    except Exception:
        converting = []
    waiting = []
    try:
        for row in _collect_processing_files():
            waiting.append({
                "username": row.get("username") or "",
                "filename": row.get("filename") or "",
                "path": row.get("path") or "",
                "size": row.get("size") or 0,
                "sizeFormatted": row.get("sizeFormatted") or "",
                "modifiedAt": row.get("modifiedAt") or 0,
                "phase": "waiting",
            })
    except Exception:
        waiting = []
    converting_out = []
    for row in converting:
        converting_out.append({
            "username": row.get("username") or "",
            "filename": row.get("filename") or "",
            "path": row.get("path") or "",
            "recordingId": row.get("recordingId"),
            "startedAt": row.get("startedAt"),
            "phase": "converting",
        })
    return {
        "converting": converting_out,
        "waiting": waiting,
        "count": len(converting_out) + len(waiting),
    }


@app.post("/api/media-library/delete-preview")
async def preview_media_library_delete(body: MediaDeletePreviewBody):
    """Report macCount/vpsCount for selected fragments (delete confirmation)."""
    requested_ids = list(dict.fromkeys(str(item_id).strip() for item_id in body.itemIds))
    requested_ids = [item_id for item_id in requested_ids if item_id]
    if not requested_ids or len(requested_ids) > 200:
        raise HTTPException(status_code=400, detail="Select between 1 and 200 videos")

    library_items = {
        str(item.get("id")): item
        for item in await _scan_media_library_items(refresh_metadata=False)
        if item.get("type") == "video"
    }
    with _mac_state_lock:
        statuses = dict((_mac_helper_snapshot.get("statuses") or {}))
        mac_files = list((_mac_helper_snapshot.get("files") or []))
    mac_by_recording = {
        str(f.get("recordingId") or "").strip()
        for f in mac_files
        if str(f.get("recordingId") or "").strip()
    }
    mac_by_name_size = {
        (str(f.get("filename") or "").split("/")[-1].lower(), int(f.get("size") or 0))
        for f in mac_files
    }

    vps_count = 0
    mac_count = 0
    both_count = 0
    unknown = 0
    for item_id in requested_ids:
        item = library_items.get(item_id)
        if item:
            on_vps = True
            rec_id = str(item.get("recordingId") or item_id)
            fname = str(item.get("filename") or "").lower()
            size = int(item.get("size") or 0)
            on_mac = (
                statuses.get(item_id) == "synced"
                or rec_id in mac_by_recording
                or (fname, size) in mac_by_name_size
            )
            if on_vps and on_mac:
                both_count += 1
                vps_count += 1
                mac_count += 1
            elif on_mac:
                mac_count += 1
            else:
                vps_count += 1
            continue
        # Mac-only project member / snapshot item
        member_mac = False
        try:
            # item_id may be a synthetic mac id; treat missing library as mac-side
            member_mac = True
        except Exception:
            pass
        if member_mac:
            mac_count += 1
        else:
            unknown += 1
    return {
        "macCount": mac_count,
        "vpsCount": vps_count,
        "bothCount": both_count,
        "unknownCount": unknown,
        "selectedCount": len(requested_ids),
    }


@app.get("/api/media-library")
async def get_media_library(
    username: Optional[str] = None,
    kind: str = "all",
    search: str = "",
    sort: str = "newest",
    watched: str = "all",
    metadata: str = "full",
    live: bool = False,
    live_refresh: bool = False,
    limit: int = 1000,
    offset: int = 0,
):
    """List media present in the records folders."""

    metadata_mode = (metadata or "full").strip().lower()
    refresh_metadata = metadata_mode not in {"lazy", "fast", "0", "false", "no"}
    scan_username = username if refresh_metadata and username else None
    all_items = await _scan_media_library_items(
        profile_username=scan_username,
        refresh_metadata=refresh_metadata,
    )
    await _repair_truncated_media_profile_usernames()
    folder_profiles = _list_media_profile_folders()
    media_profiles = await db.get_all_media_profiles()
    all_models = await db.get_all_models()
    all_followed = await db.get_all_followed()
    all_profile_sources = await db.get_all_media_profile_sources()
    followed_lookup = {
        (
            str(item.get("username") or "").strip().lower(),
            _normalize_source_type(item.get("source_type")) or "chaturbate",
        )
        for item in all_followed
        if item.get("username")
    }
    followed_by_key = {
        (
            str(item.get("username") or "").strip().lower(),
            _normalize_source_type(item.get("source_type")) or "chaturbate",
        ): item
        for item in all_followed
        if item.get("username")
    }
    media_profiles_by_username = {
        profile["username"]: profile
        for profile in media_profiles
        if profile.get("username")
    }
    profile_sources_by_username: dict[str, list[dict]] = {}
    for source in all_profile_sources:
        profile_username = source.get("profile_username")
        if not profile_username:
            continue
        profile_sources_by_username.setdefault(profile_username, []).append(source)

    profile_stats: dict[str, dict] = {}
    for item in all_items:
        profile = item["username"]
        if profile not in profile_stats:
            profile_stats[profile] = {
                "username": profile,
                "total": 0,
                "videos": 0,
                "images": 0,
                "audio": 0,
                "totalSize": 0,
                "sourceType": item.get("sourceType") or item.get("source_type") or "",
            }
        stats = profile_stats[profile]
        item_source = item.get("sourceType") or item.get("source_type") or ""
        if item_source and not stats.get("sourceType"):
            stats["sourceType"] = item_source
        stats["total"] += 1
        stats["totalSize"] += int(item.get("size") or 0)
        if item.get("type") == "video":
            stats["videos"] += 1
            if int(item.get("createdAt") or 0) >= int(stats.get("latestVideoAt") or 0):
                stats["latestVideoAt"] = int(item.get("createdAt") or 0)
                stats["latestRecordingCover"] = item.get("thumbnail") or ""
        elif item.get("type") == "image":
            stats["images"] += 1
        elif item.get("type") == "audio":
            stats["audio"] += 1

        if int(item.get("createdAt") or 0) >= int(stats.get("latestAt") or 0):
            stats["latestAt"] = int(item.get("createdAt") or 0)
            stats["latestTitle"] = item.get("title") or item.get("filename") or ""
            stats["latestType"] = item.get("type") or ""
            stats["previewUrl"] = item.get("url")

    folder_identity_names: set[str] = set()
    folder_source_hints: dict[str, str] = {}
    for folder in folder_profiles:
        parsed_name, parsed_source = parse_identity_with_source(folder)
        identity = parsed_name or folder
        folder_identity_names.add(identity)
        if parsed_source:
            folder_source_hints[identity.lower()] = parsed_source

    # Persist cards for anything scanned on disk so cards survive empty folders.
    for identity in sorted(folder_identity_names):
        if identity in media_profiles_by_username:
            continue
        hint = folder_source_hints.get(identity.lower()) or (
            (profile_stats.get(identity) or {}).get("sourceType")
        ) or "chaturbate"
        hint = _normalize_source_type(hint) or "chaturbate"
        try:
            await ensure_streamer_card(db, identity, hint)
            created = await db.get_media_profile(identity)
            if created:
                media_profiles_by_username[identity] = created
            sources = await db.get_media_profile_sources(identity)
            if sources:
                profile_sources_by_username[identity] = sources
        except Exception:
            logger.warning("Failed to persist scanned streamer card", username=identity)

    for identity, stats in list(profile_stats.items()):
        if identity in media_profiles_by_username:
            continue
        hint = _normalize_source_type(stats.get("sourceType")) or folder_source_hints.get(identity.lower()) or "chaturbate"
        try:
            await ensure_streamer_card(db, identity, hint)
            created = await db.get_media_profile(identity)
            if created:
                media_profiles_by_username[identity] = created
            sources = await db.get_media_profile_sources(identity)
            if sources:
                profile_sources_by_username[identity] = sources
        except Exception:
            logger.warning("Failed to persist media-file streamer card", username=identity)

    profile_names = set(profile_stats)
    profile_names.update(folder_identity_names)
    profile_names.update(media_profiles_by_username)
    profile_names.update(model["username"] for model in all_models if model.get("username"))
    # Remote follows alone never create Media cards. Cards come from ensure-card,
    # media folders, tracked models, or existing media_profiles rows.

    def _profile_is_following(profile_name: str, stream_sources: list[dict], primary_source_type: str) -> bool:
        candidates = [(profile_name.strip().lower(), primary_source_type)]
        for source in stream_sources or []:
            channel = str(source.get("channelUsername") or source.get("channel_username") or "").strip().lower()
            st = _normalize_source_type(source.get("sourceType") or source.get("source_type")) or primary_source_type
            if channel:
                candidates.append((channel, st))
        return any(key in followed_lookup for key in candidates)

    default_quota_gb = await _get_default_monthly_quota_gb()
    recording_enabled_flag = await _recording_enabled()
    cycle_start, cycle_end = await _quota_cycle_window()
    usage_map = await _recording_usage_bytes_map(list(profile_names), include_live=True)

    profiles = []
    for profile_name in profile_names:
        profile = profile_stats.setdefault(profile_name, {
            "username": profile_name,
            "total": 0,
            "videos": 0,
            "images": 0,
            "audio": 0,
            "totalSize": 0,
            "latestAt": None,
            "latestTitle": "",
            "latestType": "",
            "previewUrl": None,
            "latestVideoAt": None,
            "latestRecordingCover": "",
        })
        metadata = media_profiles_by_username.get(profile_name)
        model = _model_for_media_profile(profile_name, all_models)
        formatted_metadata = _media_profile_formatted(metadata)
        raw_sources = profile_sources_by_username.get(profile_name) or []
        stream_sources = [_profile_source_response(source) for source in raw_sources]
        if not stream_sources and model:
            stream_sources = await _media_profile_stream_sources(profile_name)
        primary_source = _pick_primary_profile_source(stream_sources)
        followed_source_type = None
        profile_key = str(profile_name or "").strip().lower()
        for (followed_name, followed_type), _followed_row in followed_by_key.items():
            if followed_name == profile_key:
                followed_source_type = followed_type
                break
        if followed_source_type is None:
            for followed in all_followed:
                uname = str(followed.get("username") or "").strip()
                if not _followed_identity_matches_profile(uname, profile_name):
                    continue
                followed_source_type = (
                    _normalize_source_type(followed.get("source_type")) or "chaturbate"
                )
                break
        source_type = (
            (primary_source or {}).get("sourceType")
            or profile.get("sourceType")
            or (await _infer_source_type(profile_name, model) if model else None)
            or followed_source_type
            or "chaturbate"
        )
        channel_username = (primary_source or {}).get("channelUsername") or profile_name
        last_seen_online_at = _media_profile_last_seen_online_at(
            profile_username=profile_name,
            channel_username=str(channel_username or ""),
            source_type=source_type,
            followed_by_key=followed_by_key,
            media_profile=metadata,
            model=model,
        )
        last_live_at = _media_profile_last_live_at(
            last_seen_online_at=last_seen_online_at,
            profile=profile,
        )
        followed_row = followed_by_key.get(
            (str(channel_username or "").strip().lower(), source_type)
        ) or followed_by_key.get(
            (str(profile_name or "").strip().lower(), source_type)
        )
        followers = (model or {}).get("followers", (model or {}).get("num_followers"))
        if followers is None and followed_row is not None:
            followers = followed_row.get("followers")
        # Chaturbate roomlist historically defaulted missing counts to 0; treat
        # unverified zero as unknown until live biocontext fills a real total.
        try:
            if source_type == "chaturbate" and followers is not None and int(followers) == 0:
                followers = None
        except (TypeError, ValueError):
            followers = None
        if source_type == "stripchat":
            followers = None
        followed_face = str((followed_row or {}).get("profile_image_url") or "").strip()
        card_face = _pick_media_card_avatar(
            media_profile=metadata,
            live_image="",
            model_image=followed_face or (model or {}).get("profile_image_url") or "",
        )
        formatted_for_card = dict(formatted_metadata)
        if card_face and not str(formatted_for_card.get("profileImageUrl") or "").strip():
            formatted_for_card["profileImageUrl"] = card_face
            formatted_for_card["profile_image_url"] = card_face
        stored_quota = (primary_source or {}).get("monthlyQuotaGb")
        if stored_quota is None:
            stored_quota = (model or {}).get("monthly_quota_gb")
        quota_fields = _quota_fields_for_profile(
            username=profile_name,
            stored_quota_gb=stored_quota,
            default_quota_gb=default_quota_gb,
            used_bytes=int(usage_map.get(profile_name) or 0),
            recording_enabled=recording_enabled_flag,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        profiles.append({
            **profile,
            **formatted_for_card,
            "displayName": (
                formatted_for_card["displayName"]
                or (followed_row or {}).get("display_name")
                or (model or {}).get("display_name")
                or profile_name
            ),
            "folderExists": profile_name in folder_identity_names,
            "empty": int(profile.get("total") or 0) == 0,
            "autoRecord": bool((primary_source or {}).get("autoRecord", (model or {}).get("auto_record", False))),
            "recordQuality": (primary_source or {}).get("recordQuality") or (model or {}).get("record_quality", "best"),
            "retentionDays": (primary_source or {}).get("retentionDays", (model or {}).get("retention_days", 30)),
            "sourceType": source_type,
            "source_type": source_type,
            "isFollowing": _profile_is_following(profile_name, stream_sources, source_type),
            "channelUsername": channel_username,
            "channelUrl": (primary_source or {}).get("channelUrl") or _canonical_stream_url(source_type, profile_name),
            "isOnline": bool((model or {}).get("is_online", False)) or bool((followed_row or {}).get("is_online", False)),
            "viewers": int((model or {}).get("viewers") or (followed_row or {}).get("viewers") or 0)
            if (bool((model or {}).get("is_online", False)) or bool((followed_row or {}).get("is_online", False)))
            else 0,
            "followers": int(followers) if followers is not None else None,
            "lastSeenOnlineAt": last_seen_online_at,
            "last_seen_online_at": last_seen_online_at,
            "lastLiveAt": last_live_at,
            "last_live_at": last_live_at,
            "streamSources": stream_sources,
            "stream_sources": stream_sources,
            "deleteUrl": f"/api/media-profiles/{quote(profile_name, safe='')}",
            "totalSizeFormatted": format_bytes(profile["totalSize"]),
            **quota_fields,
        })
    if live:
        live_metadata = await asyncio.gather(*[
            (
                _media_profile_live_card_meta(
                    str(profile.get("sourceType") or "chaturbate"),
                    str(profile.get("channelUsername") or profile.get("username") or ""),
                    force_refresh=True,
                )
                if live_refresh
                else _media_profile_live_card_meta(
                    str(profile.get("sourceType") or "chaturbate"),
                    str(profile.get("channelUsername") or profile.get("username") or ""),
                )
            )
            for profile in profiles
        ])
        for profile, live_meta in zip(profiles, live_metadata):
            if not live_meta:
                continue
            profile["isOnline"] = bool(live_meta.get("isOnline"))
            profile["viewers"] = int(live_meta.get("viewers") or 0) if profile["isOnline"] else 0
            profile_username = str(profile.get("username") or "").strip()
            channel_username = str(
                profile.get("channelUsername") or profile.get("channel_username") or profile_username
            ).strip()
            source_type = str(profile.get("sourceType") or profile.get("source_type") or "chaturbate")
            room_status = str(live_meta.get("roomStatus") or live_meta.get("room_status") or "").strip() or None
            if profile["isOnline"]:
                # Live right now counts as the latest seen-online moment for the card.
                now_ts = int(time.time())
                profile["lastSeenOnlineAt"] = now_ts
                profile["last_seen_online_at"] = now_ts
                try:
                    if profile_username:
                        await db.touch_media_profile_last_seen(profile_username, now_ts)
                except Exception:
                    pass
                if profile.get("isFollowing") and channel_username:
                    try:
                        await db.upsert_followed_model(
                            username=channel_username,
                            display_name=str(profile.get("displayName") or "") or None,
                            is_online=True,
                            viewers=int(profile.get("viewers") or 0),
                            source_type=source_type,
                            room_status=room_status,
                        )
                    except Exception:
                        pass
                if channel_username and live_refresh:
                    try:
                        await db.observe_live_status(
                            channel_username,
                            is_online=True,
                            source_type=source_type,
                            profile_username=profile_username or channel_username,
                            seen_at=now_ts,
                            detection="media_live_refresh",
                        )
                    except Exception:
                        pass
            else:
                live_last_seen = _coerce_unix_timestamp(
                    live_meta.get("lastSeenOnlineAt") or live_meta.get("last_seen_online_at")
                )
                if live_last_seen:
                    existing_seen = _coerce_unix_timestamp(
                        profile.get("lastSeenOnlineAt") or profile.get("last_seen_online_at")
                    )
                    merged_seen = max(stamp for stamp in (existing_seen, live_last_seen) if stamp)
                    profile["lastSeenOnlineAt"] = merged_seen
                    profile["last_seen_online_at"] = merged_seen
                if profile.get("isFollowing") and channel_username:
                    try:
                        await db.upsert_followed_model(
                            username=channel_username,
                            display_name=str(profile.get("displayName") or "") or None,
                            is_online=False,
                            viewers=0,
                            source_type=source_type,
                            room_status=room_status or "offline",
                        )
                    except Exception:
                        pass
                if channel_username and live_refresh:
                    try:
                        await db.observe_live_status(
                            channel_username,
                            is_online=False,
                            source_type=source_type,
                            profile_username=profile_username or channel_username,
                            detection="media_live_refresh",
                        )
                    except Exception:
                        pass
            profile["lastLiveAt"] = _media_profile_last_live_at(
                last_seen_online_at=_coerce_unix_timestamp(
                    profile.get("lastSeenOnlineAt") or profile.get("last_seen_online_at")
                ),
                profile=profile,
            )
            profile["last_live_at"] = profile["lastLiveAt"]
            if live_meta.get("roomStatus"):
                profile["roomStatus"] = live_meta["roomStatus"]
                profile["room_status"] = live_meta["roomStatus"]
            if live_meta.get("followers") is not None:
                profile["followers"] = int(live_meta["followers"])
            if live_meta.get("channelUrl"):
                profile["channelUrl"] = live_meta["channelUrl"]
            live_image = str(live_meta.get("profileImageUrl") or "").strip()
            if live_image.startswith("http://"):
                live_image = "https://" + live_image[len("http://") :]
            if live_image and not _is_live_cover_profile_image(live_image):
                # Never promote live webcam/snapshot covers into the circular avatar.
                profile["profileImageUrl"] = live_image
                profile["profile_image_url"] = live_image
                # Heal Babepedia mis-matches (wrong gallery person) with the
                # platform-native face so local /status hydration stays correct.
                # Also persist when the card still has no stored face (e.g. Bilibili).
                try:
                    existing_profile = await db.get_media_profile(profile_username) or {}
                    stored_face = str(existing_profile.get("profile_image_url") or "").strip()
                    should_persist = _is_babepedia_image_ref(
                        existing_profile.get("profile_image_source_url"),
                        existing_profile.get("profile_image_url"),
                    ) or not stored_face
                    if should_persist:
                        merge = dict(existing_profile)
                        old_path = str(merge.get("profile_image_path") or "").strip()
                        merge["profile_image_url"] = live_image
                        if _is_babepedia_image_ref(
                            existing_profile.get("profile_image_source_url"),
                            existing_profile.get("profile_image_url"),
                        ):
                            merge["profile_image_source_url"] = None
                            merge["profile_image_path"] = None
                        await db.upsert_media_profile(profile_username, merge)
                        if old_path and _is_babepedia_image_ref(
                            existing_profile.get("profile_image_source_url"),
                            existing_profile.get("profile_image_url"),
                        ):
                            try:
                                path = Path(old_path)
                                if path.is_file() and _path_is_inside_output(path):
                                    path.unlink(missing_ok=True)
                            except OSError:
                                pass
                except Exception:
                    pass
            live_display = str(live_meta.get("displayName") or live_meta.get("display_name") or "").strip()
            if live_display:
                current_display = str(profile.get("displayName") or "").strip()
                # Do not reuse the query param name `username` here — that would
                # shadow it and later filter the media grid to the last profile.
                # Bilibili room folders are numeric ids; promote uname when the
                # stored label is empty or still just the room id.
                if (
                    not current_display
                    or current_display.lower() == profile_username.lower()
                    or (profile_username.isdigit() and current_display == profile_username)
                ):
                    profile["displayName"] = live_display
                    # Persist so Media video cards keep the human label offline.
                    # Merge into the existing row — upsert_media_profile replaces
                    # every column and must not wipe avatar/notes/etc.
                    if live_display and live_display != profile_username:
                        try:
                            existing_profile = await db.get_media_profile(profile_username) or {}
                            merge = dict(existing_profile)
                            merge["display_name"] = live_display
                            await db.upsert_media_profile(profile_username, merge)
                        except Exception:
                            pass
    profiles.sort(key=lambda item: (
        0 if item.get("isOnline") else (1 if item.get("autoRecord") else 2),
        (item.get("displayName") or "").lower(),
        (item.get("username") or "").lower(),
    ))

    normalized_kind = (kind or "all").strip().lower()
    kind_aliases = {
        "photos": "image",
        "photo": "image",
        "images": "image",
        "videos": "video",
        "audio": "audio",
        "all": "all",
        "tous": "all",
    }
    normalized_kind = kind_aliases.get(normalized_kind, normalized_kind)
    if normalized_kind not in {"all", "video", "image", "audio"}:
        normalized_kind = "all"

    filtered = all_items
    if username:
        filtered = [item for item in filtered if item["username"] == username]
    if normalized_kind != "all":
        filtered = [item for item in filtered if item["type"] == normalized_kind]
    if search:
        query = search.strip().lower()
        if query:
            filtered = [
                item for item in filtered
                if query in item["filename"].lower()
                or query in item["title"].lower()
                or query in item["username"].lower()
                or query in item["relativePath"].lower()
            ]

    watched_threshold = await _get_watched_threshold()
    playback_positions = await db.get_all_playback_positions(username)
    playback_by_recording_id = {
        row.get("recording_id"): row
        for row in playback_positions
        if row.get("recording_id")
    }
    for item in filtered:
        _attach_media_playback_state(
            item,
            playback_by_recording_id.get(item.get("recordingId")),
            watched_threshold,
        )

    watched_filter = (watched or "all").strip().lower()
    if watched_filter in {"unwatched", "not_watched", "unseen", "non_vue", "non_vues", "non-vue", "non-vues"}:
        filtered = [
            item for item in filtered
            if item.get("type") == "video" and not item.get("isWatched")
        ]
    elif watched_filter in {"watched", "seen", "viewed", "vue", "vues"}:
        filtered = [
            item for item in filtered
            if item.get("type") == "video" and item.get("isWatched")
        ]

    sort_key = (sort or "newest").strip().lower()
    reverse = True
    if sort_key == "oldest":
        def key_func(item):
            return item["createdAt"]
        reverse = False
    elif sort_key == "largest":
        def key_func(item):
            return item["size"]
    elif sort_key == "smallest":
        def key_func(item):
            return item["size"]
        reverse = False
    elif sort_key == "name":
        def key_func(item):
            return (item["title"].lower(), item["filename"].lower())
        reverse = False
    else:
        def key_func(item):
            return item["createdAt"]

    filtered = sorted(filtered, key=key_func, reverse=reverse)
    total = len(filtered)
    limit = max(1, min(int(limit or 1000), 5000))
    offset = max(0, int(offset or 0))
    page_items = filtered[offset:offset + limit]

    library_stats = _media_library_stats(all_items)
    filtered_stats = _media_library_stats(filtered)
    storage = _media_storage_stats(all_items)
    return {
        "items": page_items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + limit < total,
        "profiles": profiles,
        "stats": {
            **filtered_stats,
            "totalSizeFormatted": format_bytes(filtered_stats["totalSize"]),
        },
        "libraryStats": {
            **library_stats,
            "totalSizeFormatted": format_bytes(library_stats["totalSize"]),
        },
        "storage": storage,
    }


def _mac_download_safe_segment(value: str, fallback: str) -> str:
    return safe_filename_part(value, fallback)


def _mac_download_folder_label(username: str, display_name: str = "") -> str:
    """Prefer a human display name when the profile key is a numeric room id."""
    user = str(username or "").strip()
    display = str(display_name or "").strip()
    if user.isdigit() and display and display != user:
        return display
    return user or display or "unknown"


async def _mac_download_display_name(username: str) -> str:
    uname = str(username or "").strip()
    if not uname:
        return ""
    try:
        profile = await db.get_media_profile(uname)
        display = str((profile or {}).get("display_name") or "").strip()
        if display:
            return display
    except Exception:
        pass
    try:
        model = await db.get_model(uname)
        display = str((model or {}).get("display_name") or "").strip()
        if display:
            return display
    except Exception:
        pass
    try:
        followed = await db.get_followed_model(uname)
        return str((followed or {}).get("display_name") or "").strip()
    except Exception:
        return ""


async def _mac_numeric_folder_rename_commands(files: list) -> list[dict[str, Any]]:
    """Rewrite Mac ``123456(bilibili)/…`` folders to ``DisplayName(bilibili)/…`` when known."""
    folders: dict[str, tuple[str, Optional[str]]] = {}
    for item in files or []:
        filename = ""
        if hasattr(item, "filename"):
            filename = str(getattr(item, "filename", "") or "")
        elif isinstance(item, dict):
            filename = str(item.get("filename") or "")
        filename = filename.replace("\\", "/").strip("/")
        if "/" not in filename:
            continue
        folder = filename.split("/", 1)[0].strip()
        if not folder or folder in folders:
            continue
        parsed_name, source = parse_identity_with_source(folder)
        if not parsed_name or not parsed_name.isdigit():
            continue
        folders[folder] = (parsed_name, source)

    commands: list[dict[str, Any]] = []
    for folder, (room_id, source) in folders.items():
        display = await _mac_download_display_name(room_id)
        label = _mac_download_folder_label(room_id, display)
        if not label or label == room_id:
            continue
        to_folder = format_identity_with_source(label, source)
        if not to_folder or to_folder == folder:
            continue
        commands.append({
            "commandId": secrets.token_urlsafe(18),
            "type": "rename-folder",
            "fromFolder": folder,
            "toFolder": to_folder,
            "status": "claimed",
        })
    return commands


def _mac_download_chrome_filename(recording_id: str, filename: str) -> str:
    """Flat basename Chrome saves into the fixed download folder."""
    # Chrome staging names stay ASCII-ish; recording ids already are.
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", (recording_id or "").strip()).strip("._") or "recording"
    original = Path(filename or "video.mp4").name
    ext = Path(original).suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}:
        ext = ".mp4"
    return f"hxylive-{safe_id}__pending{ext}"


async def _project_id_for_library_item(username: str, filename: str) -> Optional[str]:
    try:
        member = await db.get_project_member(username, Path(filename or "").name)
    except Exception:
        return None
    if not member:
        return None
    return str(member.get("project_id") or "") or None


def _mac_download_relative_path(
    username: str,
    filename: str,
    created_at: int,
    used_paths: Optional[set] = None,
    source_type: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Mac layout:
      HXYLIVE/<streamer(source)>/.projects/<projectId>/YYYY-MM-DD HH-MM-SS[_partNNN].ext
    Legacy flat layout (no project):
      HXYLIVE/<streamer(source)>/YYYY-MM-DD HH-MM-SS[_partNNN].ext
    """
    source = normalize_source_marker(source_type) or "chaturbate"
    safe_user = format_identity_with_source(
        _mac_download_safe_segment(username, "unknown"),
        source,
    )
    original = Path(filename or "video.mp4").name
    ext = Path(original).suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}:
        ext = ".mp4"

    stem = Path(original).stem
    part_match = re.search(r"(?i)(_part\d{3,})$", stem)
    part_suffix = part_match.group(1).lower() if part_match else ""
    date_token = None
    time_token = None
    match = re.search(
        r"(?<!\d)(\d{4}-\d{2}-\d{2})[ T_](\d{2})[:.\-](\d{2})[:.\-](\d{2})(?!\d)",
        stem,
    )
    if match:
        date_token = match.group(1)
        time_token = f"{match.group(2)}{match.group(3)}{match.group(4)}"
    if not match:
        match = re.search(r"(?<!\d)(\d{8})[_-](\d{6})(?!\d)", stem)
        if not match:
            match = re.match(r"^(\d{8})(?:_(\d{6}))?", stem)
        if match:
            ymd = match.group(1)
            date_token = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
            time_token = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    if not date_token or not time_token:
        stamp = int(created_at or time.time())
        try:
            dt = datetime.fromtimestamp(stamp)
        except (OverflowError, OSError, ValueError):
            dt = datetime.now()
        if not date_token:
            date_token = dt.strftime("%Y-%m-%d")
        if not time_token:
            time_token = dt.strftime("%H%M%S")

    clock = f"{time_token[0:2]}-{time_token[2:4]}-{time_token[4:6]}"
    stamp_name = f"{date_token} {clock}{part_suffix}"
    if project_id:
        from app.services.recording_projects import mac_project_relative_dir
        folder = mac_project_relative_dir(safe_user, project_id)
        candidates = [f"{folder}/{stamp_name}{ext}"]
        candidates.append(f"{folder}/{date_token}_{time_token}{part_suffix}{ext}")
    else:
        candidates = [f"{safe_user}/{stamp_name}{ext}"]
        candidates.append(f"{safe_user}/{date_token}_{time_token}{part_suffix}{ext}")
        candidates.append(
            f"{safe_user}/{stamp_name}_{_mac_download_safe_segment(stem, 'video')[:40]}{ext}"
        )

    used = used_paths if used_paths is not None else set()
    for relative in candidates:
        if relative not in used:
            used.add(relative)
            return relative
    relative = f"{candidates[0].rsplit('.', 1)[0]}_{secrets.token_hex(3)}{ext}"
    used.add(relative)
    return relative


async def _primary_source_type_for_profile(username: str) -> str:
    """Best-effort platform for Mac folder stamping when downloading a profile's videos."""
    channel = str(username or "").strip()
    if not channel:
        return "chaturbate"
    try:
        sources = await db.get_media_profile_sources(channel)
    except Exception:
        sources = []
    for source in sources or []:
        marked = normalize_source_marker(source.get("source_type") or source.get("sourceType"))
        if marked:
            return marked
    try:
        model = await db.get_model(channel)
    except Exception:
        model = None
    marked = normalize_source_marker((model or {}).get("source_type"))
    if marked:
        return marked
    try:
        followed = await db.get_followed_model(channel)
    except Exception:
        followed = None
    marked = normalize_source_marker((followed or {}).get("source_type"))
    if marked:
        return marked
    return "chaturbate"

def _prune_mac_download_state() -> None:
    now = time.time()
    expired_tokens = [
        token
        for token, entry in _mac_download_tokens.items()
        if float(entry.get("expiresAt") or 0) <= now
    ]
    for token in expired_tokens:
        _mac_download_tokens.pop(token, None)
    expired_jobs = [
        job_id
        for job_id, job in _mac_download_jobs.items()
        if float(job.get("expiresAt") or 0) <= now
    ]
    for job_id in expired_jobs:
        _mac_download_jobs.pop(job_id, None)
    expired_commands = [
        command_id
        for command_id, command in _mac_helper_commands.items()
        if float(command.get("expiresAt") or 0) <= now
    ]
    for command_id in expired_commands:
        _mac_helper_commands.pop(command_id, None)
    expired_thumbs = [
        key
        for key, entry in _mac_helper_thumbs.items()
        if float(entry.get("expiresAt") or 0) <= now
    ]
    for key in expired_thumbs:
        _mac_helper_thumbs.pop(key, None)
    expired_imports = [
        command_id
        for command_id, entry in _mac_helper_import_results.items()
        if float(entry.get("expiresAt") or 0) <= now
    ]
    for command_id in expired_imports:
        _mac_helper_import_results.pop(command_id, None)
    expired_follows = [
        command_id
        for command_id, entry in _mac_helper_follow_results.items()
        if float(entry.get("expiresAt") or 0) <= now
    ]
    for command_id in expired_follows:
        _mac_helper_follow_results.pop(command_id, None)


def _mac_session_matches(left: str, right: str) -> bool:
    a = str(left or "")
    b = str(right or "")
    if len(a) != len(b):
        return False
    return secrets.compare_digest(a, b)


def _mac_thumb_idents(recording_id: str, relative: str) -> list[str]:
    idents = []
    rid = str(recording_id or "").strip()
    rel = str(relative or "").strip().replace("\\", "/")
    if rid:
        idents.append(rid)
    if rel and rel not in idents:
        idents.append(rel)
    return idents


def _mac_thumb_keys(local_session_id: str, recording_id: str = "", relative: str = "") -> list[str]:
    session = str(local_session_id or "").strip()
    return [f"{session}\0{ident}" for ident in _mac_thumb_idents(recording_id, relative)]


def _store_mac_helper_thumb(
    local_session_id: str,
    recording_id: str,
    relative: str,
    payload: bytes,
    content_type: str,
) -> None:
    expires_at = time.time() + MAC_HELPER_THUMB_TTL_SECONDS
    entry = {
        "bytes": payload,
        "contentType": content_type or "image/png",
        "expiresAt": expires_at,
        "localSessionId": local_session_id,
    }
    with _mac_state_lock:
        _prune_mac_download_state()
        for key in _mac_thumb_keys(local_session_id, recording_id, relative):
            _mac_helper_thumbs[key] = entry


def _lookup_mac_helper_thumb(local_session_id: str, recording_id: str = "", relative: str = ""):
    with _mac_state_lock:
        _prune_mac_download_state()
        for key in _mac_thumb_keys(local_session_id, recording_id, relative):
            entry = _mac_helper_thumbs.get(key)
            if entry and _mac_session_matches(str(entry.get("localSessionId") or ""), local_session_id):
                return entry
    return None


def _queue_mac_helper_thumb(local_session_id: str, relative: str, recording_id: str) -> dict[str, Any]:
    relative = str(relative or "").strip()
    recording_id = str(recording_id or "").strip()
    with _mac_state_lock:
        queued = [
            existing
            for existing in _mac_helper_commands.values()
            if str(existing.get("status") or "") == "queued"
            and str(existing.get("type") or "") == "thumb"
            and _mac_session_matches(str(existing.get("localSessionId") or ""), local_session_id)
        ]
        for existing in queued:
            same_path = relative and str(existing.get("relativePath") or "").strip() == relative
            same_id = recording_id and str(existing.get("recordingId") or "").strip() == recording_id
            if same_path or same_id:
                return {
                    "status": "queued",
                    "commandId": existing.get("commandId"),
                    "type": "thumb",
                }
    return _queue_mac_helper_command(local_session_id, "thumb", {
        "relativePath": relative,
        "recordingId": recording_id,
    })


async def _compare_mac_local_files(
    local_session_id: str,
    files: list[MacLocalFileBody],
) -> dict[str, Any]:
    by_recording_id = {
        str(file.recordingId): int(file.size)
        for file in files
        if file.recordingId
    }
    by_filename_size = {
        (Path(file.filename).name, int(file.size))
        for file in files
        if file.filename and int(file.size) >= 0
    }
    # Motrix/date-renamed Mac copies often lose recordingId and basename parity.
    # When a byte size appears exactly once on both sides, treat it as the same file.
    mac_size_counts: dict[int, int] = {}
    for file in files:
        size = int(file.size)
        if size <= 0:
            continue
        mac_size_counts[size] = mac_size_counts.get(size, 0) + 1
    mac_unique_sizes = {size for size, count in mac_size_counts.items() if count == 1}
    try:
        items = [
            item
            for item in await _scan_media_library_items(refresh_metadata=False)
            if item.get("type") == "video"
        ]
    except Exception as exc:
        logger.error("Mac sync could not scan VPS media library", error=str(exc))
        items = []
    vps_size_counts: dict[int, int] = {}
    for item in items:
        size = int(item.get("size") or 0)
        if size <= 0:
            continue
        vps_size_counts[size] = vps_size_counts.get(size, 0) + 1
    statuses: dict[str, str] = {}
    synced = 0
    incomplete = 0
    for item in items:
        item_id = str(item.get("id") or "")
        recording_id = str(item.get("recordingId") or "")
        size = int(item.get("size") or 0)
        status = "not_synced"
        if recording_id and recording_id in by_recording_id:
            status = "synced" if by_recording_id[recording_id] == size else "incomplete"
        elif (str(item.get("filename") or ""), size) in by_filename_size:
            status = "synced"
        elif (
            size > 0
            and size in mac_unique_sizes
            and vps_size_counts.get(size, 0) == 1
        ):
            status = "synced"
        statuses[item_id] = status
        if status == "synced":
            synced += 1
        elif status == "incomplete":
            incomplete += 1
    return {
        "localSessionId": local_session_id,
        "scannedAt": int(time.time()),
        "statuses": statuses,
        "totalVideos": len(items),
        "synced": synced,
        "incomplete": incomplete,
        "notSynced": max(0, len(items) - synced - incomplete),
        "files": [
            _mac_snapshot_file_payload(file)
            for file in files
        ],
    }


def _mac_snapshot_file_payload(file: MacLocalFileBody) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recordingId": str(file.recordingId or ""),
        "filename": str(file.filename or ""),
        "size": int(file.size),
    }
    try:
        duration = int(file.durationSeconds) if file.durationSeconds is not None else 0
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        payload["durationSeconds"] = duration
    resolution = str(file.resolution or "").strip()
    if resolution:
        payload["resolution"] = resolution
    try:
        fps = float(file.fps) if file.fps is not None else None
        if fps is not None and fps <= 0:
            fps = None
    except (TypeError, ValueError):
        fps = None
    if fps is not None:
        payload["fps"] = fps
    try:
        bitrate = int(file.bitrate) if file.bitrate is not None else None
        if bitrate is not None and bitrate <= 0:
            bitrate = None
    except (TypeError, ValueError):
        bitrate = None
    if bitrate is not None:
        payload["bitrate"] = bitrate
    return payload


async def _purge_vps_videos_present_on_mac(compared: dict[str, Any]) -> dict[str, Any]:
    """Delete VPS library videos that already have a complete Mac copy.

    Mac is the final store. VPS keeps only unsynced or incomplete staging copies.
    Active recordings are skipped and left for a later scan.
    Project members stay as Mac cards after purge.
    """
    from app.services.download_queue import download_queue

    statuses = dict(compared.get("statuses") or {})
    synced_ids = [item_id for item_id, status in statuses.items() if status == "synced"]
    purged: list[dict[str, str]] = []
    if synced_ids:
        try:
            library_items = {
                str(item.get("id")): item
                for item in await _scan_media_library_items(refresh_metadata=False)
                if item.get("type") == "video"
            }
        except Exception as exc:
            logger.error("Mac sync purge could not scan VPS media library", error=str(exc))
            library_items = {}

        for item_id in synced_ids:
            item = library_items.get(item_id)
            if not item:
                # Already purged from VPS library — still mark queue/member mac.
                download_queue.mark_done_for_items({item_id})
                statuses.pop(item_id, None)
                continue
            username = str(item.get("username") or "")
            relative_path = str(item.get("relativePath") or "")
            filename = str(item.get("filename") or "")
            try:
                media_path = _resolve_library_media_path(username, relative_path)
                if not media_path.exists() or not media_path.is_file():
                    try:
                        await db.set_project_member_location(username, filename, "mac")
                    except Exception:
                        pass
                    download_queue.mark_done_for_items({item_id})
                    statuses.pop(item_id, None)
                    continue
                _assert_media_not_active(username, media_path)
                await _delete_media_library_record(
                    username,
                    media_path,
                    relative_path,
                    reason="mac_sync_purge",
                    final_status="synced_purged",
                )
                try:
                    await db.set_project_member_location(username, filename, "mac")
                except Exception:
                    pass
                download_queue.mark_done_for_items({item_id})
                purged.append({
                    "itemId": item_id,
                    "filename": filename,
                })
                recording_audit.emit(
                    "vps_purged_after_mac",
                    username=username,
                    path=str(media_path),
                    itemId=item_id,
                    filename=filename,
                )
                statuses.pop(item_id, None)
            except HTTPException as exc:
                logger.info(
                    "Skipped VPS purge for synced Mac copy",
                    item_id=item_id,
                    detail=str(getattr(exc, "detail", exc)),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to purge VPS video after Mac sync",
                    item_id=item_id,
                    error=str(exc),
                )

    synced = sum(1 for status in statuses.values() if status == "synced")
    incomplete = sum(1 for status in statuses.values() if status == "incomplete")
    not_synced = sum(1 for status in statuses.values() if status == "not_synced")
    compared["statuses"] = statuses
    compared["synced"] = synced
    compared["incomplete"] = incomplete
    compared["notSynced"] = not_synced
    compared["totalVideos"] = max(0, int(compared.get("totalVideos") or 0) - len(purged))
    compared["purgedVps"] = len(purged)
    compared["purged"] = purged
    return compared


async def _download_concurrency_settings() -> tuple[int, int]:
    from app.services.download_queue import (
        DEFAULT_FILE_CONCURRENCY,
        DEFAULT_PROJECT_CONCURRENCY,
        normalize_file_concurrency,
        normalize_project_concurrency,
    )

    project_n = normalize_project_concurrency(
        await db.get_setting("download_project_concurrency"),
        DEFAULT_PROJECT_CONCURRENCY,
    )
    file_n = normalize_file_concurrency(
        await db.get_setting("download_file_concurrency"),
        DEFAULT_FILE_CONCURRENCY,
    )
    return project_n, file_n


async def _enqueue_manual_download_items(
    *,
    local_session_id: str,
    item_ids: list[str],
    method: str,
) -> dict[str, Any]:
    """Append selected library fragments to the manual download queue (no Motrix flood)."""
    from app.services.download_queue import download_queue

    requested_ids = list(dict.fromkeys(str(i).strip() for i in item_ids if str(i).strip()))
    if not local_session_id or not requested_ids:
        raise HTTPException(status_code=400, detail="Invalid download queue request")
    if len(requested_ids) > 100:
        raise HTTPException(status_code=400, detail="Select between 1 and 100 videos")
    download_method = str(method or "motrix").strip().lower()
    if download_method not in {"chrome", "motrix"}:
        raise HTTPException(status_code=400, detail="method must be chrome or motrix")

    library_items = {
        str(item.get("id")): item
        for item in await _scan_media_library_items(refresh_metadata=False)
        if item.get("type") == "video"
    }
    missing = [item_id for item_id in requested_ids if item_id not in library_items]
    if missing:
        raise HTTPException(status_code=404, detail=f"Media items not found: {', '.join(missing[:3])}")

    entries: list[dict[str, Any]] = []
    for item_id in requested_ids:
        item = library_items[item_id]
        username = str(item.get("username") or "")
        filename = str(item.get("filename") or "")
        project_id = await _project_id_for_library_item(username, filename)
        project = await db.get_recording_project(project_id) if project_id else None
        if not project_id:
            # Ensure membership before queueing.
            from app.services import recording_projects as recording_projects_svc

            project = await recording_projects_svc.assign_fragment_to_project(
                db,
                username=username,
                filename=filename,
                recording_id=str(item.get("recordingId") or "") or None,
                item_id=item_id,
                started_at=int(item.get("createdAt") or 0),
                duration_seconds=int(item.get("duration") or 0),
                size_bytes=int(item.get("size") or 0),
                source_type=await _primary_source_type_for_profile(username),
            )
            project_id = str((project or {}).get("project_id") or "")
        member = await db.get_project_member(username, filename)
        entries.append({
            "itemId": item_id,
            "recordingId": str(item.get("recordingId") or item_id),
            "username": username,
            "filename": filename,
            "sizeBytes": int(item.get("size") or 0),
            "startedAt": int((member or {}).get("started_at") or item.get("createdAt") or 0),
            "sortIndex": int((member or {}).get("sort_index") or 0),
            "projectId": project_id or "unknown",
            "projectStatus": str((project or {}).get("status") or "open"),
            "projectStartedAt": int((project or {}).get("started_at") or 0),
            "projectTitle": "",
            "method": download_method,
        })
        if project:
            from app.services.recording_projects import format_clock

            entries[-1]["projectTitle"] = format_clock(int(project.get("started_at") or 0))

    result = download_queue.append_batch(entries)
    return {
        **result,
        "localSessionId": local_session_id,
        "method": download_method,
        "queue": download_queue.list_grouped(),
    }


async def _drain_download_queue_to_jobs(
    local_session_id: str,
    *,
    public_base: str = "",
) -> list[dict[str, Any]]:
    """Claim scheduler-eligible queue items and create small Motrix/Chrome jobs."""
    from app.services.download_queue import download_queue

    project_n, file_n = await _download_concurrency_settings()
    claimed_tasks = download_queue.claim_next(
        project_concurrency=project_n,
        file_concurrency=file_n,
        local_session_id=local_session_id,
    )
    if not claimed_tasks:
        return []

    # Group by method for job creation.
    by_method: dict[str, list[dict[str, Any]]] = {}
    for task in claimed_tasks:
        method = str(task.get("method") or "motrix").strip().lower() or "motrix"
        by_method.setdefault(method, []).append(task)

    base = str(public_base or _mac_public_base() or "").rstrip("/")
    jobs: list[dict[str, Any]] = []
    for method, tasks in by_method.items():
        item_ids = [str(t.get("itemId")) for t in tasks if t.get("itemId")]
        if not item_ids:
            continue
        try:
            result = await _create_mac_download_job_internal(
                local_session_id=local_session_id,
                item_ids=item_ids,
                method=method,
                public_base=base,
                require_all=False,
            )
        except HTTPException as exc:
            logger.warning(
                "Download queue drain failed to create job",
                detail=str(getattr(exc, "detail", exc)),
                count=len(item_ids),
            )
            # Re-queue failed claims
            download_queue.pause(task_ids=[str(t.get("taskId")) for t in tasks])
            download_queue.resume(task_ids=[str(t.get("taskId")) for t in tasks])
            continue
        except Exception as exc:
            logger.warning("Download queue drain error", error=str(exc))
            continue
        job_id = str(result.get("jobId") or "")
        download_queue.bind_job([str(t.get("taskId")) for t in tasks], job_id)
        # Attach queue task ids on job items for helper progress mapping.
        job = None
        with _mac_state_lock:
            job = _mac_download_jobs.get(job_id)
            if job:
                task_by_item = {str(t.get("itemId")): t for t in tasks}
                for entry in job.get("items") or []:
                    linked = task_by_item.get(str(entry.get("itemId") or ""))
                    if linked:
                        entry["taskId"] = linked.get("taskId")
                        entry["queueProjectId"] = linked.get("projectId")
                job = dict(job)
        if job:
            jobs.append(job)
    return jobs


async def _claim_concat_commands(local_session_id: str) -> list[dict[str, Any]]:
    """Issue at most one concat-project helper command when a concat task is ready."""
    from app.services.concat_queue import concat_queue

    task = concat_queue.claim_next()
    if not task:
        return []
    project_id = str(task.get("projectId") or "")
    try:
        await db.update_recording_project(
            project_id,
            concat_status="concatenating",
            updated_at=int(time.time()),
        )
    except Exception:
        pass
    command = {
        "commandId": secrets.token_urlsafe(18),
        "localSessionId": local_session_id,
        "type": "concat-project",
        "status": "claimed",
        "createdAt": int(time.time()),
        "expiresAt": time.time() + MAC_HELPER_COMMAND_TTL_SECONDS,
        "projectId": project_id,
        "taskId": task.get("taskId"),
        "username": task.get("username") or "",
        "memberPaths": list(task.get("memberPaths") or []),
        "finalRelativePath": task.get("finalRelativePath") or "",
        "projectDir": task.get("projectDir") or "",
    }
    with _mac_state_lock:
        _mac_helper_commands[command["commandId"]] = dict(command)
        command["status"] = "claimed"
    return [command]


def _claim_ready_mac_jobs(local_session_id: str) -> list[dict[str, Any]]:
    _prune_mac_download_state()
    claimed: list[dict[str, Any]] = []
    with _mac_state_lock:
        for job in _mac_download_jobs.values():
            if str(job.get("status") or "") != "ready":
                continue
            if not _mac_session_matches(str(job.get("localSessionId") or ""), local_session_id):
                continue
            job["status"] = "claimed"
            claimed.append(job)
    return claimed


def _claim_mac_download_job(job_id: str, local_session_id: str) -> dict[str, Any]:
    _prune_mac_download_state()
    with _mac_state_lock:
        job = _mac_download_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Download job not found or expired")
        if not _mac_session_matches(str(job.get("localSessionId") or ""), local_session_id):
            raise HTTPException(status_code=403, detail="Download job belongs to another local session")
        status = str(job.get("status") or "")
        if status != "ready":
            raise HTTPException(status_code=409, detail="Download job already claimed")
        job["status"] = "claimed"
        return job


def _queue_mac_helper_command(local_session_id: str, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    local_session_id = (local_session_id or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    with _mac_state_lock:
        snap = dict(_mac_helper_snapshot)
    last_seen = float(snap.get("lastSeenAt") or 0)
    if (
        not snap
        or (time.time() - last_seen) > MAC_HELPER_SNAPSHOT_TTL_SECONDS
        or not _mac_session_matches(str(snap.get("localSessionId") or ""), local_session_id)
    ):
        raise HTTPException(status_code=503, detail="Start the HXYLIVE Mac helper, then retry")
    command = {
        "commandId": secrets.token_urlsafe(18),
        "localSessionId": local_session_id,
        "type": command_type,
        "status": "queued",
        "createdAt": int(time.time()),
        "expiresAt": time.time() + MAC_HELPER_COMMAND_TTL_SECONDS,
    }
    command.update(payload)
    with _mac_state_lock:
        _prune_mac_download_state()
        queued = [
            existing
            for existing in _mac_helper_commands.values()
            if str(existing.get("status") or "") == "queued"
            and _mac_session_matches(str(existing.get("localSessionId") or ""), local_session_id)
        ]
        thumb_queued = [item for item in queued if str(item.get("type") or "") == "thumb"]
        other_queued = [item for item in queued if str(item.get("type") or "") != "thumb"]
        if command_type == "thumb":
            if len(thumb_queued) >= MAC_HELPER_THUMB_COMMAND_MAX_QUEUED:
                raise HTTPException(status_code=429, detail="Too many pending Mac helper commands")
        elif len(other_queued) >= MAC_HELPER_COMMAND_MAX_QUEUED:
            raise HTTPException(status_code=429, detail="Too many pending Mac helper commands")
        _mac_helper_commands[command["commandId"]] = command
    return {
        "status": "queued",
        "commandId": command["commandId"],
        "type": command_type,
    }


def _claim_ready_mac_commands(local_session_id: str) -> list[dict[str, Any]]:
    _prune_mac_download_state()
    claimed: list[dict[str, Any]] = []
    with _mac_state_lock:
        for command in _mac_helper_commands.values():
            if str(command.get("status") or "") != "queued":
                continue
            if not _mac_session_matches(str(command.get("localSessionId") or ""), local_session_id):
                continue
            command["status"] = "claimed"
            claimed.append(dict(command))
    return claimed


@app.post("/api/mac/sync-snapshot")
async def compare_mac_sync_snapshot(body: MacSyncSnapshotBody):
    """Compare one fresh Mac folder scan with the current VPS video library.

    Complete Mac matches free VPS disk by deleting the VPS copies.
    """
    local_session_id = (body.localSessionId or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    compared = await _compare_mac_local_files(local_session_id, body.files)
    compared = await _purge_vps_videos_present_on_mac(compared)
    compared.pop("files", None)
    compared.pop("purged", None)
    return compared


def _release_mac_download_items(item_ids: set[str]) -> int:
    """Drop failed download items from ready/claimed jobs so catch-up can re-queue."""
    wanted = {str(item_id or "").strip() for item_id in item_ids if str(item_id or "").strip()}
    if not wanted:
        return 0
    released = 0
    with _mac_state_lock:
        empty_jobs = []
        for job_id, job in list(_mac_download_jobs.items()):
            if str(job.get("status") or "") not in {"ready", "claimed"}:
                continue
            kept = []
            for item in job.get("items") or []:
                keys = {
                    str(item.get("itemId") or "").strip(),
                    str(item.get("recordingId") or "").strip(),
                }
                if keys & wanted:
                    released += 1
                    continue
                kept.append(item)
            if not kept:
                empty_jobs.append(job_id)
            else:
                job["items"] = kept
        for job_id in empty_jobs:
            _mac_download_jobs.pop(job_id, None)
    return released


@app.post("/api/mac/helper/heartbeat")
async def mac_helper_heartbeat(body: MacSyncSnapshotBody):
    """Accept a Mac Helper folder snapshot and return scheduler-eligible download jobs + commands."""
    from app.services.concat_queue import concat_queue
    from app.services.download_queue import download_queue

    local_session_id = (body.localSessionId or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    _remember_mac_public_base(body.origin)
    released = _release_mac_download_items(set(body.releaseItemIds or []))
    if released:
        logger.info(
            "Released failed Mac download items for catch-up",
            task="mac-download-queue",
            count=released,
        )
    if body.downloadProgress:
        download_queue.update_progress(list(body.downloadProgress or []))
    if body.concatReports:
        for report in body.concatReports or []:
            if not isinstance(report, dict):
                continue
            result = concat_queue.report(
                project_id=str(report.get("projectId") or ""),
                task_id=str(report.get("taskId") or ""),
                status=str(report.get("status") or ""),
                error=str(report.get("error") or ""),
                step=report.get("step"),
            )
            if result:
                project_id = str(result.get("projectId") or "")
                status = str(result.get("status") or "")
                fields: dict[str, Any] = {"updated_at": int(time.time())}
                if status == "done":
                    fields["concat_status"] = "concatenated"
                    fields["status"] = "concatenated"
                elif status == "failed":
                    fields["concat_status"] = "concat_failed"
                elif status == "running":
                    fields["concat_status"] = "concatenating"
                try:
                    await db.update_recording_project(project_id, **fields)
                except Exception:
                    pass

    compared = await _compare_mac_local_files(local_session_id, body.files)
    compared = await _purge_vps_videos_present_on_mac(compared)
    # Manual queue drain: only next scheduler-eligible items (not flood).
    pending = await _drain_download_queue_to_jobs(
        local_session_id,
        public_base=str(body.origin or "") or _mac_public_base(),
    )
    # Also claim any leftover ready jobs (e.g. direct create path).
    pending.extend(
        _claim_ready_mac_jobs(local_session_id)
    )
    pending_commands = await _mac_numeric_folder_rename_commands(body.files)
    pending_commands.extend(await _claim_concat_commands(local_session_id))
    pending_commands.extend(_claim_ready_mac_commands(local_session_id))
    with _mac_state_lock:
        _mac_helper_snapshot.clear()
        _mac_helper_snapshot.update(compared)
        _mac_helper_snapshot["lastSeenAt"] = time.time()
        if body.origin:
            _mac_helper_snapshot["origin"] = str(body.origin).strip().rstrip("/")
    return {
        "localSessionId": compared["localSessionId"],
        "scannedAt": compared["scannedAt"],
        "purgedVps": compared.get("purgedVps") or 0,
        "pendingJobs": pending,
        "pendingCommands": pending_commands,
        "macAutoSync": False,
        "releasedItems": released,
        "downloadQueue": download_queue.snapshot_lite(),
        "concatQueue": concat_queue.snapshot_lite(),
        "downloadsPausedAck": bool(body.downloadsPaused),
    }


@app.get("/api/mac/helper/snapshot")
async def mac_helper_snapshot():
    """Latest helper scan for the Media page when the browser cannot reach localhost."""
    with _mac_state_lock:
        snap = dict(_mac_helper_snapshot)
    last_seen = float(snap.get("lastSeenAt") or 0)
    if not snap or (time.time() - last_seen) > MAC_HELPER_SNAPSHOT_TTL_SECONDS:
        return {"available": False}
    return {
        "available": True,
        "localSessionId": snap.get("localSessionId") or "",
        "files": snap.get("files") or [],
        "scannedAt": snap.get("scannedAt") or 0,
        "statuses": snap.get("statuses") or {},
        "synced": snap.get("synced") or 0,
        "incomplete": snap.get("incomplete") or 0,
        "notSynced": snap.get("notSynced") or 0,
        "totalVideos": snap.get("totalVideos") or 0,
        "purgedVps": snap.get("purgedVps") or 0,
    }


@app.get("/api/mac/helper/commands")
async def claim_mac_helper_commands(localSessionId: str):
    """Mac Helper polls this when the browser cannot reach localhost."""
    local_session_id = (localSessionId or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    return {"pendingCommands": _claim_ready_mac_commands(local_session_id)}


@app.post("/api/mac/helper/open")
async def queue_mac_helper_open(body: MacHelperOpenBody):
    """Queue a local open/reveal when Chrome cannot fetch 127.0.0.1:17899."""
    relative = str(body.relativePath or "").strip()
    recording_id = str(body.recordingId or "").strip()
    if not relative and not recording_id:
        raise HTTPException(status_code=400, detail="relativePath or recordingId required")
    queued = _queue_mac_helper_command(body.localSessionId, "open", {
        "relativePath": relative,
        "recordingId": recording_id,
        "reveal": bool(body.reveal),
    })
    queued["relativePath"] = relative
    queued["reveal"] = bool(body.reveal)
    return queued


@app.post("/api/mac/helper/delete")
async def queue_mac_helper_delete(body: MacHelperDeleteBody):
    """Queue Mac-folder deletes when Chrome cannot fetch 127.0.0.1:17899."""
    items = []
    for item in body.items or []:
        relative = str(item.relativePath or "").strip()
        recording_id = str(item.recordingId or "").strip()
        if not relative and not recording_id:
            continue
        items.append({"relativePath": relative, "recordingId": recording_id})
    if not items or len(items) > 100:
        raise HTTPException(status_code=400, detail="Select between 1 and 100 Mac videos")
    queued = _queue_mac_helper_command(body.localSessionId, "delete", {"items": items})
    queued["deletedCount"] = len(items)
    return queued


def _public_mac_import_result(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(entry.get("status") or "queued"),
        "success": entry.get("success"),
        "detail": entry.get("detail") or entry.get("error") or "",
        "sourceType": entry.get("sourceType") or "",
        "username": entry.get("username") or "",
        "loginUrl": entry.get("loginUrl") or "",
        "commandId": entry.get("commandId") or "",
    }


def _require_live_mac_helper(local_session_id: str) -> str:
    local_session_id = (local_session_id or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    with _mac_state_lock:
        snap = dict(_mac_helper_snapshot)
    last_seen = float(snap.get("lastSeenAt") or 0)
    if (
        not snap
        or (time.time() - last_seen) > MAC_HELPER_SNAPSHOT_TTL_SECONDS
        or not _mac_session_matches(str(snap.get("localSessionId") or ""), local_session_id)
    ):
        raise HTTPException(status_code=503, detail="Start the HXYLIVE Mac helper, then retry")
    return local_session_id


@app.post("/api/mac/helper/import-session")
async def queue_mac_helper_import_session(body: MacHelperImportSessionBody):
    """Queue a Chrome cookie import when the browser cannot reach localhost."""
    source_type = _normalize_source_type(body.sourceType) or ""
    if source_type not in MAC_HELPER_CHROME_IMPORT_SOURCES:
        raise HTTPException(status_code=400, detail="Chrome import is not available for this provider")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_login", False):
        raise HTTPException(status_code=400, detail=f"{provider.display_name}: session import unsupported")
    queued = _queue_mac_helper_command(body.localSessionId, "import-session", {
        "sourceType": source_type,
    })
    command_id = str(queued.get("commandId") or "")
    with _mac_state_lock:
        _mac_helper_import_results[command_id] = {
            "commandId": command_id,
            "localSessionId": (body.localSessionId or "").strip(),
            "sourceType": source_type,
            "status": "queued",
            "success": None,
            "detail": "",
            "username": "",
            "loginUrl": "",
            "expiresAt": time.time() + MAC_HELPER_IMPORT_TTL_SECONDS,
        }
    queued["sourceType"] = source_type
    return queued


@app.get("/api/mac/helper/import-session/{command_id}")
async def mac_helper_import_session_result(command_id: str):
    command_id = (command_id or "").strip()
    if not command_id or len(command_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid import command")
    _prune_mac_download_state()
    with _mac_state_lock:
        entry = dict(_mac_helper_import_results.get(command_id) or {})
    if not entry:
        raise HTTPException(status_code=404, detail="Import result expired; retry Import from Chrome")
    return _public_mac_import_result(entry)


@app.post("/api/mac/helper/provider-follow")
async def queue_mac_helper_provider_follow(body: MacHelperProviderFollowBody):
    """Queue Twitch (etc.) remote follow on Mac Helper — avoids browser→localhost CORS."""
    source_type = _normalize_source_type(body.sourceType) or ""
    if source_type != "twitch":
        raise HTTPException(status_code=400, detail="Mac Helper remote follow is only available for Twitch")
    username = str(body.username or "").strip().lstrip("@")
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    follow = bool(body.follow)
    queued = _queue_mac_helper_command(body.localSessionId, "provider-follow", {
        "sourceType": source_type,
        "username": username,
        "follow": follow,
    })
    command_id = str(queued.get("commandId") or "")
    with _mac_state_lock:
        _mac_helper_follow_results[command_id] = {
            "commandId": command_id,
            "localSessionId": (body.localSessionId or "").strip(),
            "sourceType": source_type,
            "username": username,
            "follow": follow,
            "status": "queued",
            "success": None,
            "detail": "",
            "expiresAt": time.time() + MAC_HELPER_IMPORT_TTL_SECONDS,
        }
    queued["sourceType"] = source_type
    queued["username"] = username
    queued["follow"] = follow
    return queued


@app.get("/api/mac/helper/provider-follow/{command_id}")
async def mac_helper_provider_follow_result(command_id: str):
    command_id = (command_id or "").strip()
    if not command_id or len(command_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid follow command")
    _prune_mac_download_state()
    with _mac_state_lock:
        entry = dict(_mac_helper_follow_results.get(command_id) or {})
    if not entry:
        raise HTTPException(status_code=404, detail="Follow result expired; retry Sync")
    return {
        "status": str(entry.get("status") or "queued"),
        "success": entry.get("success"),
        "detail": entry.get("detail") or entry.get("error") or "",
        "sourceType": entry.get("sourceType") or "",
        "username": entry.get("username") or "",
        "follow": bool(entry.get("follow", True)),
        "commandId": entry.get("commandId") or "",
    }


@app.post("/api/mac/helper/provider-follow-result")
async def mac_helper_provider_follow_result_report(body: MacHelperProviderFollowResultBody):
    """Mac Helper reports a completed remote follow/unfollow and updates local cache."""
    local_session_id = _require_live_mac_helper(body.localSessionId)
    command_id = (body.commandId or "").strip()
    if not command_id or len(command_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid follow command")
    source_type = _normalize_source_type(body.sourceType) or ""
    username = str(body.username or "").strip().lstrip("@")
    follow = bool(body.follow)
    success = bool(body.success)
    detail = str(body.detail or body.error or "").strip()
    with _mac_state_lock:
        entry = dict(_mac_helper_follow_results.get(command_id) or {})
    if not entry:
        raise HTTPException(status_code=404, detail="Follow command expired")
    if not _mac_session_matches(str(entry.get("localSessionId") or ""), local_session_id):
        raise HTTPException(status_code=403, detail="Follow command belongs to another local session")
    if success and username and source_type:
        provider = _provider_for(source_type)
        if follow:
            await _persist_local_follow(provider, source_type, username)
        else:
            await _apply_unfollow_keep_card(source_type, username)
    with _mac_state_lock:
        stored = _mac_helper_follow_results.get(command_id)
        if stored:
            stored["status"] = "done"
            stored["success"] = success
            stored["detail"] = detail if not success else (
                "Followed on site" if follow else "Unfollowed on site"
            )
            stored["expiresAt"] = time.time() + MAC_HELPER_IMPORT_TTL_SECONDS
    return {"success": True, "commandId": command_id, "remoteCompleted": True}


@app.post("/api/mac/helper/provider-session")
async def mac_helper_provider_session(body: MacHelperProviderSessionBody):
    """Accept Chrome cookies collected by the Mac Helper and import the provider session."""
    local_session_id = _require_live_mac_helper(body.localSessionId)
    command_id = (body.commandId or "").strip()
    if not command_id or len(command_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid import command")
    source_type = _normalize_source_type(body.sourceType) or ""
    if source_type not in MAC_HELPER_CHROME_IMPORT_SOURCES:
        raise HTTPException(status_code=400, detail="Chrome import is not available for this provider")
    _prune_mac_download_state()
    with _mac_state_lock:
        entry = dict(_mac_helper_import_results.get(command_id) or {})
    if not entry:
        raise HTTPException(status_code=404, detail="Import command expired")
    if not _mac_session_matches(str(entry.get("localSessionId") or ""), local_session_id):
        raise HTTPException(status_code=403, detail="Import command belongs to another local session")
    if str(entry.get("sourceType") or "") != source_type:
        raise HTTPException(status_code=400, detail="Import command source mismatch")

    helper_error = (body.error or "").strip()
    cookie_header = (body.cookieHeader or "").strip()
    cookies = body.cookies if isinstance(body.cookies, list) else None
    if body.success is False or helper_error or (not cookie_header and not cookies):
        detail = helper_error or "Log into this site in Google Chrome, then retry Import from Chrome"
        with _mac_state_lock:
            stored = _mac_helper_import_results.get(command_id)
            if stored:
                stored.update({
                    "status": "done",
                    "success": False,
                    "detail": detail,
                    "loginUrl": (body.loginUrl or "").strip(),
                    "expiresAt": time.time() + MAC_HELPER_IMPORT_TTL_SECONDS,
                })
        return {"success": False, "detail": detail}

    provider = _provider_for(source_type)
    try:
        result = await provider.import_session(
            username=(body.username or "").strip() or None,
            cookie_header=cookie_header or None,
            cookies=cookies,
            local_storage=[],
            user_agent=(body.userAgent or None),
        )
    except ProviderError as exc:
        result = {"success": False, "error": str(exc)}
    success = bool(result.get("success"))
    detail = "" if success else str(result.get("error") or "Session import failed")
    username = str(result.get("username") or body.username or "").strip()
    with _mac_state_lock:
        stored = _mac_helper_import_results.get(command_id)
        if stored:
            stored.update({
                "status": "done",
                "success": success,
                "detail": detail,
                "username": username,
                "expiresAt": time.time() + MAC_HELPER_IMPORT_TTL_SECONDS,
            })
    return {"success": success, "detail": detail, "username": username}


@app.post("/api/mac/helper/thumb")
async def upload_mac_helper_thumb(body: MacHelperThumbBody):
    """Accept a Mac-folder cover uploaded by the helper."""
    local_session_id = (body.localSessionId or "").strip()
    relative = str(body.relativePath or "").strip()
    recording_id = str(body.recordingId or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    if not relative and not recording_id:
        raise HTTPException(status_code=400, detail="relativePath or recordingId required")
    with _mac_state_lock:
        snap = dict(_mac_helper_snapshot)
    last_seen = float(snap.get("lastSeenAt") or 0)
    if (
        not snap
        or (time.time() - last_seen) > MAC_HELPER_SNAPSHOT_TTL_SECONDS
        or not _mac_session_matches(str(snap.get("localSessionId") or ""), local_session_id)
    ):
        raise HTTPException(status_code=503, detail="Start the HXYLIVE Mac helper, then retry")
    try:
        payload = base64.b64decode(body.imageBase64 or "", validate=False)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid thumbnail data")
    if not payload or len(payload) > MAC_HELPER_THUMB_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Invalid thumbnail data")
    content_type = str(body.contentType or "image/png").split(";")[0].strip().lower()
    if content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        content_type = "image/png"
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    _store_mac_helper_thumb(local_session_id, recording_id, relative, payload, content_type)
    return {"status": "ok"}


@app.get("/api/mac/helper/thumb")
async def get_mac_helper_thumb(localSessionId: str, relativePath: str = "", recordingId: str = ""):
    """Serve a cached Mac cover, queuing generation when the browser cannot reach localhost."""
    local_session_id = (localSessionId or "").strip()
    relative = str(relativePath or "").strip()
    recording_id = str(recordingId or "").strip()
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    if not relative and not recording_id:
        raise HTTPException(status_code=400, detail="relativePath or recordingId required")
    cached = _lookup_mac_helper_thumb(local_session_id, recording_id, relative)
    if cached:
        return Response(
            content=cached["bytes"],
            media_type=str(cached.get("contentType") or "image/png"),
            headers={"Cache-Control": "private, max-age=86400"},
        )
    _queue_mac_helper_thumb(local_session_id, relative, recording_id)
    deadline = time.time() + MAC_HELPER_THUMB_WAIT_SECONDS
    while time.time() < deadline:
        await asyncio.sleep(0.2)
        cached = _lookup_mac_helper_thumb(local_session_id, recording_id, relative)
        if cached:
            return Response(
                content=cached["bytes"],
                media_type=str(cached.get("contentType") or "image/png"),
                headers={"Cache-Control": "private, max-age=86400"},
            )
    raise HTTPException(status_code=404, detail="Thumbnail not ready")


@app.post("/api/mac/download-jobs")
async def create_mac_download_job(body: MacDownloadJobBody, request: Request):
    """Append selected fragments to the manual download queue (scheduler drains to Motrix)."""
    _prune_mac_download_state()
    local_session_id = (body.localSessionId or "").strip()
    requested_ids = list(dict.fromkeys(str(item_id).strip() for item_id in body.itemIds))
    requested_ids = [item_id for item_id in requested_ids if item_id]
    if not local_session_id or len(local_session_id) > 200:
        raise HTTPException(status_code=400, detail="Invalid local session")
    if not requested_ids or len(requested_ids) > 100:
        raise HTTPException(status_code=400, detail="Select between 1 and 100 videos")
    download_method = str(body.method or "chrome").strip().lower()
    if download_method not in {"chrome", "motrix"}:
        raise HTTPException(status_code=400, detail="method must be chrome or motrix")

    public_base = str(request.base_url).rstrip("/")
    _remember_mac_public_base(public_base)

    result = await _enqueue_manual_download_items(
        local_session_id=local_session_id,
        item_ids=requested_ids,
        method=download_method,
    )
    # Immediate drain for free slots so the Helper/browser can claim without waiting
    # for the next heartbeat (still capped by project/file concurrency).
    pending_jobs = await _drain_download_queue_to_jobs(
        local_session_id,
        public_base=public_base,
    )
    first_job = pending_jobs[0] if pending_jobs else None
    return {
        "status": "queued",
        "method": result.get("method") or download_method,
        "addedCount": result.get("addedCount") or 0,
        "skipped": result.get("skipped") or [],
        "itemCount": result.get("addedCount") or 0,
        "queue": result.get("queue") or {},
        "pendingJobs": [
            {
                "jobId": j.get("jobId"),
                "itemCount": len(j.get("items") or []),
                "method": j.get("method"),
                "status": j.get("status"),
                "expiresAt": j.get("expiresAt"),
            }
            for j in pending_jobs
        ],
        # Compatibility: first drained job id (empty when all slots busy).
        "jobId": str((first_job or {}).get("jobId") or ""),
        "expiresAt": int((first_job or {}).get("expiresAt") or 0),
    }


def _remember_mac_public_base(origin: Optional[str]) -> None:
    global _mac_download_public_base
    value = str(origin or "").strip().rstrip("/")
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return
    # Ignore loopback bases — Mac cannot download through the container's localhost.
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    with _mac_state_lock:
        _mac_download_public_base = f"{parsed.scheme}://{parsed.netloc}"


def _mac_public_base() -> str:
    with _mac_state_lock:
        remembered = str(_mac_download_public_base or "").strip()
    if remembered:
        return remembered
    return ""


def _mac_helper_online() -> tuple[bool, str]:
    """Return (online, localSessionId) from the latest helper heartbeat."""
    with _mac_state_lock:
        snap = dict(_mac_helper_snapshot)
    last_seen = float(snap.get("lastSeenAt") or 0)
    session_id = str(snap.get("localSessionId") or "").strip()
    if not session_id or (time.time() - last_seen) > MAC_HELPER_SNAPSHOT_TTL_SECONDS:
        return False, ""
    return True, session_id


async def _mac_autosync_enabled() -> bool:
    """Auto Sync removed: downloads are manual-only under recording projects."""
    return False


async def _recording_enabled() -> bool:
    """Global recording master switch. Does not change per-streamer autoRecord."""
    raw = await db.get_setting("recording_enabled")
    if raw is None:
        return True
    return str(raw).lower() in {"1", "true", "yes"}


async def _get_default_monthly_quota_gb() -> int:
    raw = await db.get_setting("default_monthly_quota_gb")
    try:
        return normalize_monthly_quota_gb(
            raw if raw is not None else DEFAULT_MONTHLY_QUOTA_GB,
            allow_none=False,
        )
    except ValueError:
        return DEFAULT_MONTHLY_QUOTA_GB


async def _get_quota_cycle_day() -> int:
    raw = await db.get_setting("quota_cycle_day")
    try:
        return normalize_quota_cycle_day(
            raw if raw is not None else DEFAULT_QUOTA_CYCLE_DAY
        )
    except ValueError:
        return DEFAULT_QUOTA_CYCLE_DAY


async def _quota_cycle_window() -> tuple[int, int]:
    cycle_day = await _get_quota_cycle_day()
    return quota_cycle_unix(cycle_day=cycle_day, tz_name=APP_TZ)


def _live_recording_bytes_by_person(
    statuses: Optional[list[dict]] = None,
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for status in statuses if statuses is not None else _all_recording_statuses():
        if not status.get("running"):
            continue
        person = str(status.get("person") or "").strip()
        if not person:
            continue
        try:
            written = int(status.get("bytes_written") or 0)
        except (TypeError, ValueError):
            written = 0
        if written <= 0:
            continue
        totals[person] = totals.get(person, 0) + written
    return totals


async def _recording_usage_bytes_map(
    usernames: Optional[list[str]] = None,
    *,
    include_live: bool = True,
) -> dict[str, int]:
    cycle_start, _cycle_end = await _quota_cycle_window()
    names = [
        str(name).strip()
        for name in (usernames or [])
        if str(name or "").strip()
    ]
    usage = await db.sum_recording_bytes_since(
        cycle_start,
        names if names else None,
    )
    if include_live:
        for person, written in _live_recording_bytes_by_person().items():
            if names and person not in names and person.lower() not in {
                n.lower() for n in names
            }:
                continue
            usage[person] = int(usage.get(person) or 0) + int(written)
    return usage


def _quota_fields_for_profile(
    *,
    username: str,
    stored_quota_gb: Optional[int],
    default_quota_gb: int,
    used_bytes: int,
    recording_enabled: bool,
    cycle_start: int,
    cycle_end: int,
) -> dict:
    effective_gb = effective_monthly_quota_gb(stored_quota_gb, default_quota_gb)
    limit_bytes = gb_to_bytes(effective_gb)
    exceeded = is_quota_exceeded(used_bytes, limit_bytes)
    reason = _quota_blocked_reason(
        recording_enabled=recording_enabled,
        quota_exceeded=exceeded,
    )
    return {
        "monthlyQuotaGb": stored_quota_gb,
        "monthly_quota_gb": stored_quota_gb,
        "monthlyQuotaGbEffective": effective_gb,
        "monthly_quota_gb_effective": effective_gb,
        "monthlyQuotaLimitBytes": limit_bytes,
        "monthly_quota_limit_bytes": limit_bytes,
        "monthlyQuotaUsedBytes": int(used_bytes or 0),
        "monthly_quota_used_bytes": int(used_bytes or 0),
        "quotaExceeded": exceeded,
        "quota_exceeded": exceeded,
        "quotaCycleStart": cycle_start,
        "quota_cycle_start": cycle_start,
        "quotaCycleEnd": cycle_end,
        "quota_cycle_end": cycle_end,
        "recordingAllowed": reason is None,
        "recording_allowed": reason is None,
        "recordingBlockedReason": reason,
        "recording_blocked_reason": reason,
    }


async def _profile_quota_snapshot(
    username: str,
    *,
    stored_quota_gb: Optional[int] = None,
    usage_map: Optional[dict[str, int]] = None,
    recording_enabled: Optional[bool] = None,
    default_quota_gb: Optional[int] = None,
    cycle_start: Optional[int] = None,
    cycle_end: Optional[int] = None,
) -> dict:
    if default_quota_gb is None:
        default_quota_gb = await _get_default_monthly_quota_gb()
    if recording_enabled is None:
        recording_enabled = await _recording_enabled()
    if cycle_start is None or cycle_end is None:
        cycle_start, cycle_end = await _quota_cycle_window()
    if usage_map is None:
        usage_map = await _recording_usage_bytes_map([username])
    used = int(usage_map.get(username) or usage_map.get(username.lower()) or 0)
    return _quota_fields_for_profile(
        username=username,
        stored_quota_gb=stored_quota_gb,
        default_quota_gb=default_quota_gb,
        used_bytes=used,
        recording_enabled=bool(recording_enabled),
        cycle_start=int(cycle_start),
        cycle_end=int(cycle_end),
    )


async def _assert_recording_allowed_for_person(
    person: str,
    *,
    stored_quota_gb: Optional[int] = None,
) -> None:
    """Raise 503 when global pause or monthly quota blocks a new capture."""
    person = str(person or "").strip()
    if not person:
        return
    snapshot = await _profile_quota_snapshot(person, stored_quota_gb=stored_quota_gb)
    reason = snapshot.get("recordingBlockedReason")
    if reason == "global_pause":
        raise HTTPException(
            status_code=503,
            detail=(
                "Recording is paused globally. Turn on Auto Record in the header "
                "to resume; per-streamer recording settings are unchanged."
            ),
        )
    if reason == "quota_exceeded":
        effective = snapshot.get("monthlyQuotaGbEffective")
        raise HTTPException(
            status_code=503,
            detail=(
                f"Monthly recording quota reached for {person} "
                f"({effective} GB this cycle). Auto-record preference is unchanged; "
                "recording resumes automatically next cycle or when the quota is raised."
            ),
        )


def _stop_running_recordings_for_person(
    person: str,
    reason: str,
    *,
    log_task: str = "auto-record",
    log_message: str = "Recording stopped",
) -> list[str]:
    person = str(person or "").strip()
    if not person:
        return []
    stopped: list[str] = []
    for status in _all_recording_statuses():
        if not status.get("running"):
            continue
        if str(status.get("person") or "").strip() != person:
            continue
        session_id = str(status.get("id") or "").strip()
        if not session_id:
            continue
        try:
            if manager.stop_session(session_id, reason=reason):
                stopped.append(session_id)
                logger.warning(
                    log_message,
                    task=log_task,
                    session_id=session_id,
                    person=person,
                    reason=reason,
                )
        except Exception as exc:
            logger.error(
                "Failed to stop recording",
                task=log_task,
                session_id=session_id,
                person=person,
                reason=reason,
                error=str(exc),
            )
    return stopped


async def _enforce_quota_on_running_sessions() -> list[str]:
    """Stop active captures that have crossed their monthly quota this cycle."""
    statuses = [s for s in _all_recording_statuses() if s.get("running")]
    if not statuses:
        return []

    people = sorted({
        str(s.get("person") or "").strip()
        for s in statuses
        if str(s.get("person") or "").strip()
    })
    if not people:
        return []

    default_quota_gb = await _get_default_monthly_quota_gb()
    cycle_start, cycle_end = await _quota_cycle_window()
    usage_map = await _recording_usage_bytes_map(people, include_live=True)

    quota_by_person: dict[str, Optional[int]] = {}
    sources = await db.get_all_media_profile_sources()
    for source in sources:
        profile = str(source.get("profile_username") or "").strip()
        if profile and profile in people and profile not in quota_by_person:
            quota_by_person[profile] = source.get("monthly_quota_gb")
    for person in people:
        if person in quota_by_person:
            continue
        model = await db.get_model(person)
        if model is not None:
            quota_by_person[person] = model.get("monthly_quota_gb")

    stopped: list[str] = []
    for person in people:
        fields = _quota_fields_for_profile(
            username=person,
            stored_quota_gb=quota_by_person.get(person),
            default_quota_gb=default_quota_gb,
            used_bytes=int(usage_map.get(person) or 0),
            recording_enabled=True,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        if not fields["quotaExceeded"]:
            continue
        stopped.extend(
            _stop_running_recordings_for_person(
                person,
                "quota_exceeded",
                log_task="auto-record",
                log_message="Recording stopped: monthly quota exceeded",
            )
        )
    return stopped


async def _mac_autosync_method() -> str:
    raw = await db.get_setting("mac_auto_sync_method")
    method = str(raw or "motrix").strip().lower()
    return method if method in {"chrome", "motrix"} else "motrix"


def _library_item_id_for_path(media_path: Path) -> Optional[tuple[str, str, str]]:
    """Return (item_id, folder_name, relative_path) for a records/* media file."""
    try:
        records_root = (OUTPUT_DIR / "records").resolve()
        path = media_path.resolve()
    except OSError:
        return None
    if not path.is_relative_to(records_root):
        return None
    rel = path.relative_to(records_root)
    if len(rel.parts) < 2:
        return None
    folder_name = rel.parts[0]
    relative_path = Path(*rel.parts[1:]).as_posix()
    item_id = hashlib.sha256(f"{folder_name}\0{relative_path}".encode("utf-8")).hexdigest()[:16]
    return item_id, folder_name, relative_path


async def _create_mac_download_job_internal(
    *,
    local_session_id: str,
    item_ids: list[str],
    method: str,
    public_base: str,
    require_all: bool = False,
) -> dict[str, Any]:
    """Build a download job into in-memory state (manual Media queue)."""
    _prune_mac_download_state()
    requested_ids = list(dict.fromkeys(str(item_id).strip() for item_id in item_ids if str(item_id).strip()))
    if not local_session_id or not requested_ids:
        raise HTTPException(status_code=400, detail="Invalid download job request")
    if len(requested_ids) > 100:
        raise HTTPException(status_code=400, detail="Select between 1 and 100 videos")
    download_method = str(method or "chrome").strip().lower()
    if download_method not in {"chrome", "motrix"}:
        raise HTTPException(status_code=400, detail="method must be chrome or motrix")
    base = str(public_base or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Public download base URL is not known yet")

    library_items = {
        str(item.get("id")): item
        for item in await _scan_media_library_items(refresh_metadata=False)
        if item.get("type") == "video"
    }
    missing = [item_id for item_id in requested_ids if item_id not in library_items]
    if missing and require_all:
        raise HTTPException(status_code=404, detail=f"Media items not found: {', '.join(missing[:3])}")

    expires_at = time.time() + MAC_DOWNLOAD_LINK_TTL_SECONDS
    entries = []
    used_download_paths: set = set()
    folder_label_cache: dict[str, str] = {}
    for item_id in requested_ids:
        item = library_items.get(item_id)
        if not item:
            continue
        file_path = _resolve_library_media_path(item["username"], item["relativePath"])
        if not file_path.exists() or not file_path.is_file():
            if require_all:
                raise HTTPException(status_code=404, detail=f"Media file missing: {item['filename']}")
            continue
        if not require_all:
            try:
                _assert_media_not_active(str(item.get("username") or ""), file_path)
            except HTTPException:
                continue
        token = secrets.token_urlsafe(32)
        recording_id = str(item.get("recordingId") or item_id)
        source_name = str(item.get("filename") or file_path.name)
        chrome_filename = _mac_download_chrome_filename(recording_id, source_name)
        profile_username = str(item.get("username") or "")
        source_type = await _primary_source_type_for_profile(profile_username)
        if profile_username not in folder_label_cache:
            display_name = await _mac_download_display_name(profile_username)
            folder_label_cache[profile_username] = _mac_download_folder_label(
                profile_username, display_name
            )
        project_id = await _project_id_for_library_item(profile_username, source_name)
        relative_path = _mac_download_relative_path(
            folder_label_cache[profile_username],
            source_name,
            int(item.get("createdAt") or 0),
            used_download_paths,
            source_type=source_type,
            project_id=project_id,
        )
        _mac_download_tokens[token] = {
            "filePath": str(file_path),
            "filename": chrome_filename,
            "expiresAt": expires_at,
        }
        entries.append({
            "itemId": item_id,
            "recordingId": recording_id,
            "username": profile_username,
            "sourceType": source_type,
            "projectId": project_id,
            "downloadFilename": chrome_filename,
            "filename": chrome_filename,
            "relativePath": relative_path,
            "size": int(item.get("size") or 0),
            "url": f"{base}/api/mac/download/{token}",
        })

    if not entries:
        raise HTTPException(status_code=404, detail="No downloadable media items")

    job_id = secrets.token_urlsafe(18)
    job = {
        "jobId": job_id,
        "localSessionId": local_session_id,
        "status": "ready",
        "method": download_method,
        "createdAt": int(time.time()),
        "expiresAt": expires_at,
        "items": entries,
        "autoSync": False,
    }
    with _mac_state_lock:
        _mac_download_jobs[job_id] = job
    return {
        "jobId": job_id,
        "status": "ready",
        "method": download_method,
        "itemCount": len(entries),
        "expiresAt": int(expires_at),
        "items": entries,
    }


async def _on_mp4_ready_for_mac_sync(
    username: str,
    mp4_path: Path,
    recording_id: Optional[str] = None,
) -> None:
    """Convert-complete hook: assign MP4 into a recording project (no auto-download)."""
    from app.services import recording_projects as recording_projects_svc

    recording_audit.emit(
        "mp4_ready",
        username=username,
        path=str(mp4_path),
        recordingId=recording_id,
    )
    path = Path(mp4_path)
    mapped = _library_item_id_for_path(path)
    item_id = mapped[0] if mapped else None
    try:
        size_bytes = int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        size_bytes = 0
    duration = 0
    created_at = 0
    try:
        rows = await db.get_recordings(username)
        for row in rows or []:
            if str(row.get("recording_id") or "") == str(recording_id or "") or Path(
                str(row.get("mp4_path") or row.get("file_path") or "")
            ).name == path.name:
                duration = int(row.get("duration_seconds") or 0)
                created_at = int(row.get("created_at") or 0)
                size_bytes = size_bytes or int(row.get("mp4_size") or row.get("file_size") or 0)
                break
    except Exception:
        pass
    source_type = await _primary_source_type_for_profile(username)
    try:
        project = await recording_projects_svc.assign_fragment_to_project(
            db,
            username=username,
            filename=path.name,
            recording_id=recording_id,
            item_id=item_id,
            started_at=created_at,
            duration_seconds=duration,
            size_bytes=size_bytes,
            source_type=source_type,
        )
        logger.info(
            "MP4 assigned to recording project",
            username=username,
            mp4=path.name,
            project_id=(project or {}).get("project_id"),
        )
    except Exception as exc:
        logger.warning(
            "Recording project assign failed",
            username=username,
            mp4=path.name,
            error=str(exc),
        )


async def _should_pause_convert_for_disk() -> bool:
    return bool(_disk_convert_paused)


def _disk_blocks_new_recordings() -> bool:
    return bool(_library_space_blocked or _staging_blocked)


def _recording_block_detail() -> str:
    if _library_space_blocked:
        return (
            "Library disk (HDD) free space is below the configured threshold. "
            "Auto Record is off until you free space and turn it back on."
        )
    if _staging_blocked:
        return (
            "Staging disk (SSD) is critically low. Captures are paused while "
            "finished recordings are moved to the library disk."
        )
    return "Recording is temporarily blocked by storage policy."


def _stop_all_running_recordings(
    reason: str,
    *,
    log_task: str = "recording",
    log_message: str = "Recording stopped",
) -> list[str]:
    """Stop every running capture without changing per-streamer autoRecord."""
    stopped: list[str] = []
    for status in _all_recording_statuses():
        if not status.get("running"):
            continue
        session_id = str(status.get("id") or "").strip()
        if not session_id:
            continue
        try:
            if manager.stop_session(session_id, reason=reason):
                stopped.append(session_id)
                logger.warning(
                    log_message,
                    task=log_task,
                    session_id=session_id,
                    person=status.get("person"),
                    reason=reason,
                )
        except Exception as exc:
            logger.error(
                "Failed to stop recording",
                task=log_task,
                session_id=session_id,
                reason=reason,
                error=str(exc),
            )
    return stopped


def _active_media_path_keys() -> set[str]:
    keys: set[str] = set()
    for status in _all_recording_statuses():
        if not status.get("running"):
            continue
        record_path = status.get("record_path") or ""
        key = _resolved_path_key(record_path)
        if key:
            keys.add(key)
    return keys


async def _get_staging_stop_gb() -> int:
    raw = await db.get_setting("staging_stop_gb")
    return normalize_staging_stop_gb(
        raw if raw is not None else DEFAULT_STAGING_STOP_GB
    )


async def _get_library_stop_gb() -> int:
    raw = await db.get_setting("library_stop_gb")
    return normalize_library_stop_gb(
        raw if raw is not None else DEFAULT_LIBRARY_STOP_GB
    )


def _promote_staging_once() -> list[str]:
    """Move closed staging TS files onto the library volume."""
    moved = promote_closed_staging_files(
        staging_records_root=_staging_records_root(),
        library_records_root=_records_root(),
        active_path_keys=_active_media_path_keys(),
        path_key_fn=lambda src: _resolved_path_key(str(src)) or "",
    )
    return [str(path) for path in moved]


async def _storage_policy_pass_once() -> dict[str, Any]:
    """Promote staging files, then enforce library/staging recording gates."""
    global _library_space_blocked, _staging_blocked, _disk_convert_paused

    try:
        recording_audit.refresh_busy_snapshot(manager.list_status())
    except Exception:
        pass

    promoted = await asyncio.to_thread(_promote_staging_once)
    if promoted:
        logger.info(
            "Staging files promoted to library",
            task="storage-policy",
            count=len(promoted),
        )

    staging_stop_gb = await _get_staging_stop_gb()
    library_stop_gb = await _get_library_stop_gb()
    library_free = disk_free_bytes(OUTPUT_DIR)
    staging_free = disk_free_bytes(STAGING_DIR)
    has_staging_files = staging_has_recording_files(_staging_records_root())

    decision = storage_gate_decision(
        library_free_bytes=library_free,
        staging_free_bytes=staging_free,
        library_stop_bytes=library_stop_gb * DISK_GB,
        staging_stop_bytes=staging_stop_gb * DISK_GB,
        library_blocked=_library_space_blocked,
        staging_blocked=_staging_blocked,
        staging_has_files=has_staging_files,
    )

    was_library_blocked = _library_space_blocked
    was_staging_blocked = _staging_blocked
    _library_space_blocked = decision.library_blocked
    _staging_blocked = decision.staging_blocked
    _disk_convert_paused = decision.pause_convert

    stopped: list[str] = []

    if _library_space_blocked and not was_library_blocked:
        await db.set_setting("recording_enabled", "false")
        stopped.extend(
            _stop_all_running_recordings(
                "library_disk_low",
                log_task="storage-policy",
                log_message="Recording stopped: library disk below threshold",
            )
        )
        logger.warning(
            "Library disk gate engaged; Auto Record turned off",
            task="storage-policy",
            free_bytes=library_free,
            free_formatted=format_bytes(library_free),
            threshold_gb=library_stop_gb,
        )
    elif was_library_blocked and not _library_space_blocked:
        logger.info(
            "Library disk gate cleared; Auto Record stays off until manually enabled",
            task="storage-policy",
            free_bytes=library_free,
            free_formatted=format_bytes(library_free),
            threshold_gb=library_stop_gb,
        )

    if _staging_blocked and not was_staging_blocked:
        stopped.extend(
            _stop_all_running_recordings(
                "staging_disk_low",
                log_task="storage-policy",
                log_message="Recording stopped: staging disk below threshold",
            )
        )
        logger.warning(
            "Staging disk gate engaged; clearing hot TS to library",
            task="storage-policy",
            free_bytes=staging_free,
            free_formatted=format_bytes(staging_free),
            threshold_gb=staging_stop_gb,
        )
        # Promote again immediately after stopping writers.
        more = await asyncio.to_thread(_promote_staging_once)
        if more:
            promoted.extend(more)
            has_staging_files = staging_has_recording_files(_staging_records_root())
            if not has_staging_files:
                _staging_blocked = False
                logger.info(
                    "Staging cleared immediately after stop; gate released",
                    task="storage-policy",
                )
    elif was_staging_blocked and not _staging_blocked:
        logger.info(
            "Staging disk gate cleared; Auto Record intent unchanged",
            task="storage-policy",
            free_bytes=staging_free,
            free_formatted=format_bytes(staging_free),
        )

    recording_enabled = await _recording_enabled()
    return {
        "libraryFreeBytes": library_free,
        "stagingFreeBytes": staging_free,
        "libraryBlocked": _library_space_blocked,
        "stagingBlocked": _staging_blocked,
        "pauseConvert": _disk_convert_paused,
        "recordingEnabled": recording_enabled,
        "promoted": len(promoted),
        "stoppedSessions": len(stopped),
        "stagingStopGb": staging_stop_gb,
        "libraryStopGb": library_stop_gb,
        "stagingHasFiles": has_staging_files,
    }


async def disk_pressure_task():
    """Promote staging TS to library and enforce dual-disk recording gates."""
    logger.info(
        "Storage policy task started",
        task="storage-policy",
        poll_seconds=STORAGE_POLL_SECONDS,
        staging_dir=str(STAGING_DIR),
        library_dir=str(OUTPUT_DIR),
        default_staging_stop_gb=DEFAULT_STAGING_STOP_GB,
        default_library_stop_gb=DEFAULT_LIBRARY_STOP_GB,
    )
    try:
        await _storage_policy_pass_once()
    except Exception as exc:
        logger.error(
            "Storage policy initial pass error",
            task="storage-policy",
            error=str(exc),
            exc_info=True,
        )

    while True:
        try:
            stressed = (
                _library_space_blocked
                or _staging_blocked
                or _disk_convert_paused
                or staging_has_recording_files(_staging_records_root())
            )
            await asyncio.sleep(
                STORAGE_POLL_STRESSED_SECONDS if stressed else STORAGE_POLL_SECONDS
            )
            pressure = await _storage_policy_pass_once()
            if (
                pressure.get("promoted")
                or pressure.get("libraryBlocked")
                or pressure.get("stagingBlocked")
                or pressure.get("stoppedSessions")
            ):
                logger.info("Storage policy pass", task="storage-policy", **pressure)
        except Exception as exc:
            logger.error(
                "Storage policy task error",
                task="storage-policy",
                error=str(exc),
                exc_info=True,
            )
            await asyncio.sleep(STORAGE_POLL_STRESSED_SECONDS)


@app.get("/api/mac/download-jobs/{job_id}")
async def get_mac_download_job(job_id: str, localSessionId: str):
    return _claim_mac_download_job(job_id, localSessionId)


@app.get("/api/mac/download/{token}")
async def nginx_mac_download(token: str):
    """Authorize a short-lived link, then let Nginx transfer the file."""
    _prune_mac_download_state()
    entry = _mac_download_tokens.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Download link not found or expired")
    file_path = Path(str(entry["filePath"])).resolve()
    records_root = (OUTPUT_DIR / "records").resolve()
    if not file_path.is_relative_to(records_root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Download file is unavailable")
    relative_path = file_path.relative_to(records_root).as_posix()
    return Response(
        status_code=200,
        headers={
            "X-Accel-Redirect": f"/_protected_recordings/{quote(relative_path, safe='/')}",
            "Content-Disposition": _content_disposition(
                str(entry["filename"]),
                disposition="attachment",
            ),
            "Content-Type": "application/octet-stream",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/media-profiles/{username}")
async def get_media_profile(username: str):
    """Return the enriched profile card and stream settings for a media profile."""
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    recordings = await db.get_recordings(username)
    if not profile_dir.exists() and not profile and not model and not recordings:
        raise HTTPException(status_code=404, detail="Profile not found")
    return await _media_profile_payload(username)


@app.get("/api/media-profiles/{username}/profile-image")
async def get_media_profile_image(username: str):
    """Return the dedicated vertical image for a media profile."""
    username = _validate_media_profile_username(username)
    profile = await db.get_media_profile(username)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile image not found")

    image_path = Path(profile.get("profile_image_path") or "")
    if (
        not image_path
        or not image_path.exists()
        or not image_path.is_file()
        or not _path_is_inside_output(image_path)
    ):
        raise HTTPException(status_code=404, detail="Profile image not found")

    media_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    return FileResponse(str(image_path), media_type=media_type)


@app.post("/api/media-profiles/{username}/profile-image/resolve")
async def resolve_media_profile_image(username: str, body: dict):
    """Fetch and cache a vertical profile image from Babepedia or an image URL."""
    username = _validate_media_profile_username(username)
    existing_profile = await db.get_media_profile(username) or {"username": username}
    resolved = await _resolve_profile_image_from_babepedia(username, existing_profile, body or {})
    image_url = _normalize_profile_image_url(resolved["imageUrl"])
    source_url = _normalize_profile_source_url(resolved.get("sourceUrl") or image_url)
    downloaded = await _download_profile_image(username, image_url)

    old_path = existing_profile.get("profile_image_path")
    if old_path and old_path != downloaded["path"]:
        try:
            path = Path(old_path)
            if path.exists() and path.is_file() and _path_is_inside_output(path):
                path.unlink()
        except OSError as e:
            logger.debug("Old profile image not deleted", username=username, error=str(e))

    await db.upsert_media_profile(username, {
        **existing_profile,
        "profile_image_url": image_url,
        "profile_image_source_url": source_url,
        "profile_image_path": downloaded["path"],
    })
    return {
        "success": True,
        "resolved": {
            "imageUrl": image_url,
            "sourceUrl": source_url,
            "size": downloaded["size"],
            "contentType": downloaded["contentType"],
        },
        "profile": await _media_profile_payload(username),
    }


@app.put("/api/media-profiles/{username}")
async def update_media_profile(username: str, body: dict):
    """Update a profile's local info and stream settings."""
    body = body or {}
    username = _validate_media_profile_username(username)
    existing_profile = await db.get_media_profile(username) or {"username": username}
    raw_stream_sources = body.get("streamSources") if "streamSources" in body else body.get("stream_sources")
    has_stream_sources = isinstance(raw_stream_sources, list)
    requested_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    ) or "chaturbate"
    if requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' is not available",
        )

    profile_data = {
        "display_name": body.get("displayName") or body.get("display_name") or "",
        "first_name": body.get("firstName") or body.get("first_name") or "",
        "last_name": body.get("lastName") or body.get("last_name") or "",
        "age": body.get("age"),
        "birth_date": _normalize_birth_date(body.get("birthDate") or body.get("birth_date")),
        "address": body.get("address") or "",
        "city": body.get("city") or "",
        "region": body.get("region") or "",
        "postal_code": body.get("postalCode") or body.get("postal_code") or "",
        "country": body.get("country") or "",
        "aliases": body.get("aliases") or "",
        "tags": body.get("tags") or "",
        "notes": body.get("notes") or "",
        "social_urls": body.get("socialUrls") or body.get("social_urls") or [],
        "stream_urls": body.get("streamUrls") or body.get("stream_urls") or [],
        "profile_urls": body.get("profileUrls") or body.get("profile_urls") or [],
        **_profile_image_update_from_body(body, existing_profile),
    }
    await db.upsert_media_profile(username, profile_data)

    if "gapBufferMinutes" in body or "gap_buffer_minutes" in body:
        from app.services.recording_projects import ALLOWED_GAP_BUFFER_MINUTES

        raw_gap = body.get("gapBufferMinutes") if "gapBufferMinutes" in body else body.get("gap_buffer_minutes")
        if raw_gap is None or str(raw_gap).strip() == "":
            await db.set_setting(f"gap_buffer_minutes:{username}", "")
        else:
            try:
                gap_minutes = int(raw_gap)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"gap_buffer_minutes must be one of {sorted(ALLOWED_GAP_BUFFER_MINUTES)} or empty",
                )
            if gap_minutes not in ALLOWED_GAP_BUFFER_MINUTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"gap_buffer_minutes must be one of {sorted(ALLOWED_GAP_BUFFER_MINUTES)} or empty",
                )
            await db.set_setting(f"gap_buffer_minutes:{username}", str(gap_minutes))

    if has_stream_sources:
        normalized_sources = []
        seen_sources: set[tuple[str, str]] = set()
        for raw_source in raw_stream_sources:
            if not isinstance(raw_source, dict):
                continue
            normalized = await _normalize_profile_source_payload(username, raw_source, default_auto_record=False)
            key = (normalized["source_type"], normalized["channel_username"].lower())
            if key in seen_sources:
                continue
            seen_sources.add(key)
            normalized_sources.append(normalized)

        saved_sources = await db.replace_media_profile_sources(username, normalized_sources)
        for source in saved_sources:
            await db.add_or_update_model(
                username=source["channel_username"],
                display_name=profile_data["display_name"] or source["channel_username"],
                auto_record=bool(source.get("auto_record", False)),
                record_quality=source.get("record_quality") or await _get_default_record_quality(),
                retention_days=int(source.get("retention_days") if source.get("retention_days") is not None else await _get_default_retention_days()),
                source_type=source.get("source_type") or "chaturbate",
                monthly_quota_gb=source.get("monthly_quota_gb"),
            )

        return {
            "success": True,
            "profile": await _media_profile_payload(username),
        }

    existing = await db.get_model(username, source_type=requested_source)
    retention_days = _normalize_retention_days(
        body.get("retentionDays"),
        (existing or {}).get("retention_days", await _get_default_retention_days()),
    )
    record_quality = (
        body.get("recordQuality")
        or (existing or {}).get("record_quality")
        or await _get_default_record_quality()
    )
    auto_record = bool(body.get("autoRecord", (existing or {}).get("auto_record", False)))
    if "monthlyQuotaGb" in body or "monthly_quota_gb" in body:
        try:
            monthly_quota_gb = normalize_monthly_quota_gb(
                body.get("monthlyQuotaGb") if "monthlyQuotaGb" in body else body.get("monthly_quota_gb"),
                allow_none=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        monthly_quota_gb = (existing or {}).get("monthly_quota_gb")
    if "recordPath" in body or "record_path" in body:
        record_path = _normalize_record_path(
            body.get("recordPath") or body.get("record_path"),
            username,
        )
    else:
        record_path = (existing or {}).get("record_path")

    await db.add_or_update_model(
        username=username,
        display_name=profile_data["display_name"] or username,
        auto_record=auto_record,
        record_quality=record_quality,
        retention_days=retention_days,
        record_path=record_path,
        source_type=requested_source,
        monthly_quota_gb=monthly_quota_gb,
    )

    normalized_source = await _normalize_profile_source_payload(
        username,
        {
            "sourceType": requested_source,
            "channelUsername": username,
            "autoRecord": auto_record,
            "recordQuality": record_quality,
            "retentionDays": retention_days,
            "monthlyQuotaGb": monthly_quota_gb,
            "recordPath": record_path or _default_record_path(username),
        },
        default_auto_record=auto_record,
    )
    await db.upsert_media_profile_source(**normalized_source)

    return {
        "success": True,
        "profile": await _media_profile_payload(username),
    }


@app.post("/api/media-profiles/link-live")
async def link_live_to_media_profile(body: dict):
    """Link the current live to an existing or newly created Media profile."""
    body = body or {}
    live_username = _normalize_live_channel_username(
        body.get("liveUsername")
        or body.get("live_username")
        or body.get("channelUsername")
        or body.get("channel_username")
        or body.get("target"),
        body.get("channelUrl") or body.get("channel_url") or body.get("url"),
    )
    source_type = _normalize_source_type(
        body.get("sourceType")
        or body.get("source_type")
        or _source_type_from_url(str(body.get("channelUrl") or body.get("channel_url") or ""))
        or "chaturbate"
    ) or "chaturbate"
    if source_type not in _available_source_types():
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is not available")

    create_profile = bool(body.get("createProfile") or body.get("create_profile"))
    if create_profile:
        raise HTTPException(
            status_code=400,
            detail="Create profile is disabled. Follow the streamer first to create a card, then enable recording.",
        )
    profile_username = _validate_media_profile_username(
        body.get("profileUsername")
        or body.get("profile_username")
        or ""
    )
    existing_profile = await db.get_media_profile(profile_username)
    profile_dir = _media_profile_dir(profile_username)
    if not create_profile and not existing_profile and not profile_dir.exists():
        raise HTTPException(status_code=404, detail="Profile not found")

    display_name = (
        body.get("displayName")
        or body.get("display_name")
        or (existing_profile or {}).get("display_name")
        or profile_username
    )
    await db.upsert_media_profile(profile_username, {
        **(existing_profile or {}),
        "display_name": display_name,
        "first_name": (existing_profile or {}).get("first_name"),
        "last_name": (existing_profile or {}).get("last_name"),
        "age": (existing_profile or {}).get("age"),
        "birth_date": (existing_profile or {}).get("birth_date"),
        "address": (existing_profile or {}).get("address"),
        "city": (existing_profile or {}).get("city"),
        "region": (existing_profile or {}).get("region"),
        "postal_code": (existing_profile or {}).get("postal_code"),
        "country": (existing_profile or {}).get("country"),
        "aliases": (existing_profile or {}).get("aliases"),
        "tags": (existing_profile or {}).get("tags"),
        "notes": (existing_profile or {}).get("notes"),
        "social_urls": (existing_profile or {}).get("social_urls") or [],
        "stream_urls": list(dict.fromkeys([
            *((existing_profile or {}).get("stream_urls") or []),
            _canonical_stream_url(source_type, live_username, body.get("channelUrl") or body.get("channel_url")),
        ])),
        "profile_urls": (existing_profile or {}).get("profile_urls") or [],
        "profile_image_url": (existing_profile or {}).get("profile_image_url"),
        "profile_image_source_url": (existing_profile or {}).get("profile_image_source_url"),
        "profile_image_path": (existing_profile or {}).get("profile_image_path"),
    })

    source_payload = await _normalize_profile_source_payload(
        profile_username,
        {
            "sourceType": source_type,
            "channelUsername": live_username,
            "channelUrl": body.get("channelUrl") or body.get("channel_url") or _canonical_stream_url(source_type, live_username),
            "autoRecord": body.get("autoRecord", body.get("auto_record", False)),
            "recordQuality": body.get("recordQuality") or body.get("record_quality") or await _get_default_record_quality(),
            "retentionDays": body.get("retentionDays") if "retentionDays" in body else body.get("retention_days", await _get_default_retention_days()),
            "recordPath": body.get("recordPath") or body.get("record_path") or _default_record_path(profile_username),
        },
        default_auto_record=False,
    )
    saved_source = await db.upsert_media_profile_source(**source_payload)
    want_record = bool(source_payload.get("auto_record"))
    await db.add_or_update_model(
        username=source_payload["channel_username"],
        display_name=display_name,
        auto_record=want_record,
        record_quality=source_payload["record_quality"],
        retention_days=source_payload["retention_days"],
        source_type=source_payload["source_type"],
        monthly_quota_gb=source_payload.get("monthly_quota_gb"),
    )

    return {
        "success": True,
        "source": _profile_source_response(saved_source),
        "profile": await _media_profile_payload(profile_username),
        "recordingEnabled": want_record,
    }


@app.patch("/api/media-profiles/{username}/auto-record")
async def toggle_media_profile_auto_record(username: str, body: dict):
    """Enable or suspend automatic recording for all sources."""
    body = body or {}
    username = _validate_media_profile_username(username)
    profile = await db.get_media_profile(username)
    sources = await db.get_media_profile_sources(username)
    if "autoRecord" not in body and "auto_record" not in body:
        raise HTTPException(status_code=400, detail="autoRecord field required")

    enabled = bool(body.get("autoRecord", body.get("auto_record")))
    body_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    )
    body_channel = str(
        body.get("channelUsername") or body.get("channel_username") or ""
    ).strip()
    body_url = body.get("channelUrl") or body.get("channel_url")
    preferred_source = body_source or "chaturbate"
    channel_username = body_channel or username
    channel_url = body_url

    if not profile and not sources:
        if not enabled:
            raise HTTPException(status_code=404, detail="Profile not found")
        # Media may show Mac-local-only cards before a VPS profile exists.
        # Bootstrap a card when turning recording on (source hint from client/follow).
        preferred_source = body_source or "chaturbate"
        channel_username = body_channel or username
        channel_url = body_url or _canonical_stream_url(preferred_source, channel_username)
        display_name = (
            body.get("displayName")
            or body.get("display_name")
            or username
        )
        profile_image_url = body.get("profileImageUrl") or body.get("profile_image_url")
        followed = await db.get_all_followed()
        for item in followed or []:
            channel = str(item.get("username") or "").strip()
            if channel.lower() != username.strip().lower():
                continue
            preferred_source = _normalize_source_type(item.get("source_type")) or preferred_source
            channel_username = channel
            display_name = item.get("display_name") or display_name
            profile_image_url = item.get("profile_image_url") or profile_image_url
            break
        if preferred_source not in _available_source_types():
            raise HTTPException(
                status_code=400,
                detail=f"Source '{preferred_source}' is not available",
            )
        await ensure_streamer_card_for_follow(
            db,
            username,
            preferred_source,
            display_name=display_name,
            profile_image_url=profile_image_url,
            channel_url=channel_url,
        )
        if channel_username.lower() != username.lower() or channel_url:
            await db.upsert_media_profile_source(
                profile_username=username,
                source_type=preferred_source,
                channel_username=channel_username,
                channel_url=channel_url,
                auto_record=False,
                record_quality="best",
                retention_days=30,
                record_path=None,
            )
        profile = await db.get_media_profile(username)
        sources = await db.get_media_profile_sources(username)
        if not profile and not sources:
            raise HTTPException(status_code=404, detail="Profile not found")

    if enabled:
        # Discover/Watch/Media pass source hints — prefer them so a Twitch card
        # is not bootstrapped/matched as chaturbate when no matching source exists.
        if body_source:
            preferred_source = body_source
            channel_username = body_channel or username
            channel_url = body_url or _canonical_stream_url(preferred_source, channel_username)
        else:
            preferred_source = "chaturbate"
            channel_username = username
            channel_url = None
            for source in sources or []:
                channel = str(source.get("channel_username") or "").strip()
                st = _normalize_source_type(source.get("source_type")) or preferred_source
                if channel:
                    channel_username = channel
                    preferred_source = st
                    channel_url = source.get("channel_url")
                    break
            if not sources:
                followed = await db.get_all_followed()
                for item in followed or []:
                    channel = str(item.get("username") or "").strip()
                    if channel.lower() != username.strip().lower():
                        continue
                    preferred_source = _normalize_source_type(item.get("source_type")) or preferred_source
                    channel_username = channel
                    break
        if preferred_source not in _available_source_types():
            raise HTTPException(
                status_code=400,
                detail=f"Source '{preferred_source}' is not available",
            )
        if not channel_url:
            channel_url = _canonical_stream_url(preferred_source, channel_username)
        # Cards may lack media_profile_sources rows; UPDATE would no-op.
        await ensure_streamer_card_for_follow(
            db,
            username,
            preferred_source,
            display_name=(
                body.get("displayName")
                or body.get("display_name")
                or (profile or {}).get("display_name")
                or username
            ),
            profile_image_url=(
                body.get("profileImageUrl")
                or body.get("profile_image_url")
                or (profile or {}).get("profile_image_url")
            ),
            channel_url=channel_url,
        )
        await db.upsert_media_profile_source(
            profile_username=username,
            source_type=preferred_source,
            channel_username=channel_username,
            channel_url=channel_url,
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path=None,
        )
        sources = await db.get_media_profile_sources(username)

    toggle_source = preferred_source if body_source else None
    toggle_channel = (channel_username or username) if body_source else None

    updated_sources = await db.set_media_profile_auto_record(
        username,
        enabled,
        source_type=toggle_source,
        channel_username=toggle_channel,
    )
    if enabled and not updated_sources:
        raise HTTPException(
            status_code=500,
            detail="Could not enable recording because this streamer card has no stream source.",
        )
    if enabled and toggle_source:
        matched = [
            source for source in updated_sources
            if _normalize_source_type(source.get("source_type")) == toggle_source
            and (
                not toggle_channel
                or str(source.get("channel_username") or "").strip().lower()
                == toggle_channel.lower()
            )
            and bool(source.get("auto_record"))
        ]
        if not matched:
            raise HTTPException(
                status_code=500,
                detail="Could not enable recording for the requested stream source.",
            )
    if not updated_sources:
        legacy_model = await db.get_model(username)
        if legacy_model:
            await db.add_or_update_model(
                username=username,
                display_name=legacy_model.get("display_name") or username,
                auto_record=enabled,
                record_quality=legacy_model.get("record_quality", "best"),
                retention_days=legacy_model.get("retention_days", 30),
                record_path=legacy_model.get("record_path"),
                source_type=legacy_model.get("source_type") or "chaturbate",
            )
    for source in updated_sources:
        source_enabled = bool(source.get("auto_record")) if toggle_source else enabled
        model = await db.get_model(
            source["channel_username"],
            source_type=source["source_type"],
        )
        await db.add_or_update_model(
            username=source["channel_username"],
            display_name=(model or {}).get("display_name") or username,
            auto_record=source_enabled,
            record_quality=source.get("record_quality") or (model or {}).get("record_quality", "best"),
            retention_days=int(source.get("retention_days") if source.get("retention_days") is not None else (model or {}).get("retention_days", 30)),
            record_path=source.get("record_path") or (model or {}).get("record_path"),
            source_type=source["source_type"],
        )

    stopped_ids: list[str] = []
    if not enabled:
        source_keys = {
            _recording_source_session_key(
                username,
                source.get("source_type") or "chaturbate",
                source.get("channel_username") or username,
            )
            for source in updated_sources
            if not toggle_source
            or (
                _normalize_source_type(source.get("source_type")) == toggle_source
                and (
                    not toggle_channel
                    or str(source.get("channel_username") or "").strip().lower()
                    == toggle_channel.lower()
                )
            )
        }
        for status in list(_all_recording_statuses()):
            if not status.get("running"):
                continue
            if status.get("person") != username and status.get("session_key") not in source_keys:
                continue
            session_id = status.get("id")
            if not session_id:
                continue
            ffmpeg_session = manager.get_session(session_id)
            stopped = await asyncio.to_thread(manager.stop_session, session_id, "auto_record_disable")
            if stopped and ffmpeg_session:
                await _index_ffmpeg_recording(ffmpeg_session)
            if stopped:
                stopped_ids.append(session_id)

    return {
        "success": True,
        "autoRecord": enabled,
        "stoppedSessionIds": stopped_ids,
        "profile": await _media_profile_payload(username),
    }


@app.post("/api/media-profiles/bulk/unfollow")
async def bulk_unfollow_media_profiles(body: dict):
    """Unfollow selected streamers. Cards and recordings are kept; recording stays as-is."""
    body = body or {}
    raw_items = body.get("items") or body.get("usernames") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="items required")

    results = []
    for raw in raw_items:
        if isinstance(raw, str):
            username = str(raw).strip()
            source_type = "chaturbate"
        elif isinstance(raw, dict):
            username = str(raw.get("username") or raw.get("channelUsername") or "").strip()
            source_type = _normalize_source_type(
                raw.get("sourceType") or raw.get("source_type")
            ) or "chaturbate"
        else:
            continue
        if not username:
            continue
        try:
            provider = _provider_for(source_type)
            outcome = await _apply_follow_change(provider, source_type, username, follow=False)
            results.append({
                "username": username,
                "sourceType": source_type,
                "success": True,
                "cardKept": True,
                "stoppedSessionIds": outcome.get("stoppedSessionIds") or [],
            })
        except HTTPException as exc:
            results.append({
                "username": username,
                "sourceType": source_type,
                "success": False,
                "detail": exc.detail,
            })
        except Exception as exc:
            results.append({
                "username": username,
                "sourceType": source_type,
                "success": False,
                "detail": str(exc),
            })
    ok = sum(1 for row in results if row.get("success"))
    return {"success": True, "unfollowed": ok, "results": results}


@app.post("/api/media-profiles/bulk/auto-record")
async def bulk_media_profile_auto_record(body: dict):
    """Enable or disable recording for many streamer cards."""
    body = body or {}
    raw_items = body.get("items") if isinstance(body.get("items"), list) else None
    usernames = body.get("usernames") or []
    if raw_items is None and (not isinstance(usernames, list) or not usernames):
        raise HTTPException(status_code=400, detail="usernames required")
    if "autoRecord" not in body and "auto_record" not in body:
        raise HTTPException(status_code=400, detail="autoRecord field required")
    enabled = bool(body.get("autoRecord", body.get("auto_record")))

    entries: list[dict] = []
    if raw_items is not None:
        for raw in raw_items:
            if isinstance(raw, str):
                username = str(raw).strip()
                if username:
                    entries.append({"username": username})
            elif isinstance(raw, dict):
                username = str(
                    raw.get("username")
                    or raw.get("profileUsername")
                    or raw.get("profile_username")
                    or ""
                ).strip()
                if not username:
                    continue
                entries.append({
                    "username": username,
                    "sourceType": raw.get("sourceType") or raw.get("source_type"),
                    "channelUsername": raw.get("channelUsername") or raw.get("channel_username"),
                    "channelUrl": raw.get("channelUrl") or raw.get("channel_url"),
                    "displayName": raw.get("displayName") or raw.get("display_name"),
                    "profileImageUrl": raw.get("profileImageUrl") or raw.get("profile_image_url"),
                })
    else:
        for raw in usernames:
            username = str(raw or "").strip()
            if username:
                entries.append({"username": username})

    results = []
    for entry in entries:
        username = entry["username"]
        try:
            patch_body = {"autoRecord": enabled}
            for key in (
                "sourceType",
                "channelUsername",
                "channelUrl",
                "displayName",
                "profileImageUrl",
            ):
                if entry.get(key):
                    patch_body[key] = entry[key]
            outcome = await toggle_media_profile_auto_record(username, patch_body)
            results.append({
                "username": username,
                "success": True,
                "autoRecord": outcome.get("autoRecord"),
            })
        except HTTPException as exc:
            results.append({
                "username": username,
                "success": False,
                "detail": exc.detail,
            })
        except Exception as exc:
            results.append({
                "username": username,
                "success": False,
                "detail": str(exc),
            })
    ok = sum(1 for row in results if row.get("success"))
    return {"success": True, "updated": ok, "autoRecord": enabled, "results": results}


@app.post("/api/media-profiles/bulk/delete")
async def bulk_delete_media_profiles(body: dict):
    """Delete streamer cards (and their recordings folders). Requires explicit confirm."""
    body = body or {}
    usernames = body.get("usernames") or []
    confirm = bool(body.get("confirm") or body.get("confirmed"))
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required to delete streamer cards and recordings",
        )
    if not isinstance(usernames, list) or not usernames:
        raise HTTPException(status_code=400, detail="usernames required")

    results = []
    for raw in usernames:
        username = str(raw or "").strip()
        if not username:
            continue
        try:
            outcome = await delete_media_profile(username)
            results.append({
                "username": username,
                "success": True,
                "removedRecordings": outcome.get("removedRecordings"),
                "folderDeleted": outcome.get("folderDeleted"),
            })
        except HTTPException as exc:
            results.append({
                "username": username,
                "success": False,
                "detail": exc.detail,
            })
        except Exception as exc:
            results.append({
                "username": username,
                "success": False,
                "detail": str(exc),
            })
    ok = sum(1 for row in results if row.get("success"))
    return {"success": True, "deleted": ok, "results": results}


def _assert_media_profile_not_active(username: str):
    for session in _all_recording_statuses():
        if session.get("person") == username and session.get("running"):
            raise HTTPException(
                status_code=403,
                detail="Cannot delete a profile while it is recording.",
            )


def _followed_identity_matches_profile(followed_username: str, profile_username: str) -> bool:
    uname = str(followed_username or "").strip()
    target = str(profile_username or "").strip()
    if not uname or not target:
        return False
    if uname == target or uname.lower() == target.lower():
        return True
    parsed, _ = parse_identity_with_source(uname)
    return bool(parsed) and (parsed == target or parsed.lower() == target.lower())


async def _followed_rows_for_media_profile(
    username: str,
    profile_sources: Optional[list] = None,
) -> list[dict]:
    """Followed rows that should disappear when a streamer card is deleted."""
    channel_names = {str(username or "").strip()}
    for source in profile_sources or []:
        channel = str(source.get("channel_username") or "").strip()
        if channel:
            channel_names.add(channel)
    channel_names = {name for name in channel_names if name}
    if not channel_names:
        return []

    matches: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in await db.get_all_followed():
        uname = str(item.get("username") or "").strip()
        if not uname:
            continue
        if not any(_followed_identity_matches_profile(uname, name) for name in channel_names):
            continue
        source_type = _normalize_source_type(item.get("source_type")) or "chaturbate"
        key = (uname.lower(), source_type)
        if key in seen:
            continue
        seen.add(key)
        matches.append(item)
    return matches


async def _delete_followed_for_media_profile(
    username: str,
    profile_sources: Optional[list] = None,
) -> int:
    """Remove local follow rows tied to a deleted streamer card."""
    removed = 0
    for item in await _followed_rows_for_media_profile(username, profile_sources):
        uname = str(item.get("username") or "").strip()
        source_type = _normalize_source_type(item.get("source_type")) or "chaturbate"
        if not uname:
            continue
        await db.delete_followed_model(uname, source_type=source_type)
        removed += 1
    return removed


@app.delete("/api/media-profiles/{username}")
async def delete_media_profile(username: str):
    """Delete the profile card, DB recordings, and records folder."""
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    _assert_media_profile_not_active(username)

    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    profile_sources = await db.get_media_profile_sources(username)
    recordings = await db.get_recordings(username)
    folder_exists = profile_dir.exists()
    followed_rows = await _followed_rows_for_media_profile(username, profile_sources)
    if (
        not folder_exists
        and not profile
        and not model
        and not recordings
        and not profile_sources
        and not followed_rows
    ):
        raise HTTPException(status_code=404, detail="Profile not found")
    if folder_exists and not profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="The profile records path is not a folder")

    for rec in recordings:
        recording_id = rec.get("recording_id")
        if recording_id:
            await db.delete_playback_position(recording_id)
    removed_recordings = await db.delete_recordings_for_username(username)
    await db.delete_model(username)
    all_sources = await db.get_all_media_profile_sources()
    sources_used_elsewhere = {
        (source.get("source_type"), source.get("channel_username"))
        for source in all_sources
        if source.get("profile_username") != username
    }
    for source in profile_sources:
        key = (source.get("source_type"), source.get("channel_username"))
        if key not in sources_used_elsewhere and source.get("channel_username"):
            await db.delete_model(
                source["channel_username"],
                source_type=source.get("source_type") or "chaturbate",
            )
    await _delete_followed_for_media_profile(username, profile_sources)
    await db.delete_media_profile(username)

    folder_deleted = False
    if folder_exists:
        shutil.rmtree(profile_dir)
        folder_deleted = True

    thumb_dir = OUTPUT_DIR / "thumbnails" / username
    thumbs_deleted = False
    try:
        if thumb_dir.exists() and thumb_dir.is_dir() and _path_is_inside_output(thumb_dir):
            shutil.rmtree(thumb_dir)
            thumbs_deleted = True
    except OSError as e:
        logger.warning("Unable to delete profile thumbnails", username=username, error=str(e))

    profile_image_deleted = False
    image_path = Path((profile or {}).get("profile_image_path") or "")
    try:
        if image_path.exists() and image_path.is_file() and _path_is_inside_output(image_path):
            image_path.unlink()
            profile_image_deleted = True
    except OSError as e:
        logger.warning("Unable to delete profile image", username=username, error=str(e))

    logger.info(
        "Media profile deleted",
        username=username,
        folder_deleted=folder_deleted,
        removed_recordings=removed_recordings,
    )
    return {
        "success": True,
        "username": username,
        "folderDeleted": folder_deleted,
        "thumbnailsDeleted": thumbs_deleted,
        "profileImageDeleted": profile_image_deleted,
        "removedRecordings": removed_recordings,
    }


def _resolved_path_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(Path(value).resolve())
    except Exception:
        return None


def _path_is_inside_output(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(OUTPUT_DIR.resolve())
    except Exception:
        return False


def _recording_delete_path_is_allowed(path: Path, username: str) -> bool:
    """Limit regular recording deletion to this profile's media directories."""
    try:
        resolved = path.resolve()
        profile_part = _clean_record_path_part(username)
        record_profile_root = (_records_root() / profile_part).resolve()
        thumbnail_profile_root = (OUTPUT_DIR / "thumbnails" / username).resolve()
        return resolved.is_relative_to(record_profile_root) or resolved.is_relative_to(
            thumbnail_profile_root
        )
    except Exception:
        return False


async def _find_recording_for_media_path(username: str, media_path: Path, relative_path: str) -> Optional[dict]:
    target_key = _resolved_path_key(str(media_path))
    recordings = await db.get_recordings(username)
    for rec in recordings:
        for key in ("file_path", "mp4_path", "playable_path"):
            if target_key and _resolved_path_key(rec.get(key)) == target_key:
                return rec
        if len(Path(relative_path).parts) == 1 and rec.get("filename") == media_path.name:
            return rec
    return None


def _assert_media_not_active(username: str, media_path: Path):
    target_key = _resolved_path_key(str(media_path))
    for session in _all_recording_statuses():
        if not (session.get("person") == username and session.get("running")):
            continue

        record_path = session.get("record_path") or ""
        active_key = _resolved_path_key(record_path)
        if active_key and target_key and active_key == target_key:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete media while it is recording.",
            )
        if record_path and Path(record_path).stem == media_path.stem:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete media while it is recording.",
            )


async def _delete_media_library_record(
    username: str,
    media_path: Path,
    relative_path: str,
    *,
    reason: str = "media_library_delete",
    final_status: str = "deleted",
) -> dict:
    rec = await _find_recording_for_media_path(username, media_path, relative_path)
    if rec and rec.get("media_kind") == "import":
        deleted = await remove_import_record(
            db,
            rec,
            reason=reason,
            delete_original=True,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Media not found")
        recording_audit.emit(
            "recording_deleted",
            username=username,
            path=str(media_path),
            reason=reason,
            finalStatus=final_status,
            mediaKind="import",
            recordingId=rec.get("recording_id"),
        )
        return {
            "success": True,
            "message": "Media deleted",
            "deletedFiles": [media_path.name],
            "removedRecord": True,
        }

    paths_to_delete: list[Path] = [media_path]
    removed_record = False
    recording_id = None

    if rec:
        removed_record = True
        recording_id = rec.get("recording_id")
        for key in ("file_path", "mp4_path", "playable_path", "thumbnail_path"):
            value = rec.get(key)
            if value:
                paths_to_delete.append(Path(value))

    # A converted recording can leave a source TS and a hidden temporary MP4
    # beside the final MP4. Treat all of them as one recording when deleting.
    recording_media_paths = [
        path
        for path in paths_to_delete
        if path.suffix.lower() in {".mp4", ".ts"}
    ]
    for recording_path in recording_media_paths:
        stem = recording_path.stem
        parent = recording_path.parent
        paths_to_delete.extend([
            parent / f"{stem}.mp4",
            parent / f"{stem}.ts",
            OUTPUT_DIR / "thumbnails" / username / f"{stem}.jpg",
        ])
        try:
            for candidate in parent.iterdir():
                if (
                    candidate.is_file()
                    and candidate.name.startswith(f".{stem}.")
                    and candidate.name.endswith(".tmp.mp4")
                ):
                    paths_to_delete.append(candidate)
        except OSError:
            pass

    deleted_files = []
    seen = set()
    file_size = 0
    try:
        if media_path.exists() and media_path.is_file():
            file_size = int(media_path.stat().st_size)
    except OSError:
        file_size = 0
    for path in paths_to_delete:
        path_key = _resolved_path_key(str(path))
        if not path_key or path_key in seen:
            continue
        seen.add(path_key)
        if not _path_is_inside_output(path):
            continue
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted_files.append(path.name)
        except OSError as e:
            logger.warning("Unable to delete media", path=str(path), error=str(e))

    if rec:
        if recording_id:
            await db.archive_and_delete_recording_by_id(
                recording_id,
                final_status=final_status,
                reason=reason,
                extra={"deletedFiles": deleted_files, "relativePath": relative_path},
            )
            await db.delete_playback_position(recording_id)
        else:
            await db.archive_and_delete_recording(
                username,
                rec.get("filename") or media_path.name,
                final_status=final_status,
                reason=reason,
                extra={"deletedFiles": deleted_files, "relativePath": relative_path},
            )

    recording_audit.emit(
        "recording_deleted",
        username=username,
        path=str(media_path),
        filename=media_path.name,
        recordingId=recording_id,
        reason=reason,
        finalStatus=final_status,
        bytes=file_size,
        deletedFiles=deleted_files,
    )

    if not deleted_files and not removed_record:
        raise HTTPException(status_code=404, detail="Media not found")

    return {
        "success": True,
        "message": "Media deleted",
        "deletedFiles": deleted_files,
        "removedRecord": removed_record,
    }


@app.delete("/api/media-library/{username}/{file_path:path}")
async def delete_media_library_item(username: str, file_path: str):
    """Delete a media item from the local records library."""
    media_path = _resolve_library_media_path(username, file_path)
    if media_path.suffix.lower() == ".ts":
        raise HTTPException(status_code=400, detail="TS files are not supported in Media")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    _assert_media_not_active(username, media_path)
    result = await _delete_media_library_record(username, media_path, file_path)
    logger.info(
        "Library media deleted",
        username=username,
        file=file_path,
        deleted_files=result.get("deletedFiles", []),
        removed_record=result.get("removedRecord"),
    )
    return result


@app.get("/api/media-library/processing-files")
async def list_media_library_processing_files():
    """List TS and other temporary files under the VPS records folder."""
    files = _collect_processing_files()
    total_bytes = sum(int(item.get("size") or 0) for item in files)
    return {
        "recordsRoot": str(_records_root()),
        "total": len(files),
        "totalBytes": total_bytes,
        "totalFormatted": format_bytes(total_bytes),
        "files": files,
    }


@app.post("/api/media-library/processing-files/delete")
async def delete_media_library_processing_files(body: ProcessingFilesDeleteBody):
    """Delete selected processing or temporary files from the VPS records folder."""
    requested_paths = list(dict.fromkeys(
        str(path_value).strip().replace("\\", "/")
        for path_value in body.paths
        if str(path_value or "").strip()
    ))
    if not requested_paths or len(requested_paths) > 500:
        raise HTTPException(status_code=400, detail="Select between 1 and 500 files")

    deleted = []
    for relative_path in requested_paths:
        media_path = _resolve_processing_file_path(relative_path)
        username = _processing_file_username(media_path)
        if username:
            _assert_recording_not_active(username, media_path)
            _assert_media_not_active(username, media_path)
        try:
            media_path.unlink()
        except OSError as exc:
            logger.warning(
                "Unable to delete processing file",
                path=str(media_path),
                error=str(exc),
            )
            raise HTTPException(status_code=500, detail=f"Unable to delete {media_path.name}") from exc
        deleted.append({
            "path": relative_path,
            "filename": media_path.name,
        })

    logger.info("Processing files deleted", count=len(deleted))
    return {
        "success": True,
        "deletedCount": len(deleted),
        "deleted": deleted,
    }


@app.post("/api/media-library/batch-delete")
async def delete_media_library_batch(body: MediaBatchDeleteBody):
    """Delete multiple selected videos in one validated batch request."""
    requested_ids = list(dict.fromkeys(str(item_id).strip() for item_id in body.itemIds))
    requested_ids = [item_id for item_id in requested_ids if item_id]
    if not requested_ids or len(requested_ids) > 100:
        raise HTTPException(status_code=400, detail="Select between 1 and 100 videos")

    library_items = {
        str(item.get("id")): item
        for item in await _scan_media_library_items(refresh_metadata=False)
        if item.get("type") == "video"
    }
    missing = [item_id for item_id in requested_ids if item_id not in library_items]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Media items not found: {', '.join(missing[:3])}",
        )

    targets = []
    for item_id in requested_ids:
        item = library_items[item_id]
        media_path = _resolve_library_media_path(item["username"], item["relativePath"])
        if not media_path.exists() or not media_path.is_file():
            raise HTTPException(status_code=404, detail=f"Media file missing: {item['filename']}")
        _assert_media_not_active(item["username"], media_path)
        targets.append((item, media_path))

    deleted = []
    for item, media_path in targets:
        result = await _delete_media_library_record(
            item["username"],
            media_path,
            item["relativePath"],
        )
        deleted.append({
            "itemId": item["id"],
            "filename": item["filename"],
            "deletedFiles": result.get("deletedFiles", []),
        })
    logger.info("Media deleted in batch", count=len(deleted))
    return {"success": True, "deletedCount": len(deleted), "deleted": deleted}


@app.get("/api/all-recordings")
async def get_all_recordings(
    page: int = 1,
    limit: int = 20,
    username: str = None,
    show_ts: bool = False
):
    """Get all recordings across all models with pagination"""

    result = await db.get_all_recordings_paginated(
        page=page,
        limit=limit,
        username_filter=username,
        show_ts=show_ts
    )

    recordings = []
    for rec in result["recordings"]:
        rec_username = rec.get("username", "")
        if rec.get("media_kind") == "import":
            formatted_import = _format_import_recording(rec, rec_username)
            if formatted_import:
                formatted_import["username"] = rec_username
                recordings.append(formatted_import)
            continue

        is_converted = bool(rec.get("is_converted"))
        mp4_raw = rec.get("mp4_path")
        ts_raw = rec.get("file_path")

        # Determine the playable file: prefer MP4, fall back to TS
        if is_converted and mp4_raw and Path(mp4_raw).exists():
            serve_file = Path(mp4_raw)
            file_size = rec.get("mp4_size") or serve_file.stat().st_size
        elif ts_raw and Path(ts_raw).exists():
            serve_file = Path(ts_raw)
            # Only raw TS files are opt-in; browser WebM/MP4 captures are
            # directly playable and should stay visible in the default view.
            if serve_file.suffix.lower() == ".ts" and not show_ts:
                continue
            file_size = rec.get("file_size") or serve_file.stat().st_size
        else:
            continue

        file_stem = serve_file.stem

        # Format duration
        duration_seconds = rec.get("duration_seconds", 0)
        if (
            (duration_seconds and duration_seconds < MIN_RECORDING_SECONDS)
            or (not duration_seconds and file_size < MIN_RECORDING_BYTES)
        ):
            continue

        duration_str = _format_duration_label(duration_seconds)

        # Thumbnail
        thumb_path = OUTPUT_DIR / "thumbnails" / rec_username / f"{file_stem}.jpg"

        recording_id = rec.get("recording_id", file_stem)
        recording_url = f"/streams/recordings/{quote(recording_id, safe='')}"

        recordings.append({
            "recordingId": recording_id,
            "username": rec_username,
            "filename": serve_file.name,
            "date": file_stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "duration": duration_seconds,
            "duration_str": duration_str,
            "url": recording_url,
            "downloadUrl": recording_url,
            "thumbnail": f"/api/recording-thumbnail/{rec_username}/{file_stem}.jpg" if thumb_path.exists() else None,
            "createdAt": rec.get("created_at"),
            "isImported": False,
            "mediaKind": "recording",
            "importStatus": None,
            "playable": True,
        })

    # Get distinct usernames for filter dropdown
    usernames = await db.get_distinct_recording_usernames()

    return {
        "recordings": recordings,
        "total": result["total"],
        "totalSize": result["total_size"],
        "totalSizeFormatted": format_bytes(result["total_size"]),
        "page": result["page"],
        "limit": result["limit"],
        "totalPages": result["total_pages"],
        "usernames": usernames,
    }


@app.get("/api/recording-thumbnail/{username}/{filename}")
async def get_recording_thumbnail(username: str, filename: str):
    """Fetch a recording thumbnail"""
    from fastapi.responses import FileResponse, Response

    username = _validate_media_profile_username(username)
    if (
        not filename
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or Path(filename).name != filename
        or Path(filename).suffix.lower() not in {".jpg", ".jpeg"}
    ):
        raise HTTPException(status_code=400, detail="Invalid name")

    thumbnails_root = (OUTPUT_DIR / "thumbnails").resolve()
    profile_root = (thumbnails_root / username).resolve()
    thumb_path = (profile_root / filename).resolve()
    if (
        not profile_root.is_relative_to(thumbnails_root)
        or not thumb_path.is_relative_to(profile_root)
    ):
        raise HTTPException(status_code=403, detail="Thumbnail path is not allowed")

    if thumb_path.exists() and thumb_path.is_file():
        return FileResponse(
            path=str(thumb_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    # SVG placeholder when no thumbnail is found
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">
        <rect fill="#1a1f3a" width="320" height="180"/>
        <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#a0aec0" font-size="16">📹 Generating...</text>
    </svg>'''

    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/models")
async def get_models():
    """Fetch the model list from SQLite.

    Never 500: on a transient error (e.g. SQLite lock during a
    write by a background task), return an empty list to avoid
    breaking the front-end display. The next fetch will get the correct state.
    """
    try:
        models = await db.get_all_models()
        formatted_models = []
        for model in models:
            source_type = await _infer_source_type(model.get("username"), model)
            formatted_models.append({
                "username": model['username'],
                "autoRecord": bool(model.get('auto_record', True)),
                "recordQuality": model.get('record_quality', 'best'),
                "retentionDays": model.get('retention_days', 30),
                "sourceType": source_type,
                "source_type": source_type,
                **_record_path_fields(model['username'], model),
            })
        return {"models": formatted_models}
    except Exception as e:
        logger.error("Error /api/models", error=str(e), exc_info=True)
        return {"models": [], "error": "transient"}


@app.get("/api/models/{username}/volume")
async def get_model_volume(username: str):
    """Return the saved playback volume for one profile."""
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    return {
        "username": username,
        "volume": await db.get_model_volume(username),
    }


@app.put("/api/models/{username}/volume")
async def update_model_volume(username: str, body: ModelVolumeBody):
    """Persist the playback volume for one profile."""
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    volume = float(body.volume)
    if not 0 <= volume <= 1:
        raise HTTPException(status_code=400, detail="Volume must be between 0 and 1")

    await db.set_model_volume(username, volume)
    return {
        "success": True,
        "username": username,
        "volume": volume,
    }


@app.post("/api/models")
async def add_model(model: dict):
    """Add a model in SQLite"""
    raw_username = str(model.get('username') or "").strip()
    source_from_url = _source_type_from_url(raw_username)
    username = (
        _normalize_live_channel_username(raw_username, raw_username)
        if raw_username.startswith(("http://", "https://"))
        else raw_username
    )
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    requested_source = _normalize_source_type(
        model.get("sourceType") or model.get("source_type") or source_from_url
    )
    source_type = requested_source or await _infer_source_type(username)
    if source_type not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{source_type}' unavailable",
        )

    # Check whether the model already exists
    existing = await db.get_model(username, source_type=source_type)
    if existing:
        raise HTTPException(status_code=409, detail="Model already exists")

    auto_record = bool(model.get('autoRecord', True))
    # When auto-record is enabled and the caller did not pin a per-model
    # resolution, fall back to the global default. Otherwise keep "best".
    if 'recordQuality' in model and model.get('recordQuality') is not None:
        record_quality = model['recordQuality']
    elif auto_record:
        record_quality = await _get_default_record_quality()
    else:
        record_quality = 'best'
    if "retentionDays" in model and model.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(model.get("retentionDays"))
    else:
        retention_days = await _get_default_retention_days()
    record_path = _normalize_record_path(
        model.get("recordPath") or model.get("record_path"),
        username,
    )

    # Add to SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=auto_record,
        record_quality=record_quality,
        retention_days=retention_days,
        record_path=record_path,
        source_type=source_type,
    )

    # Fetch all models to return
    all_models = await db.get_all_models()
    formatted = [{
        "username": m['username'],
        "autoRecord": bool(m.get('auto_record', True)),
        "recordQuality": m.get('record_quality', 'best'),
        "retentionDays": m.get('retention_days', 30),
        "sourceType": m.get('source_type') or 'chaturbate',
        "source_type": m.get('source_type') or 'chaturbate',
        **_record_path_fields(m['username'], m),
    } for m in all_models]

    return {"success": True, "models": formatted}


@app.put("/api/models/{username}")
async def update_model(username: str, model_data: dict):
    """Update a model's settings in SQLite"""
    requested_source = _normalize_source_type(
        model_data.get('sourceType') or model_data.get('source_type')
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' unavailable",
        )

    # Check whether the model exists
    existing = await db.get_model(username, source_type=requested_source)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    if "retentionDays" in model_data and model_data.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(
            model_data.get("retentionDays"),
            existing.get("retention_days", 30),
        )
    else:
        retention_days = existing.get("retention_days", 30)

    source_type = requested_source or existing.get("source_type") or "chaturbate"
    if "recordPath" in model_data or "record_path" in model_data:
        record_path = _normalize_record_path(
            model_data.get("recordPath") or model_data.get("record_path"),
            username,
        )
    else:
        record_path = existing.get("record_path")

    # Update in SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=model_data.get('autoRecord', existing.get('auto_record', True)),
        record_quality=model_data.get('recordQuality', existing.get('record_quality', 'best')),
        retention_days=retention_days,
        record_path=record_path,
        source_type=source_type,
    )

    # Fetch the updated model
    updated = await db.get_model(username, source_type=source_type)

    return {
        "success": True,
        "model": {
            "username": updated['username'],
            "autoRecord": bool(updated.get('auto_record', True)),
            "recordQuality": updated.get('record_quality', 'best'),
            "retentionDays": updated.get('retention_days', 30),
            "sourceType": updated.get('source_type') or 'chaturbate',
            "source_type": updated.get('source_type') or 'chaturbate',
            **_record_path_fields(updated['username'], updated),
        }
    }


@app.delete("/api/models/{username}")
async def delete_model(username: str, source: Optional[str] = None):
    """Delete a model from SQLite"""
    source_type = _normalize_source_type(source)
    # Check whether the model exists
    existing = await db.get_model(username, source_type=source_type)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    # Delete from SQLite
    await db.delete_model(username, source_type=source_type)

    # Fetch the updated list
    all_models = await db.get_all_models()
    formatted = [{
        "username": m['username'],
        "autoRecord": bool(m.get('auto_record', True)),
        "recordQuality": m.get('record_quality', 'best'),
        "retentionDays": m.get('retention_days', 30),
        "sourceType": m.get('source_type') or 'chaturbate',
        "source_type": m.get('source_type') or 'chaturbate',
        **_record_path_fields(m['username'], m),
    } for m in all_models]

    return {"success": True, "models": formatted}


@app.delete("/api/recordings/{username}/{filename}")
async def delete_recording(username: str, filename: str):
    """Delete a recording (TS + MP4 + thumbnail + DB)"""

    username = _validate_media_profile_username(username)
    if (
        not filename
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or Path(filename).name != filename
    ):
        raise HTTPException(status_code=400, detail="Invalid name")

    existing_recs = await db.get_recordings(username)
    matching_rec = next((
        r for r in existing_recs
        if r.get("filename") == filename
        or Path(r.get("file_path") or "").name == filename
        or Path(r.get("mp4_path") or "").name == filename
        or Path(r.get("playable_path") or "").name == filename
    ), None)
    if matching_rec and matching_rec.get("media_kind") == "import":
        deleted = await remove_import_record(
            db,
            matching_rec,
            reason="user_delete",
            delete_original=True,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Imported media not found")
        return {
            "success": True,
            "message": "Imported media deleted",
            "deleted_files": ["Import"],
        }

    allowed_extensions = {".ts", ".mp4"} | SUPPORTED_VIDEO_EXTENSIONS
    if Path(filename).suffix.lower() not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Invalid format")

    # Ensure this is not the in-progress recording
    file_stem = Path(filename).stem

    paths_to_delete: list[tuple[Path, str]] = []
    if matching_rec:
        for key, label in (
            ("file_path", "TS"),
            ("mp4_path", "MP4"),
            ("playable_path", "File"),
            ("thumbnail_path", "Thumbnail"),
        ):
            value = matching_rec.get(key)
            if value:
                paths_to_delete.append((Path(value), label))
        selected = _select_recording_path(matching_rec, filename)
        if selected:
            _assert_recording_not_active(username, selected)
            paths_to_delete.append((selected.with_suffix(".ts"), "TS"))
            paths_to_delete.append((selected.with_suffix(".mp4"), "MP4"))
            paths_to_delete.append((OUTPUT_DIR / "thumbnails" / username / f"{selected.stem}.jpg", "Thumbnail"))
    else:
        records_dir = OUTPUT_DIR / "records" / username
        paths_to_delete.extend([
            (records_dir / f"{file_stem}.ts", "TS"),
            (records_dir / f"{file_stem}.mp4", "MP4"),
            (records_dir / filename, Path(filename).suffix.upper().lstrip(".") or "File"),
            (OUTPUT_DIR / "thumbnails" / username / f"{file_stem}.jpg", "Thumbnail"),
        ])
        _assert_recording_not_active(username, records_dir / filename)

    # If the files are already gone (external cleanup, remounted volume, etc.)
    # we must still be able to clean up the orphan DB row — otherwise it
    # stays shown on /recordings and can never be removed.
    has_db_row = any(Path(r['filename']).stem == file_stem for r in existing_recs)

    if not any(path.exists() for path, _ in paths_to_delete) and not has_db_row:
        raise HTTPException(status_code=404, detail="Recording not found")

    # Delete all associated files
    try:
        files_deleted = []

        seen_paths: set[str] = set()
        for path, label in paths_to_delete:
            key = _resolved_path_key(str(path))
            if not key or key in seen_paths:
                continue
            seen_paths.add(key)
            if not _recording_delete_path_is_allowed(path, username):
                logger.warning("Recording delete outside profile ignored", username=username, path=str(path))
                continue
            if path.exists() and path.is_file():
                path.unlink()
                files_deleted.append(label)
                logger.info("Recording file deleted", username=username, file=path.name, label=label)

        # Delete from the database
        await db.delete_recording(username, filename)
        if matching_rec and matching_rec.get("filename") != filename:
            await db.delete_recording(username, matching_rec.get("filename"))
        if filename != f"{file_stem}.ts":
            await db.delete_recording(username, f"{file_stem}.ts")
        logger.info("Recording deleted from DB", username=username, filename=filename)

        if not files_deleted and has_db_row:
            files_deleted.append("DB (orphan row)")

        return {
            "success": True,
            "message": f"Deleted: {', '.join(files_deleted)}",
            "deleted_files": files_deleted
        }
    except Exception as e:
        logger.error("Recording delete error",
                    username=username,
                    filename=filename,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# System Statistics Endpoint
# ============================================

@app.get("/api/system/stats")
async def get_system_stats():
    """Get comprehensive system statistics"""
    import psutil
    import shutil

    # --- Disk Usage ---
    output_path = str(OUTPUT_DIR)
    disk = shutil.disk_usage(output_path)
    disk_info = {
        "total": disk.total,
        "used": disk.used,
        "free": disk.free,
        "percent": round((disk.used / disk.total) * 100, 1),
    }

    # --- CPU ---
    cpu_info = {
        "cores_physical": psutil.cpu_count(logical=False) or 0,
        "cores_logical": psutil.cpu_count(logical=True) or 0,
        "usage_percent": psutil.cpu_percent(interval=0.5),
        "per_core": psutil.cpu_percent(interval=0, percpu=True),
        "frequency": None,
    }
    freq = psutil.cpu_freq()
    if freq:
        cpu_info["frequency"] = {
            "current": round(freq.current, 0),
            "max": round(freq.max, 0) if freq.max else None,
        }

    # --- RAM ---
    mem = psutil.virtual_memory()
    ram_info = {
        "total": mem.total,
        "used": mem.used,
        "available": mem.available,
        "percent": mem.percent,
    }

    # --- Current Process ---
    process = psutil.Process()
    proc_mem = process.memory_info()
    process_info = {
        "pid": process.pid,
        "cpu_percent": process.cpu_percent(interval=0.1),
        "memory_rss": proc_mem.rss,
        "memory_vms": proc_mem.vms,
        "threads": process.num_threads(),
        "open_files": len(process.open_files()),
        "connections": len(process.connections()) if hasattr(process, 'connections') else len(process.net_connections()),
        "uptime_seconds": time.time() - process.create_time(),
    }

    # --- Child Processes (ffmpeg, etc.) ---
    children = []
    for child in process.children(recursive=True):
        try:
            child_mem = child.memory_info()
            children.append({
                "pid": child.pid,
                "name": child.name(),
                "cmdline": " ".join(child.cmdline()[:3]) if child.cmdline() else child.name(),
                "cpu_percent": child.cpu_percent(interval=0),
                "memory_rss": child_mem.rss,
                "status": child.status(),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # --- Recording Storage Breakdown ---
    records_dir = OUTPUT_DIR / "records"
    storage_breakdown = {
        "ts_files": {"count": 0, "size": 0},
        "mp4_files": {"count": 0, "size": 0},
        "other_files": {"count": 0, "size": 0},
        "thumbnails": {"count": 0, "size": 0},
        "total_recordings_size": 0,
        "by_model": [],
    }

    if records_dir.exists():
        model_stats = {}
        for model_dir in records_dir.iterdir():
            if not model_dir.is_dir():
                continue
            username = model_dir.name
            model_stat = {"username": username, "ts_size": 0, "mp4_size": 0, "other_size": 0, "ts_count": 0, "mp4_count": 0}
            for f in model_dir.iterdir():
                if not f.is_file():
                    continue
                fsize = f.stat().st_size
                ext = f.suffix.lower()
                if ext == ".ts":
                    storage_breakdown["ts_files"]["count"] += 1
                    storage_breakdown["ts_files"]["size"] += fsize
                    model_stat["ts_size"] += fsize
                    model_stat["ts_count"] += 1
                elif ext == ".mp4":
                    storage_breakdown["mp4_files"]["count"] += 1
                    storage_breakdown["mp4_files"]["size"] += fsize
                    model_stat["mp4_size"] += fsize
                    model_stat["mp4_count"] += 1
                else:
                    storage_breakdown["other_files"]["count"] += 1
                    storage_breakdown["other_files"]["size"] += fsize
                    model_stat["other_size"] += fsize
            model_stat["total_size"] = model_stat["ts_size"] + model_stat["mp4_size"] + model_stat["other_size"]
            model_stats[username] = model_stat

        # Sort models by total size descending
        storage_breakdown["by_model"] = sorted(model_stats.values(), key=lambda x: x["total_size"], reverse=True)[:20]
        storage_breakdown["total_recordings_size"] = (
            storage_breakdown["ts_files"]["size"]
            + storage_breakdown["mp4_files"]["size"]
            + storage_breakdown["other_files"]["size"]
        )

    # Thumbnails
    thumbs_dir = OUTPUT_DIR / "thumbnails"
    if thumbs_dir.exists():
        for f in thumbs_dir.rglob("*"):
            if f.is_file():
                storage_breakdown["thumbnails"]["count"] += 1
                storage_breakdown["thumbnails"]["size"] += f.stat().st_size

    # --- Active Sessions ---
    active_sessions = manager.list_status()
    sessions_info = {
        "active_count": sum(1 for s in active_sessions if s.get("running")),
        "total_count": len(active_sessions),
        "sessions": [],
    }
    for s in active_sessions:
        if s.get("running"):
            sessions_info["sessions"].append({
                "person": s.get("person", "unknown"),
                "duration_seconds": s.get("duration", 0),
                "file_size": s.get("file_size", 0),
            })

    # --- Network I/O ---
    net = psutil.net_io_counters()
    network_info = {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv,
        "packets_sent": net.packets_sent,
        "packets_recv": net.packets_recv,
    }

    # --- Disk I/O ---
    try:
        disk_io = psutil.disk_io_counters()
        disk_io_info = {
            "read_bytes": disk_io.read_bytes if disk_io else 0,
            "write_bytes": disk_io.write_bytes if disk_io else 0,
            "read_count": disk_io.read_count if disk_io else 0,
            "write_count": disk_io.write_count if disk_io else 0,
        }
    except Exception:
        disk_io_info = {"read_bytes": 0, "write_bytes": 0, "read_count": 0, "write_count": 0}

    return {
        "disk": disk_info,
        "cpu": cpu_info,
        "ram": ram_info,
        "process": process_info,
        "children": children,
        "storage": storage_breakdown,
        "sessions": sessions_info,
        "network": network_info,
        "disk_io": disk_io_info,
    }


# ============================================
# Update System Endpoints
# ============================================

@app.get("/api/system/check-update")
async def check_for_update():
    """Check GitHub for the latest release and compare with current version."""
    current_version = os.getenv("APP_VERSION", "dev")
    docker_available = DOCKER_UPDATE_ENABLED and bool(PASSWORD) and os.path.exists(DOCKER_SOCKET)
    if not GITHUB_REPOSITORY:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "error": "HXYLIVE_GITHUB_REPOSITORY is not configured",
            "docker_available": docker_available,
        }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest",
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as resp:
                status_code = resp.status
                release = await resp.json(content_type=None) if status_code == 200 else None
        if status_code == 200 and release:
            latest_version = release.get("tag_name", "").lstrip("v")
            update_available = _is_update_available(current_version, latest_version)
            return {
                "current_version": current_version,
                "latest_version": latest_version,
                "update_available": update_available,
                "release_url": release.get("html_url", ""),
                "release_notes": release.get("body", ""),
                "published_at": release.get("published_at", ""),
                "docker_available": docker_available,
            }
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "error": f"GitHub API returned {status_code}",
            "docker_available": docker_available,
        }
    except Exception as e:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "error": str(e),
            "docker_available": docker_available,
        }


@app.post("/api/system/update")
async def perform_system_update():
    """Pull latest Docker image and recreate the container via docker compose."""
    if not DOCKER_UPDATE_ENABLED:
        raise HTTPException(status_code=404, detail="Automatic Docker updates are disabled")
    if not PASSWORD:
        raise HTTPException(status_code=403, detail="Automatic Docker updates require password protection")
    if not os.path.exists(DOCKER_SOCKET):
        return {
            "success": False,
            "error": "docker_socket_unavailable",
            "message": "Docker socket not available. Mount it explicitly only on a password-protected deployment.",
            "manual_commands": "docker compose pull && docker compose up -d",
        }

    container_id = _get_container_id()
    if not container_id:
        return {
            "success": False,
            "error": "container_id_unknown",
            "message": "Cannot determine container ID.",
            "manual_commands": "docker compose pull && docker compose up -d",
        }

    try:
        # 1. Inspect current container to get compose project info
        status, inspect_data = _docker_api('GET', f'/containers/{container_id}/json')
        if status != 200:
            return {"success": False, "error": "inspect_failed", "message": "Cannot inspect current container"}

        container_config = json.loads(inspect_data)
        labels = container_config.get("Config", {}).get("Labels", {})

        compose_working_dir = labels.get("com.docker.compose.project.working_dir", "")
        compose_service = labels.get("com.docker.compose.service", "")

        if not compose_working_dir or not compose_service:
            return {
                "success": False,
                "error": "not_compose",
                "message": "Container was not started via Docker Compose.",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        # 2. Pull docker:cli image for the updater container
        logger.info("Update: pulling docker:cli for updater")
        _docker_api('POST', '/images/create?fromImage=docker&tag=cli', timeout=120)

        # 3. Build updater script using docker compose (preserves the stack)
        updater_script = (
            f"sleep 2\n"
            f"echo '[HXYLIVE Updater] Pulling latest image via compose...'\n"
            f"docker compose -f /compose-project/docker-compose.yml pull {compose_service}\n"
            f"echo '[HXYLIVE Updater] Recreating container via compose...'\n"
            f"docker compose -f /compose-project/docker-compose.yml up -d --no-deps {compose_service}\n"
            f"echo '[HXYLIVE Updater] Update complete!'\n"
        )

        # 4. Create the updater container
        _docker_api('DELETE', '/containers/hxylive-updater?force=true')
        updater_body = {
            "Image": "docker:cli",
            "Cmd": ["sh", "-c", updater_script],
            "HostConfig": {
                "Binds": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    f"{compose_working_dir}:/compose-project",
                ],
                "AutoRemove": True,
            },
        }
        status, create_data = _docker_api('POST', '/containers/create?name=hxylive-updater', body=updater_body)
        if status not in (200, 201):
            return {
                "success": False,
                "error": "updater_create_failed",
                "message": f"Failed to create updater container (HTTP {status})",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        updater_id = json.loads(create_data).get("Id", "")

        # 5. Start the updater — it will pull + recreate via compose in ~5 seconds
        status, _ = _docker_api('POST', f'/containers/{updater_id}/start')
        if status not in (200, 204):
            return {
                "success": False,
                "error": "updater_start_failed",
                "message": f"Failed to start updater (HTTP {status})",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        logger.info("Update: updater started, compose will recreate container in ~5 seconds")
        return {
            "success": True,
            "message": "Update in progress. The application will restart in a few seconds.",
        }

    except Exception as e:
        logger.error("Update failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "error": "exception",
            "message": str(e),
            "manual_commands": "docker compose pull && docker compose up -d",
        }


# ============================================
# Settings / Blacklisted Tags Endpoints
# ============================================

FLARE_SERVICE_URL_SETTING_KEY = "flaresolverr_url"


def _normalize_flaresolverr_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="FlareSolverr URL is required")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="FlareSolverr URL must be an HTTP URL")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="FlareSolverr URL must not include query or fragment")

    path = parsed.path.rstrip("/")
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc,
        path,
        "",
        "",
        "",
    )).rstrip("/")


async def _get_flaresolverr_url() -> str:
    raw = await db.get_setting(FLARE_SERVICE_URL_SETTING_KEY)
    if raw:
        try:
            return _normalize_flaresolverr_url(raw)
        except HTTPException:
            pass
    return _normalize_flaresolverr_url(DEFAULT_FLARE_SERVICE_URL)


def _apply_flaresolverr_url(url: str, client: Optional[FlareSolverrClient] = None) -> FlareSolverrClient:
    global flaresolverr_client
    if client is not None:
        client.set_base_url(url)
        flaresolverr_client = client
    elif flaresolverr_client is None:
        flaresolverr_client = FlareSolverrClient(url)
    else:
        flaresolverr_client.set_base_url(url)

    auth_router.set_flaresolverr(flaresolverr_client)
    if chaturbate_api:
        chaturbate_api.flaresolverr = flaresolverr_client
    return flaresolverr_client


@app.get("/api/settings/flaresolverr")
async def get_flaresolverr_settings():
    url = await _get_flaresolverr_url()
    return {"url": url, "flaresolverrUrl": url}


@app.put("/api/settings/flaresolverr")
async def update_flaresolverr_settings(body: dict):
    url = _normalize_flaresolverr_url(
        body.get("url", body.get("flaresolverrUrl"))
    )
    await db.set_setting(FLARE_SERVICE_URL_SETTING_KEY, url)
    _apply_flaresolverr_url(url)
    return {"success": True, "url": url, "flaresolverrUrl": url}


@app.get("/api/settings/blacklisted-tags")
async def get_blacklisted_tags():
    """Get the list of blacklisted tags"""
    tags = await db.get_blacklisted_tags()
    return {"tags": tags}


@app.post("/api/settings/blacklisted-tags")
async def set_blacklisted_tags(body: dict):
    """Set the list of blacklisted tags"""
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    # Normalize: lowercase, strip, deduplicate
    tags = list(set(t.strip().lower() for t in tags if t.strip()))
    await db.set_blacklisted_tags(tags)
    return {"tags": tags}


# ============================================
# Recording Settings Endpoints
# ============================================

@app.get("/api/settings/recording")
async def get_recording_settings():
    """Get recording settings."""
    from .core.config import AUTO_CONVERT, KEEP_TS

    auto_convert_val = await db.get_setting("auto_convert")
    keep_ts_val = await db.get_setting("keep_ts")
    show_ts_val = await db.get_setting("show_ts_files")
    auto_delete_val = await db.get_setting("auto_delete_watched")
    auto_delete_threshold_val = await db.get_setting("auto_delete_threshold")

    # Fall back to env var defaults if not set in DB
    if auto_convert_val is not None:
        auto_convert = auto_convert_val.lower() in {"1", "true", "yes"}
    else:
        auto_convert = AUTO_CONVERT

    if keep_ts_val is not None:
        keep_ts = keep_ts_val.lower() in {"1", "true", "yes"}
    else:
        keep_ts = KEEP_TS

    show_ts_files = show_ts_val is not None and show_ts_val.lower() in {"1", "true", "yes"}
    auto_delete_watched = auto_delete_val is not None and auto_delete_val.lower() in {"1", "true", "yes"}

    auto_delete_threshold = _normalize_watched_threshold(
        auto_delete_threshold_val if auto_delete_threshold_val is not None else 90
    )

    # Max recording resolution (0 = best available)
    max_res_val = await db.get_setting("max_resolution")
    try:
        max_resolution = int(max_res_val) if max_res_val is not None else 0
    except (ValueError, TypeError):
        max_resolution = 0

    # Default recording resolution applied to a model when auto-record is
    # turned on (0 = best available).
    default_res_val = await db.get_setting("default_resolution")
    try:
        default_resolution = int(default_res_val) if default_res_val is not None else 0
    except (ValueError, TypeError):
        default_resolution = 0

    default_retention_days = await _get_default_retention_days()
    segment_duration_minutes = await _get_segment_duration_minutes()
    segment_size_mb = await _get_segment_size_mb()
    filename_format = normalize_filename_format(await db.get_setting("filename_format"))
    check_interval_seconds = await get_check_interval_seconds(db)
    # Auto-sync is retired; always report false regardless of stored setting.
    mac_auto_sync = False
    mac_auto_sync_method = await _mac_autosync_method()
    recording_enabled = await _recording_enabled()
    default_monthly_quota_gb = await _get_default_monthly_quota_gb()
    quota_cycle_day = await _get_quota_cycle_day()
    quota_cycle_start, quota_cycle_end = await _quota_cycle_window()
    from app.services.recording_projects import (
        DEFAULT_GAP_BUFFER_MINUTES,
        normalize_gap_buffer_minutes,
    )

    gap_buffer_minutes = normalize_gap_buffer_minutes(
        await db.get_setting("gap_buffer_minutes"),
        DEFAULT_GAP_BUFFER_MINUTES,
    )
    from app.services.download_queue import (
        DEFAULT_FILE_CONCURRENCY,
        DEFAULT_PROJECT_CONCURRENCY,
        normalize_file_concurrency,
        normalize_project_concurrency,
    )

    download_project_concurrency = normalize_project_concurrency(
        await db.get_setting("download_project_concurrency"),
        DEFAULT_PROJECT_CONCURRENCY,
    )
    download_file_concurrency = normalize_file_concurrency(
        await db.get_setting("download_file_concurrency"),
        DEFAULT_FILE_CONCURRENCY,
    )

    return {
        "auto_convert": auto_convert,
        "keep_ts": keep_ts,
        "show_ts_files": show_ts_files,
        "auto_delete_watched": auto_delete_watched,
        "auto_delete_threshold": auto_delete_threshold,
        "max_resolution": max_resolution,
        "default_resolution": default_resolution,
        "default_retention_days": default_retention_days,
        "default_monthly_quota_gb": default_monthly_quota_gb,
        "quota_cycle_day": quota_cycle_day,
        "quota_cycle_start": quota_cycle_start,
        "quota_cycle_end": quota_cycle_end,
        "segment_duration_minutes": segment_duration_minutes,
        "segment_size_mb": segment_size_mb,
        "filename_format": filename_format,
        "check_interval": check_interval_seconds,
        "check_interval_seconds": check_interval_seconds,
        "recording_enabled": recording_enabled,
        "gap_buffer_minutes": gap_buffer_minutes,
        "download_project_concurrency": download_project_concurrency,
        "download_file_concurrency": download_file_concurrency,
        "mac_auto_sync": mac_auto_sync,
        "mac_auto_sync_method": mac_auto_sync_method,
        "library_space_blocked": bool(_library_space_blocked),
        "staging_blocked": bool(_staging_blocked),
        "staging_cleaning": bool(_staging_blocked),
        "disk_convert_paused": bool(_disk_convert_paused),
        "disk_recording_frozen": bool(_library_space_blocked or _staging_blocked),
        "disk_emergency_active": False,
        "staging_stop_gb": await _get_staging_stop_gb(),
        "library_stop_gb": await _get_library_stop_gb(),
        "recording_gate": (
            "library_disk_low"
            if _library_space_blocked
            else ("staging_cleaning" if _staging_blocked else ("off" if not recording_enabled else "on"))
        ),
        "recording_gate_message": (
            f"Library disk free space is below {await _get_library_stop_gb()} GB. Free space, then turn Auto Record back on."
            if _library_space_blocked
            else (
                "Staging disk is low; finished recordings are moving to the library. Captures are paused."
                if _staging_blocked
                else (
                    "Auto Record is off."
                    if not recording_enabled
                    else ""
                )
            )
        ),
        "disk_thresholds": {
            "staging_stop_gb": await _get_staging_stop_gb(),
            "library_stop_gb": await _get_library_stop_gb(),
            "default_staging_stop_gb": DEFAULT_STAGING_STOP_GB,
            "default_library_stop_gb": DEFAULT_LIBRARY_STOP_GB,
        },
        "records_root": str(_records_root()),
        "staging_records_root": str(_staging_records_root()),
    }


# Allowed HLS heights. 0 means "best available".
_ALLOWED_MAX_RESOLUTIONS = {0, 360, 480, 720, 1080, 1440, 2160}
_ALLOWED_SEGMENT_DURATIONS = {0, 30, 60, 90}
_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 365


def _normalize_retention_days(value, default: int = _DEFAULT_RETENTION_DAYS) -> int:
    """Return a valid retention window. 0 means keep forever."""
    try:
        retention_days = int(value)
    except (ValueError, TypeError):
        retention_days = default

    if retention_days < 0:
        raise HTTPException(status_code=400, detail="retentionDays must be 0 or greater")
    if retention_days > _MAX_RETENTION_DAYS:
        raise HTTPException(status_code=400, detail=f"retentionDays must be <= {_MAX_RETENTION_DAYS}")
    return retention_days


async def _get_default_retention_days() -> int:
    raw = await db.get_setting("default_retention_days")
    try:
        return _normalize_retention_days(raw if raw is not None else _DEFAULT_RETENTION_DAYS)
    except HTTPException:
        return _DEFAULT_RETENTION_DAYS


def _normalize_segment_duration_minutes(value, default: int = 30) -> int:
    try:
        duration_minutes = int(value)
    except (ValueError, TypeError):
        duration_minutes = default

    if duration_minutes not in _ALLOWED_SEGMENT_DURATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"segment_duration_minutes must be one of {sorted(_ALLOWED_SEGMENT_DURATIONS)}",
        )
    return duration_minutes


def _normalize_segment_size_mb(value, default: int = 0) -> int:
    try:
        size_mb = int(value)
    except (ValueError, TypeError):
        size_mb = default

    if size_mb < 0:
        raise HTTPException(status_code=400, detail="segment_size_mb must be 0 or greater")
    return size_mb


def _normalize_filename_format_or_400(value) -> str:
    filename_format = str(value or "").strip().lower()
    if filename_format not in ALLOWED_FILENAME_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"filename_format must be one of {sorted(ALLOWED_FILENAME_FORMATS)}",
        )
    return filename_format


async def _get_segment_duration_minutes() -> int:
    from .core.config import RECORD_SEGMENT_DURATION_MINUTES

    raw = await db.get_setting("segment_duration_minutes")
    try:
        return _normalize_segment_duration_minutes(
            raw if raw is not None else RECORD_SEGMENT_DURATION_MINUTES
        )
    except HTTPException:
        return 30


async def _get_segment_size_mb() -> int:
    from .core.config import RECORD_SEGMENT_SIZE_MB

    raw = await db.get_setting("segment_size_mb")
    try:
        return _normalize_segment_size_mb(
            raw if raw is not None else RECORD_SEGMENT_SIZE_MB
        )
    except HTTPException:
        return 0


async def _get_recording_segment_limits() -> tuple[int, int]:
    duration_minutes = await _get_segment_duration_minutes()
    size_mb = await _get_segment_size_mb()
    return duration_minutes * 60, size_mb * 1024 * 1024


async def _get_recording_filename_format() -> str:
    return normalize_filename_format(await db.get_setting("filename_format"))


async def _get_max_recording_height() -> Optional[int]:
    """Return the configured max_resolution as an int, or None for 'best'."""
    try:
        raw = await db.get_setting("max_resolution")
        if raw is None:
            return None
        val = int(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


async def _get_default_record_quality() -> str:
    """Return the global default record quality as a string ("best"/"720p"/...).

    Used to populate `record_quality` when a model is enrolled or has
    auto-record turned on without an explicit per-model value.
    """
    try:
        raw = await db.get_setting("default_resolution")
        if raw is None:
            return "best"
        val = int(raw)
        if val <= 0:
            return "best"
        return f"{val}p"
    except (ValueError, TypeError):
        return "best"


def _record_quality_to_height(record_quality: Optional[str]) -> Optional[int]:
    """Map model-level quality settings to an HLS height cap."""
    if not record_quality:
        return None

    value = str(record_quality).strip().lower()
    if value in {"best", "auto", "highest"}:
        return None

    match = re.fullmatch(r"(\d+)\s*p?", value)
    if not match:
        return None

    height = int(match.group(1))
    return height if height in _ALLOWED_MAX_RESOLUTIONS and height > 0 else None


async def _get_recording_height_for_quality(
    record_quality: Optional[str],
) -> Optional[int]:
    """Combine per-model quality with the global max-resolution cap."""
    global_height = await _get_max_recording_height()
    quality_height = _record_quality_to_height(record_quality)

    if global_height and quality_height:
        return min(global_height, quality_height)
    return quality_height or global_height


@app.put("/api/settings/recording")
async def update_recording_settings(body: dict):
    """Update recording settings."""
    applied_retention_models = None
    default_retention_days = None

    if "auto_convert" in body:
        await db.set_setting("auto_convert", str(body["auto_convert"]).lower())
    if "keep_ts" in body:
        await db.set_setting("keep_ts", str(body["keep_ts"]).lower())
    if "recording_enabled" in body:
        enabled = str(body["recording_enabled"]).lower() in {"1", "true", "yes"}
        if enabled and _library_space_blocked:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot enable Auto Record while library disk free space is below "
                    f"{await _get_library_stop_gb()} GB."
                ),
            )
        await db.set_setting("recording_enabled", "true" if enabled else "false")
        if enabled:
            logger.info(
                "Global Auto Record enabled; per-streamer settings unchanged",
                task="auto-record",
            )
        else:
            stopped = await asyncio.to_thread(
                _stop_all_running_recordings,
                "global_recording_pause",
                log_task="auto-record",
                log_message="Recording stopped by global Auto Record pause",
            )
            logger.info(
                "Global Auto Record paused; active sessions stopped",
                task="auto-record",
                stopped=len(stopped),
            )
    if "staging_stop_gb" in body:
        staging_stop_gb = normalize_staging_stop_gb(body["staging_stop_gb"])
        await db.set_setting("staging_stop_gb", str(staging_stop_gb))
    if "library_stop_gb" in body:
        library_stop_gb = normalize_library_stop_gb(body["library_stop_gb"])
        await db.set_setting("library_stop_gb", str(library_stop_gb))
    if "mac_auto_sync" in body:
        # Auto-sync removed under recording projects: force store false, ignore enable.
        await db.set_setting("mac_auto_sync", "false")
    if "gap_buffer_minutes" in body:
        from app.services.recording_projects import ALLOWED_GAP_BUFFER_MINUTES

        try:
            gap_minutes = int(body["gap_buffer_minutes"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"gap_buffer_minutes must be one of {sorted(ALLOWED_GAP_BUFFER_MINUTES)}",
            )
        if gap_minutes not in ALLOWED_GAP_BUFFER_MINUTES:
            raise HTTPException(
                status_code=400,
                detail=f"gap_buffer_minutes must be one of {sorted(ALLOWED_GAP_BUFFER_MINUTES)}",
            )
        await db.set_setting("gap_buffer_minutes", str(gap_minutes))
    if "download_project_concurrency" in body:
        from app.services.download_queue import (
            MAX_PROJECT_CONCURRENCY,
            MIN_PROJECT_CONCURRENCY,
            normalize_project_concurrency,
        )

        try:
            project_n = int(body["download_project_concurrency"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"download_project_concurrency must be {MIN_PROJECT_CONCURRENCY}-{MAX_PROJECT_CONCURRENCY}",
            )
        project_n = normalize_project_concurrency(project_n)
        await db.set_setting("download_project_concurrency", str(project_n))
    if "download_file_concurrency" in body:
        from app.services.download_queue import (
            MAX_FILE_CONCURRENCY,
            MIN_FILE_CONCURRENCY,
            normalize_file_concurrency,
        )

        try:
            file_n = int(body["download_file_concurrency"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"download_file_concurrency must be {MIN_FILE_CONCURRENCY}-{MAX_FILE_CONCURRENCY}",
            )
        file_n = normalize_file_concurrency(file_n)
        await db.set_setting("download_file_concurrency", str(file_n))
    if "mac_auto_sync_method" in body:
        method = str(body["mac_auto_sync_method"] or "").strip().lower()
        if method not in {"chrome", "motrix"}:
            raise HTTPException(status_code=400, detail="mac_auto_sync_method must be chrome or motrix")
        await db.set_setting("mac_auto_sync_method", method)
    if "show_ts_files" in body:
        await db.set_setting("show_ts_files", str(body["show_ts_files"]).lower())
    if "auto_delete_watched" in body:
        await db.set_setting("auto_delete_watched", str(body["auto_delete_watched"]).lower())
    if "auto_delete_threshold" in body:
        threshold = _normalize_watched_threshold(body["auto_delete_threshold"])
        await db.set_setting("auto_delete_threshold", str(threshold))
    if "max_resolution" in body:
        try:
            max_res = int(body["max_resolution"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_resolution must be an integer")
        if max_res not in _ALLOWED_MAX_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"max_resolution must be one of {sorted(_ALLOWED_MAX_RESOLUTIONS)}"
            )
        await db.set_setting("max_resolution", str(max_res))
    if "default_resolution" in body:
        try:
            default_res = int(body["default_resolution"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="default_resolution must be an integer")
        if default_res not in _ALLOWED_MAX_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"default_resolution must be one of {sorted(_ALLOWED_MAX_RESOLUTIONS)}"
            )
        await db.set_setting("default_resolution", str(default_res))
    if "default_monthly_quota_gb" in body:
        try:
            default_quota = normalize_monthly_quota_gb(
                body["default_monthly_quota_gb"],
                allow_none=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.set_setting("default_monthly_quota_gb", str(default_quota))
    if "quota_cycle_day" in body:
        try:
            cycle_day = normalize_quota_cycle_day(body["quota_cycle_day"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.set_setting("quota_cycle_day", str(cycle_day))
    if "default_retention_days" in body:
        default_retention_days = _normalize_retention_days(body["default_retention_days"])
        await db.set_setting("default_retention_days", str(default_retention_days))
    if "segment_duration_minutes" in body:
        segment_duration_minutes = _normalize_segment_duration_minutes(
            body["segment_duration_minutes"]
        )
        await db.set_setting("segment_duration_minutes", str(segment_duration_minutes))
    if "segment_size_mb" in body:
        segment_size_mb = _normalize_segment_size_mb(body["segment_size_mb"])
        await db.set_setting("segment_size_mb", str(segment_size_mb))
    if "filename_format" in body:
        await db.set_setting(
            "filename_format",
            _normalize_filename_format_or_400(body["filename_format"]),
        )
    if "check_interval_seconds" in body or "check_interval" in body:
        try:
            check_interval_seconds = normalize_check_interval_seconds(
                body.get("check_interval_seconds", body.get("check_interval"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await db.set_setting(CHECK_INTERVAL_SETTING_KEY, str(check_interval_seconds))
    if body.get("apply_default_retention_to_models"):
        if default_retention_days is None:
            default_retention_days = await _get_default_retention_days()
        applied_retention_models = await db.update_all_models_retention_days(default_retention_days)

    # Return current state
    settings = await get_recording_settings()
    if applied_retention_models is not None:
        settings["applied_retention_models"] = applied_retention_models
    return settings


# ============================================
# Follow/Unfollow on Chaturbate
# ============================================

@app.post("/api/chaturbate/follow/{username}")
async def follow_model_on_chaturbate(username: str):
    """Follow a model on Chaturbate"""
    return await provider_follow("chaturbate", username)


@app.post("/api/chaturbate/unfollow/{username}")
async def unfollow_model_on_chaturbate(username: str):
    """Unfollow a model on Chaturbate"""
    return await provider_unfollow("chaturbate", username)


@app.get("/api/chaturbate/is-following/{username}")
async def is_following_model(username: str):
    """Check if following a model on Chaturbate"""
    return await provider_is_following("chaturbate", username)


# ============================================
# Auto-record Toggle
# ============================================

@app.patch("/api/models/{username}/auto-record")
async def toggle_auto_record(username: str, body: dict):
    """Toggle auto-record for a model"""
    requested_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' unavailable",
        )
    existing = await db.get_model(username, source_type=requested_source)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    auto_record = body.get("autoRecord")
    if auto_record is None:
        raise HTTPException(status_code=400, detail="autoRecord field required")

    source_type = requested_source or existing.get("source_type") or "chaturbate"
    new_auto = bool(auto_record)
    was_auto = bool(existing.get("auto_record", False))
    record_quality = existing.get("record_quality", "best")
    # On the off -> on transition, apply the global default resolution so the
    # model immediately starts recording at the configured default.
    if new_auto and not was_auto:
        record_quality = await _get_default_record_quality()

    await db.add_or_update_model(
        username=username,
        auto_record=new_auto,
        record_quality=record_quality,
        retention_days=existing.get("retention_days", 30),
        source_type=source_type,
    )
    return {
        "success": True,
        "autoRecord": new_auto,
        "recordQuality": record_quality,
        "sourceType": source_type,
    }


# ============================================
# Playback Position Endpoints
# ============================================

@app.get("/api/playback-position/{recording_id}")
async def get_playback_position(recording_id: str):
    """Get saved playback position for a recording"""
    pos = await db.get_playback_position(recording_id)
    if pos:
        watched_threshold = await _get_watched_threshold()
        is_watched = bool(pos.get("watched_at")) or _is_playback_watched(
            pos["position_seconds"],
            pos["duration_seconds"],
            watched_threshold,
        )
        return {
            "recordingId": recording_id,
            "position": pos["position_seconds"],
            "duration": pos["duration_seconds"],
            "progress": _playback_progress(pos["position_seconds"], pos["duration_seconds"]),
            "watchedThreshold": watched_threshold,
            "isWatched": is_watched,
            "watchedAt": pos.get("watched_at") or (pos.get("updated_at") if is_watched else None),
        }
    return {
        "recordingId": recording_id,
        "position": 0,
        "duration": 0,
        "progress": 0,
        "watchedThreshold": await _get_watched_threshold(),
        "isWatched": False,
        "watchedAt": None,
    }


@app.post("/api/playback-position/{recording_id}")
async def save_playback_position(recording_id: str, body: dict):
    """Save playback position for a recording. Auto-delete if threshold reached."""
    try:
        position = float(body.get("position", 0) or 0)
    except (ValueError, TypeError):
        position = 0
    try:
        duration = float(body.get("duration", 0) or 0)
    except (ValueError, TypeError):
        duration = 0
    username = body.get("username", "")
    watched_threshold = await _get_watched_threshold()
    playback_progress = _playback_progress(position, duration)
    position_reached_watched_threshold = _is_playback_watched(position, duration, watched_threshold)
    await db.save_playback_position(
        recording_id,
        username,
        position,
        duration,
        mark_watched=position_reached_watched_threshold,
    )
    saved_position = await db.get_playback_position(recording_id)
    is_watched = bool((saved_position or {}).get("watched_at")) or position_reached_watched_threshold

    # Check auto-delete
    should_delete = False
    if duration > 0 and position > 0:
        rec = await db.get_recording_by_id(recording_id)
        is_protected_import = bool(
            rec
            and rec.get("media_kind") == "import"
            and rec.get("protected_from_retention")
        )
        auto_delete_val = await db.get_setting("auto_delete_watched")
        if (
            not is_protected_import
            and auto_delete_val
            and auto_delete_val.lower() in {"1", "true", "yes"}
        ):
            if position_reached_watched_threshold:
                should_delete = True

    return {
        "success": True,
        "autoDelete": should_delete,
        "isWatched": is_watched,
        "progress": playback_progress,
        "watchedThreshold": watched_threshold,
        "watchedAt": (saved_position or {}).get("watched_at"),
    }


# ============================================
# Recordings grouped by model
# ============================================

def _recording_group_visible(rec: dict, show_ts: bool) -> bool:
    file_suffix = Path(str(rec.get("file_path") or "")).suffix.lower()
    return bool(
        show_ts
        or rec.get("media_kind") == "import"
        or rec.get("is_converted")
        or rec.get("mp4_path") is not None
        or file_suffix != ".ts"
    )


def _recording_model_identity(model: dict) -> tuple[str, str]:
    return (
        str(model.get("username") or ""),
        _normalize_source_type(model.get("source_type")) or "chaturbate",
    )


def _legacy_recording_model(models: list[dict]) -> dict:
    """Choose a stable owner when a legacy recording has no provider key."""
    return sorted(
        models,
        key=lambda model: (
            0 if _recording_model_identity(model)[1] == "chaturbate" else 1,
            _recording_model_identity(model)[1],
            str(model.get("record_path") or ""),
        ),
    )[0]


def _recording_models_for_path(rec: dict, models: list[dict]) -> list[dict]:
    raw_path = rec.get("file_path") or rec.get("playable_path") or rec.get("mp4_path")
    if not raw_path:
        return []
    try:
        recording_path = Path(raw_path).resolve()
    except Exception:
        return []

    matches: list[tuple[int, dict]] = []
    for model in models:
        try:
            model_root = _model_record_dir(
                model, str(model.get("username") or "")
            ).resolve()
            if recording_path.is_relative_to(model_root):
                matches.append((len(model_root.parts), model))
        except Exception:
            continue
    if not matches:
        return []
    most_specific_depth = max(depth for depth, _model in matches)
    return [model for depth, model in matches if depth == most_specific_depth]


def _summarize_recording_rows(username: str, records: list[dict], source_type: str) -> dict:
    created_values = [int(rec.get("created_at") or 0) for rec in records]
    return {
        "username": username,
        "source_type": source_type,
        "recording_count": len(records),
        "total_size": sum(
            int(rec.get("playable_size") or rec.get("mp4_size") or rec.get("file_size") or 0)
            for rec in records
        ),
        "last_recording_at": max(created_values, default=0) or None,
        "total_duration": sum(int(rec.get("duration_seconds") or 0) for rec in records),
    }


@app.get("/api/recordings-by-model")
async def get_recordings_by_model(show_ts: bool = False):
    """Get recordings grouped by model with stats, including models with 0 recordings"""
    groups = await db.get_recordings_grouped_by_model(show_ts=show_ts)

    all_models = await db.get_all_models()
    models_by_identity = {_recording_model_identity(model): model for model in all_models}
    models_by_username: dict[str, list[dict]] = {}
    for model in all_models:
        models_by_username.setdefault(str(model.get("username") or ""), []).append(model)

    provider_groups: list[dict] = []
    for group in groups:
        username = str(group.get("username") or "")
        candidate_models = models_by_username.get(username, [])
        if len(candidate_models) <= 1:
            source_type = (
                _recording_model_identity(candidate_models[0])[1]
                if candidate_models
                else await _infer_source_type(username)
            )
            provider_groups.append({**group, "source_type": source_type})
            continue

        # Recordings predate provider-aware model identities. Distinct custom
        # record paths let us preserve their provider association. If multiple
        # providers share the same legacy path, assign those ambiguous rows to
        # a deterministic Chaturbate-first owner and expose the others as empty.
        canonical_model = _legacy_recording_model(candidate_models)
        rows_by_identity: dict[tuple[str, str], list[dict]] = {}
        for rec in await db.get_recordings(username):
            if not _recording_group_visible(rec, show_ts):
                continue
            path_matches = _recording_models_for_path(rec, candidate_models)
            owner = path_matches[0] if len(path_matches) == 1 else canonical_model
            rows_by_identity.setdefault(_recording_model_identity(owner), []).append(rec)

        split_groups = [
            _summarize_recording_rows(identity[0], records, identity[1])
            for identity, records in rows_by_identity.items()
        ]
        split_groups.sort(key=lambda item: int(item.get("last_recording_at") or 0), reverse=True)
        provider_groups.extend(split_groups)

    identities_with_recordings: set[tuple[str, str]] = set()

    # Any model with recordings is shown here, regardless of auto_record status.
    # auto_record only controls whether NEW recordings are triggered; past
    # recordings should never disappear from the list (GH #13).
    result = []
    for group in provider_groups:
        username = group["username"]
        source_type = _normalize_source_type(group.get("source_type")) or "chaturbate"
        identity = (username, source_type)
        identities_with_recordings.add(identity)
        model = models_by_identity.get(identity)
        thumb_url = f"/api/thumbnail/{username}"
        result.append({
            "username": username,
            "recordingCount": group["recording_count"],
            "totalSize": group["total_size"],
            "lastRecordingAt": group["last_recording_at"],
            "totalDuration": group["total_duration"],
            "thumbnail": thumb_url,
            "autoRecord": bool(model.get("auto_record")) if model else True,
            "sourceType": source_type,
            "source_type": source_type,
        })

    # Also include tracked models (auto_record=1) that have 0 recordings
    for model in all_models:
        username, source_type = _recording_model_identity(model)
        if (username, source_type) not in identities_with_recordings and model.get("auto_record"):
            result.append({
                "username": username,
                "recordingCount": 0,
                "totalSize": 0,
                "lastRecordingAt": None,
                "totalDuration": 0,
                "thumbnail": f"/api/thumbnail/{username}",
                "autoRecord": True,
                "sourceType": source_type,
                "source_type": source_type,
            })

    return {"models": result}


@app.post("/api/recordings/recalculate-durations")
async def recalculate_all_durations():
    """Recalculate durations for all recordings"""
    logger.info("API: duration recalculation request", endpoint="/api/recordings/recalculate-durations")

    try:
        # Create a background task
        asyncio.create_task(_recalculate_durations_task())

        return {
            "success": True,
            "message": "Duration recalculation started in background"
        }
    except Exception as e:
        logger.error("Error starting duration recalculation", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _recalculate_durations_task():
    """Background duration recalculation task"""
    from app.tasks.monitor import generate_recording_thumbnail, get_media_created_at, get_video_duration

    logger.background_task("recalculate-durations", "Starting recalculation")

    try:
        # Fetch all models
        models = await db.get_all_models()

        total_processed = 0
        total_updated = 0

        for model in models:
            username = model['username']
            records_dirs = [path for path in _record_dirs_for_model(username, model) if path.exists()]

            if not records_dirs:
                continue

            logger.info("Recalculating durations", username=username, task="recalculate-durations")

            ts_files = []
            for records_dir in records_dirs:
                ts_files.extend(records_dir.glob("*.ts"))

            for ts_file in ts_files:
                try:
                    total_processed += 1

                    # Fetch the recording from the DB
                    recordings = await db.get_recordings(username)
                    existing_rec = next((r for r in recordings if r['filename'] == ts_file.name), None)

                    current_duration = 0
                    if existing_rec:
                        current_duration = existing_rec.get('duration_seconds', 0)

                    # Compute duration if it is 0
                    if current_duration == 0:
                        duration = await get_video_duration(ts_file, FFMPEG_PATH)

                        if duration > 0:
                            # Also generate the thumbnail
                            thumbnail_path = await generate_recording_thumbnail(
                                ts_file, OUTPUT_DIR, username, FFMPEG_PATH
                            )

                            # Update in the DB
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_file),
                                file_size=ts_file.stat().st_size,
                                duration_seconds=duration,
                                thumbnail_path=thumbnail_path,
                                created_at=await get_media_created_at(
                                    ts_file,
                                    FFMPEG_PATH,
                                    fallback_timestamp=int(ts_file.stat().st_mtime),
                                ),
                            )

                            total_updated += 1

                            logger.success("Duration computed",
                                         username=username,
                                         filename=ts_file.name,
                                         duration=duration)

                except Exception as e:
                    logger.error("File recalculation error",
                               username=username,
                               filename=ts_file.name,
                               error=str(e))
                    continue

        logger.success("Recalculation finished",
                      task="recalculate-durations",
                      updated=total_updated,
                      total=total_processed)

    except Exception as e:
        logger.error("Duration recalculation task error",
                    task="recalculate-durations",
                    error=str(e),
                    exc_info=True)


# ============================================
# Background Task - Auto-recording
# ============================================

async def ffmpeg_watchdog_task():
    """Stop stalled FFmpeg sessions and enforce staging/library free-space floors."""
    global _library_space_blocked, _staging_blocked, _disk_convert_paused
    timeout = getattr(manager, "stall_timeout_seconds", 180)
    if timeout <= 0:
        logger.info("FFmpeg watchdog disabled", task="ffmpeg-watchdog")
        return

    while True:
        try:
            staging_stop_gb = await _get_staging_stop_gb()
            library_stop_gb = await _get_library_stop_gb()
            staging_free = disk_free_bytes(STAGING_DIR)
            library_free = disk_free_bytes(OUTPUT_DIR)

            if library_free < library_stop_gb * DISK_GB and not _library_space_blocked:
                _library_space_blocked = True
                _disk_convert_paused = True
                await db.set_setting("recording_enabled", "false")
                hard_stopped = _stop_all_running_recordings(
                    "library_disk_low",
                    log_task="ffmpeg-watchdog",
                    log_message="Recording stopped: library disk below threshold",
                )
                if hard_stopped:
                    logger.error(
                        "Library disk floor: recordings stopped",
                        task="ffmpeg-watchdog",
                        free_bytes=library_free,
                        free_formatted=format_bytes(library_free),
                        threshold_gb=library_stop_gb,
                        stopped=len(hard_stopped),
                    )

            if staging_free < staging_stop_gb * DISK_GB and not _staging_blocked:
                _staging_blocked = True
                hard_stopped = _stop_all_running_recordings(
                    "staging_disk_low",
                    log_task="ffmpeg-watchdog",
                    log_message="Recording stopped: staging disk below threshold",
                )
                await asyncio.to_thread(_promote_staging_once)
                if hard_stopped:
                    logger.error(
                        "Staging disk floor: recordings stopped",
                        task="ffmpeg-watchdog",
                        free_bytes=staging_free,
                        free_formatted=format_bytes(staging_free),
                        threshold_gb=staging_stop_gb,
                        stopped=len(hard_stopped),
                    )

            stopped = manager.stop_stalled_sessions(timeout)
            if stopped:
                logger.warning(
                    "Stuck FFmpeg sessions stopped",
                    task="ffmpeg-watchdog",
                    count=len(stopped),
                    sessions=stopped,
                )
            stressed = (
                staging_free < staging_stop_gb * DISK_GB
                or library_free < library_stop_gb * DISK_GB
                or _staging_blocked
                or _library_space_blocked
            )
            await asyncio.sleep(15 if stressed else 30)
        except Exception as e:
            logger.error(
                "FFmpeg watchdog error",
                task="ffmpeg-watchdog",
                error=str(e),
                exc_info=True,
            )
            await asyncio.sleep(60)


async def _auto_record_status_for_job(
    source_type: str,
    target_username: str,
    cached_status: Optional[dict],
) -> Optional[dict]:
    """Return the freshest status available for an auto-record target."""
    if cached_status and cached_status.get("is_online"):
        return cached_status

    source_type = _normalize_source_type(source_type) or "chaturbate"
    if source_type not in _available_source_types():
        return cached_status

    try:
        status_obj = await _provider_status(source_type, target_username)
        status = status_obj.as_dict() if hasattr(status_obj, "as_dict") else dict(status_obj)
    except Exception as e:
        logger.debug(
            "Auto-record provider status refresh failed",
            task="auto-record",
            username=target_username,
            source_type=source_type,
            error=str(e),
        )
        return cached_status

    if not status.get("is_online"):
        return cached_status or status

    if cached_status:
        try:
            await db.update_model_status(
                username=target_username,
                is_online=True,
                viewers=int(status.get("viewers") or 0),
                room_status=status.get("room_status"),
                source_type=source_type,
            )
        except Exception as e:
            logger.debug(
                "Auto-record status cache update failed",
                task="auto-record",
                username=target_username,
                source_type=source_type,
                error=str(e),
            )

    logger.debug(
        "Auto-record live status refreshed",
        task="auto-record",
        username=target_username,
        source_type=source_type,
    )
    return {**(cached_status or {}), **status, "is_online": True}


async def auto_record_task():
    """Automatically check models and start recordings (uses SQLite)"""
    failure_cooldowns: dict[str, float] = {}
    while True:
        try:
            check_interval = await get_check_interval_seconds(db)
            failure_cooldown_seconds = max(60, min(check_interval, 300))
            await asyncio.sleep(check_interval)

            if not await _recording_enabled():
                logger.debug(
                    "Auto-record skipped: global Auto Record is off",
                    task="auto-record",
                )
                continue

            if _disk_blocks_new_recordings():
                logger.debug(
                    "Auto-record skipped: disk water level freeze",
                    task="auto-record",
                )
                continue

            try:
                await _enforce_quota_on_running_sessions()
            except Exception as exc:
                logger.debug(
                    "Quota enforcement pass failed",
                    task="auto-record",
                    error=str(exc),
                )

            media_sources = await db.get_media_profile_sources_for_auto_record()
            models = await db.get_models_for_auto_record()

            jobs: list[dict] = []
            media_source_keys = {
                ((source.get("source_type") or "chaturbate"), source.get("channel_username"))
                for source in media_sources
                if source.get("channel_username")
            }

            for source in media_sources:
                profile_username = source.get("profile_username")
                channel_username = source.get("channel_username")
                source_type = source.get("source_type") or "chaturbate"
                if not profile_username or not channel_username:
                    continue
                jobs.append({
                    "profile_username": profile_username,
                    "target_username": channel_username,
                    "source_type": source_type,
                    "display_name": profile_username if profile_username == channel_username else f"{profile_username} ({channel_username})",
                    "record_quality": source.get("record_quality"),
                    "monthly_quota_gb": source.get("monthly_quota_gb"),
                    "record_path": source.get("record_path") or _default_record_path(profile_username),
                    "session_key": _recording_source_session_key(profile_username, source_type, channel_username),
                })

            for model in models:
                username = model.get("username")
                source_type = model.get("source_type") or "chaturbate"
                if not username or (source_type, username) in media_source_keys:
                    continue
                jobs.append({
                    "profile_username": username,
                    "target_username": username,
                    "source_type": source_type,
                    "display_name": username,
                    "record_quality": model.get("record_quality"),
                    "monthly_quota_gb": model.get("monthly_quota_gb"),
                    "record_path": _record_path_from_model(model, username),
                    "session_key": _recording_source_session_key(username, source_type, username),
                })

            if not jobs:
                continue

            default_quota_gb = await _get_default_monthly_quota_gb()
            cycle_start, cycle_end = await _quota_cycle_window()
            usage_map = await _recording_usage_bytes_map(
                [job["profile_username"] for job in jobs],
                include_live=True,
            )

            # Fetch active sessions
            active_sessions = _all_recording_statuses()

            for job in jobs:
                profile_username = job["profile_username"]
                target_username = job["target_username"]
                source_hint = job["source_type"]
                session_key = job["session_key"]
                cooldown_until = failure_cooldowns.get(session_key, 0)
                if cooldown_until > time.time():
                    continue

                quota_fields = _quota_fields_for_profile(
                    username=profile_username,
                    stored_quota_gb=job.get("monthly_quota_gb"),
                    default_quota_gb=default_quota_gb,
                    used_bytes=int(usage_map.get(profile_username) or 0),
                    recording_enabled=True,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                )
                if quota_fields["quotaExceeded"]:
                    logger.debug(
                        "Auto-record skipped: monthly quota exceeded",
                        task="auto-record",
                        username=profile_username,
                        used_bytes=quota_fields["monthlyQuotaUsedBytes"],
                        limit_bytes=quota_fields["monthlyQuotaLimitBytes"],
                    )
                    continue

                # Check whether already recording
                is_recording = any(
                    s.get("running")
                    and (
                        s.get("session_key") == session_key
                        or (
                            not s.get("session_key")
                            and s.get("person") == profile_username
                            and (s.get("target") in {None, "", target_username})
                        )
                    )
                    for s in active_sessions
                )

                if is_recording:
                    continue  # Already in progress

                # Check status from SQLite cache (updated by monitor)
                cached_status = await db.get_model(target_username, source_type=source_hint)
                cached_status = await _auto_record_status_for_job(
                    source_hint,
                    target_username,
                    cached_status,
                )

                if cached_status and cached_status.get('is_online'):
                    # Model online: resolve HLS stream
                    try:
                        hls_source = None
                        hls_source_url = None
                        ffmpeg_video_stream_index = None
                        stream_headers = None
                        filename_format = await _get_recording_filename_format()
                        max_height = await _get_recording_height_for_quality(
                            job.get("record_quality") or cached_status.get("record_quality")
                        )
                        source_type = await _infer_source_type(target_username, cached_status)
                        if source_type not in _available_source_types():
                            logger.warning(
                                "Unknown source for auto-record",
                                task="auto-record",
                                username=target_username,
                                source_type=source_type,
                            )
                            continue
                        record_dir = _staging_records_root() / _normalize_record_path(
                            job.get("record_path"), profile_username
                        )
                        record_dir = record_dir.resolve()
                        try:
                            resolved = await _resolve_stream(
                                source_type,
                                target_username,
                                max_height,
                                pin_variant=True,
                            )
                            ffmpeg_video_stream_index = resolved.ffmpeg_video_stream_index
                            hls_source, stream_headers, hls_source_url = _ffmpeg_stream_input(resolved)
                        except Exception as e:
                            logger.debug(
                                "Auto-record resolve failed",
                                task="auto-record",
                                username=target_username,
                                error=str(e),
                            )
                            failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds

                        if hls_source:
                            # Start recording
                            logger.background_task("auto-record", "Model detected online", username=target_username, profile=profile_username)

                            try:
                                segment_duration_seconds, segment_size_bytes = await _get_recording_segment_limits()
                                sess = await asyncio.to_thread(
                                    manager.start_session,
                                    input_url=hls_source,
                                    display_name=job["display_name"],
                                    person=profile_username,
                                    max_height=max_height,
                                    segment_duration_seconds=segment_duration_seconds,
                                    segment_size_bytes=segment_size_bytes,
                                    input_headers=stream_headers,
                                    source_url=hls_source_url,
                                    ffmpeg_video_stream_index=ffmpeg_video_stream_index,
                                    filename_format=filename_format,
                                    records_dir_for_person=str(record_dir),
                                    source_type=source_type,
                                    target=target_username,
                                    session_key=session_key,
                                )

                                if sess:
                                    logger.success("Auto-recording started",
                                                   task="auto-record",
                                                   username=target_username,
                                                   profile=profile_username,
                                                   session_id=sess.id)
                                    failure_cooldowns.pop(session_key, None)
                                    active_sessions = _all_recording_statuses()
                            except RuntimeError as e:
                                logger.warning("Unable to start recording",
                                             task="auto-record",
                                             username=target_username,
                                             error=str(e))
                                failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                                continue

                    except Exception as e:
                        logger.error("Model check error",
                                   task="auto-record",
                                   username=target_username,
                                   error=str(e))
                        failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                        continue

        except Exception as e:
            logger.error("Auto-record task error", task="auto-record", exc_info=True, error=str(e))
            await asyncio.sleep(60)


def _coerce_cleanup_retention_days(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 30


async def cleanup_old_recordings_once(now_timestamp: Optional[float] = None) -> dict[str, int]:
    """Run one retention pass using each model/source's configured window."""
    media_sources = await db.get_all_media_profile_sources()
    models = await db.get_all_models()
    media_source_keys = {
        (
            str(source.get("source_type") or "chaturbate").strip().lower(),
            source.get("channel_username"),
        )
        for source in media_sources
        if source.get("channel_username")
    }

    # One directory can be referenced by duplicate provider rows. Since old
    # recording rows do not carry a provider key, retain the most conservative
    # window for that shared directory (0/forever wins).
    path_policies: dict[tuple[str, str], tuple[Path, int]] = {}

    def add_policy(username: str, path: Path, retention_days: object) -> None:
        days = _coerce_cleanup_retention_days(retention_days)
        key = (username, str(path.resolve()))
        existing = path_policies.get(key)
        if existing:
            previous_days = existing[1]
            days = 0 if 0 in {previous_days, days} else max(previous_days, days)
        path_policies[key] = (path, days)

    for source in media_sources:
        profile_username = str(source.get("profile_username") or "").strip()
        if not profile_username:
            continue
        try:
            record_dir = _record_dir_from_path(
                _normalize_record_path(
                    source.get("record_path") or _default_record_path(profile_username),
                    profile_username,
                )
            )
        except HTTPException:
            continue
        add_policy(profile_username, record_dir, source.get("retention_days", 30))

    for model in models:
        username = str(model.get("username") or "").strip()
        source_type = str(model.get("source_type") or "chaturbate").strip().lower()
        if not username or (source_type, username) in media_source_keys:
            continue
        for record_dir in _record_dirs_for_model(username, model):
            add_policy(username, record_dir, model.get("retention_days", 30))

    grouped_jobs: dict[tuple[str, int], list[Path]] = {}
    for (username, _path_key), (path, retention_days) in path_policies.items():
        grouped_jobs.setdefault((username, retention_days), []).append(path)

    active_paths = [
        status.get("record_path")
        for status in _all_recording_statuses()
        if status.get("running") and status.get("record_path")
    ]
    totals = {
        "deleted_files": 0,
        "deleted_recordings": 0,
        "deleted_playback": 0,
        "skipped_protected": 0,
        "skipped_active": 0,
        "errors": 0,
    }
    for (username, retention_days), record_dirs in grouped_jobs.items():
        if retention_days == 0:
            logger.debug("Infinite retention, skip", task="cleanup", username=username)
            continue
        current_paths = {str(path.resolve()) for path in record_dirs}
        excluded_dirs = []
        for (policy_username, _policy_key), (policy_path, _policy_days) in path_policies.items():
            if policy_username != username or str(policy_path.resolve()) in current_paths:
                continue
            if any(policy_path.resolve().is_relative_to(root.resolve()) for root in record_dirs):
                excluded_dirs.append(policy_path)
        result = await cleanup_retention_job(
            db,
            OUTPUT_DIR,
            username,
            retention_days,
            record_dirs,
            now_timestamp=now_timestamp,
            active_paths=active_paths,
            excluded_dirs=excluded_dirs,
        )
        for key in totals:
            totals[key] += result.get(key, 0)

    logger.info("Retention cleanup finished", task="cleanup", **totals)
    return totals


async def cleanup_old_recordings_task():
    """Automatically clean old recordings according to configured retention"""
    while True:
        try:
            await asyncio.sleep(3600)
            logger.background_task("cleanup", "Starting cleanup of old recordings")
            await cleanup_old_recordings_once()
        except Exception as e:
            logger.error("Cleanup task error", task="cleanup", exc_info=True, error=str(e))
            await asyncio.sleep(3600)


@app.on_event("startup")
async def startup_event():
    """Start background tasks on application startup"""
    # Initialize the database
    await db.initialize()

    # Migrate data from JSON if needed
    await db.migrate_from_json(MODELS_FILE)
    repaired_sources = await db.reconcile_model_sources_from_followed()
    if repaired_sources:
        logger.info("Model sources repaired from favorites", count=repaired_sources)
    await _import_auto_record_users_from_env()

    try:
        from app.services import recording_projects as recording_projects_svc

        library_items = await _scan_media_library_items(refresh_metadata=False)
        backfill = await recording_projects_svc.backfill_library_fragments(db, library_items)
        if backfill.get("assigned"):
            logger.info(
                "Recording project backfill on startup",
                assigned=backfill.get("assigned"),
                skipped=backfill.get("skipped"),
            )
    except Exception as exc:
        logger.warning("Recording project startup backfill failed", error=str(exc))

    # Initialize FlareSolverr client.
    # The docker-compose healthcheck normally guarantees FlareSolverr is
    # ready before we start, but we keep a short retry loop as a safety net
    # for bare-metal / non-compose deployments.
    flare_url = await _get_flaresolverr_url()
    flaresolverr = FlareSolverrClient(flare_url)
    _apply_flaresolverr_url(flare_url, flaresolverr)
    fs_status = None
    for attempt in range(6):  # ~15s max (6 tries × 2.5s sleep between)
        fs_status = await flaresolverr.check_status()
        if fs_status["available"]:
            break
        if attempt < 5:
            await asyncio.sleep(2.5)

    if fs_status and fs_status["available"]:
        logger.info(
            "FlareSolverr connected",
            url=flare_url,
            version=fs_status.get("version"),
        )
    else:
        # Log the actual reason so users can diagnose DNS / network / timing
        # issues without having to dig through DEBUG logs.
        reason = (fs_status or {}).get("message") or "unknown"
        logger.warning(
            "FlareSolverr unavailable (optional)",
            url=flare_url,
            reason=reason,
        )

    # Initialize Chaturbate auth service
    cb_auth = ChaturbateAuthService(db, flaresolverr)
    await cb_auth.initialize()
    if CHATURBATE_USERNAME and CHATURBATE_PASSWORD:
        try:
            result = await cb_auth.login(CHATURBATE_USERNAME, CHATURBATE_PASSWORD)
            if result.get("success"):
                logger.info("Chaturbate auto-login succeeded", username=CHATURBATE_USERNAME)
            else:
                logger.warning(
                    "Chaturbate auto-login failed",
                    username=CHATURBATE_USERNAME,
                    error=result.get("error"),
                )
        except Exception as exc:
            logger.warning("Chaturbate auto-login error", username=CHATURBATE_USERNAME, error=str(exc))

    # Initialize Chaturbate API client
    global chaturbate_api
    cb_api = ChaturbateAPI(cb_auth, flaresolverr)
    chaturbate_api = cb_api

    global provider_registry, SOURCE_TYPES
    provider_registry = create_provider_registry(
        db,
        chaturbate_api=cb_api,
        chaturbate_auth=cb_auth,
        output_dir=OUTPUT_DIR,
    )
    SOURCE_TYPES = provider_registry.source_types()

    # Wire up API routers
    auth_router.init(cb_auth, flaresolverr)
    discover_router.init(cb_api, db, provider_registry)
    following_router.init(cb_api, cb_auth, db, provider_registry)

    # Set authenticated resolver for chaturbate
    from .resolvers.chaturbate import set_chaturbate_api
    set_chaturbate_api(cb_api)

    # Start background tasks
    asyncio.create_task(monitor_models_task(
        db,
        manager,
        FFMPEG_PATH,
        chaturbate_auth=cb_auth,
        provider_registry=provider_registry,
    ))
    asyncio.create_task(ffmpeg_watchdog_task())
    asyncio.create_task(auto_record_task())
    asyncio.create_task(cleanup_old_recordings_task())
    asyncio.create_task(auto_convert_recordings_task(
        db,
        OUTPUT_DIR,
        manager,
        FFMPEG_PATH,
        on_mp4_ready=_on_mp4_ready_for_mac_sync,
        should_pause_convert=_should_pause_convert_for_disk,
    ))
    asyncio.create_task(disk_pressure_task())
    if MEDIA_IMPORTS_ENABLED:
        global media_import_manager
        media_import_manager = MediaImportManager(db, OUTPUT_DIR, FFMPEG_PATH)
        asyncio.create_task(media_imports_task(media_import_manager))
    logger.info("Background tasks started",
                tasks=["monitor", "ffmpeg-watchdog", "auto-record", "cleanup", "convert", "storage-policy"])
    try:
        orphans = recording_audit.reconcile_orphans_after_restart(
            alive_session_ids=[s.get("id") for s in manager.list_status() if s.get("id")]
        )
        if orphans:
            logger.warning(
                "Recording audit reconciled orphans after restart",
                count=len(orphans),
            )
        recording_audit.refresh_busy_snapshot(manager.list_status())
    except Exception:
        pass


@app.on_event("shutdown")
async def shutdown_event():
    """Write a durable busy/recording snapshot before the process exits."""
    try:
        recording_audit.write_shutdown_snapshot(manager.list_status())
        logger.info("Recording audit shutdown snapshot written")
    except Exception as exc:
        logger.warning("Recording audit shutdown snapshot failed", error=str(exc))
    # Best-effort stop of active FFmpeg writers so partial files flush.
    try:
        for status in list(manager.list_status()):
            if status.get("running") and status.get("id"):
                await asyncio.to_thread(manager.stop_session, status["id"], "shutdown")
    except Exception as exc:
        logger.warning("Shutdown stop_session sweep failed", error=str(exc))

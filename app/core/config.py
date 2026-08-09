"""
Centralized HXYLIVE application configuration
"""

import os
from pathlib import Path
from typing import Optional


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None and value < minimum:
        return minimum
    return value


# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "data")))

# FFmpeg configuration
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
HLS_TIME = int(os.getenv("HLS_TIME", "4"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))

# Chaturbate configuration
CB_RESOLVER_ENABLED = os.getenv("CB_RESOLVER_ENABLED", "false").lower() in {"1", "true", "yes"}

# Chaturbate Authentication
CHATURBATE_USERNAME: Optional[str] = os.getenv("CHATURBATE_USERNAME")
CHATURBATE_PASSWORD: Optional[str] = os.getenv("CHATURBATE_PASSWORD")

# Backward-compatible cookie env vars (fallback)
CHATURBATE_CSRFTOKEN: Optional[str] = os.getenv("CHATURBATE_CSRFTOKEN")
CHATURBATE_SESSIONID: Optional[str] = os.getenv("CHATURBATE_SESSIONID")

# Chaturbate request settings
CB_REQUEST_DELAY: float = float(os.getenv("CB_REQUEST_DELAY", "1.0"))
CHATURBATE_REQUEST_TIMEOUT_SECONDS = _env_int("CHATURBATE_REQUEST_TIMEOUT_SECONDS", 30, minimum=5)
HXYLIVE_MAX_FOLLOW_SYNC_ITEMS = _env_int("HXYLIVE_MAX_FOLLOW_SYNC_ITEMS", 5000, minimum=1)

# Outbound proxy for provider requests (HTTP(S) or SOCKS).
# Standard HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env vars are also honored.
HXYLIVE_PROXY_URL: Optional[str] = os.getenv("HXYLIVE_PROXY_URL")

# Server configuration
PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

# Auto-record configuration
AUTO_RECORD_INTERVAL = int(os.getenv("AUTO_RECORD_INTERVAL", "120"))  # seconds
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "3600"))  # seconds

# Recording settings (defaults, overridden by DB settings at runtime)
AUTO_CONVERT = os.getenv("AUTO_CONVERT", "true").lower() in {"1", "true", "yes"}
KEEP_TS = os.getenv("KEEP_TS", "false").lower() in {"1", "true", "yes"}
RECORD_SEGMENT_DURATION_MINUTES = int(os.getenv("RECORD_SEGMENT_DURATION_MINUTES", "0"))
RECORD_SEGMENT_SIZE_MB = int(os.getenv("RECORD_SEGMENT_SIZE_MB", "0"))
MIN_RECORDING_SECONDS = int(os.getenv("MIN_RECORDING_SECONDS", "10"))
MIN_RECORDING_BYTES = int(os.getenv("MIN_RECORDING_BYTES", str(5 * 1024 * 1024)))

# Timezone
TZ = os.getenv("TZ", "UTC")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Create required directories
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "sessions").mkdir(exist_ok=True)
(OUTPUT_DIR / "records").mkdir(exist_ok=True)
(OUTPUT_DIR / "thumbnails").mkdir(exist_ok=True)
(OUTPUT_DIR / "cookies").mkdir(exist_ok=True)

# Data files
MODELS_FILE = OUTPUT_DIR / "models.json"

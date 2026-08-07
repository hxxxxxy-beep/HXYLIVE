"""Shared browser/user-agent constants for providers.

Site-specific BrowserCaptureProvider implementations were removed; only
Twitch and Chaturbate remain as discover/stream sources.
"""

from __future__ import annotations

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

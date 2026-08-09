"""
HTTP helpers for outbound provider requests.

aiohttp does not use proxy environment variables unless trust_env=True, and it
does not support SOCKS proxies without a connector. Keep that behavior in one
place so provider calls behave consistently.
"""

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp

try:
    from aiohttp_socks import ProxyConnector
except ImportError:  # pragma: no cover - only hit when optional dep is missing
    ProxyConnector = None


_CUSTOM_PROXY_ENV_NAMES = (
    "HXYLIVE_PROXY_URL",
    "PROXY_URL",
)

_STANDARD_PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)

_SOCKS_SCHEMES = {"socks4", "socks4a", "socks5", "socks5h"}
_HTTP_PROXY_SCHEMES = {"http", "https"}


def _clean_env_value(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _env_first(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = _clean_env_value(os.getenv(name))
        if value:
            return value
    return None


def get_outbound_proxy_url() -> Optional[str]:
    """Return the configured outbound proxy URL, if any.

    HXYLIVE_PROXY_URL is the app-specific setting. Standard proxy env vars
    remain supported so Docker users can keep their existing compose overrides.
    """
    return _env_first(_CUSTOM_PROXY_ENV_NAMES) or _env_first(_STANDARD_PROXY_ENV_NAMES)


def _proxy_scheme(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return ""
    return urlparse(proxy_url).scheme.lower()


def is_socks_proxy(proxy_url: Optional[str] = None) -> bool:
    return _proxy_scheme(proxy_url or get_outbound_proxy_url()) in _SOCKS_SCHEMES


def aiohttp_request_kwargs() -> Dict[str, Any]:
    """Per-request kwargs for aiohttp provider calls."""
    proxy_url = get_outbound_proxy_url()
    scheme = _proxy_scheme(proxy_url)
    if proxy_url and scheme in _HTTP_PROXY_SCHEMES:
        return {"proxy": proxy_url}
    return {}


def ffmpeg_http_proxy_url() -> Optional[str]:
    """Return a proxy URL compatible with FFmpeg's HTTP protocol option."""
    proxy_url = get_outbound_proxy_url()
    return proxy_url if _proxy_scheme(proxy_url) in _HTTP_PROXY_SCHEMES else None


@asynccontextmanager
async def aiohttp_client_session(**kwargs):
    """Create an aiohttp ClientSession configured for app proxy settings."""
    proxy_url = get_outbound_proxy_url()
    connector = kwargs.pop("connector", None)

    if is_socks_proxy(proxy_url):
        if ProxyConnector is None:
            raise RuntimeError(
                "SOCKS proxy configured but aiohttp-socks is not installed"
            )
        connector = ProxyConnector.from_url(proxy_url)
        kwargs["trust_env"] = False
    else:
        kwargs.setdefault("trust_env", True)

    async with aiohttp.ClientSession(connector=connector, **kwargs) as session:
        yield session

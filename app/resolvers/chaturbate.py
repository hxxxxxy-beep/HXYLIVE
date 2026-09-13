import re
import html
import aiohttp
from typing import Optional
from urllib.parse import urljoin
from .base import ResolveError
from ..logger import logger
from ..core.config import CHATURBATE_REQUEST_TIMEOUT_SECONDS
from ..core.http_client import (
    aiohttp_client_session,
    aiohttp_request_kwargs,
)

# Optional ChaturbateAPI instance (set at startup)
_chaturbate_api = None


def _quality_field_order(max_height: Optional[int] = None):
    """Return Chaturbate HLS fields from preferred to fallback."""
    labels = {
        'hls_source_1080p': '1080p',
        'hls_source_hd': 'HD',
        'hls_source_high': 'High',
        'hls_source_720p': '720p',
        'hls_source': 'Standard',
    }

    if not max_height or max_height <= 0:
        order = [
            'hls_source_1080p', 'hls_source_hd',
            'hls_source_high', 'hls_source_720p', 'hls_source',
        ]
    else:
        order = []
        if max_height >= 1080:
            order.extend(['hls_source_1080p', 'hls_source_hd'])
        if max_height >= 720:
            order.extend(['hls_source_720p', 'hls_source_high'])
        order.append('hls_source')
        # Last-resort fallbacks keep recording available if Chaturbate omits
        # the exact capped field. Master playlists are still capped later.
        for field_name in [
            'hls_source_1080p', 'hls_source_hd',
            'hls_source_high', 'hls_source_720p',
        ]:
            if field_name not in order:
                order.append(field_name)

    return [(field_name, labels[field_name]) for field_name in order]


def set_chaturbate_api(api):
    """Set the ChaturbateAPI instance for authenticated resolution"""
    global _chaturbate_api
    _chaturbate_api = api


async def resolve_m3u8_async(username: str, max_height: Optional[int] = None) -> str:
    """
    Async M3U8 resolver with authentication support.
    Resolution chain:
    1. Authenticated get_edge_hls_url (if available)
    2. chatvideocontext API via ChaturbateAPI (FlareSolverr-aware)
    3. HTML scraping fallback

    Args:
        username: target model
        max_height: optional max resolution (e.g. 720). None = best available.
    """
    logger.subsection(f"Async M3U8 resolve - {username}")

    username = username.strip().lower()
    if not username or not re.match(r'^[a-z0-9_]+$', username):
        raise ResolveError("Invalid username")

    # Method 1: Authenticated edge HLS (best quality)
    if _chaturbate_api:
        try:
            hls_url = await _chaturbate_api.get_edge_hls_url(username)
            if hls_url:
                logger.success("M3U8 resolved via authenticated API", username=username)
                return await _resolve_variant(hls_url, max_height=max_height)
        except Exception as e:
            logger.debug("Auth resolution failed, falling back", error=str(e))

        # Method 2: chatvideocontext through the same CF-aware client.
        # If the room is offline/private, stop here — do not scrape HTML and
        # surface a misleading HTTP 403 from Cloudflare.
        try:
            api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
            resp = await _chaturbate_api._request("GET", api_url)
            if resp and resp.status == 200:
                api_data = resp.json()
                if isinstance(api_data, dict):
                    for field_name, _quality_label in _quality_field_order(max_height):
                        if api_data.get(field_name):
                            logger.success("M3U8 resolved via chatvideocontext", username=username, field=field_name)
                            return await _resolve_variant(api_data[field_name], max_height=max_height)
                    room_status = str(api_data.get("room_status") or "").strip().lower() or "offline"
                    if room_status in {
                        "private",
                        "group",
                        "ticket",
                        "password_protected",
                        "hidden",
                        "p2p",
                    }:
                        raise ResolveError(f"{username} est en {room_status}")
                    raise ResolveError(f"{username} is offline")
            elif resp is not None and resp.status == 404:
                raise ResolveError(f"{username} not found")
        except ResolveError:
            raise
        except Exception as e:
            logger.debug("Chatvideocontext resolve failed, falling back to HTML", username=username, error=str(e))

    # Method 3: Fallback to async resolver (non-blocking for event loop)
    return await _resolve_m3u8_async_fallback(username, max_height=max_height)


def _html_from_api_response(resp) -> str:
    body = getattr(resp, "_body", b"") or b""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


async def _resolve_m3u8_async_fallback(username: str, max_height: Optional[int] = None) -> str:
    """Async fallback: try the chatvideocontext API, then scrape the HTML page.
    Uses aiohttp so the FastAPI event loop isn't blocked during resolution."""
    username = username.strip().lower()
    if not username or not re.match(r'^[a-z0-9_]+$', username):
        raise ResolveError("Invalid username")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://chaturbate.com/",
    }

    try:
        # Prefer FlareSolverr-aware client when available for HTML scrape too.
        if _chaturbate_api:
            try:
                api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
                api_resp = await _chaturbate_api._request("GET", api_url)
                if api_resp and api_resp.status == 200:
                    api_data = api_resp.json()
                    if isinstance(api_data, dict):
                        for field_name, _quality_label in _quality_field_order(max_height):
                            if api_data.get(field_name):
                                return await _resolve_variant(api_data[field_name], max_height=max_height)
                        room_status = str(api_data.get("room_status") or "").strip().lower() or "offline"
                        if room_status in {
                            "private",
                            "group",
                            "ticket",
                            "password_protected",
                            "hidden",
                            "p2p",
                        }:
                            raise ResolveError(f"{username} est en {room_status}")
                        raise ResolveError(f"{username} is offline")
                html_resp = await _chaturbate_api._request("GET", f"https://chaturbate.com/{username}/")
                if html_resp is None:
                    raise ResolveError("Network error: Chaturbate HTML unreachable")
                if html_resp.status != 200:
                    raise ResolveError(f"Unable to access page (HTTP {html_resp.status})")
                html_content = _html_from_api_response(html_resp)
            except ResolveError:
                raise
            except Exception as e:
                logger.debug("API-client HTML resolve failed", username=username, error=str(e))
                html_content = None
            if html_content is not None:
                return await _extract_m3u8_from_html(html_content, username, max_height)

        async with aiohttp_client_session() as session:
            # 1) API chatvideocontext
            api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
            try:
                async with session.get(
                    api_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                    ssl=False,
                    **aiohttp_request_kwargs(),
                ) as api_resp:
                    if api_resp.status == 200:
                        api_data = await api_resp.json(content_type=None)
                        best_m3u8 = None
                        for field_name, _quality_label in _quality_field_order(max_height):
                            if api_data.get(field_name):
                                best_m3u8 = api_data[field_name]
                                break
                        if best_m3u8:
                            return await _resolve_variant(best_m3u8, max_height=max_height)
                        room_status = str(api_data.get("room_status") or "").strip().lower() or "offline"
                        if room_status in {
                            "private",
                            "group",
                            "ticket",
                            "password_protected",
                            "hidden",
                            "p2p",
                        }:
                            raise ResolveError(f"{username} est en {room_status}")
                        if not api_data.get("hls_source"):
                            raise ResolveError(f"{username} is offline")
            except ResolveError:
                raise
            except Exception as e:
                logger.debug("Async API resolve failed, falling back to HTML", username=username, error=str(e))

            # 2) Fallback: parse HTML page
            html_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            async with session.get(
                f"https://chaturbate.com/{username}/",
                headers=html_headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status != 200:
                    raise ResolveError(f"Unable to access page (HTTP {resp.status})")
                html_content = await resp.text()

        return await _extract_m3u8_from_html(html_content, username, max_height)
    except ResolveError:
        raise
    except Exception as e:
        raise ResolveError(f"Network error: {str(e)}")


async def _extract_m3u8_from_html(html_content: str, username: str, max_height: Optional[int] = None) -> str:
    m3u8_patterns = [
        r'"(https?://[^"]*\.m3u8[^"]*)"',
        r"'(https?://[^']*\.m3u8[^']*)'",
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
    ]
    for pattern in m3u8_patterns:
        matches = re.findall(pattern, html_content, re.IGNORECASE)
        if matches:
            m3u8_url = matches[0] if not isinstance(matches[0], tuple) else matches[0][-1]
            m3u8_url = m3u8_url.replace("\\/", "/").replace("\\", "")
            m3u8_url = html.unescape(m3u8_url)
            m3u8_url = re.sub(r'u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), m3u8_url)
            m3u8_url = m3u8_url.rstrip('",;: \t\n\r')
            if m3u8_url.startswith("http") and ".m3u8" in m3u8_url:
                return await _resolve_variant(m3u8_url, max_height=max_height)

    if "offline" in html_content.lower():
        raise ResolveError(f"{username} is offline")
    raise ResolveError(f"Unable to find M3U8 stream for {username}")


def _parse_master_playlist(text: str):
    """Parse a master HLS playlist.

    Returns a list of {url, width, height, bandwidth} for each variant.
    Variants without RESOLUTION info keep height=0 (sorted last).
    """
    variants = []
    variant_index = 0
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = {}
            res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            if res_match:
                attrs["width"] = int(res_match.group(1))
                attrs["height"] = int(res_match.group(2))
            if bw_match:
                attrs["bandwidth"] = int(bw_match.group(1))
            # Next non-comment line is the URL
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                attrs["url"] = lines[j].strip()
                attrs["index"] = variant_index
                variant_index += 1
                variants.append(attrs)
                i = j + 1
                continue
        i += 1
    return variants


def _pick_variant_info(variants, max_height: Optional[int]):
    """Pick a variant entry given a max height constraint.

    - max_height None or <=0: highest resolution, highest bandwidth.
    - max_height set: best variant whose height <= max_height. Fallback to the
      lowest-resolution variant if all are taller.
    - On ties (same height), prefer highest bandwidth.
    """
    if not variants:
        return None

    def sort_key(v):
        return (v.get("height", 0), v.get("bandwidth", 0))

    if not max_height or max_height <= 0:
        return sorted(variants, key=sort_key, reverse=True)[0]

    eligible = [v for v in variants if v.get("height", 0) <= max_height]
    if eligible:
        return sorted(eligible, key=sort_key, reverse=True)[0]
    # Nothing fits — return the smallest to save bandwidth
    return sorted(variants, key=sort_key)[0]


def _pick_variant(variants, max_height: Optional[int]):
    picked = _pick_variant_info(variants, max_height)
    return picked["url"] if picked else None


async def resolve_llhls_master_playlist(
    m3u8_url: str,
    max_height: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
) -> Optional[dict[str, object]]:
    """Fetch a Chaturbate LL-HLS master once and return playlist metadata."""
    if "llhls.m3u8" not in (m3u8_url or "").lower():
        return None

    request_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://chaturbate.com/",
        "Origin": "https://chaturbate.com",
    }
    request_headers.update(headers or {})

    try:
        async with aiohttp_client_session() as session:
            async with session.get(
                m3u8_url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "Chaturbate LL-HLS master fetch failed",
                        status=resp.status,
                    )
                    return None
                text = await resp.text()
                content_type = resp.headers.get("Content-Type", "")
                base_url = str(resp.url)
    except Exception as e:
        logger.debug("Could not fetch Chaturbate LL-HLS master", error=str(e))
        return None

    variants = _parse_master_playlist(text)
    picked = _pick_variant_info(variants, max_height)
    return {
        "video_stream_index": int(picked.get("index", 0)) if picked else None,
        "text": text,
        "base_url": base_url,
        "content_type": content_type,
    }


async def _resolve_variant(m3u8_url: str, max_height: Optional[int] = None) -> str:
    """If URL is a traditional master playlist, pick a variant according to max_height.

    Only operates on playlist.m3u8 (non-LL-HLS muxed streams).
    LL-HLS edge URLs (llhls.m3u8) carry separate audio rendition groups that are
    only resolvable from the master playlist — ffmpeg must receive the master URL
    so it can map both the video variant and the audio rendition. Passing a
    video-only chunk URL to ffmpeg results in silent recordings.
    """
    if 'playlist.m3u8' not in m3u8_url:
        return m3u8_url

    try:
        async with aiohttp_client_session() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            async with session.get(
                m3u8_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS), ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    variants = _parse_master_playlist(text)
                    picked = _pick_variant(variants, max_height)
                    if picked:
                        logger.debug("HLS variant picked",
                                    max_height=max_height,
                                    variant=picked,
                                    candidates=len(variants))
                        return urljoin(m3u8_url, picked)
    except Exception as e:
        logger.debug("Could not extract variant from playlist", error=str(e))

    return m3u8_url

"""YouTube discover provider (exact handle lookup + yt-dlp stream).

Discover search resolves a single @handle / channel id (or pasted channel URL)
via Channels.list forHandle, then probes live status with Search channelId.
Stream resolve uses yt-dlp on the channel /live URL or watch URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

from .base import ProviderAuthError, ProviderCapabilities, ProviderError, ProviderStatus
from .browser import DEFAULT_USER_AGENT
from .sessions import ProviderSessionStore
from .ytdlp import YtDlpProvider
from ..core.config import HXYLIVE_MAX_FOLLOW_SYNC_ITEMS
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from ..following_sync import FollowedSyncResult
from ..services.youtube_categories import (
    DEFAULT_YOUTUBE_LIVE_QUERY,
    normalize_youtube_live_query,
    youtube_discover_tags,
    youtube_live_query_q,
    youtube_video_category_title,
)

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_YOUTUBE_ORIGIN = "https://www.youtube.com"
_INNERTUBE_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_INNERTUBE_CLIENT_VERSION = "2.20250320.01.00"
_SESSION_HINT = (
    "YouTube password login is not supported; import a Chrome session that includes "
    "SAPISID (or Secure PAPISID) plus SIDCC/PSIDCC cookies"
)

_UNIQUE_POOL_TTL_SECONDS = 900.0  # Reuse search pages; Search API costs 100 units each
_STALE_CATALOGUE_TTL_SECONDS = 6 * 3600.0
_MAX_UPSTREAM_GETS_PER_CALL = 3
_UNIQUE_POOL_MAX_KEYS = 32
_ZERO_UNIQUE_STOP_WINDOWS = 2
_FOLLOWER_CACHE_TTL_SECONDS = 600.0

_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")

_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
})


def is_youtube_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if host in _YOUTUBE_HOSTS:
        return True
    return host.endswith(".youtube.com")


def is_youtube_channel_id(value: str) -> bool:
    return bool(_CHANNEL_ID_RE.fullmatch(str(value or "").strip()))


def is_youtube_video_id(value: str) -> bool:
    text = str(value or "").strip()
    if not _VIDEO_ID_RE.fullmatch(text) or is_youtube_channel_id(text):
        return False
    has_digit = any(ch.isdigit() for ch in text)
    has_upper = any(ch.isupper() for ch in text)
    has_lower = any(ch.islower() for ch in text)
    has_sep = "-" in text or "_" in text
    return has_sep or (has_digit and has_upper and has_lower)


def _clean_handle(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    if text.lower().startswith("uc") and is_youtube_channel_id(text):
        return text
    return text.strip("/")


def youtube_username_from_url(url: str) -> Optional[str]:
    """Extract a stable YouTube identity from a watch/channel/handle URL."""
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not is_youtube_host(host):
        return None
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    query = parse_qs(parsed.query or "")

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = (path_parts[0] if path_parts else "").split("?")[0]
        return video_id if _VIDEO_ID_RE.fullmatch(video_id) else None

    if path_parts:
        head = path_parts[0]
        if head in {"watch", "live"}:
            video_id = (query.get("v") or [None])[0]
            if not video_id and head == "live" and len(path_parts) > 1:
                video_id = path_parts[1]
            video_id = str(video_id or "").strip()
            return video_id if _VIDEO_ID_RE.fullmatch(video_id) else None
        if head == "embed" and len(path_parts) > 1:
            video_id = path_parts[1]
            return video_id if _VIDEO_ID_RE.fullmatch(video_id) else None
        if head == "channel" and len(path_parts) > 1:
            channel_id = path_parts[1]
            return channel_id if is_youtube_channel_id(channel_id) else None
        if head in {"c", "user"} and len(path_parts) > 1:
            return _clean_handle(path_parts[1]) or None
        if head.startswith("@"):
            return _clean_handle(head) or None

    video_id = (query.get("v") or [None])[0]
    if video_id and _VIDEO_ID_RE.fullmatch(str(video_id).strip()):
        return str(video_id).strip()
    return None


def normalize_youtube_discover_identity(value: str) -> Optional[str]:
    """Exact Discover search identity: @handle, bare handle, or channel URL/id.

    Display names and watch URLs are rejected so search stays 1:1.
    """
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        candidate = text if "://" in text else f"https://{text.lstrip('/')}"
        identity = youtube_username_from_url(candidate)
        if not identity:
            return None
        if is_youtube_channel_id(identity):
            return identity
        if is_youtube_video_id(identity):
            return None
        handle = _clean_handle(identity)
        if not handle or " " in handle or "/" in handle:
            return None
        return handle
    handle = _clean_handle(text)
    if not handle or " " in handle or "/" in handle:
        return None
    return handle


class YouTubeProvider(YtDlpProvider):
    def __init__(
        self,
        source_type,
        display_name,
        url_template,
        domains,
        session_store=None,
    ):
        super().__init__(
            source_type,
            display_name,
            url_template,
            domains,
            session_store,
        )
        self.capabilities = ProviderCapabilities(
            can_login=True,
            can_password_login=False,
            can_follow=True,
            can_sync_following=True,
            can_discover=True,
            can_stream=True,
            can_record=True,
            uses_ytdlp=True,
        )
        self.api_key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
        self.live_query = (
            normalize_youtube_live_query(
                os.getenv("YOUTUBE_LIVE_QUERY") or DEFAULT_YOUTUBE_LIVE_QUERY
            )
            or DEFAULT_YOUTUBE_LIVE_QUERY
        )
        self.video_category_id = None
        self._unique_pools: dict[tuple, dict[str, Any]] = {}
        self._stale_catalogue: dict[tuple, dict[str, Any]] = {}
        self._pool_lock_obj: Optional[asyncio.Lock] = None
        self._follower_cache: dict[str, tuple[float, int]] = {}
        self._channel_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _pool_lock(self) -> asyncio.Lock:
        if self._pool_lock_obj is None:
            self._pool_lock_obj = asyncio.Lock()
        return self._pool_lock_obj

    def canonical_url(self, target: str) -> str:
        raw = (target or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            identity = youtube_username_from_url(raw)
            if identity:
                return self.canonical_url(identity)
            return raw
        handle = _clean_handle(raw)
        if is_youtube_channel_id(handle):
            return f"https://www.youtube.com/channel/{handle}/live"
        if is_youtube_video_id(handle):
            return f"https://www.youtube.com/watch?v={handle}"
        if handle:
            return f"https://www.youtube.com/@{handle}/live"
        return self.url_template.format(username=raw)

    @staticmethod
    def _model_identity(model: dict[str, Any]) -> str:
        channel_id = str(model.get("user_id") or "").strip()
        if is_youtube_channel_id(channel_id):
            return f"youtube:{channel_id}"
        username = str(model.get("username") or "").strip().lower()
        if username:
            return f"youtube:{username}"
        video_id = str(model.get("video_id") or "").strip()
        if video_id:
            return f"youtube-video:{video_id}"
        return ""

    @staticmethod
    def _stamp_stable_id(model: dict[str, Any]) -> dict[str, Any]:
        identity = YouTubeProvider._model_identity(model)
        if identity:
            model["id"] = identity
        return model

    @staticmethod
    def _as_nonneg_int(value: Any) -> int:
        try:
            return max(0, int(str(value).strip()))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _thumbnail_from_snippet(snippet: dict[str, Any]) -> str:
        thumbs = snippet.get("thumbnails") if isinstance(snippet, dict) else None
        if not isinstance(thumbs, dict):
            return ""
        for key in ("maxres", "standard", "high", "medium", "default"):
            row = thumbs.get(key)
            if isinstance(row, dict) and row.get("url"):
                return str(row.get("url") or "").strip()
        return ""

    def _pool_key(
        self,
        *,
        video_category_id: Optional[str],
        live_query: Optional[str],
        search: str,
        limit: int,
    ) -> tuple:
        return (
            str(live_query or ""),
            str(video_category_id or ""),
            str(search or "").strip().lower(),
            int(limit),
        )

    @staticmethod
    def _is_quota_error(exc: BaseException) -> bool:
        text = str(exc or "").lower()
        return "429" in text or "quota" in text or "rate limit" in text

    @staticmethod
    def _provider_error_detail(exc: BaseException) -> str:
        text = str(exc or "").strip() or "YouTube request failed"
        if YouTubeProvider._is_quota_error(exc):
            return (
                "YouTube daily Search quota exceeded (ASMR discovery uses "
                "100 units per search). Quota resets at midnight Pacific Time. "
                "Cached results are shown when available."
            )
        return text

    def _remember_stale_catalogue(self, pool_key: tuple, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        self._stale_catalogue[pool_key] = {
            "items": [dict(item) for item in items],
            "saved_at": time.monotonic(),
        }
        if len(self._stale_catalogue) <= _UNIQUE_POOL_MAX_KEYS:
            return
        ordered = sorted(
            self._stale_catalogue.items(),
            key=lambda item: float(item[1].get("saved_at") or 0),
        )
        overflow = len(self._stale_catalogue) - _UNIQUE_POOL_MAX_KEYS
        for key, _state in ordered[:overflow]:
            self._stale_catalogue.pop(key, None)

    def _restore_stale_catalogue(self, pool_key: tuple, state: dict[str, Any]) -> bool:
        stale = self._stale_catalogue.get(pool_key)
        if not stale:
            return False
        age = time.monotonic() - float(stale.get("saved_at") or 0)
        if age > _STALE_CATALOGUE_TTL_SECONDS:
            self._stale_catalogue.pop(pool_key, None)
            return False
        items = list(stale.get("items") or [])
        if not items:
            return False
        state["items"] = [dict(item) for item in items]
        state["seen_ids"] = {
            identity
            for identity in (self._model_identity(item) for item in state["items"])
            if identity
        }
        state["exhausted"] = True
        state["next_cursor"] = None
        state["updated_at"] = time.monotonic()
        return True

    @staticmethod
    def _compose_live_search_query(needle: str, live_query: Optional[str]) -> str:
        """Build Search API q= for a named lookup.

        Category ``live_query`` (e.g. ASMR) applies to catalogue browse only.
        Prefixing it onto username/handle search hides the channel and burns quota.
        """
        text = str(needle or "").strip()
        _ = live_query
        return text

    def _prune_unique_pools(self, now: Optional[float] = None) -> None:
        pools = self._unique_pools
        if not pools:
            return
        now = time.monotonic() if now is None else float(now)
        expired = [
            key
            for key, state in list(pools.items())
            if now - float(state.get("updated_at") or 0) > _UNIQUE_POOL_TTL_SECONDS
        ]
        for key in expired:
            pools.pop(key, None)
        if len(pools) <= _UNIQUE_POOL_MAX_KEYS:
            return
        ordered = sorted(
            pools.items(),
            key=lambda item: float(item[1].get("updated_at") or 0),
        )
        overflow = len(pools) - _UNIQUE_POOL_MAX_KEYS
        for key, _state in ordered[:overflow]:
            pools.pop(key, None)

    def _unique_pool_state(self, pool_key: tuple) -> dict[str, Any]:
        now = time.monotonic()
        self._prune_unique_pools(now)
        state = self._unique_pools.get(pool_key)
        if state and now - float(state.get("updated_at") or 0) <= _UNIQUE_POOL_TTL_SECONDS:
            state["updated_at"] = now
            return state
        state = {
            "items": [],
            "seen_ids": set(),
            "next_cursor": None,
            "exhausted": False,
            "consecutive_zero_unique_windows": 0,
            "created_at": now,
            "updated_at": now,
        }
        self._unique_pools[pool_key] = state
        self._prune_unique_pools(now)
        return state

    def _pagination_contract(
        self,
        *,
        page: int,
        limit: int,
        page_items: list[dict[str, Any]],
        pool_len: int,
        exhausted: bool,
    ) -> tuple[int, int]:
        if not page_items:
            if exhausted:
                total = pool_len
                total_pages = max(1, (total + limit - 1) // limit) if total else max(1, page)
            else:
                total = max(pool_len, page * limit)
                total_pages = page + 1
        elif exhausted:
            total = pool_len
            total_pages = max(1, (total + limit - 1) // limit)
        else:
            total = max(pool_len, page * limit + 1)
            total_pages = page + 1
        return int(total), int(total_pages)

    def _auth_required_payload(self, page: int, limit: int) -> dict[str, Any]:
        return {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "provider_status": "auth_required",
            "provider_detail": "YouTube API key is not configured.",
        }

    async def _youtube_api_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("YOUTUBE_API_KEY is required")
        query = dict(params or {})
        query["key"] = self.api_key
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                url,
                params=query,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
        if not isinstance(payload, dict):
            raise RuntimeError("YouTube API returned invalid JSON")
        if status >= 400:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = error.get("message") or payload.get("message") or str(payload)
            raise RuntimeError(f"YouTube API failed ({status}): {detail}")
        return payload

    async def _search_live_page(
        self,
        *,
        first: int,
        page_token: Optional[str] = None,
        query: str = "",
        video_category_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "part": "snippet",
            "eventType": "live",
            "type": "video",
            "order": "viewCount",
            "maxResults": str(max(1, min(50, first))),
            "safeSearch": "none",
        }
        if page_token:
            params["pageToken"] = page_token
        if query:
            params["q"] = query
        if channel_id:
            params["channelId"] = channel_id
        # videoCategoryId path removed — Discover is ASMR live-search only.
        _ = video_category_id
        return await self._youtube_api_json(_SEARCH_URL, params)

    async def _videos_by_id(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [vid for vid in video_ids if _VIDEO_ID_RE.fullmatch(str(vid or "").strip())]
        if not ids:
            return {}
        payload = await self._youtube_api_json(
            _VIDEOS_URL,
            {
                "part": "snippet,liveStreamingDetails,statistics",
                "id": ",".join(ids[:50]),
                "maxResults": "50",
            },
        )
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("id") or "").strip()
            if video_id:
                out[video_id] = item
        return out

    async def _channels_by_id(self, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [cid for cid in channel_ids if is_youtube_channel_id(cid)]
        if not ids:
            return {}
        payload = await self._youtube_api_json(
            _CHANNELS_URL,
            {
                "part": "snippet,statistics",
                "id": ",".join(ids[:50]),
                "maxResults": "50",
            },
        )
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get("id") or "").strip()
            if channel_id:
                out[channel_id] = item
        return out

    async def _channel_by_handle(self, handle: str) -> Optional[dict[str, Any]]:
        needle = _clean_handle(handle)
        if not needle:
            return None
        cached = self._channel_cache.get(needle.lower())
        if cached and time.monotonic() < cached[0]:
            return dict(cached[1])
        if is_youtube_channel_id(needle):
            found = await self._channels_by_id([needle])
            row = found.get(needle)
        else:
            payload = await self._youtube_api_json(
                _CHANNELS_URL,
                {
                    "part": "snippet,statistics",
                    "forHandle": f"@{needle}",
                },
            )
            rows = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
            row = rows[0] if rows else None
        if not row:
            return None
        self._channel_cache[needle.lower()] = (
            time.monotonic() + _FOLLOWER_CACHE_TTL_SECONDS,
            dict(row),
        )
        return dict(row)

    def _username_from_channel(self, channel: dict[str, Any], fallback: str = "") -> str:
        snippet = channel.get("snippet") if isinstance(channel.get("snippet"), dict) else {}
        custom = str(snippet.get("customUrl") or "").strip()
        if custom.startswith("@"):
            handle = _clean_handle(custom)
            if handle:
                return handle
        channel_id = str(channel.get("id") or "").strip()
        if is_youtube_channel_id(channel_id):
            return channel_id
        return _clean_handle(fallback) or channel_id or fallback

    def _search_model(self, item: dict[str, Any]) -> dict[str, Any]:
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        identity = item.get("id") if isinstance(item.get("id"), dict) else {}
        video_id = str(identity.get("videoId") or item.get("video_id") or "").strip()
        channel_id = str(snippet.get("channelId") or "").strip()
        title = str(snippet.get("title") or "").strip()
        display = str(snippet.get("channelTitle") or title or channel_id).strip()
        username = _clean_handle(display) or channel_id or video_id
        thumbnail = self._thumbnail_from_snippet(snippet)
        if not thumbnail and video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        model = {
            "username": username,
            "user_id": channel_id,
            "video_id": video_id,
            "display_name": display,
            "source_type": "youtube",
            "is_online": True,
            "room_status": "public",
            "viewers": 0,
            "thumbnail": thumbnail,
            "tags": [],
            "title": title,
            "category": "",
            "video_category_id": "",
            "started_at": "",
            "channel_url": self.canonical_url(username if username else channel_id),
        }
        return self._stamp_stable_id(model)

    def _video_model(
        self,
        video: dict[str, Any],
        *,
        channel: Optional[dict[str, Any]] = None,
        stub: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
        live = (
            video.get("liveStreamingDetails")
            if isinstance(video.get("liveStreamingDetails"), dict)
            else {}
        )
        channel_snippet = (
            channel.get("snippet") if isinstance((channel or {}).get("snippet"), dict) else {}
        )
        channel_stats = (
            channel.get("statistics") if isinstance((channel or {}).get("statistics"), dict) else {}
        )
        video_id = str(video.get("id") or (stub or {}).get("video_id") or "").strip()
        channel_id = str(
            snippet.get("channelId")
            or (channel or {}).get("id")
            or (stub or {}).get("user_id")
            or ""
        ).strip()
        username = self._username_from_channel(
            channel or {},
            fallback=str((stub or {}).get("username") or snippet.get("channelTitle") or channel_id),
        )
        display = str(
            channel_snippet.get("title")
            or snippet.get("channelTitle")
            or (stub or {}).get("display_name")
            or username
        ).strip()
        viewers = self._as_nonneg_int(live.get("concurrentViewers"))
        thumbnail = self._thumbnail_from_snippet(snippet) or str((stub or {}).get("thumbnail") or "")
        if not thumbnail and video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        avatar = self._thumbnail_from_snippet(channel_snippet)
        category_id = str(snippet.get("categoryId") or "").strip()
        category_title = youtube_video_category_title(category_id) or ""
        raw_tags = snippet.get("tags") if isinstance(snippet.get("tags"), list) else None
        started = str(live.get("actualStartTime") or "").strip()
        subscribers = None
        if channel_stats.get("hiddenSubscriberCount") is not True:
            if channel_stats.get("subscriberCount") not in (None, ""):
                subscribers = self._as_nonneg_int(channel_stats.get("subscriberCount"))
        is_live = str(snippet.get("liveBroadcastContent") or "live").lower() in {"live", "upcoming"}
        if live.get("actualStartTime") and not live.get("actualEndTime"):
            is_live = True
        model = {
            "username": username,
            "user_id": channel_id,
            "video_id": video_id,
            "display_name": display,
            "source_type": "youtube",
            "is_online": bool(is_live and not live.get("actualEndTime")),
            "room_status": "public" if is_live else "offline",
            "viewers": viewers,
            "concurrentViewers": live.get("concurrentViewers"),
            "thumbnail": thumbnail,
            "profile_image_url": avatar,
            "tags": youtube_discover_tags(category_id=category_id, snippet_tags=raw_tags),
            "title": str(snippet.get("title") or (stub or {}).get("title") or "").strip(),
            "category": category_title,
            "video_category_id": category_id,
            "started_at": started,
            "channel_url": (
                f"https://www.youtube.com/channel/{channel_id}/live"
                if is_youtube_channel_id(channel_id)
                else self.canonical_url(username)
            ),
            "followers": subscribers,
        }
        if not model["is_online"]:
            model["viewers"] = 0
            model["room_status"] = "offline"
        return self._stamp_stable_id(model)

    def _offline_channel_model(self, channel: dict[str, Any], fallback: str = "") -> dict[str, Any]:
        snippet = channel.get("snippet") if isinstance(channel.get("snippet"), dict) else {}
        stats = channel.get("statistics") if isinstance(channel.get("statistics"), dict) else {}
        channel_id = str(channel.get("id") or "").strip()
        username = self._username_from_channel(channel, fallback=fallback)
        subscribers = None
        if stats.get("hiddenSubscriberCount") is not True:
            if stats.get("subscriberCount") not in (None, ""):
                subscribers = self._as_nonneg_int(stats.get("subscriberCount"))
        return self._stamp_stable_id({
            "username": username,
            "user_id": channel_id,
            "video_id": "",
            "display_name": str(snippet.get("title") or username).strip(),
            "source_type": "youtube",
            "is_online": False,
            "room_status": "offline",
            "viewers": 0,
            "thumbnail": self._thumbnail_from_snippet(snippet),
            "profile_image_url": self._thumbnail_from_snippet(snippet),
            "tags": [],
            "title": str(snippet.get("description") or "").strip(),
            "category": "",
            "started_at": "",
            "channel_url": self.canonical_url(username),
            "followers": subscribers,
        })

    async def _hydrate_models(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not models:
            return models
        video_ids = [str(item.get("video_id") or "").strip() for item in models]
        video_ids = [vid for vid in video_ids if vid]
        videos: dict[str, dict[str, Any]] = {}
        if video_ids:
            try:
                videos = await self._videos_by_id(video_ids)
            except Exception:
                videos = {}
        channel_ids = []
        for item in models:
            cid = str(item.get("user_id") or "").strip()
            video = videos.get(str(item.get("video_id") or "").strip()) or {}
            snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
            cid = str(snippet.get("channelId") or cid).strip()
            if cid:
                channel_ids.append(cid)
        channels: dict[str, dict[str, Any]] = {}
        if channel_ids:
            try:
                channels = await self._channels_by_id(channel_ids)
            except Exception:
                channels = {}
        hydrated: list[dict[str, Any]] = []
        for item in models:
            video_id = str(item.get("video_id") or "").strip()
            video = videos.get(video_id)
            channel_id = str(item.get("user_id") or "").strip()
            if video:
                snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
                channel_id = str(snippet.get("channelId") or channel_id).strip()
            channel = channels.get(channel_id)
            if video:
                live = self._video_model(video, channel=channel, stub=item)
                if not str(live.get("profile_image_url") or "") and item.get("profile_image_url"):
                    live["profile_image_url"] = item.get("profile_image_url")
                hydrated.append(live)
                continue
            row = dict(item)
            if channel:
                offline = self._offline_channel_model(channel, fallback=str(row.get("username") or ""))
                row["profile_image_url"] = offline.get("profile_image_url")
                row["followers"] = offline.get("followers")
                row["display_name"] = offline.get("display_name") or row.get("display_name")
                row["username"] = offline.get("username") or row.get("username")
                row["channel_url"] = self.canonical_url(row["username"])
            hydrated.append(self._stamp_stable_id(row))
        return hydrated

    @staticmethod
    def _search_name_hit(model: dict[str, Any], needle_lower: str) -> bool:
        if not needle_lower:
            return False
        needle = needle_lower.lstrip("@")
        uname = str(model.get("username") or "").strip().lower().lstrip("@")
        dname = str(model.get("display_name") or "").strip().lower()
        title = str(model.get("title") or "").strip().lower()
        uid = str(model.get("user_id") or "").strip().lower()
        return (
            needle in uname
            or needle in dname
            or needle in title
            or needle == uid
        )

    @staticmethod
    def _search_rank_key(model: dict[str, Any], needle_lower: str) -> tuple:
        uname = str(model.get("username") or "").strip().lower()
        dname = str(model.get("display_name") or "").strip().lower()
        if uname == needle_lower or dname == needle_lower:
            tier = 4
        elif uname.startswith(needle_lower) or dname.startswith(needle_lower):
            tier = 3
        elif needle_lower in uname or needle_lower in dname:
            tier = 2
        else:
            tier = 1
        online = 1 if model.get("is_online", True) else 0
        viewers = int(model.get("viewers") or 0)
        return (tier, online, viewers)

    def _looks_offline(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            word in text
            for word in (
                "offline",
                "not live",
                "not currently live",
                "this live event",
                "premiere",
                "upcoming",
                "is not currently streaming",
                "no live",
                "not online",
            )
        )

    def _looks_private(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            word in text
            for word in (
                "private",
                "members only",
                "members-only",
                "join this channel",
                "sign in to confirm your age",
                "age-restricted",
                "login required",
            )
        )

    async def resolve_watch_meta(self, username: str) -> dict[str, Any]:
        target = _clean_handle(username)
        if not target:
            return {}
        if not self.api_key:
            return {
                "username": target,
                "displayName": target,
                "channelUrl": self.canonical_url(target),
            }
        try:
            if is_youtube_video_id(target):
                videos = await self._videos_by_id([target])
                video = videos.get(target)
                if not video:
                    return {}
                snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
                channel_id = str(snippet.get("channelId") or "").strip()
                channel = (await self._channels_by_id([channel_id])).get(channel_id) if channel_id else None
                model = self._video_model(video, channel=channel)
            else:
                channel = await self._channel_by_handle(target)
                if not channel:
                    return {}
                channel_id = str(channel.get("id") or "").strip()
                live_payload = await self._search_live_page(
                    first=1,
                    channel_id=channel_id or None,
                )
                items = [row for row in (live_payload.get("items") or []) if isinstance(row, dict)]
                if items:
                    stub = self._search_model(items[0])
                    hydrated = await self._hydrate_models([stub])
                    model = hydrated[0] if hydrated else self._offline_channel_model(channel, target)
                else:
                    model = self._offline_channel_model(channel, target)
        except Exception:
            return {}
        followers = model.get("followers")
        return {
            "isOnline": bool(model.get("is_online")),
            "viewers": int(model.get("viewers") or 0) if model.get("is_online") else 0,
            "followers": int(followers) if followers is not None else None,
            "channelUrl": model.get("channel_url") or self.canonical_url(target),
            "profileImageUrl": model.get("profile_image_url") or "",
            "displayName": model.get("display_name") or target,
            "username": model.get("username") or target,
            "thumbnail": model.get("thumbnail") or "",
            "roomStatus": model.get("room_status") or None,
            "title": model.get("title") or "",
            "startedAt": model.get("started_at") or "",
        }

    async def check_status(self, username: str) -> ProviderStatus:
        target = _clean_handle(username)
        if not target:
            return ProviderStatus(False, room_status="offline", source_type=self.source_type)
        # Quota-free live probe for auto-record polling; Data API search is 100 units.
        return await super().check_status(target)

    async def list_live_models(
        self,
        page=1,
        limit=24,
        search="",
        **kwargs,
    ):
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        username = str(search or "").strip()
        _ = kwargs
        if not self.api_key:
            return self._auth_required_payload(page, limit)
        # Discover YouTube is handle-search only — never browse a live catalogue.
        if not username:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "empty",
            }
        try:
            return await self._list_live_search(
                page=page,
                limit=limit,
                username=username,
            )
        except Exception as exc:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "error",
                "provider_detail": self._provider_error_detail(exc),
            }

    async def _list_live_catalogue(
        self,
        *,
        page: int,
        limit: int,
        live_query: Optional[str] = None,
    ) -> dict[str, Any]:
        effective_live_query = (
            normalize_youtube_live_query(live_query)
            or self.live_query
            or DEFAULT_YOUTUBE_LIVE_QUERY
        )
        search_q = youtube_live_query_q(effective_live_query) or ""
        request_first = min(50, max(limit, 24))
        pool_key = self._pool_key(
            video_category_id=None,
            live_query=effective_live_query,
            search="",
            limit=limit,
        )
        upstream_gets = 0
        provider_status = "ok"
        provider_detail = ""

        async with self._pool_lock():
            state = self._unique_pool_state(pool_key)
            needed = page * limit
            try:
                while (
                    len(state["items"]) < needed
                    and not state["exhausted"]
                    and upstream_gets < _MAX_UPSTREAM_GETS_PER_CALL
                ):
                    payload = await self._search_live_page(
                        first=request_first,
                        page_token=state.get("next_cursor"),
                        query=search_q,
                    )
                    upstream_gets += 1
                    state["updated_at"] = time.monotonic()
                    rows = [row for row in (payload.get("items") or []) if isinstance(row, dict)]
                    next_cursor = str(payload.get("nextPageToken") or "") or None
                    state["next_cursor"] = next_cursor
                    new_unique = 0
                    seen_ids: set = state["seen_ids"]
                    for row in rows:
                        model = self._search_model(row)
                        identity = self._model_identity(model)
                        if not identity or identity in seen_ids:
                            continue
                        seen_ids.add(identity)
                        state["items"].append(model)
                        new_unique += 1
                    if new_unique == 0:
                        state["consecutive_zero_unique_windows"] = (
                            int(state.get("consecutive_zero_unique_windows") or 0) + 1
                        )
                    else:
                        state["consecutive_zero_unique_windows"] = 0
                    if not next_cursor:
                        state["exhausted"] = True
                    elif int(state["consecutive_zero_unique_windows"]) >= _ZERO_UNIQUE_STOP_WINDOWS:
                        state["exhausted"] = True
                if state.get("items"):
                    self._remember_stale_catalogue(pool_key, state["items"])
            except Exception as exc:
                provider_status = "error"
                provider_detail = self._provider_error_detail(exc)
                if not state.get("items"):
                    self._restore_stale_catalogue(pool_key, state)
                if not state.get("items"):
                    return {
                        "models": [],
                        "total": 0,
                        "page": page,
                        "limit": limit,
                        "total_pages": 1,
                        "provider_status": "error",
                        "provider_detail": provider_detail,
                    }

            start = (page - 1) * limit
            page_items = [dict(item) for item in state["items"][start:start + limit]]
            pool_len = len(state["items"])
            total, total_pages = self._pagination_contract(
                page=page,
                limit=limit,
                page_items=page_items,
                pool_len=pool_len,
                exhausted=bool(state["exhausted"]),
            )
            if provider_status != "error":
                provider_status = "ok" if page_items else "empty"

        page_items = await self._hydrate_models(page_items)
        page_items.sort(key=lambda item: int(item.get("viewers") or 0), reverse=True)
        return {
            "models": page_items,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "provider_status": provider_status,
            "provider_detail": provider_detail,
        }

    async def _list_live_search(
        self,
        *,
        page: int,
        limit: int,
        username: str,
    ) -> dict[str, Any]:
        """Exact @handle / channel-id lookup with correct online/offline state."""
        empty = {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "provider_status": "empty",
        }
        if page != 1:
            return empty
        needle = normalize_youtube_discover_identity(username)
        if not needle:
            return empty

        try:
            channel = await self._channel_by_handle(needle)
        except Exception as exc:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "error",
                "provider_detail": self._provider_error_detail(exc),
            }
        if not channel:
            return empty

        channel_id = str(channel.get("id") or "").strip()
        model: dict[str, Any]
        try:
            live_payload = await self._search_live_page(
                first=1,
                channel_id=channel_id or None,
            )
            items = [row for row in (live_payload.get("items") or []) if isinstance(row, dict)]
            if items:
                stub = self._search_model(items[0])
                hydrated = await self._hydrate_models([stub])
                model = hydrated[0] if hydrated else self._offline_channel_model(channel, needle)
            else:
                model = self._offline_channel_model(channel, needle)
        except Exception:
            model = self._offline_channel_model(channel, needle)

        return {
            "models": [model],
            "total": 1,
            "page": page,
            "limit": limit,
            "total_pages": 1,
            "provider_status": "ok",
            "provider_detail": "",
        }

    async def login(self, username: str, password: str) -> dict[str, Any]:
        _ = username, password
        return {"success": False, "error": _SESSION_HINT}

    async def logout(self) -> dict[str, Any]:
        if self.session_store:
            await self.session_store.clear(self.source_type)
        return {"success": True}

    async def import_session(
        self,
        username: Optional[str] = None,
        cookie_header: Optional[str] = None,
        cookies: Optional[list[dict[str, Any]]] = None,
        local_storage: Optional[list[dict[str, Any]]] = None,
        user_agent: Optional[str] = None,
        x_bc: Optional[str] = None,
    ) -> dict[str, Any]:
        _ = user_agent, x_bc
        incoming = self._merge_session_cookies(cookie_header, cookies)
        cookie_map = ProviderSessionStore.cookie_map(incoming)
        if not self._sapisid_from_map(cookie_map):
            return {
                "success": False,
                "error": "YouTube SAPISID cookie is required; log into YouTube in Chrome first",
            }
        if not self._session_continuity_from_map(cookie_map):
            return {
                "success": False,
                "error": (
                    "YouTube session cookies incomplete (missing SIDCC/PSIDCC); "
                    "log into YouTube in Chrome and import again"
                ),
            }
        if not self.session_store:
            return {"success": False, "error": "Session store unavailable"}
        storage = list(local_storage or [])
        await self.session_store.save(
            source_type=self.source_type,
            username=(username or "").strip() or None,
            is_logged_in=True,
            cookies=incoming,
            local_storage=storage,
            last_error=None,
        )
        try:
            current = await self._youtube_current_user()
        except ProviderAuthError as exc:
            await self.session_store.save(
                source_type=self.source_type,
                username=(username or "").strip() or None,
                is_logged_in=False,
                cookies=incoming,
                local_storage=storage,
                last_error=str(exc),
            )
            return {"success": False, "error": str(exc)}
        resolved = str(current.get("username") or username or "").strip()
        await self.session_store.save(
            source_type=self.source_type,
            username=resolved or None,
            is_logged_in=True,
            cookies=incoming,
            local_storage=storage,
            last_error=None,
        )
        return {"success": True, "username": resolved, "importedSession": True}

    async def verify_logged_in(self) -> Optional[bool]:
        cookie_map = await self._session_cookie_map()
        if not self._sapisid_from_map(cookie_map):
            return False
        if not self._session_continuity_from_map(cookie_map):
            return False
        try:
            await self._youtube_current_user()
            return True
        except ProviderAuthError:
            return False
        except Exception:
            return None

    async def sync_following(self) -> list[dict[str, object]]:
        await self._youtube_current_user()
        channel_ids = await self._youtube_sync_subscription_ids()
        if not channel_ids:
            return FollowedSyncResult(
                [],
                trusted=False,
                skipped_reason="YouTube subscription list is empty or unrecognized",
                authoritative=False,
            )
        items: list[dict[str, object]] = []
        for offset in range(0, len(channel_ids), 50):
            batch = channel_ids[offset:offset + 50]
            try:
                channels = await self._channels_by_id(batch)
            except Exception:
                channels = {}
            for channel_id in batch:
                channel = channels.get(channel_id) or {"id": channel_id}
                items.append(self._offline_channel_model(channel, fallback=channel_id))
            if len(items) >= HXYLIVE_MAX_FOLLOW_SYNC_ITEMS:
                items = items[:HXYLIVE_MAX_FOLLOW_SYNC_ITEMS]
                break
        items.sort(
            key=lambda item: (
                not bool(item.get("is_online")),
                -int(item.get("viewers") or 0),
                str(item.get("username") or "").lower(),
            )
        )
        return items[:HXYLIVE_MAX_FOLLOW_SYNC_ITEMS]

    async def follow(self, username: str) -> dict[str, object]:
        channel_id = await self._youtube_channel_id_for_target(username)
        if not channel_id:
            raise ProviderError(f"YouTube channel not found: {username}")
        await self._youtube_innertube_json(
            "subscription/subscribe",
            {"channelIds": [channel_id], "params": "EgIIAhIGCAAQAhoA"},
        )
        return {"success": True, "remote": True, "provider": "youtube", "username": username}

    async def unfollow(self, username: str) -> dict[str, object]:
        channel_id = await self._youtube_channel_id_for_target(username)
        if not channel_id:
            raise ProviderError(f"YouTube channel not found: {username}")
        await self._youtube_innertube_json(
            "subscription/unsubscribe",
            {"channelIds": [channel_id], "params": "EgIIAhIGCAAQAhoA"},
        )
        return {"success": True, "remote": True, "provider": "youtube", "username": username}

    async def is_following(self, username: str) -> bool:
        channel_id = await self._youtube_channel_id_for_target(username)
        if not channel_id:
            return False
        try:
            ids = await self._youtube_sync_subscription_ids()
        except ProviderAuthError:
            raise
        except Exception:
            return False
        return channel_id in set(ids)

    def _merge_session_cookies(
        self,
        cookie_header: Optional[str],
        cookies: Optional[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        incoming = ProviderSessionStore.parse_cookie_header(
            cookie_header,
            domain=".youtube.com",
        )
        by_name = {str(item.get("name")): item for item in incoming if item.get("name")}
        for cookie in cookies or []:
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            item = dict(cookie)
            item.setdefault("domain", ".youtube.com")
            item.setdefault("path", "/")
            by_name[str(item["name"])] = item
        return list(by_name.values())

    @staticmethod
    def _sapisid_from_map(cookie_map: dict[str, str]) -> str:
        for name in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"):
            value = str(cookie_map.get(name) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _session_continuity_from_map(cookie_map: dict[str, str]) -> str:
        for name in ("__Secure-3PSIDCC", "__Secure-1PSIDCC", "SIDCC"):
            value = str(cookie_map.get(name) or "").strip()
            if value:
                return value
        return ""

    async def _session_cookie_map(self) -> dict[str, str]:
        if not self.session_store:
            return {}
        try:
            state = await self.session_store.get(self.source_type)
        except Exception:
            return {}
        return ProviderSessionStore.cookie_map(state.get("cookies"))

    async def _session_cookie_header(self) -> str:
        if not self.session_store:
            return ""
        try:
            return await self.session_store.cookie_header(self.source_type)
        except Exception:
            return ""

    def _sapisidhash(self, sapisid: str) -> str:
        ts = int(time.time())
        digest = hashlib.sha1(
            f"{ts} {sapisid} {_YOUTUBE_ORIGIN}".encode("utf-8")
        ).hexdigest()
        return f"SAPISIDHASH {ts}_{digest}"

    async def _youtube_auth_headers(self) -> dict[str, str]:
        cookie_map = await self._session_cookie_map()
        sapisid = self._sapisid_from_map(cookie_map)
        cookie_header = await self._session_cookie_header()
        if not sapisid or not cookie_header:
            raise ProviderAuthError("YouTube login required")
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": _YOUTUBE_ORIGIN,
            "Referer": f"{_YOUTUBE_ORIGIN}/",
            "Cookie": cookie_header,
            "Authorization": self._sapisidhash(sapisid),
            "X-Goog-AuthUser": "0",
            "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": _INNERTUBE_CLIENT_VERSION,
        }

    def _innertube_context(self) -> dict[str, Any]:
        return {
            "client": {
                "hl": "en",
                "gl": "US",
                "clientName": "WEB",
                "clientVersion": _INNERTUBE_CLIENT_VERSION,
                "userAgent": DEFAULT_USER_AGENT,
            }
        }

    async def _youtube_innertube_json(
        self,
        endpoint: str,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        headers = await self._youtube_auth_headers()
        payload = {"context": self._innertube_context()}
        if body:
            payload.update(body)
        url = (
            f"{_YOUTUBE_ORIGIN}/youtubei/v1/{endpoint.lstrip('/')}"
            f"?prettyPrint=false&key={_INNERTUBE_API_KEY}"
        )
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                text = await response.text(errors="ignore")
                status = response.status
        if status in {401, 403}:
            raise ProviderAuthError("YouTube login required")
        if status >= 400:
            raise ProviderError(f"YouTube InnerTube HTTP {status}")
        stripped = (text or "").lstrip()
        if not stripped.startswith(("{", "[")):
            raise ProviderError("YouTube InnerTube response is not JSON")
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ProviderError("Invalid YouTube InnerTube JSON") from exc
        return data if isinstance(data, dict) else {}

    async def _youtube_current_user(self) -> dict[str, Any]:
        payload = await self._youtube_innertube_json("account/account_menu", {})
        if self._youtube_response_logged_out(payload):
            raise ProviderAuthError("Imported YouTube session is not logged in")
        username = self._youtube_account_name_from_payload(payload)
        if not username:
            raise ProviderAuthError("Imported YouTube session is not logged in")
        return {"username": username}

    @staticmethod
    def _youtube_response_logged_out(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        response_context = payload.get("responseContext")
        if not isinstance(response_context, dict):
            return False
        main = response_context.get("mainAppWebResponseContext")
        if isinstance(main, dict) and main.get("loggedOut") is True:
            return True
        for service in response_context.get("serviceTrackingParams") or []:
            if not isinstance(service, dict):
                continue
            for param in service.get("params") or []:
                if not isinstance(param, dict):
                    continue
                key = str(param.get("key") or "").strip().lower()
                value = str(param.get("value") or "").strip().lower()
                if key in {"logged_in", "yt_li"} and value in {"0", "false"}:
                    return True
        return False

    def _youtube_account_name_from_payload(self, payload: object) -> str:
        """Extract the signed-in account label only (never language-picker titles)."""
        account_names: list[str] = []
        handles: list[str] = []

        def take_text(value: object) -> str:
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, dict):
                for key in ("simpleText", "content", "label"):
                    text = value.get(key)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            return ""

        def walk(value: object) -> None:
            if isinstance(value, dict):
                if "accountName" in value:
                    text = take_text(value.get("accountName"))
                    if text:
                        account_names.append(text)
                for key in ("channelHandle", "accountNameByline"):
                    if key in value:
                        text = take_text(value.get(key))
                        if text:
                            handles.append(text)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        for name in handles + account_names:
            cleaned = name.strip()
            if cleaned.startswith("@"):
                cleaned = cleaned[1:]
            if cleaned and " " not in cleaned and len(cleaned) <= 64:
                return cleaned
        for name in account_names:
            cleaned = name.strip()
            if cleaned:
                return cleaned
        return ""

    async def _youtube_sync_subscription_ids(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []

        def add_many(channel_ids: list[str]) -> bool:
            for channel_id in channel_ids:
                if channel_id in seen:
                    continue
                seen.add(channel_id)
                ordered.append(channel_id)
                if len(ordered) >= HXYLIVE_MAX_FOLLOW_SYNC_ITEMS:
                    return True
            return False

        # Sidebar subscriptions survive YouTube's lockupViewModel migration on
        # /feed/channels. FEchannels is still fetched afterwards for the full list.
        guide = await self._youtube_innertube_json("guide", {})
        if add_many(self._youtube_guide_subscription_ids(guide)):
            return ordered

        continuation: Optional[str] = None
        for _ in range(40):
            body: dict[str, Any] = {"browseId": "FEchannels"}
            if continuation:
                body = {"continuation": continuation}
            payload = await self._youtube_innertube_json("browse", body)
            if add_many(self._youtube_channel_ids_from_payload(payload)):
                return ordered
            continuation = self._youtube_continuation_token(payload)
            if not continuation:
                break
        return ordered

    def _youtube_guide_subscription_ids(self, payload: object) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()

        def maybe_add(value: object) -> None:
            text = str(value or "").strip()
            if is_youtube_channel_id(text) and text not in seen:
                seen.add(text)
                found.append(text)

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key in ("channelId", "externalChannelId", "contentId", "browseId"):
                    maybe_add(value.get(key))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                section = value.get("guideSubscriptionsSectionRenderer")
                if isinstance(section, dict):
                    collect(section)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return found

    def _youtube_channel_ids_from_payload(self, payload: object) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()

        def maybe_add(value: object) -> None:
            text = str(value or "").strip()
            if is_youtube_channel_id(text) and text not in seen:
                seen.add(text)
                found.append(text)

        def collect_from(node: object) -> None:
            if isinstance(node, dict):
                for key in ("channelId", "externalChannelId", "contentId", "browseId"):
                    maybe_add(node.get(key))
                for child in node.values():
                    collect_from(child)
            elif isinstance(node, list):
                for child in node:
                    collect_from(child)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key in (
                    "gridChannelRenderer",
                    "channelRenderer",
                    "subscriptionNotificationToggleButtonRenderer",
                    "guideEntryRenderer",
                    "lockupViewModel",
                    "channelLockupViewModel",
                ):
                    renderer = value.get(key)
                    if isinstance(renderer, dict):
                        collect_from(renderer)
                for key in ("channelId", "externalChannelId"):
                    if key in value:
                        maybe_add(value.get(key))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return found

    def _youtube_continuation_token(self, payload: object) -> Optional[str]:
        tokens: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                token = value.get("token") or value.get("continuation")
                if isinstance(token, str) and token.strip():
                    if "continuationCommand" in value or "continuationItemRenderer" in str(value.keys()):
                        tokens.append(token.strip())
                    elif value.get("clickTrackingParams") and token not in tokens:
                        tokens.append(token.strip())
                cont = value.get("continuationEndpoint") or value.get("continuationCommand")
                if isinstance(cont, dict):
                    command = cont.get("continuationCommand") if "continuationCommand" in cont else cont
                    if isinstance(command, dict):
                        token = command.get("token")
                        if isinstance(token, str) and token.strip():
                            tokens.append(token.strip())
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return tokens[0] if tokens else None

    async def _youtube_channel_id_for_target(self, target: str) -> Optional[str]:
        raw = str(target or "").strip()
        if not raw:
            return None
        if is_youtube_channel_id(raw):
            return raw
        identity = youtube_username_from_url(raw) if raw.startswith("http") else raw
        identity = _clean_handle(identity or raw)
        if is_youtube_channel_id(identity):
            return identity
        channel = await self._channel_by_handle(identity)
        if not channel:
            return None
        channel_id = str(channel.get("id") or "").strip()
        return channel_id if is_youtube_channel_id(channel_id) else None

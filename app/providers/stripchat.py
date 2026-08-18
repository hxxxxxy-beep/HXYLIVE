"""Stripchat discover / HLS / favorites provider (C1 unique-pool)."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from typing import Any, Iterable, Optional
from urllib.parse import quote_plus, unquote, urlencode, urljoin, urlparse

import aiohttp
from yarl import URL

from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from ..logger import logger
from .base import (
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderInteractionRequired,
    ProviderOfflineError,
    ProviderPrivateError,
    ProviderStatus,
    ResolvedStream,
)
from .browser import DEFAULT_USER_AGENT
from .ytdlp import YtDlpProvider

STRIPCHAT_BASE_URL = "https://stripchat.com"
STRIPCHAT_API_BASE = f"{STRIPCHAT_BASE_URL}/api/front"
# Paginated live catalogue with viewersCount. /v2/models now returns homepage
# recommendation blocks and ignores offset — do not use it for listings.
STRIPCHAT_MODELS_CATALOGUE_PATH = "/models"
STRIPCHAT_FRONT_VERSION = os.getenv("HXYLIVE_STRIPCHAT_FRONT_VERSION", "11.7.28")
STRIPCHAT_LOGIN_PATHS = (
    "/auth/login",
    "/v3/auth/login",
    "/v2/auth/login",
    "/login",
)
STRIPCHAT_HLS_HOSTS = (
    "doppiocdn.net",
    "doppiocdn.com",
    "doppiocdn.org",
    "doppiocdn.live",
    "doppiocdn.media",
)
STRIPCHAT_PLAYBACK_KEY = os.getenv("HXYLIVE_STRIPCHAT_PLAYBACK_KEY", "fncnu6utiWqsDLk8")
_STRIPCHAT_UNIQUE_POOL_TTL_SECONDS = 45
_STRIPCHAT_MAX_UPSTREAM_GETS_PER_CALL = 3
_STRIPCHAT_ZERO_UNIQUE_STOP_WINDOWS = 2
_STRIPCHAT_UNIQUE_POOL_MAX_KEYS = 32
# Chat-nav online count (model-chat-nav-item-count) is fed by catalogue viewersCount.
_STRIPCHAT_VIEWERS_KEYS = (
    "viewersCount",
    "viewers",
    "usersCount",
    "guestsCount",
    "membersCount",
)
_RESERVED_PROFILE_SEGMENTS = {
    "",
    "about",
    "account",
    "accounts",
    "2257",
    "all-models",
    "api",
    "become-a-model",
    "blog",
    "cams",
    "cam",
    "chat",
    "contact",
    "cookies-policy",
    "couple",
    "couples",
    "de",
    "dmca",
    "en",
    "es",
    "female",
    "fr",
    "girls",
    "help",
    "home",
    "hc",
    "it",
    "live",
    "login",
    "logout",
    "male",
    "members",
    "models",
    "my",
    "new",
    "online",
    "performers",
    "privacy",
    "private",
    "profile",
    "profiles",
    "search",
    "settings",
    "signup",
    "tags",
    "terms",
    "trans",
    "users",
    "videos",
    "vr",
}

_INTERACTION_RE = re.compile(
    r"captcha|hcaptcha|recaptcha|turnstile|2fa|two-factor|cloudflare|cloudfront|request blocked|verify you are human",
    re.IGNORECASE,
)
_LOGIN_FAILED_RE = re.compile(
    r"invalid|incorrect|wrong|failed|try again|not recognized|not recognised|"
    r"could not log|unable to log|password.*required|username.*required",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_GENERIC_TAG_STOPWORDS = {
    "account", "all", "chat", "free", "girl", "girls", "home", "html", "img",
    "live", "login", "medium", "model", "models", "online", "profile", "search",
    "settings", "show", "shows", "signup", "stream", "streams", "video", "videos",
    "viewer", "viewers", "watching",
}
# Stripchat /search/models/{q} thumbs: id + username (either attribute order).
_SEARCH_MODEL_LINK_RE = re.compile(
    r'data-model-id="(?P<id>\d+)"[^>]*href="/(?P<username>[A-Za-z0-9_-]+)"'
    r'|href="/(?P<username2>[A-Za-z0-9_-]+)"[^>]*data-model-id="(?P<id2>\d+)"',
    re.IGNORECASE,
)
_SEARCH_MODELS_SECTION_RE = re.compile(
    r'class="[^"]*SearchPage__models[^"]*"(?P<body>.*?)(?:</section>|class="[^"]*SearchPage__)',
    re.IGNORECASE | re.DOTALL,
)
_SEARCH_THUMB_IMG_RE = re.compile(
    r'src="(https://(?:img\.doppiocdn\.(?:org|com|net)/snapshot[^"]+|static-proxy\.strpst\.com/[^"]+))"',
    re.IGNORECASE,
)
_STRIPCHAT_SEARCH_HYDRATE_CONCURRENCY = 6
_STRIPCHAT_SEARCH_CATALOGUE_MAX_GETS = 8


def _strip_html(value: str) -> str:
    return _TAG_RE.sub(" ", html.unescape(value or "")).strip()


def _normalize_tag(value: str) -> Optional[str]:
    tag = html.unescape(str(value or "")).strip().strip("#").strip()
    tag = re.sub(r"\s+", " ", tag)
    tag = tag.strip(".,;:!?()[]{}\"'")
    if not tag:
        return None
    lower = tag.lower()
    if lower in _GENERIC_TAG_STOPWORDS:
        return None
    if len(lower) < 2 or len(lower) > 48:
        return None
    if re.search(r"https?://|@|[<>/\\]", lower):
        return None
    if re.fullmatch(r"\d+", lower):
        return None
    return lower


def _normalize_tags(values: Iterable[object]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        tag = _normalize_tag(str(value or ""))
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= 12:
            break
    return out


def _parse_count(raw: object) -> int:
    value = str(raw or "").strip().lower().replace("\xa0", " ")
    match = re.search(r"(\d[\d,.\s]*)([km])?", value)
    if not match:
        return 0
    number = match.group(1).strip()
    suffix = match.group(2) or ""
    if suffix:
        number = number.replace(" ", "").replace(",", ".")
        try:
            parsed = float(number)
        except ValueError:
            return 0
        return int(parsed * (1000 if suffix == "k" else 1000000))
    digits = re.sub(r"[^\d]", "", number)
    return int(digits or 0)


def _stripchat_viewers_from_raw(model: dict[str, object]) -> int:
    """Online chat viewers (= site model-chat-nav-item-count), never favoritedCount."""
    for key in _STRIPCHAT_VIEWERS_KEYS:
        if model.get(key) is None:
            continue
        try:
            return max(0, int(model.get(key) or 0))
        except (TypeError, ValueError):
            parsed = _parse_count(model.get(key))
            if parsed > 0:
                return parsed
    return 0


_STRIPCHAT_PRIVATE_STATUSES = {
    "private",
    "p2p",
    "p2pvoice",
    "p2p_voice",
    "group",
    "groupshow",
    "group_show",
    "ticket",
    "ticketshow",
    "ticket_show",
    "premium",
    "spy",
    "virtualprivate",
    "virtual_private",
    "true_private",
    "private_spy",
    "privateshow",
    "private_show",
    "password_protected",
    "password protected",
    "hidden",
}

# Cam payload fields that mean a non-public paid/locked show (Media must say Private,
# never "Live · 0" / Offline).
_STRIPCHAT_PRIVATE_CAM_KEYS = (
    "show",
    "privateMode",
    "groupShowAnnouncement",
    "ticketShow",
    "ticketShowAnnouncement",
    "privateShow",
)

# Stripchat cam/profile APIs use short ``off`` for offline; never treat it as live.
_STRIPCHAT_OFFLINE_STATUSES = {
    "off",
    "offline",
    "away",
    "idle",
    "inactive",
    "not_live",
    "not live",
}


def _stripchat_is_private_status(value: object) -> bool:
    status = str(value or "").strip().lower().replace(" ", "_")
    if not status:
        return False
    compact = status.replace("_", "")
    if status in _STRIPCHAT_PRIVATE_STATUSES or compact in _STRIPCHAT_PRIVATE_STATUSES:
        return True
    return any(marker in status for marker in ("private", "p2p", "group", "ticket", "premium", "spy"))


def _stripchat_is_offline_status(value: object) -> bool:
    status = str(value or "").strip().lower()
    return status in _STRIPCHAT_OFFLINE_STATUSES


def _stripchat_truthy_cam_indicator(value: object) -> bool:
    if value in (None, False, "", [], {}):
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "none", "null", "no", "off", "public"}
    return bool(value)


def _subject_keyword_tags(value: str) -> list[str]:
    text = f" {_strip_html(value).lower()} "
    needles = ("anal", "asian", "bbw", "blonde", "couple", "hd", "lovense", "milf", "trans", "vr")
    found = [n for n in needles if n in text]
    return _normalize_tags(found)


class StripchatProvider(YtDlpProvider):
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
            can_password_login=True,
            can_follow=True,
            can_sync_following=True,
            can_discover=True,
            can_stream=True,
            can_record=True,
            uses_ytdlp=False,
        )
        self._stripchat_unique_pools: dict[tuple, dict[str, Any]] = {}
        self._stripchat_unique_pool_lock: Optional[asyncio.Lock] = None

    async def list_live_models(self, **kwargs) -> dict[str, object]:
        page = max(1, int(kwargs.get("page") or 1))
        limit = max(1, int(kwargs.get("limit") or 24))
        gender = kwargs.get("gender") or None
        search = (kwargs.get("search") or "").strip()
        tags = kwargs.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
        api_result = await self._stripchat_list_live_models_api(
            page=page,
            limit=limit,
            search=search,
            gender=gender,
            tags=tags,
        )
        if api_result is not None:
            return dict(api_result)
        return {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
        }

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None, **kwargs
    ) -> ResolvedStream:
        # Watch Auto needs the master playlist for ABR. Recording "best" must
        # pin a concrete rung (`pin_variant=True`) so FFmpeg does not grab a
        # low/default level from the master.
        stream = await self._resolve_stripchat_public_hls(
            target,
            max_height=max_height,
            pin_variant=bool(kwargs.get("pin_variant")),
        )
        if stream:
            return stream
        raise ProviderError(f"Stripchat stream not found for {target}")

    async def check_status(self, username: str) -> ProviderStatus:
        try:
            stream = await self.resolve_stream(username)
            return ProviderStatus(
                is_online=True,
                viewers=stream.viewers,
                room_status=stream.room_status or "public",
                hls_source=stream.url,
                thumbnail=stream.thumbnail,
                source_type=self.source_type,
                tags=list(stream.tags or []),
            )
        except ProviderPrivateError as exc:
            return ProviderStatus(False, room_status="private", source_type=self.source_type, detail=str(exc))
        except ProviderOfflineError as exc:
            return ProviderStatus(False, room_status="offline", source_type=self.source_type, detail=str(exc))
        except ProviderError as exc:
            return ProviderStatus(False, room_status="error", source_type=self.source_type, detail=str(exc))

    async def resolve_watch_meta(self, username: str) -> dict[str, object]:
        """Exact-room Media card meta; catalogue backfills viewersCount.

        Cam/profile APIs omit viewersCount. Searching via list_live_models is
        slow under Media's parallel profile refresh and used to hit /v2/models
        blocks (offset ignored) — so online rooms often showed 0 watching.
        """
        try:
            model = await self._stripchat_model_by_username(username)
        except ProviderError:
            return {}
        item = self._stripchat_model_item(model) or {}
        if not item.get("username"):
            return {}
        is_online = bool(item.get("is_online"))
        room_status = item.get("room_status")
        if _stripchat_is_private_status(room_status):
            # Always surface Private on Media cards — never Offline / Live · 0.
            is_online = True
            viewers = 0
        else:
            viewers = int(item.get("viewers") or 0) if is_online else 0
            if is_online and viewers <= 0:
                try:
                    viewers = int(
                        await self._stripchat_catalog_viewers(
                            str(item.get("username") or username),
                            model_id=self._stripchat_model_id(model),
                            model=model,
                        )
                        or 0
                    )
                except Exception:
                    viewers = 0
        return {
            "isOnline": is_online,
            "viewers": viewers if is_online else 0,
            "followers": None,
            "channelUrl": item.get("channel_url") or self.canonical_url(username),
            "profileImageUrl": item.get("profile_image_url") or "",
            "displayName": item.get("display_name") or item.get("username"),
            "username": item.get("username"),
            "thumbnail": item.get("thumbnail") or "",
            "roomStatus": room_status,
            "title": item.get("subject") or "",
        }

    async def sync_following(self) -> list[dict[str, object]]:
        return await self._stripchat_sync_following_http()

    async def follow(self, username: str) -> dict[str, object]:
        try:
            return await self._stripchat_follow_http(username, follow=True)
        except ProviderAuthError:
            raise
        except ProviderError as exc:
            return {"success": False, "error": str(exc)}

    async def unfollow(self, username: str) -> dict[str, object]:
        try:
            return await self._stripchat_follow_http(username, follow=False)
        except ProviderAuthError:
            raise
        except ProviderError as exc:
            return {"success": False, "error": str(exc)}

    async def is_following(self, username: str) -> bool:
        try:
            payload = await self._stripchat_api_json(
                "GET",
                f"/v2/models/username/{quote_plus(username)}/cam",
                auth_required=False,
                referer=self.canonical_url(username),
            )
            value = self._stripchat_find_value(payload, {"isinfavorites"})
            if value is not None:
                return bool(value)
        except Exception:
            pass
        try:
            items = await self.sync_following()
        except Exception:
            return False
        needle = username.strip().lower()
        return any(str(item.get("username") or "").lower() == needle for item in items)

    async def login(self, username: str, password: str) -> dict[str, object]:
        return await self._stripchat_login_http(username, password)

    async def logout(self) -> dict[str, object]:
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
    ) -> dict[str, object]:
        _ = user_agent, x_bc
        username = (username or "").strip()
        incoming: list[dict[str, Any]] = []
        raw_header = (cookie_header or "").strip()
        if raw_header:
            for part in raw_header.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                if not name:
                    continue
                incoming.append({
                    "name": name,
                    "value": value.strip(),
                    "domain": ".stripchat.com",
                    "path": "/",
                })
        for cookie in cookies or []:
            if isinstance(cookie, dict) and cookie.get("name"):
                incoming.append(cookie)
        storage = list(local_storage or [])
        if not incoming and not storage:
            return {"success": False, "error": "No session cookies provided"}
        if not self.session_store:
            return {"success": False, "error": "Session store unavailable"}
        await self.session_store.save(
            source_type=self.source_type,
            username=username or None,
            is_logged_in=True,
            cookies=incoming,
            local_storage=storage,
            last_error=None,
        )
        return {"success": True, "username": username, "importedSession": True}

    async def _provider_cookie_header(self) -> str:
        if not self.session_store:
            return ""
        try:
            return await self.session_store.cookie_header(self.source_type)
        except Exception:
            return ""

    def _stream_headers(self, page_url: str, cookie_header: Optional[str] = None) -> dict[str, str]:
        parsed = urlparse(page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": page_url,
        }
        if origin:
            headers["Origin"] = origin
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _find_dicts_with_key(self, value: object, key: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if key in value:
                found.append(value)
            for child in value.values():
                found.extend(self._find_dicts_with_key(child, key))
        elif isinstance(value, list):
            for child in value:
                found.extend(self._find_dicts_with_key(child, key))
        return found

    def _stripchat_models_from_payload(self, payload: object) -> list[dict[str, object]]:
        models: list[dict[str, object]] = []
        if isinstance(payload, dict):
            profile_model = self._stripchat_profile_model(payload)
            if profile_model:
                models.append(profile_model)
            direct_models = payload.get("models")
            if isinstance(direct_models, list):
                models.extend(model for model in direct_models if isinstance(model, dict))
            for block in self._find_dicts_with_key(payload, "models"):
                block_models = block.get("models")
                if isinstance(block_models, list):
                    models.extend(model for model in block_models if isinstance(model, dict))
        elif isinstance(payload, list):
            models.extend(model for model in payload if isinstance(model, dict))
        return self._dedupe_stripchat_models(models)

    def _stripchat_profile_model(self, payload: dict[str, object]) -> Optional[dict[str, object]]:
        item = payload.get("item")
        if isinstance(item, dict) and item.get("username"):
            model = dict(item)
            cam = payload.get("cam")
            if isinstance(cam, dict):
                self._stripchat_merge_cam_into_model(model, cam)
            return model
        user_block = payload.get("user")
        if isinstance(user_block, dict):
            inner = user_block.get("user")
            if isinstance(inner, dict) and inner.get("username"):
                model = dict(inner)
                for key in ("isInFavorites", "tags", "lastTagsAliases", "tagGroups"):
                    if key in user_block:
                        model[key] = user_block[key]
                cam = payload.get("cam")
                if isinstance(cam, dict):
                    self._stripchat_merge_cam_into_model(model, cam)
                return model
        if payload.get("username") or payload.get("login"):
            model = dict(payload)
            cam = payload.get("cam")
            if isinstance(cam, dict):
                self._stripchat_merge_cam_into_model(model, cam)
            return model
        return None

    def _stripchat_merge_cam_into_model(
        self,
        model: dict[str, object],
        cam: dict[str, object],
    ) -> None:
        """Copy cam show-state onto the user model for Media private detection.

        Cam often carries private/p2p/group locks while user.status still says
        ``public`` and viewersCount is missing — that used to render as
        ``Live · 0 watching`` or Offline instead of Private.
        """
        if cam.get("streamName"):
            model["streamName"] = cam.get("streamName")
        for key in _STRIPCHAT_PRIVATE_CAM_KEYS:
            if key in cam and cam.get(key) not in (None, "", [], {}):
                model[key] = cam.get(key)
        for key in ("isCamAvailable", "isCamActive", "streamStatus", "status"):
            if key in cam and cam.get(key) is not None:
                model[key if key != "status" else "_cam_status"] = cam.get(key)
                if key == "status":
                    model.setdefault("streamStatus", cam.get(key))
        cam_status = str(cam.get("status") or cam.get("streamStatus") or "").strip()
        if cam_status and (
            _stripchat_is_private_status(cam_status) or _stripchat_is_offline_status(cam_status)
        ):
            model["status"] = cam_status
        for key in _STRIPCHAT_VIEWERS_KEYS:
            if model.get(key) in (None, "", 0) and cam.get(key) not in (None, "", 0):
                model[key] = cam.get(key)
        if model.get("viewersCount") in (None, "", 0) and cam.get("groupShowUsersCount") not in (None, "", 0):
            model["viewersCount"] = cam.get("groupShowUsersCount")

    def _stripchat_model_has_private_indicators(self, model: dict[str, object]) -> bool:
        if any(_stripchat_truthy_cam_indicator(model.get(key)) for key in _STRIPCHAT_PRIVATE_CAM_KEYS):
            return True
        for value in (model.get("streamStatus"), model.get("_cam_status")):
            if _stripchat_is_private_status(value):
                return True
        status = str(model.get("status") or "").strip().lower()
        # Public label but cam locked while still flagged live → private session.
        if status in {"", "public"} and (
            model.get("isOnline") is True or model.get("isLive") is True
        ):
            if model.get("isCamAvailable") is False or model.get("isCamActive") is False:
                return True
        return False

    def _dedupe_stripchat_models(self, models: list[dict[str, object]]) -> list[dict[str, object]]:
        by_key: dict[str, dict[str, object]] = {}
        for model in models:
            username = str(model.get("username") or model.get("login") or "").strip().lower()
            model_id = str(model.get("id") or model.get("streamName") or "").strip()
            key = username or model_id
            if not key:
                continue
            existing = by_key.get(key)
            if existing:
                for field, value in model.items():
                    if value not in (None, "", [], {}):
                        existing.setdefault(field, value)
                continue
            by_key[key] = dict(model)
        return list(by_key.values())

    def _stripchat_model_item(self, model: dict[str, object]) -> Optional[dict[str, object]]:
        username = str(model.get("username") or model.get("login") or "").strip()
        if not username:
            return None
        status = str(model.get("status") or "").strip().lower()
        viewers = _stripchat_viewers_from_raw(model)
        # Stripchat login/username is the room id users search; ignore vanity
        # `name` (e.g. "Jahnvi (jaanu)") for card / watch identity.
        is_online = bool(model.get("isOnline", model.get("isLive", status == "public")))
        private_locked = self._stripchat_model_has_private_indicators(model)
        # Cam payloads can keep isOnline=true while status is already ``off``.
        # Private/p2p must win over a stale ``public`` label and over isOnline=false
        # (guest cam is unavailable during paid shows).
        if _stripchat_is_offline_status(status) and not private_locked and not _stripchat_is_private_status(status):
            is_online = False
            room_status = "offline"
            viewers = 0
        elif _stripchat_is_private_status(status) or private_locked:
            is_online = True
            room_status = status if _stripchat_is_private_status(status) else "private"
            # Media shows Private — never "Live · N". Drop guest/group tip counts.
            viewers = 0
        elif status and status != "public":
            room_status = status
        else:
            room_status = "public" if is_online else "offline"
        thumbnail = self._stripchat_thumbnail(model, is_online=is_online)
        avatar = self._stripchat_avatar_url(model)
        return {
            "username": username,
            "display_name": username,
            "thumbnail": thumbnail,
            # Never fall back to live snapshot/preview — Discover/Watch show a
            # letter avatar when the model has no uploaded face photo.
            "profile_image_url": avatar or "",
            "viewers": viewers,
            # Stripchat: do not surface follower/favorite counts.
            "followers": None,
            "subject": str(model.get("groupShowTopic") or model.get("offlineStatus") or ""),
            "age": model.get("age") if isinstance(model.get("age"), int) else None,
            "gender": str(model.get("genderGroup") or model.get("gender") or "").lower(),
            "is_online": is_online,
            "tags": self._stripchat_tags(model),
            "room_status": room_status,
            "source_type": self.source_type,
            "channel_url": self.canonical_url(username),
        }

    def _stripchat_tags(self, model: dict[str, object]) -> list[str]:
        gender = str(model.get("gender") or model.get("broadcastGender") or model.get("genderGroup") or "").strip()
        gender_map = {
            "f": "female",
            "female": "female",
            "females": "female",
            "m": "male",
            "male": "male",
            "males": "male",
            "t": "trans",
            "trans": "trans",
            "femaleTranny": "trans",
            "tranny": "trans",
            "maleFemale": "couple",
            "group": "group",
        }
        values: list[object] = [gender_map.get(gender, gender)]
        broadcast_gender = str(model.get("broadcastGender") or "").strip()
        if broadcast_gender and broadcast_gender != gender:
            values.append(gender_map.get(broadcast_gender, broadcast_gender))
        country = str(model.get("country") or "").strip()
        if country:
            values.append(country)
        if model.get("isHd"):
            values.append("hd")
        if model.get("isVr"):
            values.append("vr")
        if model.get("isMobile"):
            values.append("mobile")
        if model.get("isNew"):
            values.append("new")
        if model.get("isLovense"):
            values.append("lovense")
        if model.get("isKiiroo"):
            values.append("kiiroo")
        if model.get("isNonNude"):
            values.append("non nude")
        if str(model.get("status") or "").lower() == "public":
            values.append("public")
        values.extend(_subject_keyword_tags(str(model.get("groupShowTopic") or "")))
        return _normalize_tags(values)

    @staticmethod
    def _stripchat_abs_media_url(value: object) -> Optional[str]:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://")):
            return StripchatProvider._stripchat_prefer_static_proxy_media(raw)
        if raw.startswith("/"):
            return StripchatProvider._stripchat_prefer_static_proxy_media(
                f"https://img.doppiocdn.net{raw}"
            )
        return None

    @staticmethod
    def _stripchat_prefer_static_proxy_media(url: str) -> str:
        """Catalogue avatar/preview hosts on doppiocdn often 404; Media/Watch
        username/cam payloads use static-proxy.strpst.com with the same path.
        """
        raw = str(url or "").strip()
        if not raw:
            return raw
        lowered = raw.lower()
        # Keep live snapshots on doppiocdn (those 200); only rewrite durable media.
        if "/snapshot/" in lowered:
            return raw
        for marker in ("/avatars/", "/previews/"):
            idx = lowered.find(marker)
            if idx == -1:
                continue
            if "doppiocdn." not in lowered and "static-proxy.strpst.com" not in lowered:
                continue
            return "https://static-proxy.strpst.com" + raw[idx:]
        return raw

    def _stripchat_avatar_url(self, model: dict[str, object]) -> Optional[str]:
        for key in ("avatarUrl", "avatarUrlThumb", "avatarUrlOriginal"):
            url = self._stripchat_abs_media_url(model.get(key))
            if url:
                return url
        return None

    def _stripchat_thumbnail(
        self,
        model: dict[str, object],
        is_online: Optional[bool] = None,
    ) -> Optional[str]:
        """Cover image for Discover cards.

        Live rooms can use doppiocdn snapshots. Offline constructed snapshots often
        404 (e.g. xxxnba), so prefer Stripchat preview/avatar URLs when offline.
        """
        if is_online is None:
            status = str(model.get("status") or "").strip().lower()
            is_online = bool(model.get("isOnline", model.get("isLive", status == "public")))
            if _stripchat_is_offline_status(status):
                is_online = False

        preview = None
        for key in ("previewUrlThumbBig", "previewUrlThumbSmall", "previewUrl"):
            preview = self._stripchat_abs_media_url(model.get(key))
            if preview:
                break
        avatar = self._stripchat_avatar_url(model)

        model_id = str(model.get("id") or model.get("streamName") or "").strip()
        timestamp = str(
            model.get("snapshotTimestamp") or model.get("verifiedSnapshotTimestamp") or ""
        ).strip()
        snapshot = (
            f"https://img.doppiocdn.net/snapshot/{model_id}/{timestamp}"
            if model_id and timestamp
            else None
        )

        if is_online:
            return snapshot or preview or avatar
        return preview or avatar or snapshot

    @staticmethod
    def _stripchat_model_identity(model: dict[str, object]) -> str:
        """Stable dedupe key: provider id first, else source+username (never display name)."""
        model_id = str(model.get("id") or model.get("streamName") or "").strip()
        if model_id:
            return f"id:{model_id}"
        username = str(model.get("username") or model.get("login") or "").strip().lower()
        if username:
            return f"stripchat:{username}"
        return ""

    def _stripchat_stamp_couples_theme(
        self,
        items: list[dict[str, object]],
        primary_tag: str,
    ) -> list[dict[str, object]]:
        if primary_tag != "couples":
            return items
        stamped: list[dict[str, object]] = []
        for item in items:
            entry = dict(item)
            theme_tags = list(entry.get("tags") or [])
            if "couple" not in {str(tag).strip().lower() for tag in theme_tags}:
                theme_tags = ["couple", *theme_tags]
            entry["tags"] = _normalize_tags(theme_tags)
            stamped.append(entry)
        return stamped

    def _stripchat_pool_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_stripchat_unique_pool_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._stripchat_unique_pool_lock = lock
        return lock

    def _stripchat_pool_gender_token(self, gender: Optional[str]) -> str:
        token = str(gender or "").strip().lower()
        if not token or token == "all":
            return ""
        return token

    def _stripchat_unique_pool_key(
        self,
        primary_tag: str,
        gender: Optional[str],
        tags: list[str],
        limit: int,
        request_limit: int,
    ) -> tuple:
        # gender token isolates All ("") from Female even when primaryTag=girls.
        return (
            str(primary_tag),
            self._stripchat_pool_gender_token(gender),
            tuple(str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()),
            int(limit),
            int(request_limit),
        )

    def _stripchat_prune_unique_pools(self, now: Optional[float] = None) -> None:
        pools = getattr(self, "_stripchat_unique_pools", None)
        if not pools:
            return
        now = time.monotonic() if now is None else float(now)
        expired = [
            key
            for key, state in list(pools.items())
            if now - float(state.get("updated_at") or 0) > _STRIPCHAT_UNIQUE_POOL_TTL_SECONDS
        ]
        for key in expired:
            pools.pop(key, None)
        if len(pools) <= _STRIPCHAT_UNIQUE_POOL_MAX_KEYS:
            return
        # Drop oldest entries first when over capacity.
        ordered = sorted(
            pools.items(),
            key=lambda item: float(item[1].get("updated_at") or 0),
        )
        overflow = len(pools) - _STRIPCHAT_UNIQUE_POOL_MAX_KEYS
        for key, _state in ordered[:overflow]:
            pools.pop(key, None)

    def _stripchat_unique_pool_state(
        self,
        pool_key: tuple,
    ) -> dict[str, object]:
        pools = getattr(self, "_stripchat_unique_pools", None)
        if pools is None:
            pools = {}
            self._stripchat_unique_pools = pools
        now = time.monotonic()
        self._stripchat_prune_unique_pools(now)
        state = pools.get(pool_key)
        if state and now - float(state.get("updated_at") or 0) <= _STRIPCHAT_UNIQUE_POOL_TTL_SECONDS:
            return state
        state = {
            "items": [],
            "seen": set(),
            "next_offset": 0,
            "exhausted": False,
            "updated_at": now,
        }
        pools[pool_key] = state
        self._stripchat_prune_unique_pools(now)
        return state

    def _stripchat_drop_empty_pool(self, pool_key: Optional[tuple]) -> None:
        if pool_key is None:
            return
        pools = getattr(self, "_stripchat_unique_pools", None)
        if not pools:
            return
        state = pools.get(pool_key)
        if state is not None and not state.get("items"):
            pools.pop(pool_key, None)

    def _stripchat_payload_items(
        self,
        payload: object,
        primary_tag: str,
        search: str,
        tags: list[str],
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        seen_in_payload: set[str] = set()
        for model in self._stripchat_models_from_payload(payload):
            if not isinstance(model, dict):
                continue
            identity = self._stripchat_model_identity(model)
            if not identity or identity in seen_in_payload:
                continue
            item = self._stripchat_model_item(model)
            if not item:
                continue
            seen_in_payload.add(identity)
            # Keep identity on a private key for pool dedupe; not required by API consumers.
            item["_stripchat_identity"] = identity
            items.append(item)
        items = self._stripchat_stamp_couples_theme(items, primary_tag)
        return self._stripchat_filter_items(items, search=search, tags=tags)

    def _stripchat_parse_search_models(self, html_text: str, needle: str) -> list[dict[str, object]]:
        """Build Discover cards from /search/models HTML (id contains needle)."""
        needle_l = (needle or "").strip().lower()
        if not needle_l or not html_text:
            return []
        # Short queries (y/yy) often SSR an empty SearchPage__models shell; fall
        # back to scanning the whole document for thumbs when the section is empty.
        section = _SEARCH_MODELS_SECTION_RE.search(html_text or "")
        section_body = section.group("body") if section else ""
        blob = section_body if len(section_body) > 50 else html_text
        ordered: list[dict[str, object]] = []
        seen: set[str] = set()
        for match in _SEARCH_MODEL_LINK_RE.finditer(blob):
            username = (match.group("username") or match.group("username2") or "").strip()
            model_id = (match.group("id") or match.group("id2") or "").strip()
            login = username.lower()
            if not login or login in _RESERVED_PROFILE_SEGMENTS or login in seen:
                continue
            # Contiguous substring match on the room id (yyy → yyydgda).
            if needle_l not in login:
                continue
            seen.add(login)
            # Bound to this thumb only — the next card's "Offline Chat Room"
            # must not mark the current username offline.
            window_end = match.start() + 2500
            next_card = blob.find("SearchModelThumb", match.end())
            if next_card >= 0:
                window_end = min(window_end, next_card)
            next_link = _SEARCH_MODEL_LINK_RE.search(blob, match.end())
            if next_link is not None:
                window_end = min(window_end, next_link.start())
            window = blob[match.start(): window_end]
            imgs = _SEARCH_THUMB_IMG_RE.findall(window)
            thumbnail = ""
            for url in imgs:
                if "blurred" in url.lower():
                    continue
                thumbnail = url
                break
            if not thumbnail and imgs:
                thumbnail = imgs[0]
            is_offline = bool(re.search(r"Offline Chat Room", window, re.IGNORECASE))
            is_live = (not is_offline) and (
                "model-list-item-live" in window
                or bool(re.search(r"Live Chat Room", window, re.IGNORECASE))
            )
            room_status = "public" if is_live else "offline"
            ordered.append(
                {
                    "username": username,
                    "display_name": username,
                    "thumbnail": thumbnail,
                    "profile_image_url": "",
                    "viewers": 0,
                    "followers": None,
                    "subject": "",
                    "age": None,
                    "gender": "",
                    "is_online": is_live,
                    "tags": [room_status] if room_status != "public" else ["public"],
                    "room_status": room_status,
                    "source_type": self.source_type,
                    "channel_url": self.canonical_url(username),
                    "user_id": model_id or None,
                }
            )
        return ordered

    def _stripchat_parse_search_usernames(self, html_text: str, needle: str) -> list[str]:
        """Extract usernames from /search/models HTML whose id contains needle."""
        return [
            str(item.get("username") or "")
            for item in self._stripchat_parse_search_models(html_text, needle)
            if str(item.get("username") or "").strip()
        ]

    async def _stripchat_search_models_html(self, search: str) -> list[dict[str, object]]:
        """Username-contains hits from Stripchat's models search page (no per-user API)."""
        needle = (search or "").strip()
        if not needle:
            return []
        referer = f"{STRIPCHAT_BASE_URL}/search/{quote_plus(needle)}"
        url = f"{STRIPCHAT_BASE_URL}/search/models/{quote_plus(needle)}"
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        try:
            api_headers = await self._stripchat_api_headers(referer=referer, has_body=False)
            front_version = str(api_headers.get("Front-Version") or "").strip()
            if front_version:
                headers["Front-Version"] = front_version
            cookie_header = str(api_headers.get("Cookie") or "").strip()
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception:
            pass
        timeout = aiohttp.ClientTimeout(
            total=int(os.getenv("HXYLIVE_STRIPCHAT_SEARCH_TIMEOUT", "15") or "15")
        )
        try:
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    if resp.status >= 400:
                        return []
                    html_text = await resp.text(errors="ignore")
        except Exception as exc:
            logger.debug("Stripchat search HTML failed", error=str(exc), search=needle)
            return []
        return self._stripchat_parse_search_models(html_text, needle)

    async def _stripchat_search_usernames_html(self, search: str) -> list[str]:
        items = await self._stripchat_search_models_html(search)
        return [str(item.get("username") or "") for item in items if item.get("username")]

    async def _stripchat_hydrate_search_username(
        self,
        username: str,
        *,
        exact: bool,
    ) -> Optional[dict[str, object]]:
        try:
            profile = await self._stripchat_model_by_username(username)
        except Exception:
            return None
        item = self._stripchat_model_item(profile)
        if not item:
            return None
        # Exact id search keeps private/offline cards; substring hits stay live-only.
        if not exact and not item.get("is_online"):
            return None
        return item

    async def _stripchat_catalog_viewers_bulk(
        self,
        usernames: set[str],
        *,
        gender: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, int]:
        """One catalogue walk to fill viewers for a page of search hits."""
        pending = {str(name or "").strip().lower() for name in usernames if str(name or "").strip()}
        found: dict[str, int] = {}
        if not pending:
            return found

        pools = getattr(self, "_stripchat_unique_pools", None)
        if isinstance(pools, dict):
            for state in list(pools.values()):
                if not isinstance(state, dict):
                    continue
                for item in list(state.get("items") or []):
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("username") or "").strip().lower()
                    if key not in pending:
                        continue
                    viewers = int(item.get("viewers") or 0)
                    if viewers > 0:
                        found[key] = viewers
                        pending.discard(key)
                if not pending:
                    return found

        primary_tag = self._stripchat_primary_tag(gender, tags or [])
        scan_tags = [primary_tag]
        for tag in ("girls", "couples", "men", "trans"):
            if tag not in scan_tags:
                scan_tags.append(tag)

        for tag_index, tag in enumerate(scan_tags[:2]):
            if not pending:
                break
            # Primary gender tag: deep enough for mid-ranked rooms (filteredCount≈1000).
            max_offset = 950 if tag_index == 0 else 100
            for offset in range(0, max_offset + 1, 50):
                if not pending:
                    break
                try:
                    payload = await self._stripchat_api_json(
                        "GET",
                        STRIPCHAT_MODELS_CATALOGUE_PATH,
                        params={
                            "primaryTag": tag,
                            "limit": 50,
                            "offset": offset,
                        },
                        referer=f"{STRIPCHAT_BASE_URL}/{tag}",
                    )
                except Exception:
                    break
                for row in self._stripchat_models_from_payload(payload):
                    key = str(row.get("username") or row.get("login") or "").strip().lower()
                    if key not in pending:
                        continue
                    viewers = _stripchat_viewers_from_raw(row)
                    if viewers > 0:
                        found[key] = viewers
                        pending.discard(key)
                page_models = payload.get("models") if isinstance(payload, dict) else None
                if isinstance(page_models, list) and len(page_models) < 50:
                    break
                if not isinstance(page_models, list) and not self._stripchat_models_from_payload(payload):
                    break
        return found

    async def _stripchat_enrich_search_page(
        self,
        page_items: list[dict[str, object]],
        *,
        needle: str,
        gender: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> list[dict[str, object]]:
        """Refresh cover/avatar/status via cam API; fill viewers from catalogue."""
        if not page_items:
            return []
        needle_l = (needle or "").strip().lower()
        sem = asyncio.Semaphore(_STRIPCHAT_SEARCH_HYDRATE_CONCURRENCY)

        async def _one(item: dict[str, object]) -> dict[str, object]:
            username = str(item.get("username") or "").strip()
            if not username:
                return item
            exact = username.lower() == needle_l
            async with sem:
                hydrated = await self._stripchat_hydrate_search_username(username, exact=exact)
            if not hydrated:
                return item
            # Keep HTML live snapshot if cam omitted a thumbnail.
            if not hydrated.get("thumbnail") and item.get("thumbnail"):
                hydrated["thumbnail"] = item.get("thumbnail")
            return hydrated

        enriched = list(await asyncio.gather(*[_one(item) for item in page_items]))
        missing = {
            str(item.get("username") or "").strip().lower()
            for item in enriched
            if item.get("is_online")
            and int(item.get("viewers") or 0) <= 0
            and not _stripchat_is_private_status(item.get("room_status"))
            and str(item.get("username") or "").strip()
        }
        if missing:
            try:
                counts = await self._stripchat_catalog_viewers_bulk(
                    missing, gender=gender, tags=tags
                )
            except Exception:
                counts = {}
            for item in enriched:
                key = str(item.get("username") or "").strip().lower()
                if key in counts and counts[key] > 0:
                    item["viewers"] = counts[key]
        return enriched

    async def _stripchat_search_models_catalogue(
        self,
        *,
        search: str,
        needed: int,
        gender: Optional[str],
        tags: list[str],
    ) -> tuple[list[dict[str, object]], bool]:
        """Live-catalogue contains fallback when Stripchat SSR omits short queries."""
        needle = str(search or "").strip()
        if not needle or needed <= 0:
            return [], True
        primary_tag = self._stripchat_primary_tag(gender, tags)
        items: list[dict[str, object]] = []
        seen: set[str] = set()
        exhausted = False
        offset = 0
        request_limit = 50
        for _ in range(_STRIPCHAT_SEARCH_CATALOGUE_MAX_GETS):
            try:
                payload = await self._stripchat_api_json(
                    "GET",
                    STRIPCHAT_MODELS_CATALOGUE_PATH,
                    params={
                        "primaryTag": primary_tag,
                        "limit": request_limit,
                        "offset": offset,
                    },
                    referer=f"{STRIPCHAT_BASE_URL}/{primary_tag}",
                )
            except Exception:
                exhausted = True
                break
            window = self._stripchat_payload_items(
                payload, primary_tag, search=needle, tags=tags
            )
            raw_count = 0
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                raw_count = len(payload.get("models") or [])
            elif isinstance(payload, list):
                raw_count = len(payload)
            else:
                # blocks-only responses still carry models in nested blocks
                raw_count = len(self._stripchat_models_from_payload(payload))
            for item in window:
                key = str(item.get("username") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append(item)
                if len(items) >= needed:
                    offset += request_limit
                    more = raw_count >= request_limit
                    return items, not more and len(items) < needed
            offset += request_limit
            if raw_count < request_limit:
                exhausted = True
                break
        return items, exhausted

    async def _stripchat_list_live_models_search(
        self,
        *,
        page: int,
        limit: int,
        search: str,
        tags: list[str],
        gender: Optional[str] = None,
    ) -> dict[str, object]:
        needle = str(search or "").strip()
        needle_l = needle.lower()
        # Prefer Stripchat's models search HTML (full contains list). Short needles
        # like y/yy often SSR an empty shell — fall back to live catalogue filter.
        html_items = await self._stripchat_search_models_html(needle)
        catalogue_exhausted = True
        by_name: dict[str, dict[str, object]] = {}
        for item in html_items:
            key = str(item.get("username") or "").strip().lower()
            if key:
                by_name[key] = item

        if not by_name:
            needed = max(page * limit + 1, limit + 1)
            catalogue_items, catalogue_exhausted = await self._stripchat_search_models_catalogue(
                search=needle,
                needed=needed,
                gender=gender,
                tags=tags,
            )
            for item in catalogue_items:
                key = str(item.get("username") or "").strip().lower()
                if key:
                    by_name[key] = item

        exact_item = await self._stripchat_hydrate_search_username(needle, exact=True)
        if exact_item:
            exact_key = str(exact_item.get("username") or needle).strip().lower()
            by_name[exact_key] = exact_item

        items = list(by_name.values())
        items = self._stripchat_filter_items(items, search=needle, tags=tags)

        def _rank(item: dict[str, object]) -> tuple:
            uname = str(item.get("username") or "").strip().lower()
            dname = str(item.get("display_name") or "").strip().lower()
            if uname == needle_l or dname == needle_l:
                tier = 3
            elif uname.startswith(needle_l) or dname.startswith(needle_l):
                tier = 2
            else:
                tier = 1
            online = 1 if item.get("is_online") else 0
            viewers = int(item.get("viewers") or 0)
            return (tier, online, viewers)

        items.sort(key=_rank, reverse=True)
        if html_items:
            total = len(items)
            total_pages = max(1, (total + limit - 1) // limit) if total else 1
        elif catalogue_exhausted:
            total = len(items)
            total_pages = max(1, (total + limit - 1) // limit) if total else 1
        else:
            # Catalogue still has more windows; keep Discover scrolling.
            total = max(len(items), page * limit + 1)
            total_pages = page + 1
        start = (page - 1) * limit
        page_items = items[start:start + limit]
        page_items = await self._stripchat_enrich_search_page(
            page_items, needle=needle, gender=gender, tags=tags
        )
        return {
            "models": page_items,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }

    async def _stripchat_list_live_models_api(
        self,
        page: int,
        limit: int,
        gender: Optional[str],
        search: str,
        tags: list[str],
    ) -> Optional[dict[str, object]]:
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        if search:
            try:
                return await self._stripchat_list_live_models_search(
                    page=page,
                    limit=limit,
                    search=search,
                    tags=tags,
                    gender=gender,
                )
            except Exception as exc:
                logger.debug("Stripchat public model API failed", error=str(exc))
                return None

        # /models accepts up to ~60; callers may request larger local pages.
        request_limit = min(max(limit, 24), 60)
        primary_tag = self._stripchat_primary_tag(gender, tags)
        pool_key = self._stripchat_unique_pool_key(
            primary_tag, gender, tags, limit, request_limit
        )
        async with self._stripchat_pool_lock():
            state: Optional[dict[str, object]] = None
            upstream_gets = 0
            try:
                state = self._stripchat_unique_pool_state(pool_key)
                needed = page * limit
                zero_unique_streak = 0

                while (
                    len(state["items"]) < needed
                    and not state["exhausted"]
                    and upstream_gets < _STRIPCHAT_MAX_UPSTREAM_GETS_PER_CALL
                ):
                    payload = await self._stripchat_api_json(
                        "GET",
                        STRIPCHAT_MODELS_CATALOGUE_PATH,
                        params={
                            "primaryTag": primary_tag,
                            "limit": request_limit,
                            "offset": max(0, int(state["next_offset"])),
                        },
                        referer=f"{STRIPCHAT_BASE_URL}/{primary_tag}",
                    )
                    upstream_gets += 1
                    state["updated_at"] = time.monotonic()
                    raw_count = 0
                    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                        raw_count = len(payload.get("models") or [])
                    elif isinstance(payload, list):
                        raw_count = len(payload)
                    window_items = self._stripchat_payload_items(
                        payload, primary_tag, search="", tags=tags
                    )
                    new_unique = 0
                    for item in window_items:
                        identity = str(item.pop("_stripchat_identity", "") or "")
                        if not identity:
                            identity = (
                                f"stripchat:{str(item.get('username') or '').strip().lower()}"
                            )
                        if not identity or identity == "stripchat:":
                            continue
                        seen: set = state["seen"]  # type: ignore[assignment]
                        if identity in seen:
                            continue
                        seen.add(identity)
                        state["items"].append(item)
                        new_unique += 1
                    # Advance past this upstream window even when overlap is high so
                    # page N does not re-fetch the same offset.
                    state["next_offset"] = int(state["next_offset"]) + request_limit
                    if new_unique == 0:
                        zero_unique_streak += 1
                    else:
                        zero_unique_streak = 0
                    if raw_count < request_limit:
                        state["exhausted"] = True
                    elif zero_unique_streak >= _STRIPCHAT_ZERO_UNIQUE_STOP_WINDOWS:
                        state["exhausted"] = True
            except Exception as exc:
                logger.debug("Stripchat public model API failed", error=str(exc))
                # Legacy degrade: cold failure → None so list_live_models can try HTML.
                if state is None or not state.get("items"):
                    self._stripchat_drop_empty_pool(pool_key)
                    return None
                state["exhausted"] = True

            if state is None:
                return None

            start = (page - 1) * limit
            page_items = list(state["items"][start:start + limit])
            # Sort only the returned page slice for display; do not re-rank the
            # whole upstream window (that previously re-surfaced the same top rooms).
            page_items.sort(key=lambda item: int(item.get("viewers") or 0), reverse=True)

            pool_len = len(state["items"])
            if not page_items:
                # Empty slice must NOT mark exhausted by itself — only real
                # upstream-end conditions may. If the GET budget ran out before
                # this page's start, keep has_more so a same-page retry can
                # continue from next_offset.
                if state["exhausted"]:
                    total = pool_len
                    total_pages = max(1, (total + limit - 1) // limit) if total else max(1, page)
                else:
                    total = max(pool_len, page * limit)
                    total_pages = page + 1
            elif state["exhausted"]:
                total = pool_len
                total_pages = max(1, (total + limit - 1) // limit)
            else:
                # Unique catalogue may still grow on later page calls (budgeted GETs).
                total = max(pool_len, page * limit + 1)
                total_pages = page + 1

            return {
                "models": page_items,
                "total": int(total),
                "page": page,
                "limit": limit,
                "total_pages": int(total_pages),
            }

    def _stripchat_filter_items(
        self,
        items: list[dict[str, object]],
        search: str = "",
        tags: Optional[list[str]] = None,
    ) -> list[dict[str, object]]:
        tags = [str(tag).lower() for tag in (tags or []) if str(tag).strip()]
        if search:
            lowered = search.lower()
            items = [
                item for item in items
                if lowered in str(item.get("username") or "").lower()
                or lowered in str(item.get("display_name") or "").lower()
            ]
        if tags:
            items = [
                item for item in items
                if all(tag in [str(value).lower() for value in (item.get("tags") or [])] for tag in tags)
            ]
        return items

    def _stripchat_primary_tag(self, gender: Optional[str], tags: list[str]) -> str:
        values = {str(gender or "").lower(), *(tag.lower() for tag in tags or [])}
        if values & {"male", "men", "man"}:
            return "men"
        if values & {"trans", "transgender", "tranny"}:
            return "trans"
        if values & {"couple", "couples", "group"}:
            return "couples"
        return "girls"

    async def _stripchat_sync_following_http(self) -> list[dict[str, object]]:
        if not await self._stripchat_has_session():
            raise ProviderAuthError("Stripchat login required to sync favorites")

        models: list[dict[str, object]] = []
        page_limit = max(1, min(100, int(os.getenv("HXYLIVE_STRIPCHAT_FAVORITES_LIMIT", "50") or "50")))
        max_pages = max(1, int(os.getenv("HXYLIVE_STRIPCHAT_FAVORITES_MAX_PAGES", "20") or "20"))
        for path in ("/models/favorites", "/models/favorites/offline"):
            offset = 0
            for _ in range(max_pages):
                payload = await self._stripchat_api_json(
                    "GET",
                    path,
                    params={"limit": page_limit, "offset": offset},
                    auth_required=True,
                    referer=f"{STRIPCHAT_BASE_URL}/favorites",
                )
                page_models = self._stripchat_models_from_payload(payload)
                models.extend(page_models)
                total = int(payload.get("totalCount") or len(page_models)) if isinstance(payload, dict) else len(page_models)
                if len(page_models) < page_limit or offset + page_limit >= total:
                    break
                offset += page_limit

        items = [
            item
            for item in (self._stripchat_model_item(model) for model in self._dedupe_stripchat_models(models))
            if item
        ]
        items.sort(key=lambda item: (not bool(item.get("is_online")), -int(item.get("viewers") or 0), str(item.get("username") or "").lower()))
        return items

    async def _stripchat_follow_http(self, username: str, follow: bool) -> dict[str, object]:
        if not await self._stripchat_has_session():
            raise ProviderAuthError("Stripchat login required")
        model = await self._stripchat_model_by_username(username)
        model_id = self._stripchat_model_id(model)
        if not model_id:
            raise ProviderError(f"Stripchat model not found: {username}")
        current_user_id = await self._stripchat_current_user_id()
        if not current_user_id:
            raise ProviderAuthError("Logged-in Stripchat user not found")

        if follow:
            await self._stripchat_api_json(
                "PUT",
                f"/users/{current_user_id}/favorites/{model_id}",
                body={"uniq": int(time.time() * 1000)},
                auth_required=True,
                referer=self.canonical_url(username),
            )
        else:
            await self._stripchat_api_json(
                "DELETE",
                f"/users/{current_user_id}/favorites",
                body={"favoriteIds": [int(model_id)], "uniq": int(time.time() * 1000)},
                auth_required=True,
                referer=self.canonical_url(username),
            )
        return {"success": True, "remote": True, "provider": "stripchat", "username": username}

    async def _stripchat_model_by_username(self, username: str) -> dict[str, object]:
        payload = await self._stripchat_api_json(
            "GET",
            f"/v2/models/username/{quote_plus(username)}/cam",
            auth_required=False,
            referer=self.canonical_url(username),
        )
        model = self._stripchat_profile_model(payload)
        if model:
            return model
        payload = await self._stripchat_api_json(
            "GET",
            f"/v2/users/username/{quote_plus(username)}",
            auth_required=False,
            referer=self.canonical_url(username),
        )
        model = self._stripchat_profile_model(payload)
        if model:
            return model
        raise ProviderError(f"Stripchat model not found: {username}")

    async def _resolve_stripchat_public_hls(
        self,
        target: str,
        max_height: Optional[int] = None,
        *,
        pin_variant: bool = False,
    ) -> Optional[ResolvedStream]:
        username = self._stripchat_username_from_target(target)
        if not username:
            return None

        page_url = self.canonical_url(username)
        payload = await self._stripchat_api_json(
            "GET",
            f"/v2/models/username/{quote_plus(username)}/cam",
            auth_required=False,
            referer=page_url,
        )
        model = self._stripchat_profile_model(payload) if isinstance(payload, dict) else None
        if not model:
            raise ProviderOfflineError(f"Stripchat model not found or offline: {username}")

        self._validate_stripchat_public_stream(payload, model, username)
        stream_name = self._stripchat_stream_name(payload, model)
        if not stream_name:
            raise ProviderOfflineError(f"Stripchat stream not found: {username}")

        headers = self._stream_headers(page_url, await self._provider_cookie_header())
        hosts = self._stripchat_hls_hosts_from_payload(payload) or list(STRIPCHAT_HLS_HOSTS)
        is_vr = self._stripchat_is_vr(payload, model)
        for host in hosts:
            playlist_urls = []
            if is_vr:
                playlist_urls.append(self._stripchat_master_playlist_url(host, stream_name, vr=True))
            playlist_urls.append(self._stripchat_master_playlist_url(host, stream_name))
            for playlist_url in playlist_urls:
                stream_url = await self._stripchat_validated_hls_url(
                    playlist_url,
                    headers,
                    max_height,
                    pin_variant=pin_variant,
                )
                if stream_url:
                    item = self._stripchat_model_item(model) or {}
                    tags = list(item.get("tags") or [])
                    if is_vr and "vr" not in tags:
                        tags.append("vr")
                    viewers = int(item.get("viewers") or 0)
                    if viewers <= 0 and not _stripchat_is_private_status(item.get("room_status")):
                        viewers = await self._stripchat_catalog_viewers(
                            username,
                            model_id=self._stripchat_model_id(model),
                            model=model,
                        )
                    return ResolvedStream(
                        url=stream_url,
                        headers=headers,
                        source_type=self.source_type,
                        is_live=True,
                        room_status="public",
                        viewers=viewers,
                        tags=tags,
                        thumbnail=item.get("thumbnail") or None,
                        title=str(item.get("display_name") or username),
                    )

        raise ProviderOfflineError(f"No valid public Stripchat HLS for {username}")

    async def _stripchat_validated_hls_url(
        self,
        playlist_url: str,
        headers: dict[str, str],
        max_height: Optional[int],
        *,
        pin_variant: bool = False,
    ) -> Optional[str]:
        # Watch Auto (no height + no pin): keep the master so hls.js can ABR.
        if (not max_height or max_height <= 0) and not pin_variant:
            return playlist_url if await self._stripchat_probe_hls_playlist(playlist_url, headers) else None

        playlist_text = await self._stripchat_fetch_hls_playlist(playlist_url, headers)
        if not playlist_text:
            return playlist_url if await self._stripchat_probe_hls_playlist(playlist_url, headers) else None
        # Fixed height, or recording "best": pin one media playlist for FFmpeg.
        picked = self._stripchat_variant_url_for_height(playlist_url, playlist_text, max_height)
        return picked or playlist_url

    @staticmethod
    def _stripchat_variant_url_for_height(
        playlist_url: str,
        playlist_text: str,
        max_height: Optional[int],
    ) -> Optional[str]:
        variants: list[dict[str, object]] = []
        pending_quality_height = 0
        for raw_line in (playlist_text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF"):
                match = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.IGNORECASE)
                pending_quality_height = min(int(match.group(1)), int(match.group(2))) if match else 0
                continue
            if line.startswith("#"):
                continue
            if pending_quality_height:
                variants.append({
                    # Quality settings refer to the shorter picture dimension:
                    # 720p is 1280x720 in landscape and 720x960 in portrait.
                    "height": pending_quality_height,
                    "url": urljoin(playlist_url, line),
                })
            pending_quality_height = 0

        if not variants:
            return None

        if not max_height or max_height <= 0:
            selected = max(variants, key=lambda item: int(item["height"]))
        else:
            eligible = [item for item in variants if int(item["height"]) <= max_height]
            if eligible:
                selected = max(eligible, key=lambda item: int(item["height"]))
            else:
                selected = min(variants, key=lambda item: int(item["height"]))
        return str(selected["url"])

    def _stripchat_master_playlist_query(self) -> str:
        params = {
            "minHeight": os.getenv("HXYLIVE_STRIPCHAT_MIN_HEIGHT", "240"),
            "playlistType": os.getenv("HXYLIVE_STRIPCHAT_PLAYLIST_TYPE", "standard"),
        }
        playback_key = (os.getenv("HXYLIVE_STRIPCHAT_PLAYBACK_KEY", STRIPCHAT_PLAYBACK_KEY) or "").strip()
        if playback_key:
            params["pkey"] = playback_key
        return "?" + urlencode(params)

    def _stripchat_username_from_target(self, target: str) -> Optional[str]:
        target = (target or "").strip().strip("@/ ")
        if target.startswith(("http://", "https://")):
            return self._username_from_url(target)
        if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", target):
            return None
        if target.lower() in _RESERVED_PROFILE_SEGMENTS:
            return None
        return target

    def _username_from_url(self, value: str) -> Optional[str]:
        parsed = urlparse(value or "")
        host = parsed.netloc.lower().removeprefix("www.")
        if self.domains and not any(host == d or host.endswith("." + d) for d in self.domains):
            return None
        path_parts = [unquote(p).strip() for p in parsed.path.split("/") if p.strip()]
        candidate = path_parts[0] if path_parts else ""
        if candidate.lower() in {"model", "models"} and len(path_parts) > 1:
            candidate = path_parts[1]
        candidate = candidate.strip("@/ ").split("?", 1)[0].split("#", 1)[0]
        candidate = re.sub(r"\.html?$", "", candidate)
        if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", candidate or ""):
            return None
        if candidate.lower() in _RESERVED_PROFILE_SEGMENTS:
            return None
        return candidate

    def _stripchat_catalog_primary_tag(self, model: Optional[dict[str, object]] = None) -> str:
        raw = " ".join(
            str((model or {}).get(key) or "")
            for key in ("contestGender", "broadcastGender", "gender", "genderGroup")
        ).lower()
        if any(token in raw for token in ("couple", "malefemale", "group")):
            return "couples"
        if any(token in raw for token in ("trans", "tranny")):
            return "trans"
        if any(token in raw for token in ("male", "men", "guy")) and "female" not in raw:
            return "men"
        return "girls"

    async def _stripchat_catalog_viewers(
        self,
        username: str,
        model_id: str = "",
        model: Optional[dict[str, object]] = None,
    ) -> int:
        """Resolve chat-nav online count via catalogue viewersCount (not favorites).

        Cam/profile APIs usually omit viewersCount. Discover browse already has the
        count from /models, but Watch/Media/search resolve by username and must
        scan the catalogue. Mid-ranked rooms sit past the top page, so primary-tag
        scanning goes deeper (filteredCount on /models caps near 1000).
        """
        username_l = str(username or "").strip().lower()
        model_id = str(model_id or "").strip()
        if not username_l and not model_id:
            return 0

        for state in list(self._stripchat_unique_pools.values()):
            for item in list(state.get("items") or []):
                if not isinstance(item, dict):
                    continue
                if username_l and str(item.get("username") or "").strip().lower() == username_l:
                    viewers = int(item.get("viewers") or 0)
                    if viewers > 0:
                        return viewers
                identity = str(item.get("_stripchat_identity") or "")
                if model_id and identity in {f"id:{model_id}", model_id}:
                    viewers = int(item.get("viewers") or 0)
                    if viewers > 0:
                        return viewers

        primary_tag = self._stripchat_catalog_primary_tag(model)
        tags = [primary_tag]
        for tag in ("girls", "couples", "men", "trans"):
            if tag not in tags:
                tags.append(tag)

        # Primary gender tag: deep enough for mid-ranked live rooms.
        # Other tags stay shallow — wrong-gender scans rarely help.
        for tag_index, tag in enumerate(tags[:2]):
            max_offset = 950 if tag_index == 0 else 100
            for offset in range(0, max_offset + 1, 50):
                try:
                    payload = await self._stripchat_api_json(
                        "GET",
                        STRIPCHAT_MODELS_CATALOGUE_PATH,
                        params={
                            "primaryTag": tag,
                            "limit": 50,
                            "offset": offset,
                        },
                        referer=f"{STRIPCHAT_BASE_URL}/{tag}",
                    )
                except Exception:
                    break
                models = self._stripchat_models_from_payload(payload)
                for row in models:
                    row_id = str(row.get("id") or row.get("streamName") or "").strip()
                    row_user = str(row.get("username") or row.get("login") or "").strip().lower()
                    if (username_l and row_user == username_l) or (model_id and row_id == model_id):
                        return _stripchat_viewers_from_raw(row)
                # Stop when the primary models page itself is short (not when
                # nested recommendation blocks inflate the flattened list).
                page_models = payload.get("models") if isinstance(payload, dict) else None
                if isinstance(page_models, list) and len(page_models) < 50:
                    break
                if not isinstance(page_models, list) and len(models) < 50:
                    break
        return 0

    def _validate_stripchat_public_stream(
        self,
        payload: object,
        model: dict[str, object],
        username: str,
    ) -> None:
        if self._stripchat_login_requires_interaction(payload):
            raise ProviderInteractionRequired("Stripchat requires interactive verification")

        cam = payload.get("cam") if isinstance(payload, dict) and isinstance(payload.get("cam"), dict) else {}
        private_indicators = (
            cam.get("show"),
            cam.get("privateMode"),
            cam.get("groupShowAnnouncement"),
            cam.get("ticketShow"),
            cam.get("ticketShowAnnouncement"),
            cam.get("privateShow"),
        )
        if any(self._stripchat_truthy_indicator(value) for value in private_indicators):
            raise ProviderPrivateError(f"Stripchat/{username}: private, group, or ticket show")

        for value in (model.get("status"), cam.get("streamStatus"), cam.get("status")):
            status = str(value or "").strip().lower()
            if not status:
                continue
            if any(marker in status for marker in ("private", "group", "ticket", "premium", "spy", "p2p")):
                raise ProviderPrivateError(f"Stripchat/{username}: private, group, or ticket show")
            if _stripchat_is_offline_status(status):
                raise ProviderOfflineError(f"Stripchat/{username}: model offline")

        if cam.get("isCamAvailable") is False or cam.get("isCamActive") is False:
            raise ProviderOfflineError(f"Stripchat/{username}: model offline")
        if model.get("isOnline") is False or model.get("isLive") is False:
            raise ProviderOfflineError(f"Stripchat/{username}: model offline")

    def _stripchat_truthy_indicator(self, value: object) -> bool:
        return _stripchat_truthy_cam_indicator(value)

    def _stripchat_hls_hosts_from_payload(self, payload: object) -> list[str]:
        values: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    key_lower = str(key).lower()
                    if key_lower == "hlsstreamhost" and child:
                        values.append(str(child))
                    elif key_lower == "fallbackdomains" and isinstance(child, list):
                        values.extend(str(item) for item in child if item)
                    else:
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        hosts: list[str] = []
        seen = set()
        for value in values:
            host = str(value or "").strip().strip("/")
            if not host or host in seen:
                continue
            seen.add(host)
            hosts.append(host)
        return hosts

    async def _stripchat_fetch_hls_playlist(self, playlist_url: str, headers: dict[str, str]) -> Optional[str]:
        try:
            timeout = aiohttp.ClientTimeout(total=int(os.getenv("HXYLIVE_STRIPCHAT_HLS_PROBE_TIMEOUT", "12") or "12"))
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.get(
                    playlist_url,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    if resp.status in (401, 403):
                        raise ProviderAuthError("Stripchat stream refused or session required")
                    if resp.status >= 400:
                        return None
                    text = await resp.text(errors="ignore")
        except ProviderError:
            raise
        except Exception as exc:
            logger.debug("Stripchat HLS probe failed", url=playlist_url, error=str(exc))
            return None
        return text if text.lstrip().startswith("#EXTM3U") else None

    async def _stripchat_probe_hls_playlist(self, playlist_url: str, headers: dict[str, str]) -> bool:
        return bool(await self._stripchat_fetch_hls_playlist(playlist_url, headers))

    async def _stripchat_user_by_username(self, username: str) -> dict[str, object]:
        payload = await self._stripchat_api_json(
            "GET",
            f"/v2/users/username/{quote_plus(username)}",
            auth_required=True,
            referer=STRIPCHAT_BASE_URL,
        )
        model = self._stripchat_profile_model(payload)
        if model:
            return model
        raise ProviderAuthError("Logged-in Stripchat user not found")

    async def _stripchat_current_user_id(self) -> Optional[int]:
        state = await self._stripchat_session_state()
        saved_username = self._stripchat_saved_username(state)
        local_id = self._stripchat_find_user_id(state.get("localStorage"), saved_username)
        if local_id:
            return local_id
        if saved_username:
            user = await self._stripchat_user_by_username(saved_username)
            model_id = self._stripchat_model_id(user)
            if model_id:
                try:
                    return int(model_id)
                except (TypeError, ValueError):
                    return None
        return None

    def _stripchat_model_id(self, model: dict[str, object]) -> str:
        return str(model.get("id") or model.get("streamName") or "").strip()

    def _stripchat_stream_name(
        self,
        payload: object,
        model: dict[str, object],
    ) -> str:
        cam = payload.get("cam") if isinstance(payload, dict) and isinstance(payload.get("cam"), dict) else {}
        return str(cam.get("streamName") or model.get("streamName") or self._stripchat_model_id(model)).strip()

    def _stripchat_is_vr(
        self,
        payload: object,
        model: dict[str, object],
    ) -> bool:
        cam = payload.get("cam") if isinstance(payload, dict) and isinstance(payload.get("cam"), dict) else {}
        broadcast_settings = cam.get("broadcastSettings") if isinstance(cam.get("broadcastSettings"), dict) else {}
        payload_settings = payload.get("broadcastSettings") if isinstance(payload, dict) and isinstance(payload.get("broadcastSettings"), dict) else {}
        indicators = (
            model.get("isVr"),
            model.get("vr"),
            cam.get("isVr"),
            cam.get("vr"),
            cam.get("vrCameraSettings"),
            broadcast_settings.get("vrCameraSettings"),
            payload_settings.get("vrCameraSettings"),
        )
        return any(self._stripchat_truthy_indicator(value) for value in indicators)

    def _stripchat_master_playlist_url(self, host: str, stream_name: str, vr: bool = False) -> str:
        if vr:
            vr_name = f"{stream_name}_vr"
            return f"https://edge-hls.{host}/hls/{vr_name}/master/{vr_name}.m3u8"
        return (
            f"https://edge-hls.{host}/hls/{stream_name}/master/{stream_name}_auto.m3u8"
            f"{self._stripchat_master_playlist_query()}"
        )

    def _stripchat_front_version(self) -> str:
        return (os.getenv("HXYLIVE_STRIPCHAT_FRONT_VERSION") or STRIPCHAT_FRONT_VERSION).strip() or STRIPCHAT_FRONT_VERSION

    def _stripchat_login_paths(self) -> tuple[str, ...]:
        raw = (os.getenv("HXYLIVE_STRIPCHAT_LOGIN_PATHS") or "").strip()
        if not raw:
            return STRIPCHAT_LOGIN_PATHS
        paths = tuple(part.strip() for part in raw.split(",") if part.strip())
        return paths or STRIPCHAT_LOGIN_PATHS

    def _stripchat_login_payloads(
        self,
        username: str,
        password: str,
        seed: dict[str, object],
    ) -> list[dict[str, object]]:
        uniq = int(time.time() * 1000)
        base: dict[str, object] = {
            "loginOrEmail": username,
            "password": password,
            "uniq": uniq,
        }
        csrf_token = str(seed.get("csrfToken") or seed.get("csrf_token") or "").strip()
        if csrf_token:
            base["csrfToken"] = csrf_token

        payloads = [base]
        fingerprint = str(seed.get("fingerprint") or "").strip()
        fingerprint_v2 = seed.get("fingerprintV2")
        if fingerprint or fingerprint_v2:
            payloads.insert(0, {
                **base,
                **({"fingerprint": fingerprint} if fingerprint else {}),
                **({"fingerprintV2": fingerprint_v2} if fingerprint_v2 else {}),
            })
        if "@" in username:
            payloads.append({"email": username, "password": password, "uniq": uniq})
        else:
            payloads.append({"username": username, "password": password, "uniq": uniq})
        return payloads

    async def _stripchat_seed_http_session(self, session, username: str) -> dict[str, object]:
        seed: dict[str, object] = {"front_version": self._stripchat_front_version()}
        login_url = f"{STRIPCHAT_BASE_URL}/login"

        flaresolverr_cookies, _ = await self._refresh_flaresolverr_cookies(login_url, username=username)
        if flaresolverr_cookies:
            cookie_values = {
                str(cookie.get("name")): str(cookie.get("value") or "")
                for cookie in flaresolverr_cookies
                if cookie.get("name")
            }
            try:
                session.cookie_jar.update_cookies(cookie_values, response_url=URL(STRIPCHAT_BASE_URL))
            except Exception:
                pass

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": STRIPCHAT_BASE_URL,
        }
        try:
            async with session.get(login_url, headers=headers, allow_redirects=True, **aiohttp_request_kwargs()) as resp:
                text = await resp.text(errors="ignore")
        except Exception:
            text = ""

        release_match = re.search(r'"releaseVersion"\s*:\s*"([^"]+)"', text)
        if release_match:
            seed["front_version"] = release_match.group(1)
        csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', text)
        if csrf_match:
            seed["csrfToken"] = csrf_match.group(1)

        try:
            payload = await self._stripchat_http_json(
                session,
                "GET",
                "/v3/config/initial-dynamic",
                params={"requestPath": "/login"},
                referer=login_url,
                include_stored_auth=False,
                front_version=str(seed.get("front_version") or ""),
            )
            version = self._stripchat_find_value(payload, {"releaseversion", "frontversion"})
            if version:
                seed["front_version"] = str(version)
            csrf_token = self._stripchat_find_value(payload, {"csrftoken", "csrf"})
            if csrf_token:
                seed["csrfToken"] = str(csrf_token)
        except ProviderError:
            pass
        return seed

    async def _stripchat_http_json(
        self,
        session,
        method: str,
        path: str,
        params: Optional[dict[str, object]] = None,
        body: Optional[dict[str, object]] = None,
        referer: Optional[str] = None,
        include_stored_auth: bool = False,
        front_version: Optional[str] = None,
    ) -> object:
        headers = await self._stripchat_api_headers(
            referer=referer,
            has_body=body is not None,
            include_stored_auth=include_stored_auth,
            front_version=front_version,
        )
        url = f"{STRIPCHAT_API_BASE}{path if path.startswith('/') else '/' + path}"
        try:
            async with session.request(
                method.upper(),
                url,
                params=params or None,
                json=body if body is not None else None,
                headers=headers,
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
                text = await resp.text(errors="ignore")
                stripped = text.lstrip()
                if stripped.startswith(("{", "[")):
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("Invalid Stripchat JSON response") from exc
                    if isinstance(payload, dict):
                        payload.setdefault("_http_status", resp.status)
                    return payload
                if _INTERACTION_RE.search(text):
                    raise ProviderInteractionRequired("Stripchat requires interactive verification")
                return {"_http_status": resp.status, "_raw": text[:1000]}
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Stripchat API unavailable: {exc}") from exc

    def _stripchat_login_requires_interaction(self, value: object) -> bool:
        challenge_keys = {
            "captcha",
            "recaptcha",
            "hcaptcha",
            "turnstile",
            "needcodeconfirmation",
            "needemailconfirmation",
            "needxhconfirmation",
            "twofa",
            "twofactor",
            "twofadata",
            "isrequired",
            "isaptcharequired",
            "iscaptcharequired",
        }
        message_keys = {
            "_raw",
            "description",
            "detail",
            "error",
            "errors",
            "message",
            "messages",
            "reason",
            "title",
        }

        def walk(child: object, inspect_text: bool = False) -> bool:
            if isinstance(child, dict):
                for key, nested in child.items():
                    key_lower = str(key).lower()
                    if key_lower in challenge_keys and nested not in (None, False, "", [], {}):
                        return True
                    if key_lower in message_keys and walk(nested, inspect_text=True):
                        return True
                    if isinstance(nested, (dict, list)) and walk(nested):
                        return True
                return False
            if isinstance(child, list):
                return any(walk(nested, inspect_text=inspect_text) for nested in child)
            if isinstance(child, str):
                return inspect_text and bool(_INTERACTION_RE.search(child))
            return False

        return walk(value, inspect_text=isinstance(value, str))

    def _stripchat_text_values(self, value: object) -> list[str]:
        values: list[str] = []
        if isinstance(value, dict):
            for child in value.values():
                values.extend(self._stripchat_text_values(child))
        elif isinstance(value, list):
            for child in value:
                values.extend(self._stripchat_text_values(child))
        elif isinstance(value, str):
            text = value.strip()
            if text:
                values.append(text)
        return values

    def _stripchat_login_error(self, payload: object) -> Optional[str]:
        status = int(payload.get("_http_status") or 0) if isinstance(payload, dict) else 0
        if status in (404, 405):
            return None
        text = " ".join(self._stripchat_text_values(payload))
        if _LOGIN_FAILED_RE.search(text) or status in (400, 401):
            return "Login failed. Check username and password."
        if status == 429:
            return "Stripchat login rate limited. Retry later."
        if status >= 400:
            return f"Stripchat login refused (HTTP {status})"
        return None

    def _stripchat_user_matches(self, user: dict[str, object], username: str) -> bool:
        if not user.get("id"):
            return False
        needle = username.lower().strip()
        if not needle:
            return True
        candidates = [
            str(user.get("username") or "").lower().strip(),
            str(user.get("login") or "").lower().strip(),
            str(user.get("email") or "").lower().strip(),
        ]
        return needle in {candidate for candidate in candidates if candidate}

    def _stripchat_current_user_from_payload(self, value: object, username: str = "") -> Optional[dict[str, object]]:
        if isinstance(value, dict):
            for key in ("currentUser", "current_user"):
                child = value.get(key)
                if isinstance(child, dict) and child.get("id"):
                    return child
            for key in ("user", "account", "viewer"):
                child = value.get(key)
                if isinstance(child, dict) and self._stripchat_user_matches(child, username):
                    return child
            if self._stripchat_user_matches(value, username):
                return value
            for child in value.values():
                found = self._stripchat_current_user_from_payload(child, username)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._stripchat_current_user_from_payload(child, username)
                if found:
                    return found
        elif isinstance(value, str) and value.strip().startswith(("{", "[")):
            try:
                return self._stripchat_current_user_from_payload(json.loads(value), username)
            except Exception:
                return None
        return None

    def _stripchat_login_state_from_payload(
        self,
        payload: object,
        username: str,
    ) -> Optional[tuple[str, dict[str, object], str]]:
        user = self._stripchat_current_user_from_payload(payload, username)
        if not user:
            return None
        resolved_username = str(user.get("username") or user.get("login") or username or "").strip()
        jwt_token = self._stripchat_find_value(payload, {"jwttoken", "jwt", "authjwt", "accesstoken"})
        return resolved_username or username, user, str(jwt_token or "").strip()

    def _stripchat_local_storage_state(
        self,
        user: dict[str, object],
        jwt_token: str = "",
    ) -> list[dict[str, object]]:
        entries = [
            {"name": "currentUser", "value": json.dumps({"currentUser": user}, separators=(",", ":"))},
        ]
        if jwt_token:
            entries.append({"name": "jwtToken", "value": jwt_token})
        return [{"origin": STRIPCHAT_BASE_URL, "localStorage": entries}]

    def _stripchat_cookie_jar_to_playwright(self, session) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        try:
            morsels = session.cookie_jar.filter_cookies(URL(STRIPCHAT_BASE_URL)).values()
        except Exception:
            morsels = []
        for morsel in morsels:
            name = getattr(morsel, "key", None) or ""
            value = getattr(morsel, "value", None)
            if not name or value is None:
                continue
            try:
                path = morsel["path"] or "/"
            except Exception:
                path = "/"
            try:
                http_only = bool(morsel["httponly"])
            except Exception:
                http_only = False
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".stripchat.com",
                "path": path,
                "secure": True,
                "httpOnly": http_only,
                "sameSite": "Lax",
            })
        return cookies

    async def _stripchat_save_login_failure(self, username: str, last_error: str) -> None:
        if self.session_store:
            await self.session_store.save(
                self.source_type,
                username=username,
                is_logged_in=False,
                last_error=last_error,
            )

    async def _stripchat_login_http(self, username: str, password: str) -> dict[str, object]:
        username = (username or "").strip()
        if not username or not password:
            return {"success": False, "error": "Username and password are required"}

        timeout = aiohttp.ClientTimeout(total=int(os.getenv("HXYLIVE_STRIPCHAT_LOGIN_TIMEOUT", "30") or "30"))
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp_client_session(timeout=timeout, cookie_jar=cookie_jar) as session:
            seed = await self._stripchat_seed_http_session(session, username)
            last_error: Optional[str] = None
            for path in self._stripchat_login_paths():
                for body in self._stripchat_login_payloads(username, password, seed):
                    try:
                        payload = await self._stripchat_http_json(
                            session,
                            "POST",
                            path,
                            body=body,
                            referer=f"{STRIPCHAT_BASE_URL}/login",
                            include_stored_auth=False,
                            front_version=str(seed.get("front_version") or ""),
                        )
                    except ProviderInteractionRequired:
                        await self._stripchat_save_login_failure(username, "interaction_required")
                        raise
                    except ProviderError as exc:
                        last_error = str(exc)
                        continue

                    if self._stripchat_login_requires_interaction(payload):
                        await self._stripchat_save_login_failure(username, "interaction_required")
                        raise ProviderInteractionRequired(
                            "Stripchat requires CAPTCHA/2FA; import a verified browser session"
                        )

                    error = self._stripchat_login_error(payload)
                    if error:
                        status = int(payload.get("_http_status") or 0) if isinstance(payload, dict) else 0
                        if status in (404, 405):
                            continue
                        if status == 403:
                            await self._stripchat_save_login_failure(username, "interaction_required")
                            raise ProviderInteractionRequired(
                                "Stripchat rejected automatic login; import a verified browser session"
                            )
                        await self._stripchat_save_login_failure(username, "login_failed")
                        return {"success": False, "error": error}

                    state = self._stripchat_login_state_from_payload(payload, username)
                    if state:
                        resolved_username, user, jwt_token = state
                        if self.session_store:
                            await self.session_store.save(
                                self.source_type,
                                username=resolved_username,
                                is_logged_in=True,
                                cookies=self._stripchat_cookie_jar_to_playwright(session),
                                local_storage=self._stripchat_local_storage_state(user, jwt_token),
                                last_error=None,
                            )
                        return {"success": True, "username": resolved_username}

            for path in ("/v3/config/initial-dynamic", "/v3/config/dynamic"):
                try:
                    payload = await self._stripchat_http_json(
                        session,
                        "GET",
                        path,
                        params={"requestPath": "/"},
                        referer=STRIPCHAT_BASE_URL,
                        include_stored_auth=False,
                        front_version=str(seed.get("front_version") or ""),
                    )
                except ProviderInteractionRequired:
                    await self._stripchat_save_login_failure(username, "interaction_required")
                    raise
                except ProviderError as exc:
                    last_error = str(exc)
                    continue
                state = self._stripchat_login_state_from_payload(payload, username)
                if state:
                    resolved_username, user, jwt_token = state
                    if self.session_store:
                        await self.session_store.save(
                            self.source_type,
                            username=resolved_username,
                            is_logged_in=True,
                            cookies=self._stripchat_cookie_jar_to_playwright(session),
                            local_storage=self._stripchat_local_storage_state(user, jwt_token),
                            last_error=None,
                        )
                    return {"success": True, "username": resolved_username}

        await self._stripchat_save_login_failure(username, "interaction_required")
        if last_error:
            logger.debug("Stripchat HTTP login did not produce a verified session", error=last_error)
        raise ProviderInteractionRequired(
            "Stripchat requires CAPTCHA/2FA or rejected automatic login; import a verified browser session"
        )

    async def _stripchat_api_json(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, object]] = None,
        body: Optional[dict[str, object]] = None,
        auth_required: bool = False,
        referer: Optional[str] = None,
    ) -> object:
        if auth_required and not await self._stripchat_has_session():
            raise ProviderAuthError("Stripchat login required")
        headers = await self._stripchat_api_headers(referer=referer, has_body=body is not None)
        url = f"{STRIPCHAT_API_BASE}{path if path.startswith('/') else '/' + path}"
        timeout = aiohttp.ClientTimeout(total=int(os.getenv("HXYLIVE_STRIPCHAT_API_TIMEOUT", "20") or "20"))
        try:
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.request(
                    method.upper(),
                    url,
                    params=params or None,
                    json=body if body is not None else None,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    text = await resp.text(errors="ignore")
                    if resp.status in (401, 403):
                        raise ProviderAuthError("Session Stripchat expiree ou refusee")
                    if resp.status >= 400:
                        raise ProviderError(f"Stripchat API HTTP {resp.status}")
                    stripped = text.lstrip()
                    if not stripped:
                        return {}
                    if not stripped.startswith(("{", "[")):
                        if auth_required and _INTERACTION_RE.search(text):
                            raise ProviderInteractionRequired("Stripchat interaction required")
                        raise ProviderError("Stripchat response is not JSON")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("Invalid Stripchat JSON response") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Stripchat API unavailable: {exc}") from exc

    async def _stripchat_api_headers(
        self,
        referer: Optional[str],
        has_body: bool,
        include_stored_auth: bool = True,
        front_version: Optional[str] = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": STRIPCHAT_BASE_URL,
            "Referer": referer or f"{STRIPCHAT_BASE_URL}/",
            "Front-Version": (front_version or self._stripchat_front_version()).strip(),
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if include_stored_auth:
            cookie_header = await self._provider_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header
            jwt_token = await self._stripchat_jwt_token()
            if jwt_token:
                headers["Authorization"] = jwt_token
        return headers

    async def _stripchat_session_state(self) -> dict[str, Any]:
        if not self.session_store:
            return {}
        try:
            return await self.session_store.get(self.source_type)
        except Exception:
            return {}

    async def _stripchat_has_session(self) -> bool:
        state = await self._stripchat_session_state()
        return bool(state.get("is_logged_in") and (state.get("cookies") or state.get("localStorage")))

    def _stripchat_saved_username(self, state: dict[str, Any]) -> str:
        return str(state.get("username") or state.get("credential_username") or "").strip()

    async def _stripchat_jwt_token(self) -> str:
        state = await self._stripchat_session_state()
        token = self._stripchat_find_value(state.get("localStorage"), {"jwttoken", "jwt"})
        return str(token or "").strip()

    def _stripchat_find_user_id(self, value: object, username: str = "") -> Optional[int]:
        username = username.lower().strip()
        def to_int(raw: object) -> Optional[int]:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        if isinstance(value, dict):
            current = value.get("currentUser")
            if isinstance(current, dict) and current.get("id"):
                return to_int(current.get("id"))
            user = value.get("user")
            if isinstance(user, dict) and user.get("id") and (not username or str(user.get("username") or "").lower() == username):
                return to_int(user.get("id"))
            if value.get("id") and value.get("username") and (not username or str(value.get("username") or "").lower() == username):
                return to_int(value.get("id"))
            for child in value.values():
                found = self._stripchat_find_user_id(child, username)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._stripchat_find_user_id(child, username)
                if found:
                    return found
        elif isinstance(value, str) and value.strip().startswith(("{", "[")):
            try:
                return self._stripchat_find_user_id(json.loads(value), username)
            except Exception:
                return None
        return None

    def _stripchat_find_value(self, value: object, keys: set[str]) -> object:
        keys = {str(key).lower() for key in keys}
        if isinstance(value, dict):
            storage_name = str(value.get("name") or "").lower()
            storage_value = value.get("value")
            if storage_name in keys and storage_value not in (None, "", [], {}):
                return storage_value
            for key, child in value.items():
                if str(key).lower() in keys:
                    return child
                found = self._stripchat_find_value(child, keys)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._stripchat_find_value(child, keys)
                if found is not None:
                    return found
        elif isinstance(value, str):
            if value.strip().startswith(("{", "[")):
                try:
                    return self._stripchat_find_value(json.loads(value), keys)
                except Exception:
                    return None
        return None

import asyncio
import os
import time
from typing import Any, Optional

import aiohttp

from .base import ProviderAuthError, ProviderCapabilities, ProviderError, ProviderStatus
from .sessions import ProviderSessionStore
from .ytdlp import YtDlpProvider
from ..core.config import HXYLIVE_MAX_FOLLOW_SYNC_ITEMS
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs

_TWITCH_UNIQUE_POOL_TTL_SECONDS = 45
_TWITCH_MAX_UPSTREAM_GETS_PER_CALL = 3
_TWITCH_ZERO_UNIQUE_STOP_WINDOWS = 2
_TWITCH_UNIQUE_POOL_MAX_KEYS = 32


class TwitchProvider(YtDlpProvider):
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    STREAMS_URL = "https://api.twitch.tv/helix/streams"
    SEARCH_CHANNELS_URL = "https://api.twitch.tv/helix/search/channels"
    USERS_URL = "https://api.twitch.tv/helix/users"
    FOLLOWERS_URL = "https://api.twitch.tv/helix/channels/followers"
    FOLLOWED_CHANNELS_URL = "https://api.twitch.tv/helix/channels/followed"
    GQL_URL = "https://gql.twitch.tv/gql"
    WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    _SESSION_HINT = (
        "Twitch password login is not supported; import a browser session that includes "
        "the auth-token cookie"
    )

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
            can_follow=True,
            can_sync_following=True,
            can_discover=True,
            can_stream=True,
            can_record=True,
            uses_ytdlp=True,
        )
        self.client_id = (os.getenv("TWITCH_CLIENT_ID") or "").strip()
        self.client_secret = (os.getenv("TWITCH_CLIENT_SECRET") or "").strip()
        self.game_id = (os.getenv("TWITCH_GAME_ID") or "509659").strip()
        self._app_token = ""
        self._app_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._follower_cache: dict[str, tuple[float, int]] = {}
        self._twitch_unique_pools: dict[tuple, dict[str, Any]] = {}
        self._twitch_pool_lock_obj: Optional[asyncio.Lock] = None

    def _twitch_pool_lock(self) -> asyncio.Lock:
        if self._twitch_pool_lock_obj is None:
            self._twitch_pool_lock_obj = asyncio.Lock()
        return self._twitch_pool_lock_obj

    @staticmethod
    def _model_identity(model: dict[str, Any]) -> str:
        user_id = str(model.get("user_id") or "").strip()
        if user_id:
            return user_id
        username = str(model.get("username") or "").strip().lower()
        if username:
            return f"twitch:{username}"
        return ""

    @staticmethod
    def _stamp_stable_id(model: dict[str, Any]) -> dict[str, Any]:
        identity = TwitchProvider._model_identity(model)
        if identity:
            model["id"] = identity
        return model

    def _twitch_pool_key(
        self,
        *,
        game_id: Optional[str],
        search: str,
        limit: int,
        request_first: int,
    ) -> tuple:
        return (
            str(game_id or ""),
            str(search or "").strip().lower(),
            int(limit),
            int(request_first),
        )

    def _twitch_prune_unique_pools(self, now: Optional[float] = None) -> None:
        pools = self._twitch_unique_pools
        if not pools:
            return
        now = time.monotonic() if now is None else float(now)
        expired = [
            key
            for key, state in list(pools.items())
            if now - float(state.get("updated_at") or 0) > _TWITCH_UNIQUE_POOL_TTL_SECONDS
        ]
        for key in expired:
            pools.pop(key, None)
        if len(pools) <= _TWITCH_UNIQUE_POOL_MAX_KEYS:
            return
        ordered = sorted(
            pools.items(),
            key=lambda item: float(item[1].get("updated_at") or 0),
        )
        overflow = len(pools) - _TWITCH_UNIQUE_POOL_MAX_KEYS
        for key, _state in ordered[:overflow]:
            pools.pop(key, None)

    def _twitch_unique_pool_state(self, pool_key: tuple) -> dict[str, Any]:
        now = time.monotonic()
        self._twitch_prune_unique_pools(now)
        state = self._twitch_unique_pools.get(pool_key)
        if state and now - float(state.get("updated_at") or 0) <= _TWITCH_UNIQUE_POOL_TTL_SECONDS:
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
        self._twitch_unique_pools[pool_key] = state
        self._twitch_prune_unique_pools(now)
        return state

    async def _get_app_token(self, force_refresh: bool = False) -> str:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")

        if (
            not force_refresh
            and self._app_token
            and time.monotonic() < self._app_token_expires_at
        ):
            return self._app_token

        async with self._token_lock:
            if (
                not force_refresh
                and self._app_token
                and time.monotonic() < self._app_token_expires_at
            ):
                return self._app_token

            timeout = aiohttp.ClientTimeout(total=20)
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.post(
                    self.TOKEN_URL, data=data, **aiohttp_request_kwargs()
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                        raise RuntimeError(f"Twitch OAuth failed ({response.status}): {detail}")

            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError("Twitch OAuth response did not contain an access token")
            expires_in = max(60, int(payload.get("expires_in") or 3600))
            self._app_token = token
            self._app_token_expires_at = time.monotonic() + max(30, expires_in - 60)
            return token

    async def _helix_page(
        self,
        *,
        first: int,
        after: Optional[str] = None,
        user_login: Optional[str] = None,
        game_id: Optional[str] = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = await self._get_app_token()
        params: dict[str, str] = {"first": str(max(1, min(100, first)))}
        if after:
            params["after"] = after
        if user_login:
            params["user_login"] = user_login
        if game_id:
            params["game_id"] = game_id
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                self.STREAMS_URL,
                params=params,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status == 401 and retry_auth:
                    await self._get_app_token(force_refresh=True)
                    return await self._helix_page(
                        first=first,
                        after=after,
                        user_login=user_login,
                        game_id=game_id,
                        retry_auth=False,
                    )
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(f"Twitch Helix failed ({response.status}): {detail}")
        return payload

    async def _helix_search_channels(
        self,
        *,
        query: str,
        first: int,
        after: Optional[str] = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Partial channel-name search (login/display), live + offline."""
        needle = str(query or "").strip()
        if not needle:
            return {"data": [], "pagination": {}}
        token = await self._get_app_token()
        params: dict[str, str] = {
            "query": needle,
            "first": str(max(1, min(100, first))),
        }
        if after:
            params["after"] = after
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                self.SEARCH_CHANNELS_URL,
                params=params,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status == 401 and retry_auth:
                    await self._get_app_token(force_refresh=True)
                    return await self._helix_search_channels(
                        query=needle,
                        first=first,
                        after=after,
                        retry_auth=False,
                    )
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(
                        f"Twitch channel search failed ({response.status}): {detail}"
                    )
        return payload if isinstance(payload, dict) else {"data": [], "pagination": {}}

    async def _helix_streams_by_user_ids(
        self,
        user_ids: list[str],
        retry_auth: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Map user_id → live stream row (empty if offline / missing)."""
        ids = [str(uid or "").strip() for uid in user_ids if str(uid or "").strip()]
        if not ids:
            return {}
        token = await self._get_app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        # Helix accepts up to 100 user_id query params.
        params: list[tuple[str, str]] = [("user_id", uid) for uid in ids[:100]]
        params.append(("first", str(min(100, len(ids)))))
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                self.STREAMS_URL,
                params=params,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status == 401 and retry_auth:
                    await self._get_app_token(force_refresh=True)
                    return await self._helix_streams_by_user_ids(ids, retry_auth=False)
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(f"Twitch Helix failed ({response.status}): {detail}")
        out: dict[str, dict[str, Any]] = {}
        for stream in (payload.get("data") or []) if isinstance(payload, dict) else []:
            if not isinstance(stream, dict):
                continue
            uid = str(stream.get("user_id") or "").strip()
            if uid:
                out[uid] = stream
        return out

    async def _helix_users(
        self,
        logins: Optional[list[str]] = None,
        retry_auth: bool = True,
        ids: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        normalized = [
            str(login or "").strip().lower()
            for login in (logins or [])
            if str(login or "").strip()
        ]
        id_values = [str(uid or "").strip() for uid in (ids or []) if str(uid or "").strip()]
        if not normalized and not id_values:
            return []
        token = await self._get_app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        params: list[tuple[str, str]] = [("login", login) for login in normalized[:100]]
        params.extend(("id", uid) for uid in id_values[: max(0, 100 - len(params))])
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                self.USERS_URL,
                params=params,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status == 401 and retry_auth:
                    await self._get_app_token(force_refresh=True)
                    return await self._helix_users(
                        normalized,
                        retry_auth=False,
                        ids=id_values,
                    )
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(f"Twitch users lookup failed ({response.status}): {detail}")
        return [item for item in (payload.get("data") or []) if isinstance(item, dict)]

    @staticmethod
    def _stream_model(stream: dict[str, Any]) -> dict[str, Any]:
        thumbnail = str(stream.get("thumbnail_url") or "")
        thumbnail = thumbnail.replace("{width}", "440").replace("{height}", "248")
        tags = [str(tag).strip() for tag in (stream.get("tags") or []) if str(tag).strip()]
        language = str(stream.get("language") or "").strip()
        game_name = str(stream.get("game_name") or "").strip()
        for value in (language, game_name):
            if value and value.lower() not in {tag.lower() for tag in tags}:
                tags.append(value)
        game_id = str(stream.get("game_id") or "").strip()
        model = {
            "username": str(stream.get("user_login") or ""),
            "user_id": str(stream.get("user_id") or ""),
            "display_name": str(stream.get("user_name") or stream.get("user_login") or ""),
            "source_type": "twitch",
            "is_online": str(stream.get("type") or "live").lower() == "live",
            "room_status": "public",
            "viewers": int(stream.get("viewer_count") or 0),
            "thumbnail": thumbnail,
            "tags": tags,
            "title": str(stream.get("title") or ""),
            "category": game_name,
            "game_id": game_id,
            "game_name": game_name,
            "language": language,
            "started_at": str(stream.get("started_at") or ""),
            "channel_url": f"https://www.twitch.tv/{stream.get('user_login') or ''}",
        }
        return TwitchProvider._stamp_stable_id(model)

    async def _follower_total(self, user_id: str) -> Optional[int]:
        if not user_id:
            return None
        cached = self._follower_cache.get(user_id)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        token = await self._get_app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                self.FOLLOWERS_URL,
                params={"broadcaster_id": user_id, "first": "1"},
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    return None
        total = int(payload.get("total") or 0)
        self._follower_cache[user_id] = (time.monotonic() + 600, total)
        return total

    async def _enrich_models(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not models:
            return models
        users = await self._helix_users([model.get("username") or "" for model in models])
        users_by_login = {
            str(user.get("login") or "").lower(): user
            for user in users
        }
        for model in models:
            user = users_by_login.get(str(model.get("username") or "").lower()) or {}
            # Prefer Helix users avatar; keep search/channels thumbnail if lookup misses.
            avatar = str(user.get("profile_image_url") or "") or str(
                model.get("profile_image_url") or ""
            )
            model["profile_image_url"] = avatar
            if not str(model.get("user_id") or "").strip() and user.get("id"):
                model["user_id"] = str(user.get("id") or "")
            self._stamp_stable_id(model)
        follower_totals = await asyncio.gather(
            *(self._follower_total(model.get("user_id") or "") for model in models),
            return_exceptions=True,
        )
        for model, total in zip(models, follower_totals):
            model["followers"] = total if isinstance(total, int) else None
        return models

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

    async def _list_live_catalogue(
        self,
        *,
        page: int,
        limit: int,
        game_id: Optional[str] = None,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        # Default catalogue keeps env TWITCH_GAME_ID (C2 All path). Explicit
        # game_id from categories API selects a separate unique-pool key.
        effective_game_id = str(game_id or self.game_id or "").strip() or self.game_id
        request_first = min(100, max(limit, 24))
        pool_key = self._twitch_pool_key(
            game_id=effective_game_id,
            search="",
            limit=limit,
            request_first=request_first,
        )
        upstream_gets = 0
        provider_status = "ok"
        provider_detail = ""

        async with self._twitch_pool_lock():
            state = self._twitch_unique_pool_state(pool_key)
            needed = page * limit
            try:
                while (
                    len(state["items"]) < needed
                    and not state["exhausted"]
                    and upstream_gets < _TWITCH_MAX_UPSTREAM_GETS_PER_CALL
                ):
                    after = state.get("next_cursor")
                    payload = await self._helix_page(
                        first=request_first,
                        after=after if after else None,
                        user_login=None,
                        game_id=effective_game_id,
                    )
                    upstream_gets += 1
                    state["updated_at"] = time.monotonic()
                    streams = [
                        stream
                        for stream in (payload.get("data") or [])
                        if isinstance(stream, dict)
                    ]
                    next_cursor = str((payload.get("pagination") or {}).get("cursor") or "") or None
                    # Always advance/persist cursor after a successful GET.
                    state["next_cursor"] = next_cursor

                    new_unique = 0
                    seen_ids: set = state["seen_ids"]
                    for stream in streams:
                        model = self._stream_model(stream)
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
                    elif int(state["consecutive_zero_unique_windows"]) >= _TWITCH_ZERO_UNIQUE_STOP_WINDOWS:
                        state["exhausted"] = True
            except Exception as exc:
                provider_status = "error"
                provider_detail = str(exc)
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
                # Keep accumulated catalogue; do not pretend terminal exhaustion.

            start = (page - 1) * limit
            page_items = [dict(item) for item in state["items"][start:start + limit]]
            page_items.sort(key=lambda item: int(item.get("viewers") or 0), reverse=True)
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

        page_items = await self._enrich_models(page_items)
        return {
            "models": page_items,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "provider_status": provider_status,
            "provider_detail": provider_detail,
        }

    @staticmethod
    def _search_name_hit(channel: dict[str, Any], needle_lower: str) -> bool:
        """Contiguous contains on login or display name (not bio/description)."""
        if not needle_lower:
            return False
        login = str(channel.get("broadcaster_login") or "").strip().lower()
        dname = str(channel.get("display_name") or "").strip().lower()
        return needle_lower in login or needle_lower in dname

    @staticmethod
    def _matches_game_filter(row: dict[str, Any], game_id: Optional[str]) -> bool:
        """Category scope: every Twitch partition search stays inside its game_id."""
        wanted = str(game_id or "").strip()
        if not wanted:
            return True
        return str(row.get("game_id") or "").strip() == wanted

    @staticmethod
    def _search_rank_key(model: dict[str, Any], needle_lower: str) -> tuple:
        """exact → compact prefix (ti→tiffy) → other prefix → other contains."""
        uname = str(model.get("username") or "").strip().lower()
        dname = str(model.get("display_name") or "").strip().lower()
        max_compact = len(needle_lower) + 4

        def _compact(value: str) -> bool:
            return bool(value) and value.startswith(needle_lower) and len(value) <= max_compact

        if uname == needle_lower or dname == needle_lower:
            tier = 4
        elif _compact(uname) or _compact(dname):
            # Short id/name prefix beats long live logins so ti surfaces tiffy.
            tier = 3
        elif uname.startswith(needle_lower) or dname.startswith(needle_lower):
            tier = 2
        else:
            tier = 1
        online = 1 if model.get("is_online", True) else 0
        viewers = int(model.get("viewers") or 0)
        brevity = -min(len(uname) or 99, len(dname) or 99)
        return (tier, online, viewers, brevity)

    def _channel_search_model(self, channel: dict[str, Any]) -> dict[str, Any]:
        login = str(channel.get("broadcaster_login") or "").strip().lower()
        display = str(channel.get("display_name") or login).strip() or login
        user_id = str(channel.get("id") or "").strip()
        is_live = bool(channel.get("is_live"))
        # Search Channels thumbnail_url is the profile image, not a live frame.
        avatar = str(channel.get("thumbnail_url") or "").strip()
        game_name = str(channel.get("game_name") or "").strip()
        model = {
            "username": login,
            "user_id": user_id,
            "display_name": display,
            "source_type": "twitch",
            "is_online": is_live,
            "room_status": "public" if is_live else "offline",
            "viewers": 0,
            "thumbnail": avatar,
            "profile_image_url": avatar,
            "tags": [game_name] if game_name else [],
            "title": str(channel.get("title") or ""),
            "category": game_name,
            "game_id": str(channel.get("game_id") or "").strip(),
            "game_name": game_name,
            "language": str(channel.get("broadcaster_language") or "").strip(),
            "started_at": str(channel.get("started_at") or "") if is_live else "",
            "channel_url": f"https://www.twitch.tv/{login}",
        }
        return self._stamp_stable_id(model)

    def _offline_user_model(self, user: dict[str, Any], fallback_login: str = "") -> dict[str, Any]:
        login = str(user.get("login") or fallback_login).strip().lower()
        return self._stamp_stable_id({
            "username": login,
            "user_id": str(user.get("id") or ""),
            "display_name": str(user.get("display_name") or login),
            "source_type": "twitch",
            "is_online": False,
            "room_status": "offline",
            "viewers": 0,
            "thumbnail": str(user.get("profile_image_url") or ""),
            "profile_image_url": str(user.get("profile_image_url") or ""),
            "tags": [],
            "title": str(user.get("description") or ""),
            "category": "",
            "started_at": "",
            "channel_url": f"https://www.twitch.tv/{login}",
        })

    async def _hydrate_search_page(
        self,
        page_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Replace search stubs with live stream metadata when currently online."""
        if not page_items:
            return page_items
        user_ids = [
            str(item.get("user_id") or "").strip()
            for item in page_items
            if str(item.get("user_id") or "").strip()
        ]
        try:
            streams_by_id = await self._helix_streams_by_user_ids(user_ids)
        except Exception:
            streams_by_id = {}
        hydrated: list[dict[str, Any]] = []
        for item in page_items:
            uid = str(item.get("user_id") or "").strip()
            stream = streams_by_id.get(uid)
            if stream:
                live = self._stream_model(stream)
                # Keep avatar from search/users enrich; stream thumb is cover only.
                avatar = str(item.get("profile_image_url") or "")
                if avatar:
                    live["profile_image_url"] = avatar
                hydrated.append(live)
                continue
            offline = dict(item)
            offline["is_online"] = False
            offline["room_status"] = "offline"
            offline["viewers"] = 0
            offline["started_at"] = ""
            hydrated.append(offline)
        return hydrated

    async def _merge_category_live_contains(
        self,
        *,
        state: dict[str, Any],
        needle_lower: str,
        game_id: str,
    ) -> None:
        """Add live streams in this partition whose id/name contains needle."""
        try:
            payload = await self._helix_page(
                first=100,
                after=None,
                user_login=None,
                game_id=game_id,
            )
        except Exception:
            return
        seen_ids: set = state["seen_ids"]
        for stream in (payload.get("data") or []):
            if not isinstance(stream, dict):
                continue
            login = str(stream.get("user_login") or "").strip().lower()
            dname = str(stream.get("user_name") or "").strip().lower()
            if needle_lower not in login and needle_lower not in dname:
                continue
            if not self._matches_game_filter(stream, game_id):
                continue
            model = self._stream_model(stream)
            identity = self._model_identity(model)
            if not identity or identity in seen_ids:
                continue
            seen_ids.add(identity)
            state["items"].append(model)

    async def _list_live_search(
        self,
        *,
        page: int,
        limit: int,
        username: str,
        game_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Discover search: contiguous id/display contains, scoped to partition."""
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        needle = str(username or "").strip()
        needle_l = needle.lower()
        # Same rule for every Twitch category (ASMR / Just Chatting / …).
        effective_game_id = str(game_id or self.game_id or "").strip() or None
        if not needle_l:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "empty",
            }

        # Always pull Helix's max page (100). Short queries rank compact ids
        # like tiffy around helix~40 — a first=limit(24) window never sees them.
        request_first = 100
        pool_key = self._twitch_pool_key(
            game_id=effective_game_id,
            search=needle_l,
            limit=limit,
            request_first=request_first,
        )
        upstream_gets = 0
        provider_status = "ok"
        provider_detail = ""
        pool_len = 0
        total = 0
        total_pages = 1
        page_items: list[dict[str, Any]] = []

        async with self._twitch_pool_lock():
            state = self._twitch_unique_pool_state(pool_key)
            # Rank across a full upstream window before slicing the Discover page.
            needed = max(page * limit, request_first)
            try:
                while (
                    len(state["items"]) < needed
                    and not state["exhausted"]
                    and upstream_gets < _TWITCH_MAX_UPSTREAM_GETS_PER_CALL
                ):
                    after = state.get("next_cursor")
                    payload = await self._helix_search_channels(
                        query=needle,
                        first=request_first,
                        after=after if after else None,
                    )
                    upstream_gets += 1
                    state["updated_at"] = time.monotonic()
                    channels = [
                        row
                        for row in (payload.get("data") or [])
                        if isinstance(row, dict)
                    ]
                    next_cursor = str(
                        (payload.get("pagination") or {}).get("cursor") or ""
                    ) or None
                    state["next_cursor"] = next_cursor

                    new_unique = 0
                    seen_ids: set = state["seen_ids"]
                    for channel in channels:
                        if not self._search_name_hit(channel, needle_l):
                            continue
                        if not self._matches_game_filter(channel, effective_game_id):
                            continue
                        model = self._channel_search_model(channel)
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
                    elif int(state["consecutive_zero_unique_windows"]) >= _TWITCH_ZERO_UNIQUE_STOP_WINDOWS:
                        state["exhausted"] = True

                # Live rooms already in this partition (Helix search often ranks
                # other games first for short needles).
                if effective_game_id and not state.get("_category_live_merged"):
                    await self._merge_category_live_contains(
                        state=state,
                        needle_lower=needle_l,
                        game_id=effective_game_id,
                    )
                    state["_category_live_merged"] = True
                    state["updated_at"] = time.monotonic()
            except Exception as exc:
                provider_status = "error"
                provider_detail = str(exc)
                if not state.get("items"):
                    # Exact-login offline fallback only when not category-scoped
                    # (users API has no game_id to honor the partition).
                    if not effective_game_id:
                        try:
                            offline_users = await self._helix_users([needle])
                        except Exception:
                            offline_users = []
                        if offline_users:
                            models = [self._offline_user_model(offline_users[0], needle)]
                            models = await self._enrich_models(models)
                            return {
                                "models": models[:limit],
                                "total": len(models),
                                "page": page,
                                "limit": limit,
                                "total_pages": 1,
                                "provider_status": "ok",
                                "provider_detail": "",
                            }
                    return {
                        "models": [],
                        "total": 0,
                        "page": page,
                        "limit": limit,
                        "total_pages": 1,
                        "provider_status": "error",
                        "provider_detail": provider_detail,
                    }

            # Exact → prefix → other contains, then live/viewers.
            state["items"].sort(
                key=lambda m: self._search_rank_key(m, needle_l),
                reverse=True,
            )
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

        # Page-1 miss: exact Helix login only when search is not partition-scoped.
        if not page_items and page == 1 and not effective_game_id:
            try:
                offline_users = await self._helix_users([needle])
            except Exception:
                offline_users = []
            if offline_users:
                page_items = [self._offline_user_model(offline_users[0], needle)]
                total = 1
                total_pages = 1
                provider_status = "ok"
                provider_detail = ""

        page_items = await self._hydrate_search_page(page_items)
        # Hydrate may reveal a different live game — drop cross-category leaks.
        if effective_game_id:
            page_items = [
                item for item in page_items
                if self._matches_game_filter(item, effective_game_id)
            ]
        page_items = await self._enrich_models(page_items)
        page_items.sort(
            key=lambda m: self._search_rank_key(m, needle_l),
            reverse=True,
        )
        if not page_items and provider_status == "ok":
            provider_status = "empty"
        return {
            "models": page_items,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "provider_status": provider_status,
            "provider_detail": provider_detail,
        }

    async def check_status(self, username: str) -> ProviderStatus:
        login = (username or "").strip().lower()
        if not login:
            return ProviderStatus(False, room_status="offline", source_type=self.source_type)
        if not self.client_id or not self.client_secret:
            # Fall back to yt-dlp probe when Helix credentials are missing.
            return await super().check_status(username)
        try:
            payload = await self._helix_page(first=1, user_login=login, game_id=None)
            streams = [
                item for item in (payload.get("data") or []) if isinstance(item, dict)
            ]
            if not streams:
                return ProviderStatus(
                    False,
                    room_status="offline",
                    source_type=self.source_type,
                )
            model = self._stream_model(streams[0])
            return ProviderStatus(
                is_online=bool(model.get("is_online")),
                viewers=int(model.get("viewers") or 0),
                room_status=str(model.get("room_status") or "public"),
                thumbnail=str(model.get("thumbnail") or "") or None,
                source_type=self.source_type,
                tags=list(model.get("tags") or []),
                started_at=str(model.get("started_at") or "") or None,
            )
        except Exception as exc:
            return ProviderStatus(
                False,
                room_status="error",
                source_type=self.source_type,
                detail=str(exc),
            )

    async def list_live_models(
        self,
        page=1,
        limit=24,
        search="",
        **kwargs,
    ):
        username = (search or "").strip()
        raw_game_id = kwargs.get("game_id")
        override_game_id = None
        if raw_game_id is not None and str(raw_game_id).strip():
            candidate = str(raw_game_id).strip()
            if candidate.isdigit():
                override_game_id = candidate
        if not self.client_id or not self.client_secret:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "auth_required",
                "provider_detail": "Twitch API credentials are not configured.",
            }

        try:
            if username:
                # Default partition (env TWITCH_GAME_ID / ASMR) applies when
                # Discover omits game_id — same scoping as catalogue browse.
                search_game_id = str(
                    override_game_id or self.game_id or ""
                ).strip() or None
                return await self._list_live_search(
                    page=page,
                    limit=limit,
                    username=username,
                    game_id=search_game_id,
                )
            return await self._list_live_catalogue(
                page=page,
                limit=limit,
                game_id=override_game_id,
            )
        except Exception as exc:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "error",
                "provider_detail": str(exc),
            }

    async def login(self, username: str, password: str) -> dict[str, object]:
        _ = username, password
        return {"success": False, "error": self._SESSION_HINT}

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
        incoming = self._merge_session_cookies(cookie_header, cookies)
        cookie_map = ProviderSessionStore.cookie_map(incoming)
        if not cookie_map.get("auth-token"):
            return {
                "success": False,
                "error": "Twitch auth-token cookie is required for session import",
            }
        if not self.session_store:
            return {"success": False, "error": "Session store unavailable"}
        await self.session_store.save(
            source_type=self.source_type,
            username=(username or cookie_map.get("login") or "").strip() or None,
            is_logged_in=True,
            cookies=incoming,
            local_storage=list(local_storage or []),
            last_error=None,
        )
        try:
            current = await self._twitch_current_user()
        except ProviderAuthError as exc:
            await self.session_store.save(
                source_type=self.source_type,
                username=(username or cookie_map.get("login") or "").strip() or None,
                is_logged_in=False,
                cookies=incoming,
                local_storage=list(local_storage or []),
                last_error=str(exc),
            )
            return {"success": False, "error": str(exc)}
        resolved = str(
            current.get("login") or username or cookie_map.get("login") or ""
        ).strip()
        await self.session_store.save(
            source_type=self.source_type,
            username=resolved or None,
            is_logged_in=True,
            cookies=incoming,
            local_storage=list(local_storage or []),
            last_error=None,
        )
        return {"success": True, "username": resolved, "importedSession": True}

    async def sync_following(self) -> list[dict[str, object]]:
        current = await self._twitch_current_user()
        user_id = str(current.get("id") or "").strip()
        items = await self._twitch_sync_helix_followed(user_id)
        if items is None:
            items = await self._twitch_sync_gql_followed()
        items = await self._hydrate_followed_live_status(items)
        items.sort(
            key=lambda item: (
                not bool(item.get("is_online")),
                -int(item.get("viewers") or 0),
                str(item.get("username") or "").lower(),
            )
        )
        return items[:HXYLIVE_MAX_FOLLOW_SYNC_ITEMS]

    async def follow(self, username: str) -> dict[str, object]:
        return await self._twitch_follow_gql(username, follow=True)

    async def unfollow(self, username: str) -> dict[str, object]:
        return await self._twitch_follow_gql(username, follow=False)

    async def is_following(self, username: str) -> bool:
        current = await self._twitch_current_user()
        user_id = str(current.get("id") or "").strip()
        target = await self._twitch_user_id(username)
        if not user_id or not target:
            return False
        try:
            payload = await self._user_helix_get(
                self.FOLLOWED_CHANNELS_URL,
                params={"user_id": user_id, "broadcaster_id": target, "first": "1"},
            )
        except ProviderAuthError:
            raise
        except Exception:
            return False
        rows = payload.get("data") if isinstance(payload, dict) else []
        return bool(rows)

    def _merge_session_cookies(
        self,
        cookie_header: Optional[str],
        cookies: Optional[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        incoming = ProviderSessionStore.parse_cookie_header(
            cookie_header,
            domain=".twitch.tv",
        )
        by_name = {str(item.get("name")): item for item in incoming if item.get("name")}
        for cookie in cookies or []:
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            item = dict(cookie)
            item.setdefault("domain", ".twitch.tv")
            item.setdefault("path", "/")
            by_name[str(item["name"])] = item
        return list(by_name.values())

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

    async def _user_oauth_token(self) -> str:
        token = (await self._session_cookie_map()).get("auth-token") or ""
        if not token:
            raise ProviderAuthError("Twitch login required")
        return token

    async def _user_helix_get(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        token = await self._user_oauth_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.WEB_CLIENT_ID,
            "Cookie": await self._session_cookie_header(),
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                url,
                params=params,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
        if status in {401, 403}:
            raise ProviderAuthError("Twitch login required")
        if status >= 400:
            detail = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise ProviderError(f"Twitch Helix failed ({status}): {detail}")
        return payload if isinstance(payload, dict) else {}

    async def _twitch_gql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        token = await self._user_oauth_token()
        headers = {
            "Authorization": f"OAuth {token}",
            "Client-Id": self.WEB_CLIENT_ID,
            "Content-Type": "application/json",
            "Cookie": await self._session_cookie_header(),
        }
        timeout = aiohttp.ClientTimeout(total=20)
        body = {"query": query, "variables": variables or {}}
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.post(
                self.GQL_URL,
                json=body,
                headers=headers,
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
        if status in {401, 403}:
            raise ProviderAuthError("Twitch login required")
        if status >= 400:
            raise ProviderError(f"Twitch GQL failed ({status})")
        if not isinstance(payload, dict):
            raise ProviderError("Twitch GQL returned invalid payload")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            message = str((errors[0] or {}).get("message") or "Twitch GQL error")
            lowered = message.lower()
            if "auth" in lowered or "login" in lowered or "unauthorized" in lowered:
                raise ProviderAuthError(message)
            raise ProviderError(message)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def _twitch_current_user(self) -> dict[str, Any]:
        try:
            payload = await self._user_helix_get(self.USERS_URL)
            rows = payload.get("data") if isinstance(payload.get("data"), list) else []
            if rows and isinstance(rows[0], dict) and rows[0].get("id"):
                return rows[0]
        except ProviderAuthError:
            raise
        except Exception:
            pass
        data = await self._twitch_gql("query { currentUser { id login displayName } }")
        current = data.get("currentUser")
        if not isinstance(current, dict) or not current.get("id"):
            raise ProviderAuthError("Twitch login required")
        return current

    async def _twitch_user_id(self, username: str) -> str:
        login = str(username or "").strip().lstrip("@")
        if not login:
            return ""
        users = await self._helix_users([login])
        if users:
            return str(users[0].get("id") or "").strip()
        return ""

    async def _twitch_sync_helix_followed(self, user_id: str) -> Optional[list[dict[str, object]]]:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        items: list[dict[str, object]] = []
        cursor = None
        for _ in range(50):
            params = {"user_id": uid, "first": "100"}
            if cursor:
                params["after"] = cursor
            try:
                payload = await self._user_helix_get(self.FOLLOWED_CHANNELS_URL, params=params)
            except ProviderAuthError:
                raise
            except Exception:
                return None if not items else items
            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                login = str(row.get("broadcaster_login") or "").strip()
                if not login:
                    continue
                items.append({
                    "username": login,
                    "display_name": str(row.get("broadcaster_name") or login),
                    "user_id": str(row.get("broadcaster_id") or ""),
                    "is_online": False,
                    "viewers": 0,
                    "source_type": "twitch",
                    "room_status": "offline",
                    "channel_url": f"https://www.twitch.tv/{login}",
                })
            if len(items) >= HXYLIVE_MAX_FOLLOW_SYNC_ITEMS:
                return items[:HXYLIVE_MAX_FOLLOW_SYNC_ITEMS]
            cursor = str((payload.get("pagination") or {}).get("cursor") or "").strip() or None
            if not cursor:
                break
        return items

    async def _twitch_sync_gql_followed(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        cursor = None
        query = """
        query HXYLIVEFollowing($cursor: Cursor) {
          currentUser {
            follows(first: 100, after: $cursor) {
              edges {
                node {
                  id
                  login
                  displayName
                  profileImageURL(width: 300)
                  stream {
                    viewersCount
                    previewImageURL(width: 440, height: 248)
                  }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        for _ in range(50):
            data = await self._twitch_gql(query, {"cursor": cursor})
            current = data.get("currentUser") if isinstance(data.get("currentUser"), dict) else {}
            follows = current.get("follows") if isinstance(current.get("follows"), dict) else {}
            edges = follows.get("edges") or []
            for edge in edges:
                node = (edge or {}).get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                login = str(node.get("login") or "").strip()
                if not login:
                    continue
                stream = node.get("stream") if isinstance(node.get("stream"), dict) else {}
                is_online = bool(stream)
                face = str(node.get("profileImageURL") or "").strip()
                preview = str(stream.get("previewImageURL") or "").strip()
                items.append({
                    "username": login,
                    "display_name": str(node.get("displayName") or login),
                    "user_id": str(node.get("id") or ""),
                    "is_online": is_online,
                    "viewers": int(stream.get("viewersCount") or 0) if is_online else 0,
                    "thumbnail_url": preview or face,
                    "profile_image_url": face,
                    "source_type": "twitch",
                    "room_status": "public" if is_online else "offline",
                    "channel_url": f"https://www.twitch.tv/{login}",
                })
            if len(items) >= HXYLIVE_MAX_FOLLOW_SYNC_ITEMS:
                return items[:HXYLIVE_MAX_FOLLOW_SYNC_ITEMS]
            page_info = follows.get("pageInfo") if isinstance(follows.get("pageInfo"), dict) else {}
            if not page_info.get("hasNextPage"):
                break
            cursor = str(page_info.get("endCursor") or "").strip() or None
            if not cursor:
                break
        return items

    async def _hydrate_followed_live_status(
        self,
        items: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not items:
            return items
        user_ids = [
            str(item.get("user_id") or "").strip()
            for item in items
            if str(item.get("user_id") or "").strip()
        ]
        streams_by_id: dict[str, dict[str, Any]] = {}
        try:
            for offset in range(0, len(user_ids), 100):
                streams_by_id.update(
                    await self._helix_streams_by_user_ids(user_ids[offset:offset + 100])
                )
        except Exception:
            streams_by_id = {}
        missing_logins = [
            str(item.get("username") or "")
            for item in items
            if not str(item.get("profile_image_url") or "").strip()
        ]
        try:
            users = await self._helix_users(missing_logins)
        except Exception:
            users = []
        users_by_login = {
            str(user.get("login") or "").lower(): user for user in users
        }
        hydrated: list[dict[str, object]] = []
        for item in items:
            row = dict(item)
            uid = str(row.get("user_id") or "").strip()
            stream = streams_by_id.get(uid)
            if stream:
                live = self._stream_model(stream)
                live["profile_image_url"] = row.get("profile_image_url") or live.get("profile_image_url")
                hydrated.append(live)
                continue
            login = str(row.get("username") or "").lower()
            user = users_by_login.get(login) or {}
            if user.get("profile_image_url") and not row.get("profile_image_url"):
                row["profile_image_url"] = user.get("profile_image_url")
            hydrated.append(row)
        return hydrated

    async def _twitch_follow_gql(self, username: str, *, follow: bool) -> dict[str, object]:
        target_id = await self._twitch_user_id(username)
        if not target_id:
            raise ProviderError(f"Twitch user not found: {username}")
        mutation = (
            """
            mutation HXYLIVEFollow($id: ID!) {
              followUser(input: {disableNotifications: false, targetID: $id}) {
                follow { user { id login } }
              }
            }
            """
            if follow
            else """
            mutation HXYLIVEUnfollow($id: ID!) {
              unfollowUser(input: {targetID: $id}) {
                follow { user { id } }
              }
            }
            """
        )
        await self._twitch_gql(mutation, {"id": target_id})
        return {
            "success": True,
            "remote": True,
            "provider": "twitch",
            "username": str(username or "").strip(),
        }

from __future__ import annotations

import json
import time
from typing import Any, Optional

from .base import (
    BaseProvider,
    ProviderAuthError,
    ProviderCapabilities,
    ProviderOfflineError,
    ProviderStatus,
    ResolvedStream,
)
from .browser import DEFAULT_USER_AGENT


def _cookies_to_dict(
    cookies: Optional[list[dict[str, Any]]] = None,
    cookie_header: Optional[str] = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if cookie_header:
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                values[name] = value.strip()
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            values[str(name)] = str(value)
    return values


def _cookie_list(cookie_map: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in cookie_map.items()]


class ChaturbateProvider(BaseProvider):
    source_type = "chaturbate"
    display_name = "Chaturbate"
    domains = ("chaturbate.com", "highwebmedia.com", "mmcdn.com")
    capabilities = ProviderCapabilities(
        can_login=True,
        can_follow=True,
        can_sync_following=True,
        can_discover=True,
    )

    def __init__(self, api=None, auth=None, session_store=None):
        super().__init__(session_store=session_store)
        self.api = api
        self.auth = auth

    def canonical_url(self, target: str) -> str:
        target = (target or "").strip().strip("/")
        if target.startswith("http://") or target.startswith("https://"):
            return target
        return f"https://chaturbate.com/{target}/"

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None, **kwargs
    ) -> ResolvedStream:
        from ..resolvers.chaturbate import (
            resolve_llhls_master_playlist,
            resolve_m3u8_async,
        )

        status = ProviderStatus(False, source_type=self.source_type)
        if self.api:
            try:
                status = await self.check_status(target)
            except Exception:
                status = ProviderStatus(False, source_type=self.source_type)
        headers = self._headers(target)
        url = await resolve_m3u8_async(target, max_height=max_height)
        if not url:
            raise ProviderOfflineError(f"No Chaturbate stream for {target}")
        hls_master = await resolve_llhls_master_playlist(
            url,
            max_height=max_height,
            headers=headers,
        )
        ffmpeg_video_stream_index = (
            hls_master.get("video_stream_index") if hls_master else None
        )
        return ResolvedStream(
            url=url,
            headers=headers,
            source_type=self.source_type,
            ffmpeg_video_stream_index=(
                int(ffmpeg_video_stream_index)
                if ffmpeg_video_stream_index is not None else None
            ),
            hls_playlist_text=hls_master.get("text") if hls_master else None,
            hls_playlist_base_url=hls_master.get("base_url") if hls_master else None,
            hls_playlist_content_type=hls_master.get("content_type") if hls_master else None,
            is_live=True,
            room_status="public",
            viewers=int(status.viewers or 0),
            tags=list(status.tags or []),
            thumbnail=status.thumbnail,
        )

    async def check_status(self, username: str) -> ProviderStatus:
        if self.api:
            data = await self.api.check_status(username)
            meta = {}
            if bool(data.get("is_online")) and (not data.get("tags") or not int(data.get("viewers") or 0)):
                meta = await self._discover_metadata(username)
            return ProviderStatus(
                is_online=bool(data.get("is_online")),
                viewers=int(data.get("viewers") or meta.get("viewers") or 0),
                room_status=data.get("room_status") or meta.get("room_status"),
                hls_source=data.get("hls_source"),
                thumbnail=meta.get("thumbnail"),
                source_type=self.source_type,
                tags=list(data.get("tags") or meta.get("tags") or []),
            )
        return await super().check_status(username)

    async def resolve_watch_meta(self, username: str) -> dict[str, Any]:
        """Exact-room identity for Media cards (avatar, live, followers).

        Roomlist keyword search only returns live `riw` webcam frames and can
        miss offline rooms under concurrency. chatvideocontext lookup exposes
        ``summary_card_image`` which is the durable profile photo.
        """
        if not self.api or not hasattr(self.api, "lookup_username"):
            return {}
        item = await self.api.lookup_username(username)
        if not isinstance(item, dict) or not item.get("username"):
            return {}
        followers = item.get("followers")
        if followers is None:
            followers = item.get("num_followers")
        is_online = bool(item.get("is_online"))
        viewers = int(item.get("viewers") or 0) if is_online else 0
        # chatvideocontext sometimes omits/zeros audience while roomlist still
        # has num_users — same backfill path as check_status.
        if is_online and viewers <= 0:
            meta = await self._discover_metadata(username)
            viewers = int(meta.get("viewers") or 0)
            if not item.get("room_status") and meta.get("room_status"):
                item = dict(item)
                item["room_status"] = meta.get("room_status")
        return {
            "isOnline": is_online,
            "viewers": viewers,
            "followers": int(followers) if followers is not None else None,
            "channelUrl": item.get("channel_url") or item.get("channelUrl") or self.canonical_url(username),
            "profileImageUrl": item.get("profile_image_url") or item.get("profileImageUrl") or "",
            "displayName": item.get("display_name") or item.get("displayName") or item.get("username"),
            "username": item.get("username"),
            "thumbnail": item.get("thumbnail") or "",
            "roomStatus": item.get("room_status") or item.get("roomStatus") or None,
            "title": item.get("title") or item.get("subject") or "",
        }

    async def _discover_metadata(self, username: str) -> dict[str, Any]:
        if not self.api:
            return {}
        try:
            data = await self.api.get_live_models(page=1, limit=12, search=username, tag="")
        except Exception:
            return {}
        needle = (username or "").strip().lower()
        for item in data.get("models") or []:
            if str(item.get("username") or "").strip().lower() == needle:
                return item
        return {}

    async def list_live_models(self, **kwargs) -> dict[str, Any]:
        if not self.api:
            return await super().list_live_models(**kwargs)
        tags = kwargs.get("tags") or []
        first_tag = tags[0] if isinstance(tags, list) and tags else kwargs.get("tag", "")
        page = max(1, int(kwargs.get("page", 1) or 1))
        limit = max(1, int(kwargs.get("limit", 24) or 24))
        search = str(kwargs.get("search") or "").strip()
        data = await self.api.get_live_models(
            page=page,
            limit=limit,
            gender=kwargs.get("gender") or "",
            search=search,
            tag=first_tag or "",
        )
        raw_models = [dict(item) for item in (data.get("models") or []) if isinstance(item, dict)]
        for item in raw_models:
            item["source_type"] = self.source_type
        raw_count = len(raw_models)

        # Roomlist keywords only cover live rooms — and Chaturbate sometimes
        # ignores `keywords` entirely, returning the global inventory. Keep only
        # username/display matches before exact-offline fallback.
        needle = search.lower()

        def _matches_search(item: dict) -> bool:
            if not needle:
                return True
            uname = str(item.get("username") or "").strip().lower()
            dname = str(item.get("display_name") or "").strip().lower()
            return bool(uname) and (uname == needle or needle in uname or needle in dname)

        models = [item for item in raw_models if _matches_search(item)] if needle else list(raw_models)

        if needle and page == 1 and hasattr(self.api, "lookup_username"):
            # Chaturbate usernames often end with "_"; searching "mazzanti" should
            # still resolve password/offline room "mazzanti_".
            lookup_candidates = [needle]
            if not needle.endswith("_"):
                lookup_candidates.append(f"{needle}_")
            elif len(needle) > 1:
                lookup_candidates.append(needle.rstrip("_"))

            present = {
                str(item.get("username") or "").strip().lower()
                for item in models
                if str(item.get("username") or "").strip()
            }
            for candidate in lookup_candidates:
                if candidate in present:
                    break
                try:
                    exact = await self.api.lookup_username(candidate)
                except Exception:
                    exact = None
                if not exact:
                    continue
                exact = dict(exact)
                exact["source_type"] = self.source_type
                exact_name = str(exact.get("username") or candidate).strip().lower()
                # Keep roomlist audience when chatvideocontext reports 0 viewers.
                if int(exact.get("viewers") or 0) <= 0:
                    for item in models:
                        if str(item.get("username") or "").strip().lower() != exact_name:
                            continue
                        roomlist_viewers = int(item.get("viewers") or 0)
                        if roomlist_viewers > 0:
                            exact["viewers"] = roomlist_viewers
                        break
                models = [exact] + [
                    item
                    for item in models
                    if str(item.get("username") or "").strip().lower() != exact_name
                ]
                break

        data = dict(data or {})
        data["models"] = models[:limit]
        data["page"] = page
        data["limit"] = limit
        if needle:
            # Search pagination is only trustworthy when roomlist actually honored
            # keywords (every returned row matched). Otherwise clamp to this page.
            keywords_honored = raw_count > 0 and len([m for m in raw_models if _matches_search(m)]) == raw_count
            if keywords_honored and models:
                data["total"] = max(int(data.get("total") or 0), len(data["models"]))
            else:
                data["total"] = len(data["models"])
                data["total_pages"] = 1
        elif models:
            data["total"] = max(int(data.get("total") or 0), len(data["models"]))
        return data

    async def login(self, username: str, password: str) -> dict[str, Any]:
        if not self.auth:
            raise ProviderAuthError("Chaturbate auth service not initialized")
        return await self.auth.login(username, password)

    async def logout(self) -> dict[str, Any]:
        if self.auth:
            await self.auth.logout()
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
        if not self.auth:
            raise ProviderAuthError("Chaturbate auth service not initialized")
        cookie_map = _cookies_to_dict(cookies, cookie_header)
        if not cookie_map:
            return {"success": False, "error": "Chaturbate cookies are required; include sessionid and csrftoken from the same browser session"}
        if not cookie_map.get("sessionid"):
            return {"success": False, "error": "Chaturbate sessionid cookie is required for session import"}
        if user_agent:
            self.auth._user_agent = user_agent
        self.auth._cookies = cookie_map
        self.auth._username = (username or self.auth._username or "").strip() or None
        self.auth._last_error = None
        verified = await self.auth._validate_session()
        self.auth._is_logged_in = bool(verified)
        now = int(time.time())
        row = await self.auth.db.get_auth_state()
        saved_username = self.auth._username or (row or {}).get("username") or ""
        password_hash = (row or {}).get("password_hash") or ""
        validation_error = getattr(self.auth, "_last_validation_error", None) or getattr(self.auth, "_last_error", None)
        last_error = None if verified else (
            validation_error
            or "Imported Chaturbate session is not verified; include the same browser cookies and User-Agent"
        )
        if not verified:
            self.auth._last_error = last_error
        await self.auth.db.save_auth_state(
            username=saved_username,
            password_hash=password_hash,
            is_logged_in=bool(verified),
            session_cookies=json.dumps(cookie_map),
            cf_clearance=cookie_map.get("cf_clearance"),
            csrf_token=cookie_map.get("csrftoken"),
            last_login_at=now if verified else None,
            last_error=last_error,
        )
        if self.session_store:
            await self.session_store.save(
                self.source_type,
                username=saved_username or None,
                is_logged_in=bool(verified),
                cookies=cookies or _cookie_list(cookie_map),
                local_storage=local_storage or [],
                last_error=last_error,
            )
        if not verified:
            return {"success": False, "error": last_error}
        return {"success": True, "username": saved_username, "hasCookies": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API not initialized")
        self._require_verified_auth()
        return await self.api.get_followed_models()

    async def follow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API not initialized")
        self._require_verified_auth()
        ok = await self.api.follow_model(username)
        return {"success": bool(ok)}

    async def unfollow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API not initialized")
        self._require_verified_auth()
        ok = await self.api.unfollow_model(username)
        return {"success": bool(ok)}

    async def is_following(self, username: str) -> bool:
        if not self.api:
            return False
        if not self._has_verified_auth():
            return False
        return bool(await self.api.is_following(username))

    def _has_verified_auth(self) -> bool:
        if not self.auth:
            return False
        status = self.auth.get_status()
        cookies = self.auth.get_cookies()
        return bool(status.get("isLoggedIn") and cookies.get("sessionid"))

    def _require_verified_auth(self) -> None:
        if not self._has_verified_auth():
            raise ProviderAuthError("Chaturbate login required")

    def _headers(self, target: str) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": self.canonical_url(target),
            "Origin": "https://chaturbate.com",
            "Connection": "keep-alive",
        }
        if self.auth:
            try:
                cookies = self.auth.get_cookies()
            except Exception:
                cookies = {}
            if cookies:
                headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        return headers

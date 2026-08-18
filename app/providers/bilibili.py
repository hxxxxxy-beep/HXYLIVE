"""Bilibili live discover provider (Twitch-style catalogue + yt-dlp stream).

Public room list by parent area; stream resolve via yt-dlp on live.bilibili.com/{roomid}.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from html import unescape
from typing import Any, Optional

import aiohttp

from .base import ProviderCapabilities, ProviderStatus
from .ytdlp import YtDlpProvider
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from ..discover_category_catalog import DEFAULT_BILIBILI_PARENT_AREA_ID
from ..services.bilibili_categories import (
    list_bilibili_parent_areas,
    normalize_bilibili_area_id,
    normalize_bilibili_parent_area_id,
    resolve_bilibili_area_by_name,
)

_ROOM_LIST_URL = "https://api.live.bilibili.com/room/v1/area/getRoomList"
_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
_ROOM_INFO_OLD_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
_ROOM_GET_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
_INFO_BY_ROOM_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
_ONLINE_GOLD_RANK_URL = (
    "https://api.live.bilibili.com/xlive/general-interface/v1/rank/getOnlineGoldRank"
)
_MASTER_INFO_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"
_FOLLOWER_STAT_URL = "https://api.bilibili.com/x/relation/stat"
_UNIQUE_POOL_TTL_SECONDS = 45.0
_MAX_UPSTREAM_GETS_PER_CALL = 8
_UNIQUE_POOL_MAX_KEYS = 32
_FOLLOWER_CACHE_TTL_SECONDS = 600.0
_SEARCH_FALLBACK_PAGES = 3
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_DOC_RE = re.compile(r"<!DOCTYPE\s+html|<html[\s>]", re.I)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}


class BilibiliProvider(YtDlpProvider):
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
            can_discover=True,
            can_stream=True,
            can_record=True,
            uses_ytdlp=True,
        )
        self._unique_pools: dict[tuple, dict[str, Any]] = {}
        self._pool_lock_obj: Optional[asyncio.Lock] = None
        self._follower_cache: dict[str, tuple[float, int]] = {}
        # getInfoByRoom rejects anonymous calls (-352) without a buvid cookie.
        self._buvid3 = "XY" + uuid.uuid4().hex.upper()

    def _request_headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = dict(_DEFAULT_HEADERS)
        if extra:
            headers.update(extra)
        cookie = str(headers.get("Cookie") or "").strip()
        if "buvid3=" not in cookie.lower():
            headers["Cookie"] = f"{cookie}; buvid3={self._buvid3}".strip("; ").strip()
        return headers

    def _pool_lock(self) -> asyncio.Lock:
        if self._pool_lock_obj is None:
            self._pool_lock_obj = asyncio.Lock()
        return self._pool_lock_obj

    @staticmethod
    def _as_nonneg_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _viewers_from_payload(cls, payload: Optional[dict[str, Any]]) -> Optional[int]:
        """Extract room-audience count only — never popularity (`online`) or watched_show.

        Live page tab source (e.g. https://live.bilibili.com/24678311):
        room_rank_info.user_rank_entry.user_contribution_rank_entry.count
        → UI text like room audience (upstream may send compact count strings).
        """
        if not isinstance(payload, dict):
            return None
        rank_info = payload.get("room_rank_info")
        if isinstance(rank_info, dict):
            user_entry = rank_info.get("user_rank_entry")
            if isinstance(user_entry, dict):
                contrib = user_entry.get("user_contribution_rank_entry")
                if isinstance(contrib, dict) and contrib.get("count") is not None:
                    return cls._as_nonneg_int(contrib.get("count"))
        for key in ("audience_count", "room_audience", "onlineNum", "online_num"):
            if payload.get(key) is not None and payload.get(key) != "":
                return cls._as_nonneg_int(payload.get(key))
        return None

    @classmethod
    def _viewers_from_room(cls, room: dict[str, Any]) -> int:
        # Room list `online` is popularity heat — do not treat it as viewers.
        audience = cls._viewers_from_payload(room)
        return audience if audience is not None else 0

    @staticmethod
    def _optional_nonneg_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip Bilibili search highlight tags like <em class=\"keyword\">."""
        text = unescape(str(value or ""))
        return _HTML_TAG_RE.sub("", text).strip()

    @staticmethod
    def _absolute_url(value: Any) -> str:
        url = str(value or "").strip()
        if url.startswith("//"):
            return "https:" + url
        return url

    @staticmethod
    def _model_identity(model: dict[str, Any]) -> str:
        # Prefer the long roomid for dedupe so short_id (e.g. 6) and roomid
        # (e.g. 7734200) never create duplicate cards.
        room_id = str(model.get("room_id") or "").strip()
        if room_id and room_id.isdigit():
            return f"bilibili:{room_id}"
        public_id = str(model.get("username") or "").strip()
        if public_id and public_id.isdigit():
            return f"bilibili:{public_id}"
        uid = str(model.get("user_id") or "").strip()
        if uid:
            return f"bilibili-uid:{uid}"
        return ""

    @staticmethod
    def _stamp_stable_id(model: dict[str, Any]) -> dict[str, Any]:
        identity = BilibiliProvider._model_identity(model)
        if identity:
            model["id"] = identity
        return model

    @staticmethod
    def _parse_short_id(room: dict[str, Any], room_id: str = "") -> str:
        """Public short room number, e.g. 6 for LPL vs long roomid 7734200."""
        long_id = str(room_id or room.get("roomid") or room.get("room_id") or "").strip()

        raw = room.get("short_id")
        if raw is not None and str(raw).strip() not in ("", "0"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                short = str(value)
                if short != long_id:
                    return short

        for key in ("link", "url"):
            link = str(room.get(key) or "").strip()
            if not link:
                continue
            path = link.split("?", 1)[0].rstrip("/")
            match = re.search(r"/(\d+)$", path)
            if not match:
                continue
            short = match.group(1)
            # getRoomInfoOld often returns url with the long roomid; ignore that.
            if short and short not in ("0", long_id):
                return short
        return ""

    def _pool_key(
        self,
        *,
        parent_area_id: Optional[str],
        area_id: Optional[str],
        search: str,
        limit: int,
    ) -> tuple:
        return (
            str(parent_area_id or "0"),
            str(area_id or "0"),
            str(search or "").strip().lower(),
            int(limit),
        )

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
            "next_page": 1,
            "exhausted": False,
            "updated_at": now,
            # All-catalogue (parent=0) walks real parent areas; parent=0's own
            # getRoomList pages are identical and cannot infinite-scroll.
            "parent_ids": None,
            "parent_index": 0,
        }
        self._unique_pools[pool_key] = state
        return state

    def _room_model(self, room: dict[str, Any]) -> dict[str, Any]:
        room_id = str(room.get("roomid") or room.get("room_id") or "").strip()
        short_id = self._parse_short_id(room, room_id)
        # Watch/follow identity must be the long roomid. Short ids like "6" collide
        # with unrelated search hits and look wrong as the streamer id.
        public_id = room_id
        uname = self._clean_text(room.get("uname") or room.get("username") or "")
        title = self._clean_text(room.get("title") or "")
        viewers = self._viewers_from_room(room)
        face = self._absolute_url(
            room.get("face")
            or room.get("uface")
            or room.get("upic")
            or room.get("avatar")
            or ""
        )
        cover = self._absolute_url(
            room.get("user_cover")
            or room.get("cover")
            or room.get("system_cover")
            or ""
        )
        area_name = str(
            room.get("area_v2_name")
            or room.get("area_name")
            or ""
        ).strip()
        parent_name = str(
            room.get("area_v2_parent_name")
            or room.get("parent_name")
            or ""
        ).strip()
        parent_area_id = str(
            room.get("area_v2_parent_id")
            or room.get("parent_id")
            or ""
        ).strip()
        area_id = str(room.get("area_v2_id") or room.get("area_id") or "").strip()
        uid = str(room.get("uid") or room.get("user_id") or room.get("mid") or "").strip()
        followers = None
        for key in ("attention", "attentions", "followers", "follower"):
            if room.get(key) is not None and room.get(key) != "":
                followers = self._optional_nonneg_int(room.get(key))
                break
        live_flag = True
        if "is_live" in room:
            live_flag = room.get("is_live") in (1, "1", True)
        elif "live_status" in room:
            live_flag = room.get("live_status") in (1, "1", True)
        tags = []
        for value in (parent_name, area_name):
            if value and value.lower() not in {tag.lower() for tag in tags}:
                tags.append(value)
        model = {
            "username": public_id,
            "user_id": uid,
            "room_id": room_id,
            "short_id": short_id,
            "display_name": uname or public_id,
            "source_type": "bilibili",
            "is_online": live_flag,
            "room_status": "public" if live_flag else "offline",
            "viewers": viewers if live_flag else 0,
            "followers": followers,
            "thumbnail": cover or face,
            # Avatar must stay the UP face; room cover looks wrong in the circle.
            "profile_image_url": face,
            "tags": tags,
            "title": title,
            "category": area_name or parent_name,
            "parent_area_id": parent_area_id,
            "area_id": area_id,
            "area_name": area_name,
            "parent_area_name": parent_name,
            "channel_url": f"https://live.bilibili.com/{public_id}" if public_id else "",
        }
        return self._stamp_stable_id(model)

    async def _follower_total(self, user_id: str) -> Optional[int]:
        uid = str(user_id or "").strip()
        if not uid or not uid.isdigit():
            return None
        cached = self._follower_cache.get(uid)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        headers = dict(_DEFAULT_HEADERS)
        headers["Referer"] = "https://www.bilibili.com/"
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(_FOLLOWER_STAT_URL, params={"vmid": uid}) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        return None
        except Exception:
            return None
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        total = self._optional_nonneg_int(data.get("follower"))
        if total is None:
            return None
        self._follower_cache[uid] = (time.monotonic() + _FOLLOWER_CACHE_TTL_SECONDS, total)
        return total

    async def _enrich_models(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not models:
            return models

        # Always refresh Discover/watch viewers from room-audience for every live card.
        live_models = [
            model
            for model in models
            if model.get("is_online")
            and str(model.get("room_id") or model.get("username") or "").strip()
        ]
        if live_models:
            infos = await asyncio.gather(
                *(
                    self._info_by_room(str(model.get("room_id") or model.get("username") or ""))
                    for model in live_models
                ),
                return_exceptions=True,
            )
            need_gold: list[tuple[dict[str, Any], str, str]] = []
            for model, info in zip(live_models, infos):
                room_id = str(model.get("room_id") or model.get("username") or "").strip()
                uid = str(model.get("user_id") or "").strip()
                audience = None
                if isinstance(info, dict):
                    audience = self._viewers_from_payload(info)
                    anchor = (
                        info.get("anchor_info")
                        if isinstance(info.get("anchor_info"), dict)
                        else {}
                    )
                    relation = (
                        anchor.get("relation_info")
                        if isinstance(anchor.get("relation_info"), dict)
                        else None
                    )
                    if isinstance(relation, dict) and model.get("followers") is None:
                        model["followers"] = self._optional_nonneg_int(relation.get("attention"))
                    base = (
                        anchor.get("base_info")
                        if isinstance(anchor.get("base_info"), dict)
                        else {}
                    )
                    uname = self._clean_text(base.get("uname") or "")
                    face = self._absolute_url(base.get("face") or "")
                    # Catalogue rows can omit uname/face; backfill so cards aren't
                    # numeric room ids with letter-digit avatars.
                    if uname and (
                        not model.get("display_name")
                        or str(model.get("display_name") or "").strip() == room_id
                    ):
                        model["display_name"] = uname
                    if face and not str(model.get("profile_image_url") or "").strip():
                        model["profile_image_url"] = face
                    if not uid:
                        room_info = info.get("room_info") if isinstance(info.get("room_info"), dict) else {}
                        uid = str(room_info.get("uid") or "").strip()
                        if uid:
                            model["user_id"] = uid
                if audience is not None:
                    model["viewers"] = audience
                else:
                    need_gold.append((model, room_id, uid))
                    model["viewers"] = 0

            if need_gold:
                gold_counts = await asyncio.gather(
                    *(self._online_gold_audience(room_id, uid) for _, room_id, uid in need_gold),
                    return_exceptions=True,
                )
                for (model, _room_id, _uid), count in zip(need_gold, gold_counts):
                    if isinstance(count, int):
                        model["viewers"] = count

        for model in models:
            if not model.get("is_online"):
                model["viewers"] = 0

        need_followers = [
            model
            for model in models
            if model.get("followers") is None and str(model.get("user_id") or "").strip()
        ]
        if need_followers:
            totals = await asyncio.gather(
                *(self._follower_total(model.get("user_id") or "") for model in need_followers),
                return_exceptions=True,
            )
            for model, total in zip(need_followers, totals):
                model["followers"] = total if isinstance(total, int) else None
        return models

    async def _fetch_room_page(
        self,
        *,
        page_no: int,
        page_size: int,
        parent_area_id: str,
        area_id: str = "0",
    ) -> list[dict[str, Any]]:
        params = {
            "parent_area_id": parent_area_id or "0",
            "area_id": normalize_bilibili_area_id(area_id) or "0",
            "sort_type": "online",
            "page_size": str(max(1, min(30, int(page_size)))),
            # getRoomList paginates on `page`. `page_no` is ignored and every
            # request returns the same first page (breaks infinite scroll).
            "page": str(max(1, int(page_no))),
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._request_headers()) as session:
            async with session.get(_ROOM_LIST_URL, params=params) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(f"Bilibili room list failed ({response.status}): {detail}")
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
            raise RuntimeError(f"Bilibili room list rejected: {payload}")
        rows = payload.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    async def _fetch_json(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: Optional[dict[str, str]] = None,
        use_proxy: bool = False,
        label: str = "Bilibili request",
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=15)
        req_headers = self._request_headers(headers)
        request_kwargs = aiohttp_request_kwargs() if use_proxy else {}
        async with aiohttp_client_session(timeout=timeout, headers=req_headers) as session:
            async with session.get(url, params=params, **request_kwargs) as response:
                raw = await response.text()
                status = response.status
        if status >= 400 or _HTML_DOC_RE.search(raw or ""):
            raise RuntimeError(f"{label} blocked ({status})")
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"{label} returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} returned invalid payload")
        if int(payload.get("code") or 0) != 0:
            raise RuntimeError(f"{label} rejected: {payload.get('message') or payload.get('msg') or payload.get('code')}")
        return payload

    async def _room_info_by_mid(self, mid: str) -> Optional[dict[str, Any]]:
        uid = str(mid or "").strip()
        if not uid or not uid.isdigit():
            return None
        try:
            payload = await self._fetch_json(
                _ROOM_INFO_OLD_URL,
                params={"mid": uid},
                label="Bilibili room info",
            )
        except Exception:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def _short_id_by_room_id(self, room_id: str) -> str:
        rid = str(room_id or "").strip()
        if not rid or not rid.isdigit():
            return ""
        try:
            payload = await self._fetch_json(
                _ROOM_GET_INFO_URL,
                params={"room_id": rid},
                label="Bilibili room get_info",
            )
        except Exception:
            return ""
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""
        return self._parse_short_id(data, rid)

    async def _info_by_room(self, room_key: str) -> Optional[dict[str, Any]]:
        room_id = str(room_key or "").strip()
        if not room_id:
            return None
        try:
            payload = await self._fetch_json(
                _INFO_BY_ROOM_URL,
                params={"room_id": room_id},
                label="Bilibili infoByRoom",
            )
        except Exception:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def _room_get_info(self, room_key: str) -> Optional[dict[str, Any]]:
        rid = str(room_key or "").strip()
        if not rid or not rid.isdigit():
            return None
        try:
            payload = await self._fetch_json(
                _ROOM_GET_INFO_URL,
                params={"room_id": rid},
                label="Bilibili room get_info",
            )
        except Exception:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def _online_gold_audience(self, room_id: str, uid: str) -> Optional[int]:
        """Fallback room-audience from online gold rank when room_rank_info.count is absent."""
        rid = str(room_id or "").strip()
        mid = str(uid or "").strip()
        if not rid or not rid.isdigit():
            return None
        params = {
            "roomId": rid,
            "page": "1",
            "pageSize": "1",
            "ruid": mid if mid.isdigit() else "0",
        }
        try:
            payload = await self._fetch_json(
                _ONLINE_GOLD_RANK_URL,
                params=params,
                label="Bilibili online gold rank",
            )
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if data.get("onlineNum") is None and data.get("online_num") is None:
            return None
        return self._as_nonneg_int(
            data.get("onlineNum") if data.get("onlineNum") is not None else data.get("online_num")
        )

    async def _master_info(self, uid: str) -> Optional[dict[str, Any]]:
        mid = str(uid or "").strip()
        if not mid or not mid.isdigit():
            return None
        try:
            payload = await self._fetch_json(
                _MASTER_INFO_URL,
                params={"uid": mid},
                label="Bilibili master info",
            )
        except Exception:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def resolve_watch_meta(self, room_key: str) -> dict[str, Any]:
        """Same identity fields Discover cards use: uname, face, roomid, audience, fans."""
        info_by_room = await self._info_by_room(room_key)
        room_info = (
            info_by_room.get("room_info")
            if isinstance(info_by_room, dict) and isinstance(info_by_room.get("room_info"), dict)
            else None
        )
        info = room_info or await self._room_get_info(room_key)
        if not info:
            return {}

        room_id = str(info.get("room_id") or room_key or "").strip()
        uid = str(info.get("uid") or "").strip()
        live = info.get("live_status") in (1, "1", True)
        viewers = 0
        if live:
            audience = self._viewers_from_payload(info_by_room) if info_by_room else None
            if audience is None:
                audience = await self._online_gold_audience(room_id, uid)
            viewers = audience if audience is not None else 0
        followers = self._optional_nonneg_int(info.get("attention"))
        cover = self._absolute_url(info.get("user_cover") or info.get("keyframe") or "")
        title = self._clean_text(info.get("title") or "")
        short_id = self._parse_short_id(info, room_id)

        face = ""
        uname = ""
        if isinstance(info_by_room, dict):
            anchor = info_by_room.get("anchor_info") if isinstance(info_by_room.get("anchor_info"), dict) else {}
            base = anchor.get("base_info") if isinstance(anchor.get("base_info"), dict) else {}
            face = self._absolute_url(base.get("face") or "")
            uname = self._clean_text(base.get("uname") or "")
            relation = anchor.get("relation_info") if isinstance(anchor.get("relation_info"), dict) else {}
            if relation.get("attention") is not None:
                followers = self._optional_nonneg_int(relation.get("attention"))

        if (not face or not uname or followers is None) and uid:
            master = await self._master_info(uid)
            if isinstance(master, dict):
                block = master.get("info") if isinstance(master.get("info"), dict) else {}
                face = face or self._absolute_url(block.get("face") or "")
                uname = uname or self._clean_text(block.get("uname") or "")
                if followers is None and master.get("follower_num") is not None:
                    followers = self._optional_nonneg_int(master.get("follower_num"))

        return {
            "isOnline": live,
            "viewers": viewers,
            "followers": followers,
            "channelUrl": f"https://live.bilibili.com/{room_id}" if room_id else "",
            "profileImageUrl": face,
            "displayName": uname or room_id,
            "username": room_id,
            "roomId": room_id,
            "shortId": short_id,
            "userId": uid,
            "thumbnail": cover or face,
            "title": title,
        }

    async def check_status(self, username: str) -> ProviderStatus:
        meta = await self.resolve_watch_meta(username)
        if meta:
            return ProviderStatus(
                is_online=bool(meta.get("isOnline")),
                viewers=int(meta.get("viewers") or 0),
                room_status="public" if meta.get("isOnline") else "offline",
                thumbnail=str(meta.get("thumbnail") or meta.get("profileImageUrl") or "") or None,
                source_type=self.source_type,
            )
        return await super().check_status(username)

    async def _bili_user_search_page(self, keyword: str, page: int) -> list[dict[str, Any]]:
        params = {
            "search_type": "bili_user",
            "keyword": keyword,
            "page": str(max(1, int(page))),
        }
        headers = dict(_DEFAULT_HEADERS)
        headers["Referer"] = "https://search.bilibili.com/"
        headers["Origin"] = "https://search.bilibili.com"
        try:
            payload = await self._fetch_json(
                _SEARCH_URL,
                params=params,
                headers=headers,
                use_proxy=True,
                label="Bilibili user search",
            )
        except Exception:
            payload = await self._fetch_json(
                _SEARCH_URL,
                params=params,
                headers=headers,
                use_proxy=False,
                label="Bilibili user search",
            )
        data = payload.get("data") or {}
        rows = data.get("result") if isinstance(data, dict) else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _search_keyword_variants(keyword: str) -> list[str]:
        """Broaden risk-controlled exact queries (e.g. trailing '_' often 412s)."""
        base = str(keyword or "").strip()
        if not base:
            return []
        variants = [base]
        stripped = base.rstrip(" _-·.•")
        if stripped and stripped != base:
            variants.append(stripped)
        return list(dict.fromkeys(variants))

    async def _fetch_search(self, *, keyword: str, page: int) -> list[dict[str, Any]]:
        """Search via bili_user (live search is frequently HTTP 412).

        Includes offline anchors that own a live room so Discover search can
        surface them (same idea as Twitch offline exact hits).
        """
        original = str(keyword or "").strip()
        needle = self._clean_text(original).lower()
        rows: list[dict[str, Any]] = []
        last_error: Optional[Exception] = None
        used_variant = original
        for variant in self._search_keyword_variants(original):
            try:
                rows = await self._bili_user_search_page(variant, page)
            except Exception as exc:
                last_error = exc
                rows = []
                continue
            used_variant = variant
            if rows:
                break
        if not rows:
            if last_error is not None:
                raise last_error
            return []

        # When we broadened the keyword, keep only names/ids matching the original needle.
        if used_variant != original and needle:
            narrowed = []
            for row in rows:
                uname = self._clean_text(row.get("uname") or row.get("name") or "").lower()
                mid = str(row.get("mid") or row.get("uid") or "").strip().lower()
                if needle in uname or needle == mid:
                    narrowed.append(row)
            if narrowed:
                rows = narrowed

        candidates: list[dict[str, Any]] = []
        seen_mids: set[str] = set()
        for row in rows:
            mid = str(row.get("mid") or row.get("uid") or "").strip()
            if not mid or mid in seen_mids:
                continue
            seen_mids.add(mid)
            room_id = str(row.get("room_id") or row.get("roomid") or "").strip()
            if room_id == "0":
                room_id = ""
            is_live = row.get("is_live") in (1, "1", True)
            candidates.append(
                {
                    "roomid": room_id,
                    "uid": mid,
                    "title": "",
                    "uname": row.get("uname") or row.get("name") or "",
                    "online": 0,
                    "attention": row.get("fans") if row.get("fans") is not None else row.get("attentions"),
                    "face": row.get("upic") or row.get("uface") or row.get("face") or "",
                    "user_cover": "",
                    "cover": "",
                    "parent_name": "",
                    "area_name": "",
                    "is_live": is_live,
                    "live_status": 1 if is_live else 0,
                }
            )

        if not candidates:
            return []

        infos = await asyncio.gather(
            *(self._room_info_by_mid(item.get("uid") or "") for item in candidates),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        for item, info in zip(candidates, infos):
            live = bool(item.get("is_live"))
            if isinstance(info, dict):
                live_status = info.get("liveStatus")
                if live_status is not None:
                    live = live_status in (1, "1", True)
                room_id = str(info.get("roomid") or item.get("roomid") or "").strip()
                if not room_id or room_id == "0":
                    # No live room bound to this account.
                    continue
                item["roomid"] = room_id
                item["title"] = info.get("title") or item.get("title") or ""
                item["online"] = (
                    info.get("online") if live and info.get("online") is not None else 0
                )
                cover = info.get("cover") or ""
                if cover:
                    item["user_cover"] = cover
                    item["cover"] = cover
            elif not str(item.get("roomid") or "").strip():
                continue
            item["is_live"] = live
            item["live_status"] = 1 if live else 0
            if not live:
                item["online"] = 0
            out.append(item)

        # get_info carries parent_area_id (partition) + short_id for every room.
        metas = await asyncio.gather(
            *(self._room_get_info(item.get("roomid") or "") for item in out),
            return_exceptions=True,
        )
        for item, meta in zip(out, metas):
            if not isinstance(meta, dict):
                continue
            short_id = self._parse_short_id(meta, str(item.get("roomid") or ""))
            if short_id:
                item["short_id"] = short_id
            parent_id = str(meta.get("parent_area_id") or "").strip()
            if parent_id:
                item["area_v2_parent_id"] = parent_id
                item["parent_id"] = parent_id
            parent_name = str(meta.get("parent_area_name") or "").strip()
            if parent_name:
                item["area_v2_parent_name"] = parent_name
                item["parent_name"] = parent_name
            area_id = str(meta.get("area_id") or "").strip()
            if area_id:
                item["area_v2_id"] = area_id
                item["area_id"] = area_id
            area_name = str(meta.get("area_name") or "").strip()
            if area_name:
                item["area_v2_name"] = area_name
                item["area_name"] = area_name

        # Live hits first, then offline.
        out.sort(key=lambda row: (0 if row.get("is_live") else 1, -int(row.get("online") or 0)))
        return out

    @staticmethod
    def _matches_parent_area_filter(row: dict[str, Any], parent_area_id: Optional[str]) -> bool:
        """Category scope: every Bilibili partition search stays inside parent_area_id."""
        wanted = normalize_bilibili_parent_area_id(parent_area_id)
        if not wanted or wanted == "0":
            return True
        return str(row.get("parent_area_id") or "").strip() == wanted

    def _normalize_search_result(self, result: Any) -> list[dict[str, Any]]:
        """Legacy live/live_user payload normalizer (kept for unit tests/helpers)."""
        room_rows: list[dict[str, Any]] = []
        user_rows: list[dict[str, Any]] = []
        if isinstance(result, dict):
            room_rows = [row for row in (result.get("live_room") or []) if isinstance(row, dict)]
            user_rows = [row for row in (result.get("live_user") or []) if isinstance(row, dict)]
        elif isinstance(result, list):
            user_rows = [row for row in result if isinstance(row, dict)]

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in room_rows + user_rows:
            room_id = str(row.get("roomid") or row.get("room_id") or "").strip()
            if not room_id or room_id in seen:
                continue
            if "is_live" in row and row.get("is_live") not in (1, "1", True):
                continue
            if "live_status" in row and row.get("live_status") not in (1, "1", True):
                continue
            seen.add(room_id)
            cover = (
                row.get("user_cover")
                or row.get("cover")
                or row.get("uface")
                or row.get("face")
                or ""
            )
            out.append(
                {
                    "roomid": room_id,
                    "uid": row.get("uid") or row.get("mid") or "",
                    "title": row.get("title") or row.get("live_title") or "",
                    "uname": row.get("uname") or row.get("name") or "",
                    "online": row.get("online") if row.get("online") is not None else 0,
                    "attention": row.get("attentions")
                    if row.get("attentions") is not None
                    else row.get("attention"),
                    "face": row.get("uface") or row.get("face") or "",
                    "user_cover": cover,
                    "cover": cover,
                    "parent_name": row.get("cate_name") or row.get("area") or "",
                    "area_name": row.get("cate_name") or "",
                    "is_live": True,
                    "live_status": 1,
                }
            )
        return out

    async def _search_catalogue_fallback(
        self,
        *,
        keyword: str,
        limit: int,
        parent_area_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        needle = self._clean_text(keyword).lower()
        if not needle:
            return []
        # Stay inside the active Discover parent partition.
        effective_parent = (
            normalize_bilibili_parent_area_id(parent_area_id)
            or DEFAULT_BILIBILI_PARENT_AREA_ID
        )
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_no in range(1, _SEARCH_FALLBACK_PAGES + 1):
            try:
                rows = await self._fetch_room_page(
                    page_no=page_no,
                    page_size=30,
                    parent_area_id=effective_parent,
                )
            except Exception:
                break
            if not rows:
                break
            for room in rows:
                model = self._room_model(room)
                identity = self._model_identity(model)
                if not identity or identity in seen:
                    continue
                if not self._matches_parent_area_filter(model, effective_parent):
                    continue
                haystack = " ".join(
                    [
                        str(model.get("display_name") or ""),
                        str(model.get("username") or ""),
                        str(model.get("user_id") or ""),
                        str(model.get("room_id") or ""),
                        str(model.get("short_id") or ""),
                    ]
                ).lower()
                if needle not in haystack:
                    continue
                seen.add(identity)
                matches.append(model)
                if len(matches) >= limit:
                    return matches
        return matches

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
        parent_area_id: Optional[str] = None,
        area_id: Optional[str] = None,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        effective_parent = normalize_bilibili_parent_area_id(parent_area_id) or "0"
        effective_area = normalize_bilibili_area_id(area_id) or "0"
        request_size = min(30, max(limit, 24))
        # parent_area_id=0 + area_id=0 returns the same ~30 hot rooms on every
        # page_no. Walk real parent partitions instead (Chaturbate-style depth).
        all_areas_mode = effective_parent == "0" and effective_area == "0"
        pool_key = self._pool_key(
            parent_area_id=effective_parent,
            area_id=effective_area,
            search="",
            limit=limit,
        )
        upstream_gets = 0
        provider_status = "ok"
        provider_detail = ""

        async with self._pool_lock():
            state = self._unique_pool_state(pool_key)
            needed = page * limit
            if all_areas_mode and state.get("parent_ids") is None:
                try:
                    parents = await list_bilibili_parent_areas()
                    state["parent_ids"] = [
                        str(item.get("parent_area_id") or "").strip()
                        for item in parents
                        if str(item.get("parent_area_id") or "").strip()
                        and str(item.get("parent_area_id") or "").strip() != "0"
                    ]
                except Exception as exc:
                    provider_status = "error"
                    provider_detail = str(exc)
                    state["parent_ids"] = []
                state["parent_index"] = 0
                state["next_page"] = 1
                if not state["parent_ids"]:
                    state["exhausted"] = True

            remaining_needed = max(0, needed - len(state["items"]))
            gets_budget = max(
                _MAX_UPSTREAM_GETS_PER_CALL,
                (remaining_needed + request_size - 1) // request_size + 1,
            )
            gets_budget = min(max(1, gets_budget), 12)

            try:
                while (
                    len(state["items"]) < needed
                    and not state["exhausted"]
                    and upstream_gets < gets_budget
                ):
                    if all_areas_mode:
                        parent_ids = list(state.get("parent_ids") or [])
                        parent_index = int(state.get("parent_index") or 0)
                        if parent_index >= len(parent_ids):
                            state["exhausted"] = True
                            break
                        fetch_parent = parent_ids[parent_index]
                        fetch_area = "0"
                    else:
                        fetch_parent = effective_parent
                        fetch_area = effective_area

                    page_no = int(state.get("next_page") or 1)
                    rows = await self._fetch_room_page(
                        page_no=page_no,
                        page_size=request_size,
                        parent_area_id=fetch_parent,
                        area_id=fetch_area,
                    )
                    upstream_gets += 1
                    state["updated_at"] = time.monotonic()

                    new_unique = 0
                    seen_ids: set = state["seen_ids"]
                    for room in rows:
                        model = self._room_model(room)
                        identity = self._model_identity(model)
                        if not identity or identity in seen_ids:
                            continue
                        seen_ids.add(identity)
                        state["items"].append(model)
                        new_unique += 1

                    if all_areas_mode:
                        if not rows or new_unique == 0:
                            # This parent is done; move to the next partition.
                            state["parent_index"] = int(state.get("parent_index") or 0) + 1
                            state["next_page"] = 1
                            parent_ids = list(state.get("parent_ids") or [])
                            if int(state["parent_index"]) >= len(parent_ids):
                                state["exhausted"] = True
                        else:
                            state["next_page"] = page_no + 1
                    else:
                        state["next_page"] = page_no + 1
                        if not rows or new_unique == 0:
                            state["exhausted"] = True
            except Exception as exc:
                provider_status = "error"
                provider_detail = str(exc)
                if not state["items"]:
                    return {
                        "models": [],
                        "total": 0,
                        "page": page,
                        "limit": limit,
                        "total_pages": 1,
                        "provider_status": provider_status,
                        "provider_detail": provider_detail,
                    }

            start = (page - 1) * limit
            end = start + limit
            page_items = [dict(item) for item in state["items"][start:end]]
            exhausted = bool(state["exhausted"]) and end >= len(state["items"])
            total, total_pages = self._pagination_contract(
                page=page,
                limit=limit,
                page_items=page_items,
                pool_len=len(state["items"]),
                exhausted=exhausted or (end >= len(state["items"]) and state["exhausted"]),
            )
            if not page_items and not state["items"]:
                provider_status = "empty"

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

    async def _list_live_search(
        self,
        *,
        page: int,
        limit: int,
        search: str,
        parent_area_id: Optional[str] = None,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        limit = max(1, int(limit or 24))
        keyword = str(search or "").strip()
        # Same rule for every Bilibili parent partition.
        effective_parent = (
            normalize_bilibili_parent_area_id(parent_area_id)
            or DEFAULT_BILIBILI_PARENT_AREA_ID
        )
        provider_detail = ""
        rows: list[dict[str, Any]] = []
        try:
            rows = await self._fetch_search(keyword=keyword, page=page)
        except Exception as exc:
            provider_detail = str(exc)

        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for room in rows:
            model = self._room_model(room)
            identity = self._model_identity(model)
            if not identity or identity in seen:
                continue
            if not self._matches_parent_area_filter(model, effective_parent):
                continue
            seen.add(identity)
            models.append(model)

        # Partition catalogue contains: search API is global and may 412 / miss
        # in-partition rooms; always merge page-1 live rooms from this parent.
        if page == 1:
            try:
                fallback = await self._search_catalogue_fallback(
                    keyword=keyword,
                    limit=max(limit, 24),
                    parent_area_id=effective_parent,
                )
                for model in fallback:
                    identity = self._model_identity(model)
                    if not identity or identity in seen:
                        continue
                    if not self._matches_parent_area_filter(model, effective_parent):
                        continue
                    seen.add(identity)
                    models.append(model)
                if models and provider_detail:
                    provider_detail = ""
            except Exception as exc:
                if not provider_detail and not models:
                    provider_detail = str(exc)

        models.sort(
            key=lambda item: (
                0 if item.get("is_online", True) else 1,
                -int(item.get("viewers") or 0),
            )
        )
        models = models[:limit]
        models = await self._enrich_models(models)
        total = len(models)
        if models:
            status = "ok"
            detail = ""
        elif provider_detail:
            status = "error"
            # Never surface upstream HTML risk-control pages in the UI.
            detail = "Bilibili search is temporarily unavailable"
        else:
            status = "empty"
            detail = ""
        return {
            "models": models,
            "total": max(total, page * limit if models else 0),
            "page": page,
            "limit": limit,
            "total_pages": page + (1 if models else 0),
            "provider_status": status,
            "provider_detail": detail,
        }

    async def list_live_models(self, page: int = 1, limit: int = 24, search: str = "", **kwargs):
        _ = kwargs.get("gender")
        _ = kwargs.get("allow_browser")
        _ = kwargs.get("exact_search_fallback")
        parent_area_id = kwargs.get("parent_area_id")
        area_id = kwargs.get("area_id")
        tags = kwargs.get("tags") or []
        query = str(search or "").strip()
        if query:
            search_parent = (
                normalize_bilibili_parent_area_id(parent_area_id)
                or DEFAULT_BILIBILI_PARENT_AREA_ID
            )
            return await self._list_live_search(
                page=page,
                limit=limit,
                search=query,
                parent_area_id=search_parent,
            )

        # Tag filters are area/partition labels on Bilibili. Resolve the first
        # tag to getRoomList parent_area_id+area_id so we do not post-filter the
        # tiny mixed homepage slice (e.g. League of Legends area 86 under PC games).
        tag_needles = [
            str(tag).strip()
            for tag in (tags if isinstance(tags, list) else [tags])
            if str(tag).strip()
        ]
        if tag_needles and not normalize_bilibili_area_id(area_id):
            resolved = await resolve_bilibili_area_by_name(tag_needles[0])
            if resolved:
                parent_area_id = resolved.get("parent_area_id") or parent_area_id
                area_id = resolved.get("area_id") or "0"

        return await self._list_live_catalogue(
            page=page,
            limit=limit,
            parent_area_id=parent_area_id,
            area_id=area_id,
        )

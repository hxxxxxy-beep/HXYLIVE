"""Bilibili live parent-area categories (Twitch-style native content filters).

Uses public Area/getList. Isolated short TTL cache. No gender invention.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

AREA_LIST_URL = "https://api.live.bilibili.com/room/v1/Area/getList"

_CATEGORY_CACHE_TTL_SECONDS = 300.0
_CATEGORY_MAX_ITEMS = 24

_cache_lock = asyncio.Lock()
_cache_expires_at = 0.0
_cache_items: List[Dict[str, str]] = []
_name_index_expires_at = 0.0
_name_index_items: List[Dict[str, str]] = []

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}


def reset_bilibili_category_cache_for_tests() -> None:
    global _cache_expires_at, _cache_items, _name_index_expires_at, _name_index_items
    _cache_expires_at = 0.0
    _cache_items = []
    _name_index_expires_at = 0.0
    _name_index_items = []


def normalize_bilibili_parent_area_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    return text


def normalize_bilibili_area_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    return text


def build_bilibili_area_name_index(raw_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten Area/getList into name→(parent_area_id, area_id) rows.

    Child areas keep their own area_id; parent rows use area_id \"0\" (all children).
    Child exact names are listed before parents so lookups can prefer them.
    """
    children: List[Dict[str, str]] = []
    parents: List[Dict[str, str]] = []
    seen_child: set[str] = set()
    seen_parent: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        parent_id = normalize_bilibili_parent_area_id(item.get("id") or item.get("parent_area_id"))
        parent_name = str(item.get("name") or "").strip()
        if parent_id and parent_name and parent_id not in seen_parent:
            seen_parent.add(parent_id)
            parents.append(
                {
                    "name": parent_name,
                    "parent_area_id": parent_id,
                    "area_id": "0",
                    "match_kind": "parent",
                }
            )
        for child in item.get("list") or []:
            if not isinstance(child, dict):
                continue
            area_id = normalize_bilibili_area_id(child.get("id") or child.get("area_id"))
            child_name = str(child.get("name") or "").strip()
            child_parent = normalize_bilibili_parent_area_id(
                child.get("parent_id") or child.get("parent_area_id") or parent_id
            )
            if not area_id or not child_name or not child_parent:
                continue
            key = f"{child_parent}:{area_id}"
            if key in seen_child:
                continue
            seen_child.add(key)
            children.append(
                {
                    "name": child_name,
                    "parent_area_id": child_parent,
                    "area_id": area_id,
                    "match_kind": "child",
                }
            )
    return children + parents


def dedupe_bilibili_parent_areas(raw_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        area_id = normalize_bilibili_parent_area_id(item.get("id") or item.get("parent_area_id"))
        name = str(item.get("name") or "").strip()
        if not area_id or not name or area_id in seen:
            continue
        seen.add(area_id)
        out.append({"parent_area_id": area_id, "name": name})
        if len(out) >= _CATEGORY_MAX_ITEMS:
            break
    return out


async def _fetch_area_list(*, fetcher=None) -> List[Dict[str, Any]]:
    if fetcher is not None:
        return await fetcher()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=_DEFAULT_HEADERS) as session:
        async with session.get(AREA_LIST_URL) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                raise RuntimeError(f"Bilibili area list failed ({response.status}): {detail}")
    if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
        raise RuntimeError(f"Bilibili area list rejected: {payload}")
    rows = payload.get("data") or []
    return [row for row in rows if isinstance(row, dict)]


async def list_bilibili_parent_areas(
    *,
    force_refresh: bool = False,
    area_fetcher=None,
) -> List[Dict[str, str]]:
    """Return parent live areas for the categories API."""
    global _cache_expires_at, _cache_items
    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _cache_items
            and now < float(_cache_expires_at)
        ):
            return [dict(item) for item in _cache_items]
        try:
            raw = await _fetch_area_list(fetcher=area_fetcher)
            items = dedupe_bilibili_parent_areas(raw)
            _cache_items = items
            _cache_expires_at = now + _CATEGORY_CACHE_TTL_SECONDS
            return [dict(item) for item in items]
        except Exception:
            if _cache_items and now < float(_cache_expires_at) + _CATEGORY_CACHE_TTL_SECONDS:
                return [dict(item) for item in _cache_items]
            _cache_items = []
            _cache_expires_at = now + min(30.0, _CATEGORY_CACHE_TTL_SECONDS)
            return []


async def list_bilibili_area_name_index(
    *,
    force_refresh: bool = False,
    area_fetcher=None,
) -> List[Dict[str, str]]:
    """Cached parent+child area rows for tag→area resolution."""
    global _name_index_expires_at, _name_index_items, _cache_expires_at, _cache_items
    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _name_index_items
            and now < float(_name_index_expires_at)
        ):
            return [dict(item) for item in _name_index_items]
        try:
            raw = await _fetch_area_list(fetcher=area_fetcher)
            items = build_bilibili_area_name_index(raw)
            _name_index_items = items
            _name_index_expires_at = now + _CATEGORY_CACHE_TTL_SECONDS
            # Keep parent cache warm from the same payload when possible.
            parents = dedupe_bilibili_parent_areas(raw)
            if parents:
                _cache_items = parents
                _cache_expires_at = now + _CATEGORY_CACHE_TTL_SECONDS
            return [dict(item) for item in items]
        except Exception:
            if _name_index_items and now < float(_name_index_expires_at) + _CATEGORY_CACHE_TTL_SECONDS:
                return [dict(item) for item in _name_index_items]
            _name_index_items = []
            _name_index_expires_at = now + min(30.0, _CATEGORY_CACHE_TTL_SECONDS)
            return []


async def resolve_bilibili_area_by_name(
    name: str,
    *,
    area_fetcher=None,
) -> Optional[Dict[str, str]]:
    """Map a Discover tag / area label to getRoomList parent_area_id + area_id.

    Prefers exact child area names (e.g. League of Legends → area_id=86) over
    parent names (e.g. PC games → parent only, area_id=0).
    """
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    rows = await list_bilibili_area_name_index(area_fetcher=area_fetcher)
    for row in rows:
        if str(row.get("name") or "").strip().lower() == needle:
            return {
                "name": str(row.get("name") or "").strip(),
                "parent_area_id": str(row.get("parent_area_id") or "0"),
                "area_id": str(row.get("area_id") or "0"),
                "match_kind": str(row.get("match_kind") or ""),
            }
    return None

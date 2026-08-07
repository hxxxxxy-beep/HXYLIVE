"""Twitch native content-category discovery (A P5).

Discover Twitch pills use a curated allowlist (not Helix top-games).
Short TTL cache. Never invents Female/Male/Trans/Couple.
Helix streams filtering still uses game_id from these rows.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs

TOP_GAMES_URL = "https://api.twitch.tv/helix/games/top"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

# Discover UI: only these Twitch partitions (no All pill; ASMR is the default).
# game_id is the Helix streams filter; name is the pill label.
CURATED_TWITCH_CONTENT_CATEGORIES: Tuple[Dict[str, str], ...] = (
    {"game_id": "509659", "name": "ASMR"},
    {"game_id": "509658", "name": "Just Chatting"},
    {"game_id": "21779", "name": "LOL"},  # League of Legends
    {"game_id": "509672", "name": "IRL"},
)
_CURATED_GAME_IDS = frozenset(row["game_id"] for row in CURATED_TWITCH_CONTENT_CATEGORIES)

# Isolated from Twitch C2 unique-pool TTL (45s) and cursor state.
_CATEGORY_CACHE_TTL_SECONDS = 120.0
_CATEGORY_MAX_ITEMS = 24
_CATEGORY_HELIX_FIRST = 20
_CATEGORY_MAX_UPSTREAM_GETS = 1

_cache_lock = asyncio.Lock()
_cache_expires_at = 0.0
_cache_items: List[Dict[str, str]] = []
_token = ""
_token_expires_at = 0.0
_token_lock = asyncio.Lock()


def curated_twitch_content_categories() -> List[Dict[str, str]]:
    """Stable allowlist for Discover Twitch category pills."""
    return [dict(row) for row in CURATED_TWITCH_CONTENT_CATEGORIES]


def filter_twitch_games_to_allowlist(
    raw_items: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Map Helix rows onto the curated allowlist (labels/order fixed)."""
    _ = raw_items
    return curated_twitch_content_categories()


def _client_credentials() -> tuple[str, str]:
    return (
        (os.getenv("TWITCH_CLIENT_ID") or "").strip(),
        (os.getenv("TWITCH_CLIENT_SECRET") or "").strip(),
    )


def reset_twitch_category_cache_for_tests() -> None:
    """Test helper — clears in-process category cache and token."""
    global _cache_expires_at, _cache_items, _token, _token_expires_at
    _cache_expires_at = 0.0
    _cache_items = []
    _token = ""
    _token_expires_at = 0.0


def normalize_twitch_game_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    return text


def dedupe_twitch_games(raw_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Keep stable game_id + non-empty name; first-seen wins."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        game_id = normalize_twitch_game_id(item.get("id") or item.get("game_id"))
        name = str(item.get("name") or "").strip()
        if not game_id or not name:
            continue
        if game_id in seen:
            continue
        seen.add(game_id)
        out.append({"game_id": game_id, "name": name})
        if len(out) >= _CATEGORY_MAX_ITEMS:
            break
    return out


async def _get_app_token(*, force_refresh: bool = False) -> str:
    global _token, _token_expires_at
    client_id, client_secret = _client_credentials()
    if not client_id or not client_secret:
        raise RuntimeError("Twitch API credentials are not configured.")
    async with _token_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _token
            and now < float(_token_expires_at)
        ):
            return _token
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                **aiohttp_request_kwargs(),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise RuntimeError(f"Twitch token failed ({response.status}): {detail}")
        token = str((payload or {}).get("access_token") or "").strip()
        expires_in = int((payload or {}).get("expires_in") or 3600)
        if not token:
            raise RuntimeError("Twitch token response missing access_token.")
        _token = token
        _token_expires_at = time.monotonic() + max(30, expires_in - 60)
        return token


async def _helix_top_games(*, first: int = _CATEGORY_HELIX_FIRST) -> List[Dict[str, Any]]:
    client_id, _secret = _client_credentials()
    token = await _get_app_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Client-Id": client_id,
    }
    params = {"first": str(max(1, min(100, int(first))))}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp_client_session(timeout=timeout) as session:
        async with session.get(
            TOP_GAMES_URL, params=params, headers=headers, **aiohttp_request_kwargs()
        ) as response:
            payload = await response.json(content_type=None)
            if response.status == 401:
                token = await _get_app_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {token}"
                async with session.get(
                    TOP_GAMES_URL, params=params, headers=headers, **aiohttp_request_kwargs()
                ) as retry:
                    payload = await retry.json(content_type=None)
                    if retry.status >= 400:
                        detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                        raise RuntimeError(f"Twitch top games failed ({retry.status}): {detail}")
                    return [row for row in (payload.get("data") or []) if isinstance(row, dict)]
            if response.status >= 400:
                detail = payload.get("message") if isinstance(payload, dict) else str(payload)
                raise RuntimeError(f"Twitch top games failed ({response.status}): {detail}")
    return [row for row in (payload.get("data") or []) if isinstance(row, dict)]


async def list_twitch_content_categories(
    *,
    force_refresh: bool = False,
    helix_fetcher=None,
) -> List[Dict[str, str]]:
    """Return curated Twitch content categories for the categories API.

    Always returns the allowlist (ASMR / Just Chatting / LoL / IRL). Helix is
    optional and does not expand the set. Never touches Twitch C2 unique pools.
    """
    global _cache_expires_at, _cache_items
    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _cache_items
            and now < float(_cache_expires_at)
        ):
            return [dict(item) for item in _cache_items]

        curated = curated_twitch_content_categories()
        # Optional Helix probe kept for tests/injector; output stays curated.
        fetcher = helix_fetcher
        if fetcher is not None:
            try:
                for _attempt in range(_CATEGORY_MAX_UPSTREAM_GETS):
                    raw = await fetcher(first=_CATEGORY_HELIX_FIRST)
                    _ = filter_twitch_games_to_allowlist(dedupe_twitch_games(list(raw or [])))
                    break
            except Exception:
                pass

        _cache_items = curated
        _cache_expires_at = now + _CATEGORY_CACHE_TTL_SECONDS
        return [dict(item) for item in _cache_items]

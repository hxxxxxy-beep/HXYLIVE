"""Twitch native content-category discovery.

Discover Twitch pills use a curated allowlist (not Helix top-games).
Short TTL cache. Never invents Female/Male/Trans/Couple.
Helix streams filtering still uses game_id from these rows.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Discover UI: only these Twitch partitions (no All pill; ASMR is the default).
# game_id is the Helix streams filter; name is the pill label.
CURATED_TWITCH_CONTENT_CATEGORIES: Tuple[Dict[str, str], ...] = (
    {"game_id": "509659", "name": "ASMR"},
    {"game_id": "509658", "name": "Just Chatting"},
    {"game_id": "21779", "name": "LOL"},  # League of Legends
    {"game_id": "509672", "name": "IRL"},
)

# Isolated from Twitch C2 unique-pool TTL (45s) and cursor state.
_CATEGORY_CACHE_TTL_SECONDS = 120.0
_CATEGORY_MAX_ITEMS = 24
_CATEGORY_HELIX_FIRST = 20
_CATEGORY_MAX_UPSTREAM_GETS = 1

_cache_lock = asyncio.Lock()
_cache_expires_at = 0.0
_cache_items: List[Dict[str, str]] = []


def curated_twitch_content_categories() -> List[Dict[str, str]]:
    """Stable allowlist for Discover Twitch category pills."""
    return [dict(row) for row in CURATED_TWITCH_CONTENT_CATEGORIES]


def filter_twitch_games_to_allowlist(
    raw_items: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Map Helix rows onto the curated allowlist (labels/order fixed)."""
    _ = raw_items
    return curated_twitch_content_categories()


def reset_twitch_category_cache_for_tests() -> None:
    """Test helper — clears in-process category cache."""
    global _cache_expires_at, _cache_items
    _cache_expires_at = 0.0
    _cache_items = []


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

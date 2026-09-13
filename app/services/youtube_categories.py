"""YouTube live content-category helpers.

Discover YouTube is handle-search only (no category pills / live catalogue).
Official ``snippet.categoryId`` titles and creator ``snippet.tags`` remain
available for card metadata after a successful handle lookup.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Discover UI: no YouTube category pills (exact @handle search only).
CURATED_YOUTUBE_CONTENT_CATEGORIES: Tuple[Dict[str, str], ...] = ()
CURATED_YOUTUBE_VIDEO_CATEGORIES = CURATED_YOUTUBE_CONTENT_CATEGORIES
CURATED_YOUTUBE_LIVE_QUERY_Q: Dict[str, str] = {}
CURATED_YOUTUBE_LIVE_QUERIES = frozenset(CURATED_YOUTUBE_LIVE_QUERY_Q)
DEFAULT_YOUTUBE_LIVE_QUERY = "asmr"
_LIVE_QUERY_RE = re.compile(r"^[a-z][a-z0-9]{0,31}$")

# YouTube Data API videoCategories.list (regionCode=US, hl=en_US) titles.
YOUTUBE_VIDEO_CATEGORY_TITLES: Dict[str, str] = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "18": "Short Movies",
    "19": "Travel & Events",
    "20": "Gaming",
    "21": "Videoblogging",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
    "30": "Movies",
    "31": "Anime/Animation",
    "32": "Action/Adventure",
    "33": "Classics",
    "34": "Comedy",
    "35": "Documentary",
    "36": "Drama",
    "37": "Family",
    "38": "Foreign",
    "39": "Horror",
    "40": "Sci-Fi/Fantasy",
    "41": "Thriller",
    "42": "Shorts",
    "43": "Shows",
    "44": "Trailers",
}


def curated_youtube_video_categories() -> List[Dict[str, str]]:
    """Stable allowlist for Discover YouTube category pills."""
    return [dict(row) for row in CURATED_YOUTUBE_CONTENT_CATEGORIES]


def normalize_youtube_live_query(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text or not _LIVE_QUERY_RE.fullmatch(text):
        return None
    if text not in CURATED_YOUTUBE_LIVE_QUERIES:
        return None
    return text


def is_curated_youtube_live_query(value: Any) -> bool:
    return bool(normalize_youtube_live_query(value))


def youtube_live_query_q(value: Any) -> Optional[str]:
    key = normalize_youtube_live_query(value)
    if not key:
        return None
    return CURATED_YOUTUBE_LIVE_QUERY_Q.get(key)


def youtube_video_category_title(category_id: Any) -> Optional[str]:
    """Return the official en_US videoCategory title for a categoryId."""
    key = str(category_id or "").strip()
    if not key:
        return None
    return YOUTUBE_VIDEO_CATEGORY_TITLES.get(key)


def youtube_discover_tags(
    *,
    category_id: Any = None,
    snippet_tags: Optional[Sequence[Any]] = None,
) -> List[str]:
    """Build Discover tags from official YouTube fields only.

    Order: video category title first, then creator ``snippet.tags`` (deduped,
    case-insensitive). Unknown categoryIds are omitted rather than shown as digits.
    """
    out: List[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    title = youtube_video_category_title(category_id)
    if title:
        _add(title)
    if snippet_tags:
        for tag in snippet_tags:
            _add(tag)
    return out


async def list_youtube_content_categories(*, force_refresh: bool = False) -> List[Dict[str, str]]:
    """Return curated YouTube content categories for the categories API."""
    _ = force_refresh
    return curated_youtube_video_categories()

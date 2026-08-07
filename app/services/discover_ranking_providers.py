"""Generic Discover ranking page fetchers (Bilibili / Twitch / multi-source All).

Used by viewers_desc multi_page_global pools. Models keep their own source_type.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence

from .discover_ranking import PageFetcher


class RankingProviderAdapterError(RuntimeError):
    """Controlled adapter failure while building a ranking pool page."""


def attach_generic_viewer_evidence(model: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure viewers are present for ranking when providers only set viewers."""
    out = dict(model)
    if out.get("viewer_count_raw") is None and out.get("viewers") is not None:
        try:
            n = max(0, int(out.get("viewers") or 0))
        except (TypeError, ValueError):
            n = 0
        out["viewers"] = n
        out["viewer_count_raw"] = n
        if not out.get("viewer_count_source"):
            out["viewer_count_source"] = "viewers"
        if not out.get("viewer_count_precision_hint"):
            # Bilibili room-audience and Twitch Helix viewer_count are numeric but
            # may lack a named exact raw field on Discover cards.
            src = str(out.get("source_type") or "").strip().lower()
            if src == "twitch" and "viewer_count" in out:
                out["viewer_count_precision_hint"] = "exact"
                out["viewer_count_source"] = "viewer_count"
            elif src == "bilibili":
                out["viewer_count_precision_hint"] = "exact"
                out["viewer_count_source"] = "audience_count"
            else:
                out["viewer_count_precision_hint"] = "unverified"
        out["viewer_count_present"] = True
    return out


def normalize_provider_page_models(
    payload: Any,
    *,
    default_source: str = "",
) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("models") if isinstance(payload.get("models"), list) else []
    else:
        return []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if not item.get("source_type") and default_source:
            item["source_type"] = default_source
        username = str(item.get("username") or "").strip()
        if not username:
            continue
        out.append(attach_generic_viewer_evidence(item))
    return out


def make_provider_page_fetcher(
    provider: Any,
    *,
    default_source: str = "",
    gender: str = "",
    search: str = "",
    tags: Optional[Sequence[str]] = None,
    game_id: Optional[str] = None,
    parent_area_id: Optional[str] = None,
) -> PageFetcher:
    source = str(
        default_source or getattr(provider, "source_type", "") or ""
    ).strip().lower()
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]

    async def fetch_page(page: int, page_size: int) -> Sequence[Dict[str, Any]]:
        if provider is None or not hasattr(provider, "list_live_models"):
            raise RankingProviderAdapterError(f"{source or 'provider'} unavailable for ranking")
        kwargs: Dict[str, Any] = {
            "page": int(page),
            "limit": int(page_size),
            "search": search or "",
            "gender": gender or "",
            "tags": tag_list or None,
            "allow_browser": True,
            "exact_search_fallback": True,
        }
        if game_id:
            kwargs["game_id"] = game_id
        if parent_area_id:
            kwargs["parent_area_id"] = parent_area_id
        try:
            data = await provider.list_live_models(**kwargs)
        except Exception as exc:
            raise RankingProviderAdapterError(
                f"{source or 'provider'} list_live_models failed (page={page})"
            ) from exc
        return normalize_provider_page_models(data, default_source=source)

    return fetch_page


def make_aggregate_page_fetcher(
    providers: Sequence[Any],
    *,
    gender: str = "",
    search: str = "",
    tags: Optional[Sequence[str]] = None,
    game_id: Optional[str] = None,
    parent_area_id: Optional[str] = None,
) -> PageFetcher:
    """One ranking 'page' = merge of the same page index from every provider."""
    fetchers = [
        make_provider_page_fetcher(
            provider,
            default_source=str(getattr(provider, "source_type", "") or ""),
            gender=gender,
            search=search,
            tags=tags,
            game_id=game_id if str(getattr(provider, "source_type", "")).lower() == "twitch" else None,
            parent_area_id=(
                parent_area_id
                if str(getattr(provider, "source_type", "")).lower() == "bilibili"
                else None
            ),
        )
        for provider in providers
    ]

    async def fetch_page(page: int, page_size: int) -> Sequence[Dict[str, Any]]:
        if not fetchers:
            return []
        results = await asyncio.gather(
            *[fn(page, page_size) for fn in fetchers],
            return_exceptions=True,
        )
        merged: List[Dict[str, Any]] = []
        errors: List[str] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(str(result) or result.__class__.__name__)
                continue
            merged.extend(list(result or []))
        if not merged and errors:
            raise RankingProviderAdapterError(
                "aggregate ranking page failed: " + "; ".join(errors[:3])
            )
        return merged

    return fetch_page

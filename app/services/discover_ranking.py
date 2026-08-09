"""B2/B3.1 Discover ranking service (unit-test / local bypass only).

Not wired into /api/discover. No real Provider calls, no network, no disk/Redis.
Uses an injected async page fetcher for pool builds.

B3.1: pool_id index, continuation validation, controlled errors for page>=2
snapshot reads (no silent rebuild, no fetcher on slice path).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from .discover_ranking_types import (
    CONTRACT_VERSION,
    DiscoverSortRequest,
    RankingMode,
    ViewerCountPrecision,
    annotate_model_viewer_fields,
    normalize_sort_param,
    sorts_by_viewers,
)

DEFAULT_POOL_TTL_SECONDS = 120
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_REQUESTS = 10
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POOL_LIMIT = 240
DEFAULT_SLICE_LIMIT_MAX = 100

# Precisions that may use a numeric viewer_count in viewers_desc ranking.
_RANKABLE_PRECISIONS = frozenset({
    ViewerCountPrecision.EXACT.value,
    ViewerCountPrecision.APPROXIMATE.value,
    ViewerCountPrecision.UNVERIFIED.value,
    ViewerCountPrecision.STALE.value,
})

PageFetcher = Callable[[int, int], Awaitable[Sequence[Dict[str, Any]]]]


# ---------------------------------------------------------------------------
# B3.1 controlled errors (service layer only — no HTTP mapping here)
# ---------------------------------------------------------------------------


class RankingPoolError(Exception):
    """Base error for ranking snapshot lookup / continuation."""

    code = "ranking_pool_error"

    def __init__(
        self,
        message: str,
        *,
        pool_id: Optional[str] = None,
        retryable: bool = False,
        mismatch_field: Optional[str] = None,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.pool_id = pool_id
        self.restart_from_page = 1
        self.retryable = bool(retryable)
        self.mismatch_field = mismatch_field
        self.expected = expected
        self.actual = actual

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "error": self.code,
            "message": self.message,
            "pool_id": self.pool_id,
            "restart_from_page": self.restart_from_page,
            "retryable": self.retryable,
        }
        if self.mismatch_field is not None:
            payload["mismatch_field"] = self.mismatch_field
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        return payload


class RankingPoolNotFound(RankingPoolError):
    code = "ranking_pool_not_found"

    def __init__(self, pool_id: Optional[str], message: Optional[str] = None) -> None:
        super().__init__(
            message or f"ranking pool not found: {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
        )


class RankingPoolExpired(RankingPoolError):
    code = "ranking_pool_expired"

    def __init__(self, pool_id: Optional[str], message: Optional[str] = None) -> None:
        super().__init__(
            message or f"ranking pool expired: {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
        )


class RankingPoolFilterMismatch(RankingPoolError):
    code = "ranking_pool_filter_mismatch"

    def __init__(
        self,
        pool_id: Optional[str],
        *,
        mismatch_field: str,
        expected: Any = None,
        actual: Any = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(
            message
            or f"ranking pool filter mismatch on {mismatch_field} for {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
            mismatch_field=mismatch_field,
            expected=expected,
            actual=actual,
        )


class RankingPoolSourceMismatch(RankingPoolError):
    code = "ranking_pool_source_mismatch"

    def __init__(
        self,
        pool_id: Optional[str],
        *,
        expected: Any = None,
        actual: Any = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(
            message or f"ranking pool source mismatch for {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
            mismatch_field="source",
            expected=expected,
            actual=actual,
        )


class RankingPoolSortMismatch(RankingPoolError):
    code = "ranking_pool_sort_mismatch"

    def __init__(
        self,
        pool_id: Optional[str],
        *,
        expected: Any = None,
        actual: Any = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(
            message or f"ranking pool sort mismatch for {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
            mismatch_field="sort",
            expected=expected,
            actual=actual,
        )


class RankingPoolPageOutOfRange(RankingPoolError):
    """Invalid page/limit parameters (not soft end-of-snapshot empty page)."""

    code = "ranking_pool_page_out_of_range"

    def __init__(
        self,
        pool_id: Optional[str],
        *,
        message: Optional[str] = None,
        page: Any = None,
        limit: Any = None,
    ) -> None:
        super().__init__(
            message or f"ranking pool page/limit out of range for {pool_id!r}",
            pool_id=pool_id,
            retryable=False,
            mismatch_field="page",
            expected="page>=1 and 1<=limit<=max",
            actual={"page": page, "limit": limit},
        )


def normalize_tags(tags: Optional[Sequence[str]]) -> List[str]:
    seen = set()
    out: List[str] = []
    for tag in tags or []:
        value = str(tag or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    out.sort()
    return out


def model_tag_set(model: Dict[str, Any]) -> set[str]:
    """Lowercased tag strings present on a Discover model card."""
    out: set[str] = set()
    for tag in model.get("tags") or []:
        value = str(tag or "").strip().lower()
        if value:
            out.add(value)
    return out


def model_matches_all_tags(
    model: Dict[str, Any],
    required: Optional[Sequence[str]],
) -> bool:
    """True when every requested tag appears on the model (case-insensitive).

    Empty required → always True. Subject/keyword hits without the tag do not count;
    ranking pools must not pad shortfalls with non-matching rooms.
    """
    need = normalize_tags(required)
    if not need:
        return True
    have = model_tag_set(model)
    return all(tag in have for tag in need)


def filter_models_by_tags(
    models: Sequence[Dict[str, Any]],
    required: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    need = normalize_tags(required)
    if not need:
        return [dict(m) for m in models]
    return [dict(m) for m in models if model_matches_all_tags(m, need)]


def stable_id_for_model(model: Dict[str, Any], default_source: str = "") -> str:
    if model.get("stable_id"):
        return str(model["stable_id"])
    source = str(model.get("source_type") or default_source or "").strip().lower()
    username = str(model.get("username") or "").strip().lower()
    return f"{source}:{username}"


def compute_filters_hash(
    *,
    source: str,
    canonical_category: str = "all",
    language: str = "",
    tags: Optional[Sequence[str]] = None,
    sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
    search: str = "",
    extra_filters: Optional[Dict[str, Any]] = None,
) -> str:
    """Stable hash of candidate-set filters (excludes page/cursor/seen_ids)."""
    payload = {
        "source": str(source or "").strip().lower(),
        "canonical_category": str(canonical_category or "all").strip().lower() or "all",
        "language": str(language or "").strip().lower(),
        "tags": normalize_tags(tags),
        "sort": normalize_sort_param(sort),
        "search": str(search or "").strip().lower(),
        "extra_filters": _normalize_extra_filters(extra_filters),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"fh_{digest[:16]}"


def _normalize_extra_filters(extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not extra:
        return {}
    normalized: Dict[str, Any] = {}
    for key in sorted(extra.keys()):
        value = extra[key]
        if isinstance(value, (list, tuple)):
            normalized[str(key)] = sorted(str(v).strip().lower() for v in value if str(v).strip())
        elif isinstance(value, dict):
            normalized[str(key)] = _normalize_extra_filters(value)
        else:
            normalized[str(key)] = str(value).strip().lower() if value is not None else ""
    return normalized


@dataclass
class RankingPoolBudget:
    max_pages: int = DEFAULT_MAX_PAGES
    max_requests: int = DEFAULT_MAX_REQUESTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    pool_limit: int = DEFAULT_POOL_LIMIT


@dataclass
class RankingSnapshot:
    pool_id: str
    filters_hash: str
    generated_at: float
    expires_at: float
    ranking_mode: str
    sort: str
    source: str
    canonical_category: str
    candidate_count: int
    pages_scanned: int
    requests_used: int
    is_complete: bool
    partial_reason: Optional[str]
    models: List[Dict[str, Any]] = field(default_factory=list)
    language: str = ""
    tags: List[str] = field(default_factory=list)
    search: str = ""
    contract_version: str = CONTRACT_VERSION

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


def _sort_key_viewers_desc(model: Dict[str, Any]) -> tuple:
    """Deterministic viewers_desc key.

    Rankable precisions use (-count, stable_id).
    missing sorts after all numeric counts (not treated as real 0).
    """
    precision = str(model.get("viewer_count_precision") or ViewerCountPrecision.MISSING.value)
    stable = str(model.get("stable_id") or "")
    if precision == ViewerCountPrecision.MISSING.value or model.get("viewer_count") is None:
        return (1, 0, stable)
    try:
        count = int(model.get("viewer_count") or 0)
    except (TypeError, ValueError):
        return (1, 0, stable)
    if precision not in _RANKABLE_PRECISIONS:
        return (1, 0, stable)
    return (0, -count, stable)


def rank_models(
    models: Sequence[Dict[str, Any]],
    *,
    sort: str,
    source: str = "",
) -> List[Dict[str, Any]]:
    sort_mode = normalize_sort_param(sort)
    prepared: List[Dict[str, Any]] = []
    for raw in models:
        item = dict(raw)
        if not item.get("source_type"):
            item["source_type"] = source
        annotate_model_viewer_fields(item)
        item["stable_id"] = stable_id_for_model(item, default_source=source)
        prepared.append(item)

    if sorts_by_viewers(sort_mode) or sort_mode == DiscoverSortRequest.VIEWERS_DESC.value:
        prepared.sort(key=_sort_key_viewers_desc)
    else:
        # source_default / unknown: preserve encounter order, still assign stable_id.
        pass
    return prepared


def dedupe_by_stable_id(models: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep first occurrence per stable_id (caller should rank after merge)."""
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for model in models:
        key = str(model.get("stable_id") or stable_id_for_model(model))
        if not key or key.endswith(":") or key in seen:
            continue
        seen.add(key)
        entry = dict(model)
        entry["stable_id"] = key
        out.append(entry)
    return out


def validate_snapshot_request(
    snapshot: RankingSnapshot,
    *,
    source: str,
    canonical_category: str = "all",
    language: str = "",
    tags: Optional[Sequence[str]] = None,
    sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
    search: str = "",
    filters_hash: Optional[str] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> None:
    """Raise controlled mismatch errors; never rebuild or slice on failure."""
    pool_id = snapshot.pool_id
    req_source = str(source or "").strip().lower()
    if req_source != snapshot.source:
        raise RankingPoolSourceMismatch(
            pool_id,
            expected=snapshot.source,
            actual=req_source,
        )

    req_sort = normalize_sort_param(sort)
    if req_sort != snapshot.sort:
        raise RankingPoolSortMismatch(
            pool_id,
            expected=snapshot.sort,
            actual=req_sort,
        )

    req_category = str(canonical_category or "all").strip().lower() or "all"
    if req_category != snapshot.canonical_category:
        raise RankingPoolFilterMismatch(
            pool_id,
            mismatch_field="canonical_category",
            expected=snapshot.canonical_category,
            actual=req_category,
        )

    req_language = str(language or "").strip().lower()
    if req_language != str(snapshot.language or "").strip().lower():
        raise RankingPoolFilterMismatch(
            pool_id,
            mismatch_field="language",
            expected=snapshot.language,
            actual=req_language,
        )

    req_tags = normalize_tags(tags)
    snap_tags = normalize_tags(snapshot.tags)
    if req_tags != snap_tags:
        raise RankingPoolFilterMismatch(
            pool_id,
            mismatch_field="tags",
            expected=snap_tags,
            actual=req_tags,
        )

    req_search = str(search or "").strip().lower()
    if req_search != str(snapshot.search or "").strip().lower():
        raise RankingPoolFilterMismatch(
            pool_id,
            mismatch_field="search",
            expected=snapshot.search,
            actual=req_search,
        )

    if filters_hash is not None:
        expected_hash = str(filters_hash)
    else:
        expected_hash = compute_filters_hash(
            source=req_source,
            canonical_category=req_category,
            language=req_language,
            tags=req_tags,
            sort=req_sort,
            search=req_search,
            extra_filters=extra_filters,
        )

    if expected_hash != snapshot.filters_hash:
        raise RankingPoolFilterMismatch(
            pool_id,
            mismatch_field="filters_hash",
            expected=snapshot.filters_hash,
            actual=expected_hash,
        )


def slice_pool(
    snapshot: RankingSnapshot,
    *,
    page: int = 1,
    limit: int = 24,
    limit_max: int = DEFAULT_SLICE_LIMIT_MAX,
) -> Dict[str, Any]:
    """Stable page slice from an immutable ranked snapshot (no validation)."""
    try:
        page_i = int(page)
        limit_i = int(limit)
    except (TypeError, ValueError) as exc:
        raise RankingPoolPageOutOfRange(
            snapshot.pool_id,
            page=page,
            limit=limit,
        ) from exc
    if page_i < 1 or limit_i < 1 or limit_i > int(limit_max):
        raise RankingPoolPageOutOfRange(
            snapshot.pool_id,
            page=page_i,
            limit=limit_i,
        )

    start = (page_i - 1) * limit_i
    end = start + limit_i
    total = len(snapshot.models)
    # Beyond end → empty models, has_more=false; do not rebuild.
    models = list(snapshot.models[start:end]) if start < total else []
    has_more = end < total
    return {
        "pool_id": snapshot.pool_id,
        "filters_hash": snapshot.filters_hash,
        "page": page_i,
        "limit": limit_i,
        "models": models,
        "candidate_count": snapshot.candidate_count,
        "has_more": has_more,
        "ranking_mode": snapshot.ranking_mode,
        "is_complete": snapshot.is_complete,
        "partial_reason": snapshot.partial_reason,
        "sort": snapshot.sort,
        "source": snapshot.source,
        "canonical_category": snapshot.canonical_category,
        "language": snapshot.language,
        "tags": list(snapshot.tags),
        "contract_version": snapshot.contract_version,
    }


def slice_snapshot(
    snapshot: RankingSnapshot,
    *,
    page: int = 1,
    limit: int = 24,
    source: str,
    canonical_category: str = "all",
    language: str = "",
    tags: Optional[Sequence[str]] = None,
    sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
    search: str = "",
    filters_hash: Optional[str] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
    limit_max: int = DEFAULT_SLICE_LIMIT_MAX,
) -> Dict[str, Any]:
    """Validate request against snapshot binding, then slice (no fetch / rebuild)."""
    validate_snapshot_request(
        snapshot,
        source=source,
        canonical_category=canonical_category,
        language=language,
        tags=tags,
        sort=sort,
        search=search,
        filters_hash=filters_hash,
        extra_filters=extra_filters,
    )
    return slice_pool(snapshot, page=page, limit=limit, limit_max=limit_max)


class DiscoverRankingService:
    """In-memory ranking pool builder (single-flight + TTL + pool_id index)."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_POOL_TTL_SECONDS) -> None:
        self.ttl_seconds = float(ttl_seconds)
        # filters_hash → live snapshot (page1 reuse within TTL)
        self._by_filters_hash: Dict[str, RankingSnapshot] = {}
        # pool_id → snapshot (continuation); expired entries may linger until require/purge
        self._by_pool_id: Dict[str, RankingSnapshot] = {}
        self._expired_pool_ids: set[str] = set()
        self._inflight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    def _forget_snapshot(self, snap: RankingSnapshot, *, as_expired: bool = False) -> None:
        if self._by_pool_id.get(snap.pool_id) is snap:
            del self._by_pool_id[snap.pool_id]
        if self._by_filters_hash.get(snap.filters_hash) is snap:
            del self._by_filters_hash[snap.filters_hash]
        if as_expired and snap.pool_id:
            self._expired_pool_ids.add(snap.pool_id)

    def _purge_expired_lazy(self) -> int:
        """Drop expired filter-index entries; keep pool_id until require/purge_expired."""
        removed = 0
        for fh, snap in list(self._by_filters_hash.items()):
            if snap.is_expired():
                if self._by_filters_hash.get(fh) is snap:
                    del self._by_filters_hash[fh]
                    removed += 1
                # Keep _by_pool_id so require_snapshot can return Expired.
        return removed

    def purge_expired(self) -> int:
        """Eagerly drop expired snapshots from both indexes (tombstone pool_id)."""
        removed = 0
        for pool_id, snap in list(self._by_pool_id.items()):
            if snap.is_expired():
                self._forget_snapshot(snap, as_expired=True)
                removed += 1
        for fh, snap in list(self._by_filters_hash.items()):
            if snap.is_expired():
                self._forget_snapshot(snap, as_expired=True)
                removed += 1
        return removed

    def _register_snapshot(self, snapshot: RankingSnapshot) -> None:
        prev = self._by_filters_hash.get(snapshot.filters_hash)
        if prev is not None and prev is not snapshot:
            if prev.is_expired():
                self._forget_snapshot(prev, as_expired=True)
            elif self._by_filters_hash.get(snapshot.filters_hash) is prev:
                # Should not replace a live pool for same hash (reuse path avoids this).
                del self._by_filters_hash[snapshot.filters_hash]
        self._by_filters_hash[snapshot.filters_hash] = snapshot
        self._by_pool_id[snapshot.pool_id] = snapshot
        self._expired_pool_ids.discard(snapshot.pool_id)

    def get_cached(self, filters_hash: str) -> Optional[RankingSnapshot]:
        self._purge_expired_lazy()
        snap = self._by_filters_hash.get(filters_hash)
        if snap is None:
            return None
        if snap.is_expired():
            if self._by_filters_hash.get(filters_hash) is snap:
                del self._by_filters_hash[filters_hash]
            # Retain pool_id mapping for Expired on continuation.
            return None
        return snap

    def get_snapshot(self, pool_id: Optional[str]) -> Optional[RankingSnapshot]:
        """Return live snapshot for pool_id, or None if missing/expired (no rebuild)."""
        if not pool_id:
            return None
        self._purge_expired_lazy()
        snap = self._by_pool_id.get(str(pool_id))
        if snap is None:
            return None
        if snap.is_expired():
            return None
        return snap

    def require_snapshot(self, pool_id: Optional[str]) -> RankingSnapshot:
        """Return live snapshot or raise NotFound / Expired (never rebuild)."""
        if not pool_id:
            raise RankingPoolNotFound(pool_id)
        key = str(pool_id)
        snap = self._by_pool_id.get(key)
        if snap is not None:
            if snap.is_expired():
                self._forget_snapshot(snap, as_expired=True)
                raise RankingPoolExpired(key)
            return snap
        if key in self._expired_pool_ids:
            raise RankingPoolExpired(key)
        self._purge_expired_lazy()
        raise RankingPoolNotFound(key)

    def validate_snapshot_request(
        self,
        snapshot: RankingSnapshot,
        *,
        source: str,
        canonical_category: str = "all",
        language: str = "",
        tags: Optional[Sequence[str]] = None,
        sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
        search: str = "",
        filters_hash: Optional[str] = None,
        extra_filters: Optional[Dict[str, Any]] = None,
    ) -> None:
        validate_snapshot_request(
            snapshot,
            source=source,
            canonical_category=canonical_category,
            language=language,
            tags=tags,
            sort=sort,
            search=search,
            filters_hash=filters_hash,
            extra_filters=extra_filters,
        )

    def slice_snapshot(
        self,
        pool_id: str,
        *,
        page: int = 1,
        limit: int = 24,
        source: str,
        canonical_category: str = "all",
        language: str = "",
        tags: Optional[Sequence[str]] = None,
        sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
        search: str = "",
        filters_hash: Optional[str] = None,
        extra_filters: Optional[Dict[str, Any]] = None,
        limit_max: int = DEFAULT_SLICE_LIMIT_MAX,
    ) -> Dict[str, Any]:
        """page>=2 path: require + validate + slice. Never calls fetcher/build_pool."""
        snapshot = self.require_snapshot(pool_id)
        return slice_snapshot(
            snapshot,
            page=page,
            limit=limit,
            source=source,
            canonical_category=canonical_category,
            language=language,
            tags=tags,
            sort=sort,
            search=search,
            filters_hash=filters_hash,
            extra_filters=extra_filters,
            limit_max=limit_max,
        )

    async def build_pool(
        self,
        *,
        source: str,
        fetch_page: PageFetcher,
        canonical_category: str = "all",
        language: str = "",
        tags: Optional[Sequence[str]] = None,
        sort: str = DiscoverSortRequest.VIEWERS_DESC.value,
        search: str = "",
        extra_filters: Optional[Dict[str, Any]] = None,
        budget: Optional[RankingPoolBudget] = None,
        ranking_mode: str = RankingMode.MULTI_PAGE_GLOBAL.value,
        page_size: int = 24,
        start_page: int = 1,
    ) -> RankingSnapshot:
        """Build or reuse a ranked snapshot via injected fetcher (no network here)."""
        start_i = max(1, int(start_page or 1))
        merged_extra = dict(extra_filters or {})
        if start_i > 1:
            merged_extra["ranking_start_page"] = start_i
        filters_hash = compute_filters_hash(
            source=source,
            canonical_category=canonical_category,
            language=language,
            tags=tags,
            sort=sort,
            search=search,
            extra_filters=merged_extra or None,
        )
        cached = self.get_cached(filters_hash)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self.get_cached(filters_hash)
            if cached is not None:
                return cached
            inflight = self._inflight.get(filters_hash)
            if inflight is None:
                loop = asyncio.get_running_loop()
                inflight = loop.create_future()
                self._inflight[filters_hash] = inflight
                owner = True
            else:
                owner = False

        if not owner:
            return await asyncio.shield(inflight)

        try:
            snapshot = await self._build_pool_uncached(
                source=source,
                fetch_page=fetch_page,
                filters_hash=filters_hash,
                canonical_category=canonical_category,
                language=language,
                tags=tags,
                search=search,
                sort=sort,
                budget=budget or RankingPoolBudget(),
                ranking_mode=ranking_mode,
                page_size=page_size,
                start_page=start_i,
            )
            self._register_snapshot(snapshot)
            inflight.set_result(snapshot)
            return snapshot
        except Exception as exc:
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(filters_hash, None)

    async def _build_pool_uncached(
        self,
        *,
        source: str,
        fetch_page: PageFetcher,
        filters_hash: str,
        canonical_category: str,
        language: str,
        tags: Optional[Sequence[str]],
        search: str,
        sort: str,
        budget: RankingPoolBudget,
        ranking_mode: str,
        page_size: int,
        start_page: int = 1,
    ) -> RankingSnapshot:
        started = time.monotonic()
        collected: List[Dict[str, Any]] = []
        pages_scanned = 0
        requests_used = 0
        is_complete = True
        partial_reason: Optional[str] = None
        last_batch_full = False
        hit_page_ceiling = False

        max_pages = max(1, int(budget.max_pages))
        max_requests = max(1, int(budget.max_requests))
        timeout = max(0.01, float(budget.timeout_seconds))
        pool_limit = max(1, int(budget.pool_limit))
        norm_tags = normalize_tags(tags)
        norm_language = str(language or "").strip().lower()
        norm_search = str(search or "").strip().lower()
        norm_category = str(canonical_category or "all").strip().lower() or "all"
        norm_source = str(source or "").strip().lower()
        start_i = max(1, int(start_page or 1))
        end_page = start_i + max_pages - 1

        for page in range(start_i, end_page + 1):
            if requests_used >= max_requests:
                is_complete = False
                partial_reason = "max_requests"
                break
            if (time.monotonic() - started) >= timeout:
                is_complete = False
                partial_reason = "timeout"
                break

            try:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    is_complete = False
                    partial_reason = "timeout"
                    break
                page_models = await asyncio.wait_for(
                    fetch_page(page, page_size),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                is_complete = False
                partial_reason = "timeout"
                break

            requests_used += 1
            pages_scanned += 1
            batch = list(page_models or [])
            if not batch:
                last_batch_full = False
                break
            collected.extend(dict(m) for m in batch)
            last_batch_full = len(batch) >= page_size
            if not last_batch_full:
                break
            if page == end_page:
                hit_page_ceiling = True
                break
            # With tags: scan the full page budget, then keep matches only.
            # Do not stop early on raw volume — keyword/upstream pages are often
            # mostly non-matches, and padding with them is forbidden.
            if not norm_tags and len(collected) >= pool_limit * 2:
                is_complete = False
                partial_reason = partial_reason or "pool_limit"
                break

        if hit_page_ceiling and last_batch_full and is_complete:
            is_complete = False
            partial_reason = "max_pages"

        unique = dedupe_by_stable_id(
            [
                {
                    **m,
                    "source_type": m.get("source_type") or source,
                    "stable_id": stable_id_for_model(m, source),
                }
                for m in collected
            ]
        )
        # Strict tag gate: pool contains only matching rooms. A short match set
        # (e.g. 2 pages from 10 scanned) stays short; next batch via max_pages.
        if norm_tags:
            unique = filter_models_by_tags(unique, norm_tags)
        ranked = rank_models(unique, sort=sort, source=source)
        if len(ranked) > pool_limit:
            ranked = ranked[:pool_limit]
            if is_complete or partial_reason is None:
                is_complete = False
                partial_reason = partial_reason or "pool_limit"

        now = time.time()
        allowed = {m.value for m in RankingMode}
        mode = ranking_mode if ranking_mode in allowed else RankingMode.MULTI_PAGE_GLOBAL.value
        if mode == RankingMode.PROVIDER_NATIVE.value and ranking_mode != RankingMode.PROVIDER_NATIVE.value:
            mode = RankingMode.MULTI_PAGE_GLOBAL.value

        return RankingSnapshot(
            pool_id=f"pl_{uuid.uuid4().hex[:12]}",
            filters_hash=filters_hash,
            generated_at=now,
            expires_at=now + self.ttl_seconds,
            ranking_mode=mode,
            sort=normalize_sort_param(sort),
            source=norm_source,
            canonical_category=norm_category,
            language=norm_language,
            tags=list(norm_tags),
            search=norm_search,
            candidate_count=len(ranked),
            pages_scanned=pages_scanned,
            requests_used=requests_used,
            is_complete=is_complete,
            partial_reason=partial_reason,
            models=ranked,
        )

    def clear(self) -> None:
        self._by_filters_hash.clear()
        self._by_pool_id.clear()
        self._expired_pool_ids.clear()

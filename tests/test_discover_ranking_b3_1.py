"""B3.1 pool_id lookup, continuation validation, controlled errors (no /api/discover)."""

from __future__ import annotations

import asyncio
import time
import unittest

from app.services.discover_ranking import (
    DiscoverRankingService,
    RankingPoolBudget,
    RankingPoolExpired,
    RankingPoolFilterMismatch,
    RankingPoolNotFound,
    RankingPoolPageOutOfRange,
    RankingPoolSortMismatch,
    RankingPoolSourceMismatch,
    RankingMode,
    compute_filters_hash,
    slice_pool,
)
from app.services.discover_ranking_chaturbate import (
    build_chaturbate_ranking_pool,
    make_chaturbate_page_fetcher,
)


def _model(username: str, **kwargs):
    base = {
        "username": username,
        "source_type": kwargs.pop("source_type", "chaturbate"),
        "is_online": True,
        "room_status": "public",
    }
    base.update(kwargs)
    return base


def _room(username: str, num_users: int):
    return {
        "username": username,
        "display_name": username,
        "num_users": num_users,
        "current_show": "public",
        "tags": [],
        "room_subject": "",
        "gender": "f",
    }


class FixtureRoomlist:
    def __init__(self, pages: dict[int, list]):
        self.pages = pages
        self.calls: list[tuple[int, int, int]] = []

    async def __call__(self, page: int, limit: int, **kwargs):
        offset = int(kwargs.get("offset", (page - 1) * limit))
        self.calls.append((page, limit, offset))
        batch = list(self.pages.get(page, []))
        return {"rooms": batch[:limit]}


class RankingB31PoolLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = DiscoverRankingService(ttl_seconds=60)

    def _make_fetcher(self, pages, counter=None):
        async def fetch_page(page, limit):
            if counter is not None:
                counter["n"] = counter.get("n", 0) + 1
            return list(pages.get(page, []))[:limit]

        return fetch_page

    async def test_pool_lookup_by_pool_id(self):
        pages = {
            1: [
                _model("a", num_users=30, viewers=30),
                _model("b", num_users=20, viewers=20),
                _model("c", num_users=10, viewers=10),
            ],
            2: [],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=3,
        )
        found = self.service.get_snapshot(snap.pool_id)
        self.assertIsNotNone(found)
        self.assertEqual(snap.pool_id, found.pool_id)
        self.assertEqual(
            [m["stable_id"] for m in snap.models],
            [m["stable_id"] for m in found.models],
        )
        required = self.service.require_snapshot(snap.pool_id)
        self.assertEqual(snap.pool_id, required.pool_id)

    async def test_page2_slice_does_not_fetch_again(self):
        pages = {
            1: [_model(f"u{i}", num_users=100 - i, viewers=100 - i) for i in range(4)],
            2: [],
        }
        counter = {"n": 0}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages, counter=counter),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=4,
        )
        builds = counter["n"]
        self.assertGreaterEqual(builds, 1)

        page1 = self.service.slice_snapshot(
            snap.pool_id,
            page=1,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        page2 = self.service.slice_snapshot(
            snap.pool_id,
            page=2,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        self.assertEqual(builds, counter["n"])
        self.assertEqual(2, len(page1["models"]))
        self.assertEqual(2, len(page2["models"]))
        self.assertTrue(page1["has_more"])
        self.assertFalse(page2["has_more"])
        ids1 = {m["stable_id"] for m in page1["models"]}
        ids2 = {m["stable_id"] for m in page2["models"]}
        self.assertEqual(set(), ids1 & ids2)
        self.assertEqual(snap.pool_id, page2["pool_id"])

    async def test_pool_not_found_controlled(self):
        with self.assertRaises(RankingPoolNotFound) as ctx:
            self.service.require_snapshot("pl_does_not_exist")
        err = ctx.exception
        self.assertEqual("ranking_pool_not_found", err.code)
        self.assertEqual(1, err.restart_from_page)
        self.assertFalse(err.retryable)
        self.assertEqual("pl_does_not_exist", err.to_dict()["pool_id"])

    async def test_pool_expired_controlled(self):
        service = DiscoverRankingService(ttl_seconds=0.05)
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        await asyncio.sleep(0.08)
        with self.assertRaises(RankingPoolExpired) as ctx:
            service.require_snapshot(snap.pool_id)
        self.assertEqual("ranking_pool_expired", ctx.exception.code)
        self.assertEqual(1, ctx.exception.restart_from_page)
        self.assertIsNone(service.get_snapshot(snap.pool_id))

    async def test_expired_pool_id_not_remapped_to_new_pool(self):
        service = DiscoverRankingService(ttl_seconds=0.05)
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap1 = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        old_id = snap1.pool_id
        await asyncio.sleep(0.08)
        snap2 = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertNotEqual(old_id, snap2.pool_id)
        with self.assertRaises((RankingPoolExpired, RankingPoolNotFound)):
            service.require_snapshot(old_id)
        self.assertEqual(snap2.pool_id, service.require_snapshot(snap2.pool_id).pool_id)

    async def test_source_mismatch(self):
        pages = {1: [_model("a", num_users=5, viewers=5)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        with self.assertRaises(RankingPoolSourceMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="twitch",
                sort="viewers_desc",
            )
        self.assertEqual("source", ctx.exception.mismatch_field)
        self.assertEqual("ranking_pool_source_mismatch", ctx.exception.code)

    async def test_filters_hash_mismatch(self):
        pages = {1: [_model("a", num_users=5, viewers=5)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            canonical_category="female",
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        wrong_hash = compute_filters_hash(
            source="chaturbate",
            canonical_category="male",
            sort="viewers_desc",
        )
        with self.assertRaises(RankingPoolFilterMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="chaturbate",
                canonical_category="female",
                sort="viewers_desc",
                filters_hash=wrong_hash,
            )
        self.assertEqual("filters_hash", ctx.exception.mismatch_field)

    async def test_sort_mismatch(self):
        pages = {1: [_model("a", num_users=5, viewers=5)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        with self.assertRaises(RankingPoolSortMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="chaturbate",
                sort="source_default",
            )
        self.assertEqual("sort", ctx.exception.mismatch_field)

    async def test_category_language_tags_mismatch(self):
        pages = {1: [_model("a", num_users=5, viewers=5)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            canonical_category="female",
            language="en",
            tags=["hd", "new"],
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        with self.assertRaises(RankingPoolFilterMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="chaturbate",
                canonical_category="male",
                language="en",
                tags=["hd", "new"],
                sort="viewers_desc",
            )
        self.assertEqual("canonical_category", ctx.exception.mismatch_field)

        with self.assertRaises(RankingPoolFilterMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="chaturbate",
                canonical_category="female",
                language="fr",
                tags=["hd", "new"],
                sort="viewers_desc",
            )
        self.assertEqual("language", ctx.exception.mismatch_field)

        with self.assertRaises(RankingPoolFilterMismatch) as ctx:
            self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=24,
                source="chaturbate",
                canonical_category="female",
                language="en",
                tags=["hd"],
                sort="viewers_desc",
            )
        self.assertEqual("tags", ctx.exception.mismatch_field)

    async def test_page_past_end_empty_no_rebuild(self):
        pages = {
            1: [_model("a", num_users=3, viewers=3), _model("b", num_users=2, viewers=2)],
            2: [],
        }
        counter = {"n": 0}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages, counter=counter),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=2,
        )
        builds = counter["n"]
        past = self.service.slice_snapshot(
            snap.pool_id,
            page=99,
            limit=24,
            source="chaturbate",
            sort="viewers_desc",
        )
        self.assertEqual([], past["models"])
        self.assertFalse(past["has_more"])
        self.assertEqual(builds, counter["n"])
        self.assertEqual(snap.pool_id, past["pool_id"])

    async def test_invalid_page_raises_out_of_range(self):
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        with self.assertRaises(RankingPoolPageOutOfRange):
            self.service.slice_snapshot(
                snap.pool_id,
                page=0,
                limit=24,
                source="chaturbate",
                sort="viewers_desc",
            )

    async def test_partial_snapshot_continues_with_partial_reason(self):
        pages = {
            1: [
                _model("a", num_users=40, viewers=40),
                _model("b", num_users=30, viewers=30),
                _model("c", num_users=20, viewers=20),
            ],
            2: [
                _model("d", num_users=100, viewers=100),
                _model("e", num_users=10, viewers=10),
                _model("f", num_users=5, viewers=5),
            ],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=5, max_requests=1, timeout_seconds=5),
            page_size=3,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_requests", snap.partial_reason)
        page1 = self.service.slice_snapshot(
            snap.pool_id,
            page=1,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        page2 = self.service.slice_snapshot(
            snap.pool_id,
            page=2,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        self.assertFalse(page1["is_complete"])
        self.assertEqual("max_requests", page1["partial_reason"])
        self.assertEqual(page1["partial_reason"], page2["partial_reason"])
        self.assertNotEqual(
            [m["stable_id"] for m in page1["models"]],
            [m["stable_id"] for m in page2["models"]],
        )

    async def test_stable_order_across_repeated_reads(self):
        pages = {
            1: [_model(f"u{i}", num_users=50 - i, viewers=50 - i) for i in range(5)],
            2: [],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=5,
        )
        orders = []
        for _ in range(5):
            sliced = self.service.slice_snapshot(
                snap.pool_id,
                page=1,
                limit=5,
                source="chaturbate",
                sort="viewers_desc",
            )
            orders.append([m["stable_id"] for m in sliced["models"]])
        self.assertTrue(all(o == orders[0] for o in orders))

    async def test_lazy_expiry_cleanup(self):
        service = DiscoverRankingService(ttl_seconds=0.05)
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertIn(snap.pool_id, service._by_pool_id)
        await asyncio.sleep(0.08)
        removed = service.purge_expired()
        self.assertGreaterEqual(removed, 1)
        self.assertNotIn(snap.pool_id, service._by_pool_id)
        self.assertIsNone(service.get_snapshot(snap.pool_id))

    async def test_has_more_false_not_equal_is_complete_true(self):
        pages = {
            1: [_model("a", num_users=3, viewers=3), _model("b", num_users=2, viewers=2)],
            2: [_model("c", num_users=1, viewers=1)],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1, timeout_seconds=5),
            page_size=2,
        )
        self.assertFalse(snap.is_complete)
        last = self.service.slice_snapshot(
            snap.pool_id,
            page=1,
            limit=50,
            source="chaturbate",
            sort="viewers_desc",
        )
        self.assertFalse(last["has_more"])
        self.assertFalse(last["is_complete"])
        self.assertIsNotNone(last["partial_reason"])


class RankingB31ChaturbateAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_page1_build_page2_slice_no_extra_roomlist(self):
        src = FixtureRoomlist(
            {
                1: [_room("p1_a", 40), _room("p1_b", 30), _room("p1_c", 20)],
                2: [_room("p2_high", 500), _room("p2_b", 15)],
                3: [_room("p3_a", 5)],
            }
        )
        service = DiscoverRankingService(ttl_seconds=60)
        fetch = make_chaturbate_page_fetcher(src)
        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=fetch,
            sort="viewers_desc",
            page_size=3,
        )
        calls_after_build = list(src.calls)
        self.assertTrue(snap.pool_id)
        self.assertEqual(RankingMode.MULTI_PAGE_GLOBAL.value, snap.ranking_mode)

        page1 = service.slice_snapshot(
            snap.pool_id,
            page=1,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        page2 = service.slice_snapshot(
            snap.pool_id,
            page=2,
            limit=2,
            source="chaturbate",
            sort="viewers_desc",
        )
        self.assertEqual(calls_after_build, src.calls)
        self.assertEqual(page1["pool_id"], page2["pool_id"])
        self.assertEqual("p2_high", page1["models"][0]["username"])
        ids = [m["stable_id"] for m in page1["models"]] + [
            m["stable_id"] for m in page2["models"]
        ]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_new_pool_different_pool_id_after_ttl(self):
        src = FixtureRoomlist({1: [_room("solo", 11)]})
        service = DiscoverRankingService(ttl_seconds=0.05)
        fetch = make_chaturbate_page_fetcher(src)
        snap1 = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=fetch,
            sort="viewers_desc",
            page_size=1,
            budget=RankingPoolBudget(max_pages=1, max_requests=1, timeout_seconds=5),
        )
        await asyncio.sleep(0.08)
        snap2 = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=fetch,
            sort="viewers_desc",
            page_size=1,
            budget=RankingPoolBudget(max_pages=1, max_requests=1, timeout_seconds=5),
        )
        self.assertNotEqual(snap1.pool_id, snap2.pool_id)


class RankingB31ErrorPayloadTests(unittest.TestCase):
    def test_error_to_dict_shape(self):
        err = RankingPoolFilterMismatch(
            "pl_abc",
            mismatch_field="filters_hash",
            expected="fh_1",
            actual="fh_2",
        )
        payload = err.to_dict()
        self.assertEqual("ranking_pool_filter_mismatch", payload["error"])
        self.assertEqual("pl_abc", payload["pool_id"])
        self.assertEqual(1, payload["restart_from_page"])
        self.assertFalse(payload["retryable"])
        self.assertEqual("filters_hash", payload["mismatch_field"])

    def test_slice_pool_past_end_via_helper(self):
        from app.services.discover_ranking import RankingSnapshot

        snap = RankingSnapshot(
            pool_id="pl_x",
            filters_hash="fh_x",
            generated_at=time.time(),
            expires_at=time.time() + 60,
            ranking_mode="multi_page_global",
            sort="viewers_desc",
            source="chaturbate",
            canonical_category="all",
            candidate_count=1,
            pages_scanned=1,
            requests_used=1,
            is_complete=True,
            partial_reason=None,
            models=[_model("only", num_users=1, viewers=1)],
        )
        out = slice_pool(snap, page=2, limit=24)
        self.assertEqual([], out["models"])
        self.assertFalse(out["has_more"])
        self.assertTrue(out["is_complete"])

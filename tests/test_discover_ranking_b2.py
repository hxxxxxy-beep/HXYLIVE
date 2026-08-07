"""B2 ranking service skeleton — unit tests only (no /api/discover wiring)."""

from __future__ import annotations

import asyncio
import time
import unittest

from app.services.discover_ranking import (
    DiscoverRankingService,
    RankingPoolBudget,
    RankingMode,
    compute_filters_hash,
    filter_models_by_tags,
    model_matches_all_tags,
    rank_models,
    slice_pool,
    stable_id_for_model,
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


class FiltersHashTests(unittest.TestCase):
    def test_stable_for_same_semantics(self):
        a = compute_filters_hash(
            source="Chaturbate",
            canonical_category="Female",
            language="EN",
            tags=["a", "b"],
            sort="viewers_desc",
            search="x",
        )
        b = compute_filters_hash(
            source="chaturbate",
            canonical_category="female",
            language="en",
            tags=["a", "b"],
            sort="viewers_desc",
            search="x",
        )
        self.assertEqual(a, b)

    def test_tags_order_does_not_change_hash(self):
        a = compute_filters_hash(source="chaturbate", tags=["zoo", "alpha", "mid"])
        b = compute_filters_hash(source="chaturbate", tags=["mid", "alpha", "zoo"])
        self.assertEqual(a, b)

    def test_filter_changes_change_hash(self):
        base = dict(source="chaturbate", canonical_category="all", tags=["t"], sort="viewers_desc")
        h0 = compute_filters_hash(**base)
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "source": "twitch"}))
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "canonical_category": "female"}))
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "tags": ["t", "u"]}))
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "sort": "source_default"}))
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "language": "fr"}))
        self.assertNotEqual(h0, compute_filters_hash(**{**base, "search": "q"}))
        self.assertNotEqual(
            h0,
            compute_filters_hash(**{**base, "extra_filters": {"region": "eu"}}),
        )

    def test_hash_excludes_page_progress_fields(self):
        # page/cursor/seen_ids are not parameters — documenting via API shape.
        h = compute_filters_hash(source="chaturbate", tags=["a"])
        self.assertTrue(h.startswith("fh_"))
        self.assertEqual(19, len(h))  # fh_ + 16 hex


class TagFilterHelpersTests(unittest.TestCase):
    def test_model_matches_all_tags_requires_every_tag(self):
        model = _model("a", tags=["French", "outdoor", "hd"])
        self.assertTrue(model_matches_all_tags(model, ["french"]))
        self.assertTrue(model_matches_all_tags(model, ["HD", "french"]))
        self.assertFalse(model_matches_all_tags(model, ["french", "asian"]))
        self.assertFalse(model_matches_all_tags(_model("b", tags=[]), ["french"]))
        self.assertTrue(model_matches_all_tags(model, None))
        self.assertTrue(model_matches_all_tags(model, []))

    def test_filter_models_by_tags_does_not_pad(self):
        models = [
            _model("match_hi", tags=["french"], num_users=90, viewers=90),
            _model("subject_only", tags=["outdoor"], num_users=80, viewers=80),
            _model("match_lo", tags=["French", "hd"], num_users=10, viewers=10),
        ]
        kept = filter_models_by_tags(models, ["french"])
        self.assertEqual(["match_hi", "match_lo"], [m["username"] for m in kept])


class RankModelsTests(unittest.TestCase):
    def test_viewers_desc_global_order_and_stable_tiebreak(self):
        models = [
            _model("b", viewers=50, num_users=50),
            _model("a", viewers=50, num_users=50),
            _model("c", viewers=90, num_users=90),
            _model("missing_only"),  # missing
            _model("padded_zero", viewers=0),  # missing via B1.1
        ]
        ranked = rank_models(models, sort="viewers_desc", source="chaturbate")
        names = [m["username"] for m in ranked]
        self.assertEqual(["c", "a", "b", "missing_only", "padded_zero"], names)
        # Same count: stable_id asc → chaturbate:a before chaturbate:b
        self.assertLess(
            ranked[1]["stable_id"],
            ranked[2]["stable_id"],
        )

    def test_missing_not_treated_as_real_zero(self):
        models = [
            _model("real_zero", num_users=0, viewers=0),
            _model("absent"),
            _model("positive", num_users=3, viewers=3),
        ]
        ranked = rank_models(models, sort="viewers_desc", source="chaturbate")
        self.assertEqual("positive", ranked[0]["username"])
        self.assertEqual("real_zero", ranked[1]["username"])
        self.assertEqual(0, ranked[1]["viewer_count"])
        self.assertEqual("exact", ranked[1]["viewer_count_precision"])
        self.assertEqual("absent", ranked[2]["username"])
        self.assertIsNone(ranked[2]["viewer_count"])
        self.assertEqual("missing", ranked[2]["viewer_count_precision"])

    def test_approximate_keeps_quality_mark(self):
        models = [
            _model("abbr", viewer_count_raw="1.2k", viewer_count_source="html_abbrev"),
            _model("exact_low", num_users=100, viewers=100),
        ]
        ranked = rank_models(models, sort="viewers_desc", source="twitch")
        self.assertEqual("abbr", ranked[0]["username"])
        self.assertEqual("approximate", ranked[0]["viewer_count_precision"])
        self.assertFalse(ranked[0]["viewer_count_reliable"])


class RankingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = DiscoverRankingService(ttl_seconds=60)

    def _pages_fixture(self):
        """Three upstream pages; page2 has a higher viewer than page1 floor."""
        return {
            1: [
                _model("p1a", num_users=40, viewers=40),
                _model("p1b", num_users=30, viewers=30),
                _model("dup", num_users=10, viewers=10),
            ],
            2: [
                _model("p2_high", num_users=100, viewers=100),
                _model("dup", num_users=10, viewers=10),  # duplicate stable_id
                _model("p2c", num_users=20, viewers=20),
            ],
            3: [
                _model("p3a", num_users=5, viewers=5),
            ],
        }

    def _make_fetcher(self, pages, counter=None, delay=0.0):
        async def fetch_page(page, limit):
            if counter is not None:
                counter["n"] = counter.get("n", 0) + 1
            if delay:
                await asyncio.sleep(delay)
            batch = list(pages.get(page, []))
            return batch[:limit]

        return fetch_page

    async def test_merge_dedupe_and_global_sort(self):
        pages = self._pages_fixture()
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=3, max_requests=3, timeout_seconds=5),
            page_size=3,
        )
        names = [m["username"] for m in snap.models]
        self.assertEqual("p2_high", names[0])
        self.assertEqual(1, names.count("dup"))
        self.assertEqual(RankingMode.MULTI_PAGE_GLOBAL.value, snap.ranking_mode)
        self.assertNotEqual(RankingMode.PROVIDER_NATIVE.value, snap.ranking_mode)
        self.assertTrue(snap.pool_id.startswith("pl_"))

    async def test_slice_no_cross_page_dup_and_stable_pool(self):
        pages = {
            1: [_model(f"u{i}", num_users=100 - i, viewers=100 - i) for i in range(4)],
            2: [],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=2),
            page_size=4,
        )
        s1 = slice_pool(snap, page=1, limit=2)
        s2 = slice_pool(snap, page=2, limit=2)
        ids1 = {m["stable_id"] for m in s1["models"]}
        ids2 = {m["stable_id"] for m in s2["models"]}
        self.assertEqual(set(), ids1 & ids2)
        self.assertEqual(s1["pool_id"], s2["pool_id"])
        self.assertTrue(s1["has_more"])
        self.assertFalse(s2["has_more"])

        again = slice_pool(snap, page=1, limit=2)
        self.assertEqual(
            [m["stable_id"] for m in s1["models"]],
            [m["stable_id"] for m in again["models"]],
        )

    async def test_same_pool_id_order_stable_across_reads(self):
        pages = self._pages_fixture()
        snap1 = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=3, max_requests=3),
            page_size=3,
        )
        snap2 = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=3, max_requests=3),
            page_size=3,
        )
        self.assertEqual(snap1.pool_id, snap2.pool_id)
        self.assertEqual(
            [m["stable_id"] for m in snap1.models],
            [m["stable_id"] for m in snap2.models],
        )

    async def test_ttl_expiry_rebuilds(self):
        service = DiscoverRankingService(ttl_seconds=0.05)
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        counter = {"n": 0}
        snap1 = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages, counter=counter),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertEqual(1, counter["n"])
        await asyncio.sleep(0.08)
        snap2 = await service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages, counter=counter),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertEqual(2, counter["n"])
        self.assertNotEqual(snap1.pool_id, snap2.pool_id)

    async def test_single_flight_builds_once(self):
        pages = {1: [_model("a", num_users=9, viewers=9)]}
        counter = {"n": 0}
        started = asyncio.Event()

        async def slow_fetch(page, limit):
            counter["n"] += 1
            started.set()
            await asyncio.sleep(0.05)
            return pages[1]

        service = DiscoverRankingService(ttl_seconds=60)
        t1 = asyncio.create_task(
            service.build_pool(
                source="chaturbate",
                fetch_page=slow_fetch,
                sort="viewers_desc",
                budget=RankingPoolBudget(max_pages=1, max_requests=1),
            )
        )
        await started.wait()
        t2 = asyncio.create_task(
            service.build_pool(
                source="chaturbate",
                fetch_page=slow_fetch,
                sort="viewers_desc",
                budget=RankingPoolBudget(max_pages=1, max_requests=1),
            )
        )
        s1, s2 = await asyncio.gather(t1, t2)
        self.assertEqual(1, counter["n"])
        self.assertEqual(s1.pool_id, s2.pool_id)

    async def test_max_requests_partial(self):
        pages = self._pages_fixture()
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=5, max_requests=1, timeout_seconds=5),
            page_size=3,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_requests", snap.partial_reason)
        self.assertEqual(1, snap.requests_used)

    async def test_max_pages_partial(self):
        pages = {
            1: [_model("a", num_users=3, viewers=3), _model("b", num_users=2, viewers=2)],
            2: [_model("c", num_users=1, viewers=1), _model("d", num_users=1, viewers=1)],
            3: [_model("e", num_users=1, viewers=1)],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=2, max_requests=5, timeout_seconds=5),
            page_size=2,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_pages", snap.partial_reason)
        self.assertEqual(2, snap.pages_scanned)

    async def test_timeout_partial(self):
        async def slow_fetch(page, limit):
            await asyncio.sleep(0.2)
            return [_model("late", num_users=1, viewers=1)]

        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=slow_fetch,
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=3, max_requests=3, timeout_seconds=0.05),
            page_size=1,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("timeout", snap.partial_reason)

    async def test_default_not_provider_native(self):
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertEqual(RankingMode.MULTI_PAGE_GLOBAL.value, snap.ranking_mode)

    async def test_provider_native_only_when_explicit(self):
        pages = {1: [_model("a", num_users=1, viewers=1)]}
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            ranking_mode=RankingMode.PROVIDER_NATIVE.value,
            budget=RankingPoolBudget(max_pages=1, max_requests=1),
        )
        self.assertEqual(RankingMode.PROVIDER_NATIVE.value, snap.ranking_mode)

    async def test_stable_id_helper(self):
        self.assertEqual(
            "chaturbate:alice",
            stable_id_for_model({"username": "Alice", "source_type": "Chaturbate"}),
        )

    async def test_tag_pool_keeps_matches_only_and_stays_short(self):
        """Load N pages, keep tag matches only — never pad with non-matches."""
        pages = {
            1: [
                _model("m1", tags=["french"], num_users=50, viewers=50),
                _model("x1", tags=["asian"], num_users=200, viewers=200),
                _model("x2", tags=[], num_users=180, viewers=180),
            ],
            2: [
                _model("m2", tags=["French", "hd"], num_users=40, viewers=40),
                _model("x3", tags=["outdoor"], num_users=150, viewers=150),
                _model("x4", tags=["teen"], num_users=140, viewers=140),
            ],
            3: [
                _model("x5", tags=["squirt"], num_users=130, viewers=130),
                _model("x6", tags=[], num_users=120, viewers=120),
                _model("x7", tags=["latin"], num_users=110, viewers=110),
            ],
        }
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            tags=["french"],
            budget=RankingPoolBudget(max_pages=3, max_requests=3, timeout_seconds=5),
            page_size=3,
        )
        names = [m["username"] for m in snap.models]
        self.assertEqual(["m1", "m2"], names)
        self.assertEqual(2, snap.candidate_count)
        self.assertEqual(3, snap.pages_scanned)
        # Page budget exhausted → next batch may continue; pool is short on purpose.
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_pages", snap.partial_reason)
        for model in snap.models:
            self.assertTrue(model_matches_all_tags(model, ["french"]))

    async def test_tag_pool_scans_full_budget_despite_raw_volume(self):
        """Tagged pools must not early-stop on unfiltered volume (All-style pages)."""
        pages = {}
        for i in range(1, 5):
            batch = [
                _model(
                    f"p{i}_x{j}",
                    tags=["other"],
                    num_users=900 - i * 10 - j,
                    viewers=900 - i * 10 - j,
                )
                for j in range(9)
            ]
            batch.append(
                _model(
                    f"p{i}_match",
                    tags=["french"],
                    num_users=i,
                    viewers=i,
                )
            )
            pages[i] = batch
        snap = await self.service.build_pool(
            source="chaturbate",
            fetch_page=self._make_fetcher(pages),
            sort="viewers_desc",
            tags=["french"],
            budget=RankingPoolBudget(
                max_pages=4,
                max_requests=4,
                timeout_seconds=5,
                pool_limit=10,
            ),
            page_size=10,
        )
        # Untagged early-stop would halt at ~20 raw rows (page 2). Tags force full budget.
        self.assertEqual(4, snap.pages_scanned)
        self.assertEqual(4, snap.candidate_count)
        self.assertEqual(
            [f"p{i}_match" for i in range(4, 0, -1)],
            [m["username"] for m in snap.models],
        )


if __name__ == "__main__":
    unittest.main()

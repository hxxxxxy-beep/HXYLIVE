"""Global viewers_desc ranking for Chaturbate / Bilibili / All (10 pages / 240)."""

from __future__ import annotations

import unittest

from app.api import discover
from app.providers.base import BaseProvider, ProviderCapabilities
from app.services import discover_ranking_wire as wire
from app.services.discover_ranking import (
    DEFAULT_MAX_PAGES,
    DEFAULT_POOL_LIMIT,
    DiscoverRankingService,
)


class _Provider(BaseProvider):
    def __init__(self, source_type, models=None):
        super().__init__()
        self.source_type = source_type
        self.display_name = source_type.title()
        self.capabilities = ProviderCapabilities(
            can_discover=True,
            can_follow=True,
            can_stream=True,
            can_record=True,
        )
        self._models = models or []
        self.calls = []

    async def list_live_models(self, **kwargs):
        page = int(kwargs.get("page") or 1)
        limit = int(kwargs.get("limit") or 24)
        search = str(kwargs.get("search") or "").strip().lower()
        self.calls.append((page, limit, search))
        pool = self._models
        if search:
            pool = [
                m
                for m in self._models
                if search in str(m.get("username") or "").lower()
                or search in str(m.get("display_name") or "").lower()
            ]
        start = (page - 1) * limit
        batch = pool[start : start + limit]
        models = [dict(m, source_type=self.source_type) for m in batch]
        return {
            "models": models,
            "total": len(pool),
            "page": page,
            "limit": limit,
            "total_pages": max(1, (len(pool) + limit - 1) // limit),
        }


class _Registry:
    def __init__(self, providers):
        self.providers = {p.source_type: p for p in providers}

    def all(self):
        return list(self.providers.values())

    def has(self, source_type):
        return source_type in self.providers

    def get(self, source_type):
        return self.providers[source_type]


def _model(username: str, viewers: int, source: str, tags=None):
    return {
        "username": username,
        "display_name": username,
        "viewers": viewers,
        "is_online": True,
        "room_status": "public",
        "source_type": source,
        "tags": list(tags or []),
    }


class GlobalRankingEligibilityTests(unittest.TestCase):
    def test_budget_defaults(self):
        self.assertEqual(10, DEFAULT_MAX_PAGES)
        self.assertEqual(240, DEFAULT_POOL_LIMIT)
        budget = wire.default_ranking_budget()
        self.assertEqual(10, budget.max_pages)
        self.assertEqual(240, budget.pool_limit)

    def test_eligible_sources(self):
        # Kill switch: multi_page_global pools disabled for All/CB/Bili.
        self.assertFalse(wire._GLOBAL_VIEWER_RANKING_ENABLED)
        self.assertFalse(wire.is_global_ranking_eligible(None, "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("", "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("all", "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("chaturbate", "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("bilibili", "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("twitch", "viewers_desc"))
        self.assertFalse(wire.is_global_ranking_eligible("bilibili", "viewers"))
        self.assertFalse(wire.is_global_ranking_eligible("all", "viewers_desc", "soficb"))
        # B4 helper remains defined for offline unit tests of pool builders.
        self.assertTrue(wire.is_b4_ranking_eligible("chaturbate", "viewers_desc"))


class GlobalRankingDiscoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cb = _Provider(
            "chaturbate",
            [
                _model("cb_low", 10, "chaturbate"),
                _model("cb_high", 100, "chaturbate"),
                _model("cb_mid", 50, "chaturbate"),
            ],
        )
        self.bili = _Provider(
            "bilibili",
            [
                _model("bi_a", 80, "bilibili"),
                _model("bi_b", 20, "bilibili"),
                _model("bi_c", 90, "bilibili"),
            ],
        )
        self.tw = _Provider(
            "twitch",
            [
                _model("tw_a", 70, "twitch"),
                _model("tw_b", 5, "twitch"),
            ],
        )
        self.service = DiscoverRankingService(ttl_seconds=60)
        wire.reset_ranking_wire_for_tests(service=self.service, clear_override=True)
        discover.init(None, None, _Registry([self.cb, self.bili, self.tw]))

    async def asyncTearDown(self):
        wire.reset_ranking_wire_for_tests(clear_override=True)

    async def test_bilibili_viewers_desc_global_order(self):
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="bilibili",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        # Pool disabled → page_local; still sorted by viewers desc.
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result.get("pool_id"))
        names = [m["username"] for m in result["models"]]
        self.assertEqual(["bi_c", "bi_a", "bi_b"], names)
        viewers = [int(m["viewers"]) for m in result["models"]]
        self.assertEqual(viewers, sorted(viewers, reverse=True))

    async def test_all_merges_and_sorts_across_sources(self):
        result = await discover.discover_models(
            page=1,
            limit=24,
            source=None,
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result.get("pool_id"))
        names = [m["username"] for m in result["models"]]
        # 100, 90, 80, 70, 50, 20, 10, 5
        self.assertEqual(
            ["cb_high", "bi_c", "bi_a", "tw_a", "cb_mid", "bi_b", "cb_low", "tw_b"],
            names,
        )
        viewers = [int(m["viewers"]) for m in result["models"]]
        self.assertEqual(viewers, sorted(viewers, reverse=True))

    async def test_all_page2_slices_merged_local_rank(self):
        page1 = await discover.discover_models(
            page=1,
            limit=3,
            source=None,
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        page2 = await discover.discover_models(
            page=2,
            limit=3,
            source=None,
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertIsNone(page1.get("pool_id"))
        self.assertIsNone(page2.get("pool_id"))
        self.assertEqual(
            ["tw_a", "cb_mid", "bi_b"],
            [m["username"] for m in page2["models"]],
        )
        self.assertEqual(
            ["cb_high", "bi_c", "bi_a"],
            [m["username"] for m in page1["models"]],
        )

    async def test_tags_keep_matches_only_no_padding(self):
        self.cb._models = [
            _model("cb_match", 30, "chaturbate", tags=["french"]),
            _model("cb_noise", 999, "chaturbate", tags=["asian"]),
            _model("cb_subject", 800, "chaturbate", tags=[]),
        ]
        self.bili._models = [
            _model("bi_noise", 700, "bilibili", tags=["英雄联盟"]),
            _model("bi_match", 20, "bilibili", tags=["french", "hd"]),
        ]
        self.tw._models = [
            _model("tw_noise", 600, "twitch", tags=["english"]),
        ]
        result = await discover.discover_models(
            page=1,
            limit=24,
            source=None,
            gender=None,
            search=None,
            tags="french",
            sort="viewers_desc",
            pool_id=None,
        )
        names = [m["username"] for m in result["models"]]
        self.assertEqual(["cb_match", "bi_match"], names)
        self.assertNotIn("cb_noise", names)
        self.assertNotIn("tw_noise", names)

    async def test_search_bypasses_global_ranking_pool(self):
        # Unrelated top rooms must not drown an exact search hit.
        self.cb._models = [
            _model("wasianbby", 50000, "chaturbate"),
            {
                **_model("soficb", 0, "chaturbate"),
                "is_online": False,
                "room_status": "offline",
            },
        ]
        self.bili._models = [_model("bi_noise", 900, "bilibili")]
        self.tw._models = [
            {
                **_model("soficb", 0, "twitch"),
                "is_online": False,
                "room_status": "offline",
            },
        ]
        result = await discover.discover_models(
            page=1,
            limit=24,
            source=None,
            gender=None,
            search="soficb",
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertIsNone(result.get("pool_id"))
        self.assertNotEqual("multi_page_global", result.get("ranking_mode"))
        names = [m["username"] for m in result["models"]]
        self.assertEqual(["soficb", "soficb"], names)
        self.assertNotIn("wasianbby", names)
        self.assertNotIn("bi_noise", names)


if __name__ == "__main__":
    unittest.main()

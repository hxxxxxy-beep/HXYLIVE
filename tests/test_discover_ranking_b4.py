"""B4 Chaturbate viewers_desc opt-in wiring into /api/discover."""

from __future__ import annotations

import asyncio
import unittest

from fastapi import HTTPException

from app.api import discover
from app.providers.base import BaseProvider, ProviderCapabilities
from app.services import discover_ranking_wire as wire
from app.services.discover_ranking import DiscoverRankingService, RankingPoolBudget
from app.services.discover_ranking_chaturbate import make_chaturbate_page_fetcher


def _room(username: str, num_users: int):
    return {
        "username": username,
        "display_name": username,
        "num_users": num_users,
        "current_show": "public",
        "tags": [],
        "gender": "f",
    }


class CountingRoomlist:
    def __init__(self, pages: dict[int, list]):
        self.pages = pages
        self.calls = 0

    async def __call__(self, page: int, limit: int, **kwargs):
        self.calls += 1
        return {"rooms": list(self.pages.get(page, []))[:limit]}


class _Provider(BaseProvider):
    def __init__(self, source_type, models=None, *, fetch_counter=None):
        super().__init__()
        self.source_type = source_type
        self.display_name = source_type
        self.capabilities = ProviderCapabilities(
            can_discover=True,
            can_follow=True,
            can_stream=True,
            can_record=True,
        )
        self._models = models or []
        self.fetch_counter = fetch_counter

    async def list_live_models(self, **kwargs):
        if self.fetch_counter is not None:
            self.fetch_counter["n"] = self.fetch_counter.get("n", 0) + 1
        page = int(kwargs.get("page") or 1)
        limit = int(kwargs.get("limit") or 24)
        start = (page - 1) * limit
        batch = self._models[start : start + limit]
        models = [dict(m, source_type=self.source_type) for m in batch]
        return {
            "models": models,
            "total": len(self._models),
            "page": page,
            "limit": limit,
            "total_pages": max(1, (len(self._models) + limit - 1) // limit),
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


class _SettingsDB:
    def __init__(self, disabled=None):
        self.disabled = list(disabled or [])

    async def get_disabled_providers(self):
        return list(self.disabled)

    async def get_blacklisted_tags(self):
        return []

    async def get_all_models(self):
        return []

    async def get_all_followed(self):
        return []


class B4EligibilityTests(unittest.TestCase):
    def test_only_chaturbate_viewers_desc(self):
        self.assertTrue(wire.is_b4_ranking_eligible("chaturbate", "viewers_desc"))
        self.assertTrue(wire.is_b4_ranking_eligible("Chaturbate", "VIEWERS_DESC"))

    def test_defaults_and_legacy_viewers_do_not_trigger(self):
        self.assertFalse(wire.is_b4_ranking_eligible("chaturbate", None))
        self.assertFalse(wire.is_b4_ranking_eligible("chaturbate", ""))
        self.assertFalse(wire.is_b4_ranking_eligible("chaturbate", "viewers"))
        self.assertFalse(wire.is_b4_ranking_eligible("chaturbate", "source_default"))
        self.assertFalse(wire.is_b4_ranking_eligible(None, "viewers_desc"))
        self.assertFalse(wire.is_b4_ranking_eligible("twitch", "viewers_desc"))
        self.assertFalse(wire.is_b4_ranking_eligible("unknown_source", "viewers_desc"))


@unittest.skipUnless(
    wire._GLOBAL_VIEWER_RANKING_ENABLED,
    "global viewer ranking pool disabled",
)
class B4DiscoverWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.src = CountingRoomlist(
            {
                1: [
                    _room("p1_a", 40),
                    _room("p1_b", 30),
                    _room("dup", 10),
                ],
                2: [
                    _room("p2_high", 500),
                    _room("dup", 10),
                    _room("p2_c", 20),
                ],
                3: [_room("p3_a", 5)],
            }
        )
        self.service = DiscoverRankingService(ttl_seconds=60)
        wire.reset_ranking_wire_for_tests(
            service=self.service,
            page_fetcher=make_chaturbate_page_fetcher(self.src),
        )
        discover.init(None, None, _Registry([_Provider("chaturbate")]))

    async def asyncTearDown(self):
        wire.reset_ranking_wire_for_tests(
            service=DiscoverRankingService(),
            clear_override=True,
        )
        discover.init(None, None, None)

    async def test_page1_returns_pool_and_global_top(self):
        result = await discover.discover_models(
            page=1,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual("multi_page_global", result["ranking_mode"])
        self.assertTrue(str(result["pool_id"]).startswith("pl_"))
        self.assertEqual("viewers_desc", result["requested_sort"])
        self.assertEqual("viewers_desc", result["sort"])
        self.assertIn("filters_hash", result)
        self.assertIn("ranking", result)
        self.assertEqual(result["pool_id"], result["ranking"]["pool_id"])
        self.assertEqual("p2_high", result["models"][0]["username"])
        self.assertEqual(500, result["models"][0]["viewers"])
        self.assertEqual(500, result["models"][0]["viewer_count"])
        self.assertEqual("exact", result["models"][0]["viewer_count_precision"])
        self.assertGreaterEqual(self.src.calls, 1)

    async def test_page2_same_pool_zero_fetch(self):
        page1 = await discover.discover_models(
            page=1,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        calls = self.src.calls
        page2 = await discover.discover_models(
            page=2,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=page1["pool_id"],
        )
        self.assertEqual(calls, self.src.calls)
        self.assertEqual(page1["pool_id"], page2["pool_id"])
        ids1 = {m["username"] for m in page1["models"]}
        ids2 = {m["username"] for m in page2["models"]}
        self.assertEqual(set(), ids1 & ids2)
        self.assertEqual("multi_page_global", page2["ranking_mode"])

    async def test_page2_missing_pool_id_controlled(self):
        with self.assertRaises(HTTPException) as ctx:
            await discover.discover_models(
                page=2,
                limit=2,
                source="chaturbate",
                gender=None,
                search=None,
                tags=None,
                sort="viewers_desc",
                pool_id=None,
            )
        self.assertEqual(400, ctx.exception.status_code)
        self.assertEqual("ranking_pool_id_required", ctx.exception.detail["error"])
        self.assertEqual(1, ctx.exception.detail["restart_from_page"])

    async def test_pool_expired(self):
        wire.reset_ranking_wire_for_tests(
            service=DiscoverRankingService(ttl_seconds=0.05),
            page_fetcher=make_chaturbate_page_fetcher(self.src),
        )
        page1 = await discover.discover_models(
            page=1,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        await asyncio.sleep(0.08)
        with self.assertRaises(HTTPException) as ctx:
            await discover.discover_models(
                page=2,
                limit=2,
                source="chaturbate",
                gender=None,
                search=None,
                tags=None,
                sort="viewers_desc",
                pool_id=page1["pool_id"],
            )
        self.assertEqual(410, ctx.exception.status_code)
        self.assertEqual("ranking_pool_expired", ctx.exception.detail["error"])
        self.assertEqual(1, ctx.exception.detail["restart_from_page"])

    async def test_source_mismatch_on_continuation(self):
        page1 = await discover.discover_models(
            page=1,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        # Force mismatch via wire handle with wrong source gate bypassed — use continue path
        # through service after swapping eligibility is impossible; call handle with same
        # chaturbate but altered category via tags mismatch instead.
        with self.assertRaises(HTTPException) as ctx:
            await wire.handle_b4_discover(
                page=2,
                limit=2,
                pool_id=page1["pool_id"],
                gender="male",  # page1 built with all
                search="",
                tags=[],
                sort="viewers_desc",
            )
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn(
            ctx.exception.detail["error"],
            {
                "ranking_pool_filter_mismatch",
                "ranking_pool_source_mismatch",
            },
        )
        self.assertEqual(1, ctx.exception.detail["restart_from_page"])

    async def test_sort_viewers_stays_page_local(self):
        counter = {"n": 0}
        models = [
            {"username": "a", "viewers": 10, "num_users": 10, "is_online": True, "room_status": "public"},
            {"username": "b", "viewers": 50, "num_users": 50, "is_online": True, "room_status": "public"},
        ]
        discover.init(None, None, _Registry([_Provider("chaturbate", models, fetch_counter=counter)]))
        # Clear B4 fetcher override so we don't accidentally enter pool with fake fetcher
        # — eligibility is False for sort=viewers so wire not called.
        wire.reset_ranking_wire_for_tests(clear_override=True)
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers",
            pool_id=None,
        )
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result["pool_id"])
        self.assertNotEqual("multi_page_global", result["ranking_mode"])

    async def test_default_sort_omitted_path_page_local(self):
        models = [
            {"username": "a", "viewers": 3, "is_online": True, "room_status": "public", "gender": "female"},
        ]
        discover.init(None, None, _Registry([_Provider("chaturbate", models)]))
        wire.reset_ranking_wire_for_tests(clear_override=True)
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers",  # FastAPI default
            pool_id=None,
        )
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result["pool_id"])

    async def test_non_chaturbate_viewers_desc_not_pooled(self):
        models = [
            {"username": "s1", "viewers": 9, "is_online": True, "room_status": "public"},
        ]
        discover.init(None, None, _Registry([_Provider("twitch", models)]))
        wire.reset_ranking_wire_for_tests(clear_override=True)
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="twitch",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result["pool_id"])

    async def test_snapshot_past_end_empty(self):
        page1 = await discover.discover_models(
            page=1,
            limit=50,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        calls = self.src.calls
        far = await discover.discover_models(
            page=99,
            limit=50,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=page1["pool_id"],
        )
        self.assertEqual([], far["models"])
        self.assertFalse(far["has_more"])
        self.assertEqual(calls, self.src.calls)

    async def test_partial_snapshot_semantics(self):
        # Force partial via small budget by building through service then reading via wire
        pages = {
            1: [_room(f"u{i}", 100 - i) for i in range(3)],
            2: [_room(f"v{i}", 50 - i) for i in range(3)],
            3: [_room("w", 1)],
        }
        src = CountingRoomlist(pages)
        service = DiscoverRankingService(ttl_seconds=60)
        wire.reset_ranking_wire_for_tests(
            service=service,
            page_fetcher=make_chaturbate_page_fetcher(src),
        )
        # Monkeypatch budget by calling handle after replacing build path — use
        # service build with max_pages=2 then continue via discover page2.
        from app.services.discover_ranking_chaturbate import build_chaturbate_ranking_pool

        snap = await build_chaturbate_ranking_pool(
            service=service,
            fetch_page=make_chaturbate_page_fetcher(src),
            budget=RankingPoolBudget(max_pages=2, max_requests=2, timeout_seconds=5),
            page_size=3,
        )
        self.assertFalse(snap.is_complete)
        self.assertEqual("max_pages", snap.partial_reason)
        page2 = await discover.discover_models(
            page=2,
            limit=2,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=snap.pool_id,
        )
        self.assertFalse(page2["is_complete"])
        self.assertEqual("max_pages", page2["partial_reason"])
        # has_more is independent
        self.assertIn("has_more", page2)


class B4HttpMappingTests(unittest.TestCase):
    def test_status_mapping(self):
        from app.services.discover_ranking import (
            RankingPoolExpired,
            RankingPoolFilterMismatch,
            RankingPoolNotFound,
            RankingPoolPageOutOfRange,
            RankingPoolSortMismatch,
            RankingPoolSourceMismatch,
        )

        self.assertEqual(410, wire.ranking_pool_http_status(RankingPoolExpired("pl_x")))
        self.assertEqual(404, wire.ranking_pool_http_status(RankingPoolNotFound("pl_x")))
        self.assertEqual(
            409,
            wire.ranking_pool_http_status(
                RankingPoolSourceMismatch("pl_x", expected="chaturbate", actual="cams")
            ),
        )
        self.assertEqual(
            409,
            wire.ranking_pool_http_status(
                RankingPoolSortMismatch("pl_x", expected="viewers_desc", actual="viewers")
            ),
        )
        self.assertEqual(
            409,
            wire.ranking_pool_http_status(
                RankingPoolFilterMismatch("pl_x", mismatch_field="filters_hash")
            ),
        )
        self.assertEqual(
            400,
            wire.ranking_pool_http_status(
                RankingPoolPageOutOfRange("pl_x", page=0, limit=24)
            ),
        )


class B41DisabledProvidersGateTests(unittest.IsolatedAsyncioTestCase):
    """B4.1: disabled_providers must run before ranking pool build/fetch."""

    async def asyncTearDown(self):
        wire.reset_ranking_wire_for_tests(
            service=DiscoverRankingService(),
            clear_override=True,
        )
        discover.init(None, None, None)

    async def test_disabled_chaturbate_viewers_desc_skips_pool_and_fetch(self):
        src = CountingRoomlist({1: [_room("a", 9)]})
        wire.reset_ranking_wire_for_tests(
            service=DiscoverRankingService(ttl_seconds=60),
            page_fetcher=make_chaturbate_page_fetcher(src),
        )
        discover.init(
            None,
            _SettingsDB(disabled=["chaturbate"]),
            _Registry([_Provider("chaturbate")]),
        )
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual([], result["models"])
        self.assertEqual(0, src.calls)
        self.assertNotEqual("multi_page_global", result.get("ranking_mode"))
        self.assertIsNone(result.get("pool_id"))
        self.assertEqual("page_local", result["ranking_mode"])

    @unittest.skipUnless(
        wire._GLOBAL_VIEWER_RANKING_ENABLED,
        "global viewer ranking pool disabled",
    )
    async def test_enabled_chaturbate_viewers_desc_still_enters_b4(self):
        src = CountingRoomlist(
            {
                1: [_room("low", 10), _room("high", 90)],
                2: [],
            }
        )
        wire.reset_ranking_wire_for_tests(
            service=DiscoverRankingService(ttl_seconds=60),
            page_fetcher=make_chaturbate_page_fetcher(src),
        )
        discover.init(
            None,
            _SettingsDB(disabled=[]),
            _Registry([_Provider("chaturbate")]),
        )
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual("multi_page_global", result["ranking_mode"])
        self.assertTrue(str(result["pool_id"]).startswith("pl_"))
        self.assertGreaterEqual(src.calls, 1)
        self.assertEqual("high", result["models"][0]["username"])

    async def test_viewers_desc_stays_page_local_when_pool_disabled(self):
        if wire._GLOBAL_VIEWER_RANKING_ENABLED:
            self.skipTest("pool enabled — covered by test_enabled_chaturbate_viewers_desc_still_enters_b4")
        discover.init(
            None,
            _SettingsDB(disabled=[]),
            _Registry([
                _Provider(
                    "chaturbate",
                    models=[
                        {
                            "username": "low",
                            "viewers": 10,
                            "is_online": True,
                            "room_status": "public",
                        },
                        {
                            "username": "high",
                            "viewers": 90,
                            "is_online": True,
                            "room_status": "public",
                        },
                    ],
                )
            ]),
        )
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers_desc",
            pool_id=None,
        )
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result.get("pool_id"))
        self.assertEqual("high", result["models"][0]["username"])

    async def test_disabled_does_not_affect_non_b4_sort_path_shape(self):
        """Default viewers path still uses shared empty-provider semantics when disabled."""
        wire.reset_ranking_wire_for_tests(clear_override=True)
        discover.init(
            None,
            _SettingsDB(disabled=["chaturbate"]),
            _Registry([_Provider("chaturbate", models=[
                {"username": "x", "viewers": 1, "is_online": True, "room_status": "public"},
            ])]),
        )
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search=None,
            tags=None,
            sort="viewers",
            pool_id=None,
        )
        self.assertEqual([], result["models"])
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result["pool_id"])


if __name__ == "__main__":
    unittest.main()

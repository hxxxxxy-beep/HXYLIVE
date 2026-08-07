"""A P5 — Twitch native dynamic content categories."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from app.discover_category_catalog import (
    build_categories_payload,
    twitch_content_category_item,
)
from app.services import twitch_categories as tc
from app.api import discover
from app.providers.twitch import TwitchProvider


class TwitchCategoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tc.reset_twitch_category_cache_for_tests()

    def tearDown(self):
        tc.reset_twitch_category_cache_for_tests()

    def test_dedupe_by_stable_game_id(self):
        rows = [
            {"id": "509658", "name": "Just Chatting"},
            {"id": "509658", "name": "Just Chatting Dup"},
            {"id": "33214", "name": "Fortnite"},
            {"id": "", "name": "Bad"},
            {"id": "abc", "name": "NonNumeric"},
            {"id": "21779", "name": ""},
        ]
        out = tc.dedupe_twitch_games(rows)
        self.assertEqual(
            [
                {"game_id": "509658", "name": "Just Chatting"},
                {"game_id": "33214", "name": "Fortnite"},
            ],
            out,
        )

    def test_normalize_game_id(self):
        self.assertEqual("509658", tc.normalize_twitch_game_id("509658"))
        self.assertIsNone(tc.normalize_twitch_game_id("Just Chatting"))
        self.assertIsNone(tc.normalize_twitch_game_id(""))

    async def test_list_categories_returns_curated_allowlist(self):
        calls = {"n": 0}

        async def fake_fetcher(*, first=20):
            calls["n"] += 1
            return [
                {"id": "509658", "name": "Just Chatting"},
                {"id": "33214", "name": "Fortnite"},
            ]

        with patch.dict(
            os.environ,
            {"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "sec"},
            clear=False,
        ):
            first = await tc.list_twitch_content_categories(helix_fetcher=fake_fetcher)
            second = await tc.list_twitch_content_categories(helix_fetcher=fake_fetcher)
        self.assertEqual(1, calls["n"])
        self.assertEqual(
            [
                {"game_id": "509659", "name": "ASMR"},
                {"game_id": "509658", "name": "Just Chatting"},
                {"game_id": "21779", "name": "LOL"},
                {"game_id": "509672", "name": "IRL"},
            ],
            first,
        )
        self.assertEqual(first, second)
        self.assertNotIn("33214", [x["game_id"] for x in first])

    async def test_list_categories_helix_failure_still_returns_curated(self):
        async def boom(*, first=20):
            raise RuntimeError("helix down")

        with patch.dict(
            os.environ,
            {"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "sec"},
            clear=False,
        ):
            out = await tc.list_twitch_content_categories(helix_fetcher=boom)
        self.assertEqual(
            ["509659", "509658", "21779", "509672"],
            [x["game_id"] for x in out],
        )

    def test_curated_constant_order(self):
        rows = tc.curated_twitch_content_categories()
        self.assertEqual(
            ["ASMR", "Just Chatting", "LOL", "IRL"],
            [r["name"] for r in rows],
        )


class TwitchCategoryCatalogTests(unittest.TestCase):
    def test_without_games_no_all_no_gender_formal(self):
        payload = build_categories_payload("twitch", twitch_games=[])
        keys = {c["canonical_key"] for c in payload["categories"]}
        self.assertEqual(set(), keys)
        self.assertNotIn("all", keys)
        for banned in ("female", "male", "trans", "couple"):
            self.assertNotIn(banned, keys)
            self.assertFalse(
                any(c.get("display_label") == banned.title() for c in payload["categories"])
            )
        unavailable = {c["canonical_key"] for c in payload["unavailable_categories"]}
        self.assertTrue({"female", "male", "trans", "couple"}.issubset(unavailable))

    def test_with_games_builds_content_categories(self):
        payload = build_categories_payload(
            "twitch",
            twitch_games=[
                {"game_id": "509658", "name": "Just Chatting"},
                {"game_id": "509658", "name": "Dup"},
                {"game_id": "33214", "name": "Fortnite"},
            ],
        )
        formal = payload["categories"]
        keys = [c["canonical_key"] for c in formal]
        self.assertEqual(["game:509658", "game:33214"], keys)
        self.assertNotIn("all", keys)
        jc = formal[0]
        self.assertEqual("Just Chatting", jc["display_label"])
        self.assertEqual("game_id", jc["request_param"])
        self.assertEqual("509658", jc["request_value"])
        self.assertEqual("content", jc["filter_scope"])
        self.assertEqual("content", jc["category_type"])
        self.assertTrue(jc["available"])
        self.assertEqual("verified", jc["readiness"])
        for banned in ("female", "male", "trans", "couple"):
            self.assertNotIn(banned, keys)

    def test_content_item_helper(self):
        item = twitch_content_category_item("21779", "League of Legends")
        self.assertEqual("game:21779", item["canonical_key"])
        self.assertEqual("League of Legends", item["display_label"])
        self.assertEqual("game_id", item["request_param"])


class TwitchCategoriesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_injects_games_and_excludes_gender(self):
        async def fake_list():
            return [
                {"game_id": "509658", "name": "Just Chatting"},
                {"game_id": "33214", "name": "Fortnite"},
            ]

        with patch.object(discover, "list_twitch_content_categories", AsyncMock(side_effect=fake_list)):
            payload = await discover.discover_categories(source="twitch")
        keys = [c["canonical_key"] for c in payload["categories"]]
        self.assertEqual(["game:509658", "game:33214"], keys)
        self.assertNotIn("all", keys)
        for banned in ("female", "male", "trans", "couple"):
            self.assertNotIn(banned, keys)
        self.assertTrue(all(c["available"] and c["readiness"] == "verified" for c in payload["categories"]))

    async def test_endpoint_empty_games_no_all(self):
        with patch.object(discover, "list_twitch_content_categories", AsyncMock(return_value=[])):
            payload = await discover.discover_categories(source="twitch")
        self.assertEqual([], [c["canonical_key"] for c in payload["categories"]])
        self.assertNotIn("all", [c["canonical_key"] for c in payload["categories"]])


class TwitchGameIdFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_live_models_passes_override_game_id(self):
        provider = TwitchProvider(
            "twitch", "Twitch", "https://www.twitch.tv/{username}", ("twitch.tv",)
        )
        provider.client_id = "cid"
        provider.client_secret = "sec"
        provider.game_id = "509659"
        seen = {}

        async def capture(*, page, limit, game_id=None):
            seen["game_id"] = game_id
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "empty",
            }

        provider._list_live_catalogue = capture  # type: ignore
        await provider.list_live_models(page=1, limit=24, game_id="33214")
        self.assertEqual("33214", seen["game_id"])

    async def test_default_all_keeps_env_game_id_pool(self):
        provider = TwitchProvider(
            "twitch", "Twitch", "https://www.twitch.tv/{username}", ("twitch.tv",)
        )
        provider.client_id = "cid"
        provider.client_secret = "sec"
        provider.game_id = "509659"
        seen = {}

        async def capture(*, page, limit, game_id=None):
            seen["game_id"] = game_id
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
                "provider_status": "empty",
            }

        provider._list_live_catalogue = capture  # type: ignore
        await provider.list_live_models(page=1, limit=24)
        self.assertIsNone(seen["game_id"])

    async def test_stream_model_exposes_stable_game_id(self):
        model = TwitchProvider._stream_model({
            "user_id": "1",
            "user_login": "a",
            "user_name": "A",
            "type": "live",
            "viewer_count": 1,
            "thumbnail_url": "https://x/{width}x{height}.jpg",
            "tags": [],
            "language": "en",
            "game_id": "33214",
            "game_name": "Fortnite",
            "title": "t",
            "started_at": "",
        })
        self.assertEqual("33214", model["game_id"])
        self.assertEqual("Fortnite", model["game_name"])
        self.assertEqual("Fortnite", model["category"])
        self.assertEqual("en", model["language"])

    async def test_unique_pools_isolated_by_game_id(self):
        provider = TwitchProvider(
            "twitch", "Twitch", "https://www.twitch.tv/{username}", ("twitch.tv",)
        )
        provider.client_id = "cid"
        provider.client_secret = "sec"
        provider.game_id = "509659"
        calls = []

        async def page(*, first, after=None, user_login=None, game_id=None, retry_auth=True):
            calls.append(game_id)
            uid = "1" if game_id == "509659" else "2"
            login = "a" if game_id == "509659" else "b"
            return {
                "data": [{
                    "user_id": uid,
                    "user_login": login,
                    "user_name": login,
                    "type": "live",
                    "viewer_count": 10,
                    "thumbnail_url": "https://x/{width}x{height}.jpg",
                    "tags": [],
                    "language": "en",
                    "game_id": game_id,
                    "game_name": "G",
                    "title": "t",
                    "started_at": "",
                }],
                "pagination": {},
            }

        provider._helix_page = page  # type: ignore
        provider._enrich_models = AsyncMock(side_effect=lambda models: models)
        r1 = await provider.list_live_models(page=1, limit=1)
        r2 = await provider.list_live_models(page=1, limit=1, game_id="33214")
        self.assertEqual("509659", calls[0])
        self.assertEqual("33214", calls[1])
        self.assertEqual("a", r1["models"][0]["username"])
        self.assertEqual("b", r2["models"][0]["username"])
        # Separate pool keys — default catalogue cursor not reset by category pool.
        self.assertIn(("509659", "", 1, 24), provider._twitch_unique_pools)
        self.assertIn(("33214", "", 1, 24), provider._twitch_unique_pools)


class TwitchFrontendContractP5Tests(unittest.TestCase):
    def test_js_supports_game_id_not_gender_for_content(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        helpers = (root / "static" / "discover_categories.js").read_text(encoding="utf-8")
        js = (root / "static" / "discover.js").read_text(encoding="utf-8")
        html = (root / "static" / "discover.html").read_text(encoding="utf-8")
        self.assertIn("game_id: true", helpers)
        self.assertIn("discoverGameId", helpers)
        self.assertIn("Never silently map content/language/tag into gender", helpers)
        self.assertIn("params.set('game_id'", js)
        self.assertIn("resetDiscoverListState", js)
        self.assertIn("discover_categories.js?v=9", html)
        self.assertIn("discover.js?v=72", html)
        self.assertNotIn('data-gender="female"', html)


if __name__ == "__main__":
    unittest.main()

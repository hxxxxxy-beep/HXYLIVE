import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import discover
from app.discover_category_catalog import CONTRACT_VERSION, SCHEMA_VERSION


class DiscoverCategoriesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_twitch_categories_endpoint(self):
        with patch.object(
            discover,
            "list_twitch_content_categories",
            AsyncMock(return_value=[
                {"game_id": "509658", "name": "Just Chatting"},
                {"game_id": "33214", "name": "Fortnite"},
            ]),
        ):
            payload = await discover.discover_categories(source="twitch")
        self.assertEqual(CONTRACT_VERSION, payload["contract_version"])
        self.assertEqual(SCHEMA_VERSION, payload["schema_version"])
        keys = [c["canonical_key"] for c in payload["categories"]]
        self.assertEqual(["game:509658", "game:33214"], keys)
        self.assertNotIn("all", keys)
        labels = {c["display_label"] for c in payload["categories"]}
        self.assertTrue(labels.isdisjoint({"Female", "Male", "Trans", "Couple", "All"}))
        game = payload["categories"][0]
        self.assertEqual("game_id", game["request_param"])
        self.assertEqual("509658", game["request_value"])
        self.assertEqual("content", game["filter_scope"])

    async def test_bilibili_categories_endpoint(self):
        with patch.object(
            discover,
            "list_bilibili_parent_areas",
            AsyncMock(
                return_value=[
                    {"parent_area_id": "9", "name": "虚拟主播"},
                    {"parent_area_id": "1", "name": "娱乐"},
                ]
            ),
        ):
            payload = await discover.discover_categories(source="bilibili")
        self.assertEqual("bilibili", payload["source"])
        keys = [c["canonical_key"] for c in payload["categories"]]
        self.assertEqual(["parent_area:9", "parent_area:1"], keys)
        self.assertNotIn("all", keys)
        area = payload["categories"][0]
        self.assertEqual("parent_area_id", area["request_param"])
        self.assertEqual("9", area["request_value"])
        self.assertEqual("虚拟主播", area["display_label"])
        labels = {c["display_label"] for c in payload["categories"]}
        self.assertTrue(labels.isdisjoint({"Female", "Male", "Trans", "Couple", "All"}))

    async def test_chaturbate_categories_endpoint_formal_only_available(self):
        payload = await discover.discover_categories(source="chaturbate")
        keys = {c["canonical_category"] for c in payload["categories"]}
        self.assertEqual({"all", "female", "male", "trans", "couple"}, keys)
        for key in ("female", "male", "trans", "couple"):
            item = next(c for c in payload["categories"] if c["canonical_category"] == key)
            self.assertEqual("gender", item["request_param"])
            self.assertEqual(key, item["request_value"])
            self.assertEqual("primary", item["filter_scope"])
            self.assertEqual("verified", item["readiness"])
            self.assertTrue(item["available"])

    async def test_chaturbate_ranking_hints_b4_projection(self):
        payload = await discover.discover_categories(source="chaturbate")
        hints = payload["ranking_hints"]
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertEqual("num_users", hints["evidence_source"])
        self.assertEqual("exact", hints["viewer_count_precision_default"])
        self.assertTrue(hints["viewer_count_reliable"])
        self.assertTrue(hints["supports_viewer_count"])
        self.assertEqual("verified", hints["implementation_status"])
        self.assertEqual(["page_local"], hints["ranking_modes"])
        self.assertNotIn("multi_page_global", hints["ranking_modes"])
        self.assertNotIn("provider_native", hints["ranking_modes"])

    async def test_twitch_ranking_hints_no_viewers_desc(self):
        payload = await discover.discover_categories(source="twitch")
        hints = payload["ranking_hints"]
        self.assertNotIn("viewers_desc", hints["supported_sort_modes"])
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertFalse(hints["supports_viewer_count"])

    async def test_unknown_source_controlled_error(self):
        with self.assertRaises(HTTPException) as ctx:
            await discover.discover_categories(source="nope_source_xyz")
        self.assertEqual(404, ctx.exception.status_code)
        detail = ctx.exception.detail
        self.assertEqual("unknown_source", detail["error"])
        self.assertEqual(CONTRACT_VERSION, detail["contract_version"])
        self.assertEqual([], detail["categories"])
        hints = detail["ranking_hints"]
        self.assertNotIn("viewers_desc", hints.get("supported_sort_modes") or [])
        self.assertEqual([], hints.get("ranking_modes") or [])

    async def test_supported_sources_still_listed_via_catalog(self):
        with patch.object(
            discover,
            "list_bilibili_parent_areas",
            AsyncMock(return_value=[{"parent_area_id": "9", "name": "虚拟主播"}]),
        ):
            for source in ("twitch", "chaturbate", "bilibili"):
                payload = await discover.discover_categories(source=source)
                self.assertEqual(source, payload["source"])
                self.assertTrue(payload["categories"])
                first = payload["categories"][0]["canonical_category"]
                if source == "chaturbate":
                    self.assertEqual("all", first)
                elif source == "bilibili":
                    self.assertEqual("parent_area:9", first)
                else:
                    self.assertNotEqual("all", first)
                    self.assertTrue(str(first).startswith("game:"))
                for item in payload["categories"]:
                    self.assertTrue(item["available"])
                    if source in {"twitch", "bilibili"}:
                        self.assertNotEqual("all", item["canonical_category"])


if __name__ == "__main__":
    unittest.main()

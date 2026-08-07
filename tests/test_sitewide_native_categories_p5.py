"""Site-wide A P5 — formal categories gate for every Discover source."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.discover_category_catalog import (
    KNOWN_SOURCES,
    build_categories_payload,
    is_formal_deliverable_category,
)
from app.api import discover

ROOT = Path(__file__).resolve().parents[1]


class SitewideFormalGateTests(unittest.TestCase):
    def test_every_source_formal_items_are_deliverable(self):
        for source in KNOWN_SOURCES:
            games = (
                [{"game_id": "509658", "name": "Just Chatting"}]
                if source == "twitch"
                else None
            )
            payload = build_categories_payload(source, twitch_games=games)
            self.assertIsNotNone(payload)
            for item in payload["categories"]:
                self.assertTrue(
                    is_formal_deliverable_category(item),
                    msg=f"{source} leaked non-deliverable {item}",
                )
                self.assertEqual("verified", item["readiness"])
                self.assertTrue(item["available"])
                self.assertTrue(str(item.get("display_label") or item.get("label") or "").strip())
                if item["canonical_key"] in {"female", "male", "trans", "couple"}:
                    self.assertEqual("gender", item["request_param"])

    def test_unavailable_never_in_formal(self):
        for source in KNOWN_SOURCES:
            games = (
                [{"game_id": "509658", "name": "Just Chatting"}]
                if source == "twitch"
                else None
            )
            payload = build_categories_payload(source, twitch_games=games)
            formal_keys = {c["canonical_key"] for c in payload["categories"]}
            for item in payload["unavailable_categories"]:
                self.assertNotIn(item["canonical_key"], formal_keys)
                self.assertFalse(is_formal_deliverable_category(item))

    def test_twitch_hides_gender_from_formal(self):
        payload = build_categories_payload(
            "twitch",
            twitch_games=[{"game_id": "33214", "name": "Fortnite"}],
        )
        keys = {c["canonical_key"] for c in payload["categories"]}
        self.assertEqual({"game:33214"}, keys)
        self.assertNotIn("all", keys)
        for banned in ("female", "male", "trans", "couple"):
            self.assertNotIn(banned, keys)

    def test_chaturbate_keeps_verified_genders(self):
        payload = build_categories_payload("chaturbate")
        keys = {c["canonical_key"] for c in payload["categories"]}
        self.assertTrue({"all", "female", "male", "trans", "couple"}.issubset(keys))


class SitewideFrontendStaticTests(unittest.TestCase):
    def test_no_fixed_five_keys_and_no_grey_unsupported_render(self):
        html = (ROOT / "static" / "discover.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "discover.js").read_text(encoding="utf-8")
        helpers = (ROOT / "static" / "discover_categories.js").read_text(encoding="utf-8")
        self.assertIn('id="categoryFilters"', html)
        self.assertNotIn('onclick="setGender(', html)
        self.assertNotIn('data-gender="female"', html)
        self.assertIn("discover_categories.js?v=9", html)
        self.assertIn("discover.js?v=72", html)
        self.assertIn("never render unsupported", js.lower())
        self.assertIn("no grey disabled pills", js.lower())
        self.assertNotIn("unsupportedClass", js)
        self.assertNotIn("filter-pill' + active + unsupported", js)
        self.assertNotIn('filter-pill" + active + unsupported', js)
        self.assertIn("evaluateCategoryRequestSupport", helpers)
        self.assertIn("filterFormalCategoriesFromPayload", helpers)
        self.assertIn("gate.supported", helpers)
        self.assertIn("preferredDefaultForSource", helpers)
        self.assertIn("safeFallbackItemsForSource", helpers)
        self.assertIn("game:509659", helpers)
        self.assertIn("parent_area:9", helpers)
        self.assertIn("Virtual streamers", helpers)
        self.assertIn("preferredDefaultForSource", js)


class SitewideCategoriesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_sources_match_catalog_formal_gate(self):
        with patch.object(
            discover,
            "list_twitch_content_categories",
            AsyncMock(return_value=[]),
        ), patch.object(
            discover,
            "list_bilibili_parent_areas",
            AsyncMock(return_value=[]),
        ):
            for source in ("twitch", "chaturbate", "bilibili"):
                payload = await discover.discover_categories(source=source)
                for item in payload["categories"]:
                    self.assertTrue(is_formal_deliverable_category(item), msg=source)


if __name__ == "__main__":
    unittest.main()

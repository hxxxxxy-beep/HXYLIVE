"""Discover frontend: page-local browse path, no retired sort UI."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DiscoverFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "discover.js").read_text(encoding="utf-8")
        cls.cat = (ROOT / "static" / "discover_categories.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "static" / "discover.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

    def test_no_sort_control_ui(self):
        self.assertNotIn("人数最多", self.js)
        self.assertNotIn("人数最多", self.html)
        self.assertNotIn("网站默认", self.js)
        self.assertNotIn('id="sortControls"', self.html)
        self.assertNotIn('id="sortFilters"', self.html)
        self.assertNotIn("sortControlsEnabled", self.js)
        self.assertNotIn("selectedSortMode", self.js)

    def test_global_viewer_ranking_pool_removed(self):
        self.assertNotIn("usesGlobalViewerRanking", self.js)
        self.assertNotIn("viewers_desc", self.js)
        self.assertNotIn("pool_id", self.js)
        self.assertNotIn("ranking_start_page", self.js)
        self.assertNotIn("nextBatch", self.js)

    def test_categories_helper_no_longer_offers_sort_ui(self):
        self.assertNotIn("canOfferViewersDescSort", self.cat)
        self.assertNotIn("rankingSortControlsEnabled", self.cat)
        self.assertNotIn("parseRankingHints", self.cat)

    def test_static_cache_token(self):
        self.assertIn("discover.js?v=hxylive", self.html)
        self.assertIn("discover_categories.js?v=hxylive", self.html)
        self.assertIn("styles.css?v=hxylive", self.html)

    def test_source_row_has_no_aggregate_all_button(self):
        self.assertNotIn('data-source="all">All</button>', self.js)
        self.assertNotIn("isAggregateSourceMode", self.js)
        self.assertIn("const DISCOVER_DEFAULT_SOURCE = 'twitch';", self.js)
        self.assertIn("let currentSource = 'twitch';", self.js)

    def test_card_meta_uses_labeled_status_and_viewers(self):
        self.assertIn("discover-live-status", self.js)
        self.assertIn("Viewers:", self.js)
        self.assertIn("Followers:", self.js)
        self.assertIn("discover-tags", self.js)
        self.assertIn("addTagFilter", self.js)
        self.assertNotIn("discover-inline-viewers", self.js)
        self.assertNotIn(" watching</span>", self.js)
        self.assertNotIn("discover-room-id", self.js)
        card_at = self.js.find("return '<div class=\"' + cardClass")
        self.assertGreater(card_at, 0)
        card_chunk = self.js[card_at:card_at + 4000]
        identity_at = card_chunk.find("discover-identity")
        live_at = card_chunk.find("discover-live-status")
        channel_at = card_chunk.find("discover-channel-line")
        tags_at = card_chunk.find("tagsHtml")
        self.assertGreater(identity_at, 0)
        self.assertGreater(live_at, identity_at)
        self.assertGreater(channel_at, live_at)
        self.assertGreater(tags_at, channel_at)

    def test_no_client_resort(self):
        self.assertIn("Never re-sort models client-side", self.js)
        self.assertNotIn("models.sort(", self.js)

    def test_display_tags_prioritize_active_filters(self):
        self.assertIn("function pickDiscoverDisplayTags", self.js)
        self.assertIn("discover-tag-active", self.js)
        self.assertIn("pickDiscoverDisplayTags(model.tags, activeTags)", self.js)
        self.assertNotIn("pickDiscoverDisplayTags(model.tags, activeTags, 3)", self.js)
        self.assertNotIn("model.tags.slice(0, 3)", self.js)


if __name__ == "__main__":
    unittest.main()

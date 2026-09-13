"""Frontend contract tests for Discover empty-page same-page refill."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DiscoverEmptyPageRefillStaticTests(unittest.TestCase):
    def test_discover_js_has_bounded_same_page_refill_contract(self):
        js = (ROOT / "static" / "discover.js").read_text()
        html = (ROOT / "static" / "discover.html").read_text()

        self.assertIn("const DISCOVER_EMPTY_PAGE_RETRY_MAX = 2", js)
        self.assertIn("const DISCOVER_TAG_EMPTY_PAGE_ADVANCE_MAX = 8", js)
        self.assertIn("refillSamePage", js)
        self.assertIn("scheduleSamePageRefill", js)
        self.assertIn("scheduleTagPageAdvance", js)
        self.assertIn("fetchDiscover({ refillSamePage: true })", js)
        self.assertIn("fetchDiscover({ append: true })", js)
        self.assertIn("emptyPageRetryCount = 0", js)
        # page+1 only on append, never on same-page refill
        self.assertIn("requestedPage = currentPage + 1", js)
        self.assertIn("refillSamePage) {\n    requestedPage = Math.max(1, currentPage)", js)
        # has_more=false / unsupported must not refill
        self.assertIn("data.supported === false", js)
        self.assertIn("Category not supported on this platform", js)
        self.assertIn("discover.js?v=hxylive", html)
        self.assertNotIn("discoverHasMoreBatches", js)
        self.assertNotIn("scheduleNextRankingBatch", js)
        # Empty inventory must not stack footer "Loading more/end" with in-grid empty.
        self.assertIn("deferEmptyForRetry", js)
        self.assertIn("Auto-retry / append with an empty grid", js)
        self.assertIn("if (!hasCards) {", js)
        self.assertIn("Final empty result with no cards must replace the in-grid loader.", js)

    def test_empty_page_refill_state_machine(self):
        """Mirror discover.js empty+has_more decision table in pure Python."""

        max_retries = 2

        def decide(models, has_more, supported, retry_count):
            if supported is False:
                return {
                    "has_more": False,
                    "retry": 0,
                    "schedule_refill": False,
                    "advance_page": False,
                }
            if not models and has_more:
                if retry_count < max_retries:
                    return {
                        "has_more": True,
                        "retry": retry_count + 1,
                        "schedule_refill": True,
                        "advance_page": False,
                    }
                return {
                    "has_more": False,
                    "retry": 0,
                    "schedule_refill": False,
                    "advance_page": False,
                }
            if models:
                return {
                    "has_more": has_more,
                    "retry": 0,
                    "schedule_refill": False,
                    "advance_page": True,
                }
            # empty + has_more false (LiveJasmin terminal)
            return {
                "has_more": False,
                "retry": 0,
                "schedule_refill": False,
                "advance_page": False,
            }

        # 1) empty + has_more true → same-page retry
        d1 = decide([], True, True, 0)
        self.assertTrue(d1["schedule_refill"])
        self.assertEqual(1, d1["retry"])
        self.assertFalse(d1["advance_page"])

        # 2) first empty, second non-empty → clear retry, may advance later
        d2 = decide([{"u": 1}], True, True, 1)
        self.assertFalse(d2["schedule_refill"])
        self.assertEqual(0, d2["retry"])
        self.assertTrue(d2["advance_page"])

        # 3) two empties then stop
        d3a = decide([], True, True, 0)
        d3b = decide([], True, True, d3a["retry"])
        d3c = decide([], True, True, d3b["retry"])
        self.assertTrue(d3a["schedule_refill"])
        self.assertTrue(d3b["schedule_refill"])
        self.assertFalse(d3c["schedule_refill"])
        self.assertFalse(d3c["has_more"])

        # 4) empty + has_more false → zero retry
        d4 = decide([], False, True, 0)
        self.assertFalse(d4["schedule_refill"])
        self.assertFalse(d4["has_more"])

        # 5) unsupported → zero retry
        d5 = decide([], True, False, 0)
        self.assertFalse(d5["schedule_refill"])
        self.assertFalse(d5["has_more"])


if __name__ == "__main__":
    unittest.main()

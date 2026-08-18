"""Discover viewer-count fields, sort helpers, and page-local browse API."""

import time
import unittest

from app.api import discover
from app.providers.base import BaseProvider, ProviderCapabilities
from app.services import discover_ranking_types as ranking


class _Provider(BaseProvider):
    def __init__(self, source_type, models):
        super().__init__()
        self.source_type = source_type
        self.display_name = source_type
        self.capabilities = ProviderCapabilities(
            can_discover=True,
            can_follow=True,
            can_stream=True,
            can_record=True,
        )
        self._models = models

    async def list_live_models(self, **kwargs):
        models = [dict(m, source_type=self.source_type) for m in self._models]
        limit = kwargs.get("limit", 24)
        return {
            "models": models,
            "total": len(models),
            "page": kwargs.get("page", 1),
            "limit": limit,
            "total_pages": 1,
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


class DiscoverRankingTypesTests(unittest.TestCase):
    def test_sort_request_enum_values(self):
        self.assertEqual("source_default", ranking.DiscoverSortRequest.SOURCE_DEFAULT.value)
        self.assertEqual("viewers_desc", ranking.DiscoverSortRequest.VIEWERS_DESC.value)

    def test_ranking_mode_enum_values(self):
        self.assertEqual(
            {
                "page_local",
                "provider_native",
                "cached_pool",
                "multi_page_global",
                "unavailable",
            },
            {mode.value for mode in ranking.RankingMode},
        )

    def test_precision_enum_values(self):
        self.assertEqual(
            {"exact", "approximate", "missing", "stale", "unverified"},
            {mode.value for mode in ranking.ViewerCountPrecision},
        )

    def test_normalize_sort_defaults_to_legacy_viewers(self):
        self.assertEqual("viewers", ranking.normalize_sort_param(None))
        self.assertEqual("viewers", ranking.normalize_sort_param(""))
        self.assertEqual("viewers_desc", ranking.normalize_sort_param("viewers_desc"))
        self.assertEqual("source_default", ranking.normalize_sort_param("source_default"))

    def test_viewer_count_reliable_derived_from_precision_only(self):
        self.assertTrue(ranking.derive_viewer_count_reliable("exact"))
        self.assertFalse(ranking.derive_viewer_count_reliable("approximate"))
        self.assertFalse(ranking.derive_viewer_count_reliable("missing"))
        self.assertFalse(ranking.derive_viewer_count_reliable("unverified"))
        self.assertFalse(ranking.derive_viewer_count_reliable("stale"))

    def test_integer_without_evidence_is_unverified(self):
        item = ranking.annotate_model_viewer_fields(
            {"source_type": "chaturbate", "viewers": 42}
        )
        self.assertEqual(42, item["viewers"])
        self.assertEqual(42, item["viewer_count"])
        self.assertEqual("unverified", item["viewer_count_precision"])
        self.assertFalse(item["viewer_count_reliable"])

    def test_abbrev_parse_is_approximate(self):
        item = ranking.annotate_model_viewer_fields(
            {
                "source_type": "twitch",
                "viewer_count_raw": "1.2k",
                "viewer_count_source": "html_abbrev",
            }
        )
        self.assertEqual(1200, item["viewers"])
        self.assertEqual(1200, item["viewer_count"])
        self.assertEqual("approximate", item["viewer_count_precision"])
        self.assertFalse(item["viewer_count_reliable"])

    def test_missing_compat_viewers_zero(self):
        missing = ranking.annotate_model_viewer_fields({"source_type": "chaturbate"})
        self.assertEqual(0, missing["viewers"])
        self.assertIsNone(missing["viewer_count"])
        self.assertEqual("missing", missing["viewer_count_precision"])
        self.assertFalse(missing["viewer_count_reliable"])

        missing_none = ranking.annotate_model_viewer_fields(
            {"source_type": "twitch", "viewers": None}
        )
        self.assertEqual(0, missing_none["viewers"])
        self.assertIsNone(missing_none["viewer_count"])
        self.assertEqual("missing", missing_none["viewer_count_precision"])

    def test_padded_zero_without_evidence_is_missing_not_exact(self):
        for source in ("chaturbate", "twitch"):
            item = ranking.annotate_model_viewer_fields(
                {"source_type": source, "viewers": 0}
            )
            self.assertEqual(0, item["viewers"], source)
            self.assertIsNone(item["viewer_count"], source)
            self.assertEqual("missing", item["viewer_count_precision"], source)
            self.assertFalse(item["viewer_count_reliable"], source)

    def test_real_zero_exact_only_with_num_users_evidence(self):
        item = ranking.annotate_model_viewer_fields(
            {
                "source_type": "chaturbate",
                "viewers": 0,
                "num_users": 0,
            }
        )
        self.assertEqual(0, item["viewers"])
        self.assertEqual(0, item["viewer_count"])
        self.assertEqual("exact", item["viewer_count_precision"])
        self.assertTrue(item["viewer_count_reliable"])
        self.assertEqual("num_users", item.get("viewer_count_source"))

    def test_chaturbate_exact_with_num_users_evidence(self):
        item = ranking.annotate_model_viewer_fields(
            {
                "source_type": "chaturbate",
                "viewers": 99,
                "num_users": 99,
            }
        )
        self.assertEqual(99, item["viewers"])
        self.assertEqual(99, item["viewer_count"])
        self.assertEqual("exact", item["viewer_count_precision"])
        self.assertTrue(item["viewer_count_reliable"])

    def test_precision_hint_exact_requires_present_count(self):
        item = ranking.annotate_model_viewer_fields(
            {
                "source_type": "twitch",
                "viewers": 7,
                "viewer_count_present": True,
                "viewer_count_source": "viewer_count",
                "viewer_count_precision_hint": "exact",
                "viewer_count_raw": 7,
            }
        )
        self.assertEqual("exact", item["viewer_count_precision"])
        self.assertTrue(item["viewer_count_reliable"])

    def test_stale_when_updated_at_expired(self):
        item = ranking.annotate_model_viewer_fields(
            {
                "source_type": "chaturbate",
                "num_users": 5,
                "viewers": 5,
                "viewer_count_updated_at": time.time() - 10_000,
            }
        )
        self.assertEqual("stale", item["viewer_count_precision"])
        self.assertFalse(item["viewer_count_reliable"])

    def test_b1_extras_never_claim_global_or_native(self):
        extras = ranking.b1_discover_response_extras(
            sort_mode="viewers",
            models=[{"viewer_count_precision": "exact"}],
            supported=True,
        )
        self.assertEqual("ab-shared-v1", extras["contract_version"])
        self.assertEqual("page_local", extras["ranking_mode"])
        self.assertIsNone(extras["pool_id"])
        self.assertNotEqual("multi_page_global", extras["ranking_mode"])
        self.assertNotEqual("provider_native", extras["ranking_mode"])


class DiscoverRankingB1ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        models = [
            {
                "username": "low",
                "is_online": True,
                "room_status": "public",
                "viewers": 10,
                "tags": ["public"],
            },
            {
                "username": "high",
                "is_online": True,
                "room_status": "public",
                "viewers": 99,
                "tags": ["public"],
            },
            {
                "username": "absent",
                "is_online": True,
                "room_status": "public",
                "tags": ["public"],
            },
            {
                "username": "zero",
                "is_online": True,
                "room_status": "public",
                "viewers": 0,
                "tags": ["public"],
            },
        ]
        self.registry = _Registry([_Provider("chaturbate", models)])
        discover.init(None, None, self.registry)

    async def asyncTearDown(self):
        discover.init(None, None, None)

    async def test_default_sort_viewers_order_unchanged(self):
        result = await discover.discover_models(
            page=1, limit=24, source="chaturbate",
            gender=None, search=None, tags=None, sort="viewers",
        )
        names = [m["username"] for m in result["models"]]
        self.assertEqual(["high", "low", "absent", "zero"], names)
        self.assertEqual("page_local", result["ranking_mode"])
        self.assertIsNone(result["pool_id"])
        self.assertEqual("viewers", result["sort"])

    async def test_viewers_desc_falls_back_to_page_local(self):
        """Global pool disabled: viewers_desc uses page_local (no pl_* snapshot)."""
        legacy = await discover.discover_models(
            page=1, limit=24, source="chaturbate",
            gender=None, search=None, tags=None, sort="viewers",
        )
        desc = await discover.discover_models(
            page=1, limit=24, source="chaturbate",
            gender=None, search=None, tags=None, sort="viewers_desc",
        )
        self.assertEqual("page_local", legacy["ranking_mode"])
        self.assertIsNone(legacy["pool_id"])
        self.assertEqual("page_local", desc["ranking_mode"])
        self.assertIsNone(desc["pool_id"])
        # Local viewers sort (legacy cohort padding; absent may sort with zero).
        self.assertEqual(
            [m["username"] for m in legacy["models"]],
            [m["username"] for m in desc["models"]],
        )

    async def test_viewers_preserved_precision_conservative(self):
        result = await discover.discover_models(
            page=1, limit=24, source="chaturbate",
            gender=None, search=None, tags=None, sort="viewers",
        )
        by_name = {m["username"]: m for m in result["models"]}
        self.assertEqual(99, by_name["high"]["viewers"])
        self.assertEqual(99, by_name["high"]["viewer_count"])
        self.assertEqual("unverified", by_name["high"]["viewer_count_precision"])
        self.assertFalse(by_name["high"]["viewer_count_reliable"])

        self.assertEqual(0, by_name["absent"]["viewers"])
        self.assertIsNone(by_name["absent"]["viewer_count"])
        self.assertEqual("missing", by_name["absent"]["viewer_count_precision"])

        self.assertEqual(0, by_name["zero"]["viewers"])
        self.assertIsNone(by_name["zero"]["viewer_count"])
        self.assertEqual("missing", by_name["zero"]["viewer_count_precision"])

    async def test_omitted_new_params_keeps_pagination_keys(self):
        result = await discover.discover_models(
            page=1, limit=2, source="chaturbate",
            gender=None, search=None, tags=None, sort="viewers",
        )
        self.assertIn("has_more", result)
        self.assertIn("total", result)
        self.assertIn("total_pages", result)
        self.assertEqual(2, len(result["models"]))
        self.assertIsInstance(result["has_more"], bool)


if __name__ == "__main__":
    unittest.main()

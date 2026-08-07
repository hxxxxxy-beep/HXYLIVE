import asyncio
import os
import time
import unittest
from unittest.mock import patch

from app.api import discover
from app.providers.base import BaseProvider, ProviderCapabilities


class _Provider(BaseProvider):
    def __init__(
        self,
        source_type,
        can_follow=False,
        can_stream=True,
        provider_status=None,
        provider_detail="",
        models=None,
    ):
        super().__init__()
        self.source_type = source_type
        self.display_name = source_type
        self.capabilities = ProviderCapabilities(
            can_discover=True,
            can_follow=can_follow,
            can_stream=can_stream,
            can_record=can_stream,
        )
        self.provider_status = provider_status
        self.provider_detail = provider_detail
        self.models = models

    async def list_live_models(self, **kwargs):
        if self.provider_status:
            return {
                "models": [],
                "total": 0,
                "page": kwargs.get("page", 1),
                "limit": kwargs.get("limit", 24),
                "total_pages": 1,
                "provider_status": self.provider_status,
                "provider_detail": self.provider_detail,
            }
        if self.models is not None:
            models = [dict(model, source_type=model.get("source_type") or self.source_type) for model in self.models]
            limit = kwargs.get("limit", 24)
            return {
                "models": models,
                "total": len(models),
                "page": kwargs.get("page", 1),
                "limit": limit,
                "total_pages": max(1, (len(models) + limit - 1) // limit),
            }
        return {
            "models": [
                {
                    "username": f"{self.source_type}_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 10,
                    "tags": [],
                }
            ],
            "total": 1,
            "page": kwargs.get("page", 1),
            "limit": kwargs.get("limit", 24),
            "total_pages": 1,
        }


class _Registry:
    def __init__(self):
        self.providers = {
            "chaturbate": _Provider("chaturbate", can_follow=True),
            "twitch": _Provider("twitch"),
        }

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


class _UnstableTotalProvider(_Provider):
    async def list_live_models(self, **kwargs):
        page = int(kwargs.get("page", 1) or 1)
        limit = int(kwargs.get("limit", 24) or 24)
        total_by_page = {1: 240, 2: 96, 3: 48}
        total = total_by_page.get(page, 24)
        return {
            "models": [
                {
                    "username": f"{self.source_type}_{page}_{idx}",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": max(1, 100 - idx),
                    "tags": ["public"],
                    "source_type": self.source_type,
                }
                for idx in range(limit)
            ],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": max(1, (total + limit - 1) // limit),
        }


class _SlowProvider(_Provider):
    async def list_live_models(self, **kwargs):
        await asyncio.sleep(1)
        return await super().list_live_models(**kwargs)


class _CursorProvider(_Provider):
    async def list_live_models(self, **kwargs):
        page = int(kwargs.get("page", 1) or 1)
        limit = int(kwargs.get("limit", 24) or 24)
        has_more = page < 4
        return {
            "models": [
                {
                    "username": f"{self.source_type}_{page}_{idx}",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 100 - idx,
                    "tags": ["public"],
                    "source_type": self.source_type,
                }
                for idx in range(limit)
            ],
            "total": page * limit + (1 if has_more else 0),
            "page": page,
            "limit": limit,
            "total_pages": page + 1 if has_more else page,
        }


class DiscoverProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        discover.init(None, None, _Registry())

    async def asyncTearDown(self):
        discover.init(None, None, None)

    async def test_discover_aggregates_registered_sources(self):
        result = await discover.discover_models(
            page=1, limit=6, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        sources = {item["source_type"] for item in result["models"]}
        self.assertEqual({"chaturbate", "twitch"}, sources)
        follow_flags = {
            item["source_type"]: item["can_follow"]
            for item in result["models"]
        }
        self.assertTrue(follow_flags["chaturbate"])
        self.assertTrue(follow_flags["twitch"])

    async def test_discover_source_filter_limits_provider(self):
        result = await discover.discover_models(
            page=1, limit=6, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["twitch"], [item["source_type"] for item in result["models"]])

    async def test_discover_excludes_disabled_providers_from_all_sources(self):
        registry = _Registry()
        discover.init(None, _SettingsDB(disabled=["twitch"]), registry)

        result = await discover.discover_models(
            page=1, limit=6, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(
            {"chaturbate"},
            {item["source_type"] for item in result["models"]},
        )

    async def test_discover_filters_gender_after_provider_results(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {
                    "username": "male_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 50,
                    "gender": "male",
                    "tags": ["men", "public"],
                },
                {
                    "username": "trans_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 500,
                    "gender": "trans",
                    "tags": ["trans", "public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=6, source=None, gender="male", search=None, tags=None, sort="viewers"
        )

        self.assertTrue(result["supported"])
        self.assertEqual(["male_model"], [item["username"] for item in result["models"]])

    async def test_discover_returns_provider_status_for_empty_source(self):
        registry = _Registry()
        registry.providers["lockedsite"] = _Provider(
            "lockedsite",
            provider_status="auth_required",
            provider_detail="Provider key required",
        )
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=6, source="lockedsite", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual([], result["models"])
        self.assertEqual("auth_required", result["provider_statuses"][0]["status"])
        self.assertEqual("Provider key required", result["provider_statuses"][0]["detail"])

    async def test_discover_sorts_globally_by_viewers(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {"username": "cb_big", "is_online": True, "room_status": "public", "viewers": 300, "tags": ["public"]},
                {"username": "cb_mid", "is_online": True, "room_status": "public", "viewers": 120, "tags": ["public"]},
            ]),
            "twitch": _Provider("twitch", models=[
                {"username": "tw_top", "is_online": True, "room_status": "public", "viewers": 250, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=3, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(
            ["cb_big", "tw_top", "cb_mid"],
            [item["username"] for item in result["models"]],
        )

    async def test_discover_global_timeout_returns_partial_results(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {"username": "cb_mid", "is_online": True, "room_status": "public", "viewers": 120, "tags": ["public"]},
            ]),
            "slow": _SlowProvider("slow"),
        }
        discover.init(None, None, registry)

        with patch.dict(os.environ, {"HXYLIVE_DISCOVER_AGGREGATE_PROVIDER_TIMEOUT": "0.01"}):
            started = time.monotonic()
            result = await discover.discover_models(
                page=1, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
            )

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(["cb_mid"], [item["username"] for item in result["models"]])
        slow_status = [item for item in result["provider_statuses"] if item["source_type"] == "slow"][0]
        self.assertEqual("timeout", slow_status["status"])

    async def test_discover_total_pages_is_stable_between_aggregate_pages(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {
                    "username": f"cb_{idx}",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 100 - idx,
                    "tags": ["public"],
                }
                for idx in range(72)
            ]),
        }
        discover.init(None, None, registry)

        page_one = await discover.discover_models(
            page=1, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )
        page_two = await discover.discover_models(
            page=2, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(3, page_one["total_pages"])
        self.assertEqual(page_one["total_pages"], page_two["total_pages"])
        self.assertEqual(24, len(page_two["models"]))

    async def test_discover_source_total_pages_is_stable_between_pages(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _UnstableTotalProvider("chaturbate"),
        }
        discover.init(None, None, registry)

        page_one = await discover.discover_models(
            page=1, limit=24, source="chaturbate", gender=None, search=None, tags=None, sort="viewers"
        )
        page_two = await discover.discover_models(
            page=2, limit=24, source="chaturbate", gender=None, search=None, tags=None, sort="viewers"
        )
        page_three = await discover.discover_models(
            page=3, limit=24, source="chaturbate", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(10, page_one["total_pages"])
        self.assertEqual(page_one["total_pages"], page_three["total_pages"])
        self.assertEqual(page_one["total"], page_two["total"])
        self.assertEqual(page_one["total"], page_three["total"])

    async def test_discover_cursor_source_keeps_loading_beyond_initial_page_estimate(self):
        registry = _Registry()
        registry.providers = {"twitch": _CursorProvider("twitch")}
        discover.init(None, None, registry)

        page_one = await discover.discover_models(
            page=1, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )
        page_two = await discover.discover_models(
            page=2, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )
        page_three = await discover.discover_models(
            page=3, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertTrue(page_one["has_more"])
        self.assertTrue(page_two["has_more"])
        self.assertTrue(page_three["has_more"])
        self.assertEqual("twitch_3_0", page_three["models"][0]["username"])

    async def test_discover_global_excludes_non_streamable_sources(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {"username": "cb_mid", "is_online": True, "room_status": "public", "viewers": 120, "tags": ["public"]},
            ]),
            "discoveronly": _Provider("discoveronly", can_stream=False, models=[
                {"username": "disc_top", "is_online": True, "room_status": "public", "viewers": 500, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["cb_mid"], [item["username"] for item in result["models"]])
        lj_status = [item for item in result["provider_statuses"] if item["source_type"] == "discoveronly"][0]
        self.assertEqual("discover_only", lj_status["status"])

    async def test_discover_source_filter_marks_non_streamable_cards(self):
        registry = _Registry()
        registry.providers = {
            "discoveronly": _Provider("discoveronly", can_stream=False, models=[
                {"username": "disc_model", "is_online": True, "room_status": "public", "viewers": 0, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source="discoveronly", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["disc_model"], [item["username"] for item in result["models"]])
        self.assertFalse(result["models"][0]["stream_available"])
        self.assertEqual("discover_only", result["provider_statuses"][0]["status"])

    def test_matches_gender_filter_couple_tag_not_blocked_by_female_primary(self):
        item = {"gender": "female", "tags": ["couple", "public"]}
        tags = ["couple", "public"]
        self.assertTrue(discover._matches_gender_filter(item, tags, "couple"))
        self.assertTrue(discover._matches_gender_filter(item, tags, "female"))

    def test_matches_gender_filter_female_without_couple_tag_rejects_couple(self):
        item = {"gender": "female", "tags": ["female", "public"]}
        tags = ["female", "public"]
        self.assertFalse(discover._matches_gender_filter(item, tags, "couple"))
        self.assertTrue(discover._matches_gender_filter(item, tags, "female"))

    async def test_discover_couple_filter_accepts_female_primary_with_couple_tag(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {
                    "username": "room_theme_couple",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 90,
                    "gender": "couple",
                    "tags": ["couple", "female", "public"],
                },
                {
                    "username": "plain_female",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 80,
                    "gender": "female",
                    "tags": ["female", "public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        couple = await discover.discover_models(
            page=1, limit=10, source="chaturbate", gender="couple", search=None, tags=None, sort="viewers"
        )
        female = await discover.discover_models(
            page=1, limit=10, source="chaturbate", gender="female", search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["room_theme_couple"], [item["username"] for item in couple["models"]])
        self.assertEqual(["plain_female"], [item["username"] for item in female["models"]])

    async def test_discover_search_keeps_substring_private_and_ranks_prefix_first(self):
        """Search keeps ids containing the needle; prefix ranks above mid-string."""
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", can_follow=True, models=[
                {
                    "username": "unrelated_cam",
                    "display_name": "unrelated_cam",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 900,
                    "tags": [],
                },
                {
                    "username": "mazzanti_",
                    "display_name": "mazzanti_",
                    "is_online": True,
                    "room_status": "password_protected",
                    "viewers": 0,
                    "tags": [],
                },
                {
                    "username": "xx_mazzanti_fan",
                    "display_name": "xx_mazzanti_fan",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 12,
                    "tags": [],
                },
            ]),
        }
        discover.init(None, _SettingsDB(), registry)
        result = await discover.discover_models(
            page=1,
            limit=24,
            source="chaturbate",
            gender=None,
            search="mazzanti",
            tags=None,
            sort="viewers",
        )
        names = [item["username"] for item in result["models"]]
        self.assertEqual(["mazzanti_", "xx_mazzanti_fan"], names)
        self.assertEqual("password_protected", result["models"][0]["room_status"])


class DiscoverGenderAliasTests(unittest.IsolatedAsyncioTestCase):
    def test_global_aliases_do_not_map_isolated_c_or_s(self):
        self.assertIsNone(discover._canonical_gender("c"))
        self.assertIsNone(discover._canonical_gender("s"))
        self.assertFalse(
            discover._matches_gender_filter({"gender": "c", "tags": []}, [], "couple")
        )
        self.assertFalse(
            discover._matches_gender_filter({"gender": "s", "tags": []}, [], "trans")
        )
        # Existing single-letter aliases must remain intact.
        self.assertEqual("female", discover._canonical_gender("f"))
        self.assertEqual("male", discover._canonical_gender("m"))
        # Other single letters must not spuriously match couple/trans.
        for token in ("a", "b", "d", "e", "g", "h", "i", "j", "k", "n", "o", "p", "q", "r", "u", "v", "w", "x", "y", "z"):
            self.assertIsNone(discover._canonical_gender(token), token)
            self.assertFalse(
                discover._matches_gender_filter({"gender": token, "tags": []}, [], "couple"),
                token,
            )
            self.assertFalse(
                discover._matches_gender_filter({"gender": token, "tags": []}, [], "trans"),
                token,
            )

    async def test_twitch_gender_filters_are_unsupported(self):
        registry = _Registry()
        registry.providers = {
            "twitch": _Provider("twitch", models=[
                {
                    "username": "tw_live",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 50,
                    "tags": ["public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        for gender in ("female", "male", "trans", "couple"):
            result = await discover.discover_models(
                page=1, limit=10, source="twitch", gender=gender, search=None, tags=None, sort="viewers"
            )
            self.assertFalse(result["supported"])
            self.assertEqual([], result["models"])

    async def test_discover_chaturbate_normalized_couple_and_trans_pass_filters(self):
        # Chaturbate provider/API normalizes c→couple and s→trans before discover.
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {
                    "username": "cb_couple",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 50,
                    "gender": "couple",
                    "tags": ["public"],
                },
                {
                    "username": "cb_trans",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 40,
                    "gender": "trans",
                    "tags": ["public"],
                },
                {
                    "username": "cb_female",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 30,
                    "gender": "f",
                    "tags": ["public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        couple = await discover.discover_models(
            page=1, limit=10, source="chaturbate", gender="couple", search=None, tags=None, sort="viewers"
        )
        trans = await discover.discover_models(
            page=1, limit=10, source="chaturbate", gender="trans", search=None, tags=None, sort="viewers"
        )
        female = await discover.discover_models(
            page=1, limit=10, source="chaturbate", gender="female", search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["cb_couple"], [item["username"] for item in couple["models"]])
        self.assertEqual(["cb_trans"], [item["username"] for item in trans["models"]])
        self.assertEqual(["cb_female"], [item["username"] for item in female["models"]])

    async def test_unsupported_gender_returns_capability_payload(self):
        registry = _Registry()
        registry.providers = {
            "twitch": _Provider("twitch", models=[
                {
                    "username": "tw_live",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 10,
                    "tags": ["public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source="twitch", gender="male", search=None, tags=None, sort="viewers"
        )
        self.assertFalse(result["supported"])
        self.assertEqual("gender_not_supported_by_provider", result["unsupported_reason"])
        self.assertEqual([], result["models"])
        self.assertEqual(0, result["total"])
        self.assertFalse(result["has_more"])
        self.assertEqual("unsupported", result["provider_statuses"][0]["status"])

    async def test_supported_live_inventory_zero_stays_supported(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider(
                "chaturbate",
                provider_status="empty",
                provider_detail="No live rooms",
            ),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source="chaturbate", gender="trans", search=None, tags=None, sort="viewers"
        )
        self.assertTrue(result["supported"])
        self.assertIsNone(result["unsupported_reason"])
        self.assertEqual([], result["models"])
        self.assertFalse(result["has_more"])
        self.assertEqual("empty", result["provider_statuses"][0]["status"])

    async def test_twitch_all_supported_other_genders_unsupported(self):
        registry = _Registry()
        registry.providers = {
            "twitch": _Provider("twitch", models=[
                {
                    "username": "twitch_live",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 5,
                    "tags": ["public"],
                },
            ]),
        }
        discover.init(None, None, registry)
        all_result = await discover.discover_models(
            page=1, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )
        self.assertTrue(all_result["supported"])
        self.assertEqual(1, len(all_result["models"]))
        for gender in ("female", "male", "trans", "couple"):
            result = await discover.discover_models(
                page=1, limit=24, source="twitch", gender=gender, search=None, tags=None, sort="viewers"
            )
            self.assertFalse(result["supported"], msg=f"twitch/{gender}")
            self.assertEqual([], result["models"])


if __name__ == "__main__":
    unittest.main()

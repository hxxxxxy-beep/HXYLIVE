import unittest

from app.discover_category_catalog import (
    CONTRACT_VERSION,
    KNOWN_SOURCES,
    READINESS_VALUES,
    SCHEMA_VERSION,
    build_categories_payload,
    canonicalize_category_value,
    known_source_list,
)


class DiscoverCategoryCatalogTests(unittest.TestCase):
    def test_contract_and_schema_versions(self):
        self.assertEqual("ab-shared-v1", CONTRACT_VERSION)
        self.assertEqual("categories-request-v1", SCHEMA_VERSION)

    def test_known_sources_cover_registry_set(self):
        expected = {"twitch", "chaturbate", "bilibili", "stripchat"}
        self.assertEqual(expected, set(KNOWN_SOURCES))
        self.assertEqual(sorted(expected), known_source_list())

    def test_chaturbate_synonyms_require_source_field_locus(self):
        self.assertEqual(
            "female",
            canonicalize_category_value("chaturbate", "gender", "structured_alias", "women"),
        )
        self.assertEqual(
            "male",
            canonicalize_category_value("chaturbate", "gender", "structured_alias", "man"),
        )
        self.assertEqual(
            "trans",
            canonicalize_category_value("chaturbate", "gender", "structured_alias", "transgender"),
        )
        self.assertEqual(
            "couple",
            canonicalize_category_value("chaturbate", "gender", "structured_alias", "couples"),
        )
        self.assertIsNone(
            canonicalize_category_value("chaturbate", "title", "structured_alias", "women"),
        )
        self.assertIsNone(
            canonicalize_category_value("twitch", "gender", "structured_alias", "female"),
        )
        self.assertEqual(
            "female",
            canonicalize_category_value("stripchat", "gender", "structured_alias", "women"),
        )

    def test_chaturbate_genders_api_codes(self):
        self.assertEqual(
            "female",
            canonicalize_category_value("chaturbate", "genders", "chaturbate_roomlist_api", "f"),
        )
        self.assertEqual(
            "couple",
            canonicalize_category_value("chaturbate", "genders", "chaturbate_roomlist_api", "c"),
        )

    def test_twitch_categories_exclude_gender(self):
        payload = build_categories_payload(
            "twitch",
            twitch_games=[{"game_id": "509659", "name": "ASMR"}],
        )
        self.assertIsNotNone(payload)
        keys = {c["canonical_category"] for c in payload["categories"]}
        self.assertEqual({"game:509659"}, keys)
        self.assertNotIn("all", keys)
        for banned in ("Female", "Male", "Trans", "Couple", "female", "male", "trans", "couple", "All"):
            self.assertNotIn(banned, keys)
            self.assertFalse(
                any(c.get("display_label") == banned for c in payload["categories"])
            )
        unavailable = {c["canonical_category"] for c in payload["unavailable_categories"]}
        self.assertTrue({"female", "male", "trans", "couple"}.issubset(unavailable))

    def test_chaturbate_formal_categories_include_verified_genders(self):
        payload = build_categories_payload("chaturbate")
        formal = {c["canonical_category"] for c in payload["categories"]}
        self.assertEqual({"all", "female", "male", "trans", "couple"}, formal)

        by_key = {c["canonical_category"]: c for c in payload["categories"]}
        for key in ("female", "male", "trans", "couple"):
            item = by_key[key]
            self.assertTrue(item["source_signal_present"])
            self.assertTrue(item["available"])
            self.assertEqual("verified", item["readiness"])
            self.assertEqual("native_categories", item["source_mode"])
            self.assertEqual("high", item["reliability"])

        all_item = by_key["all"]
        self.assertTrue(all_item["source_signal_present"])
        self.assertTrue(all_item["available"])
        self.assertEqual("verified", all_item["readiness"])

    def test_signal_and_available_are_independent(self):
        twitch = build_categories_payload("twitch")
        female = next(
            c for c in twitch["unavailable_categories"]
            if c["canonical_category"] == "female"
        )
        self.assertFalse(female["source_signal_present"])
        self.assertFalse(female["available"])
        self.assertFalse(
            any(c["canonical_category"] == "female" for c in twitch["categories"])
        )

    def test_readiness_diagnostics_list_four_states(self):
        payload = build_categories_payload("chaturbate")
        self.assertEqual(
            sorted(READINESS_VALUES),
            payload["diagnostics"]["readiness_values"],
        )
        states = {c["readiness"] for c in payload["categories"]}
        states |= {c["readiness"] for c in payload["unavailable_categories"]}
        self.assertIn("verified", states)

    def test_formal_categories_only_available_true(self):
        for source in sorted(KNOWN_SOURCES):
            payload = build_categories_payload(source)
            for item in payload["categories"]:
                self.assertTrue(
                    item["available"],
                    msg=f"{source}:{item['canonical_category']} in formal list but available=false",
                )
                self.assertNotIn(item["readiness"], {"not_ready", "unsupported"})
            for item in payload["unavailable_categories"]:
                self.assertFalse(item["available"])

    def test_ranking_hints_chaturbate_b4_projection(self):
        payload = build_categories_payload("chaturbate")
        hints = payload["ranking_hints"]
        self.assertTrue(hints["supports_viewer_count"])
        self.assertTrue(hints["viewer_count_reliable"])
        self.assertEqual("exact", hints["viewer_count_precision_default"])
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertEqual(["page_local"], hints["ranking_modes"])
        self.assertEqual(hints["ranking_modes"], hints["ranking_modes_available"])
        self.assertEqual("num_users", hints["evidence_source"])
        self.assertEqual("verified", hints["implementation_status"])
        self.assertNotIn("multi_page_global", hints["ranking_modes"])
        self.assertNotIn("provider_native", hints["ranking_modes"])
    def test_ranking_hints_twitch_remains_conservative(self):
        payload = build_categories_payload("twitch")
        hints = payload["ranking_hints"]
        self.assertFalse(hints["supports_viewer_count"])
        self.assertFalse(hints["viewer_count_reliable"])
        self.assertEqual("unverified", hints["viewer_count_precision_default"])
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertNotIn("viewers_desc", hints["supported_sort_modes"])
        self.assertEqual([], hints["ranking_modes"])
        self.assertEqual([], hints["ranking_modes_available"])
        self.assertNotEqual("verified", hints.get("implementation_status"))

    def test_ranking_hints_bilibili_viewers_desc(self):
        payload = build_categories_payload("bilibili")
        hints = payload["ranking_hints"]
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertEqual(["page_local"], hints["ranking_modes"])
        self.assertNotIn("viewers_desc", hints["supported_sort_modes"])
        self.assertNotIn("multi_page_global", hints["ranking_modes"])
        self.assertEqual("verified", hints["implementation_status"])
        self.assertEqual("audience_count", hints["evidence_source"])

    def test_stripchat_formal_categories_and_conservative_ranking(self):
        payload = build_categories_payload("stripchat")
        self.assertIsNotNone(payload)
        formal = {c["canonical_category"] for c in payload["categories"]}
        self.assertEqual({"all", "female", "male", "trans", "couple"}, formal)
        by_key = {c["canonical_category"]: c for c in payload["categories"]}
        for key in ("female", "male", "trans", "couple"):
            item = by_key[key]
            self.assertTrue(item["available"])
            self.assertEqual("verified", item["readiness"])
            self.assertEqual("native_categories", item["source_mode"])
        self.assertEqual(
            "female",
            canonicalize_category_value("stripchat", "primaryTag", "stripchat_api", "girls"),
        )
        self.assertEqual(
            "male",
            canonicalize_category_value("stripchat", "primaryTag", "stripchat_api", "men"),
        )
        hints = payload["ranking_hints"]
        self.assertEqual(["source_default"], hints["supported_sort_modes"])
        self.assertNotIn("viewers_desc", hints["supported_sort_modes"])
        self.assertEqual([], hints["ranking_modes"])

    def test_unknown_source_returns_none(self):
        self.assertIsNone(build_categories_payload("not-a-real-source"))
        self.assertIsNone(build_categories_payload(""))
        self.assertIsNone(build_categories_payload("cam4"))

    def test_payload_contract_fields(self):
        payload = build_categories_payload("chaturbate")
        self.assertEqual(CONTRACT_VERSION, payload["contract_version"])
        self.assertEqual(SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual("chaturbate", payload["source"])
        self.assertEqual("all", payload["default_category"])
        self.assertIn("secondary_filters", payload)
        self.assertIn("unavailable_categories", payload)
        self.assertIn("diagnostics", payload)
        self.assertIn("capability_evidence", payload)
        keys = {c["canonical_category"] for c in payload["categories"]}
        self.assertEqual({"all", "female", "male", "trans", "couple"}, keys)


if __name__ == "__main__":
    unittest.main()

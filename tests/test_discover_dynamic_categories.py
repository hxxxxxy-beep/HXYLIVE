"""A P3 / P3.1 dynamic category frontend contract + pure-logic tests."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def is_formal_category_item(item):
    if not isinstance(item, dict):
        return False
    if item.get("available") is not True:
        return False
    if item.get("readiness") != "verified":
        return False
    key = str(item.get("canonical_key") or item.get("canonical_category") or "").strip()
    if not key:
        return False
    label = str(item.get("display_label") or item.get("label") or "").strip()
    if not label and key.lower() == "all":
        label = "All"
    return bool(label)


def filter_formal_categories_from_payload(payload):
    items = (payload or {}).get("categories") or []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not is_formal_category_item(item):
            continue
        gate = evaluate_category_request_support(item)
        if gate.get("supported"):
            out.append(item)
    return out


def normalize_category_item(item):
    raw = item if isinstance(item, dict) else {}
    key = str(raw.get("canonical_key") or raw.get("canonical_category") or "all").strip().lower() or "all"
    ctype = str(raw.get("category_type") or ("all" if key == "all" else "gender")).lower()
    request_param = raw.get("request_param", "__missing__")
    request_value = raw.get("request_value", "__missing__")
    if request_param == "__missing__" or request_value == "__missing__":
        if ctype == "all" or key == "all":
            request_param, request_value = None, None
        elif ctype == "gender":
            request_param, request_value = "gender", key
        elif ctype == "content":
            request_param, request_value = "category", key
        elif ctype == "language":
            request_param, request_value = "language", key
        else:
            request_param = None if request_param == "__missing__" else request_param
            request_value = key if request_value == "__missing__" else request_value
    if request_param == "":
        request_param = None
    if request_value == "":
        request_value = None
    return {
        "canonical_key": key,
        "canonical_category": key,
        "category_type": ctype,
        "request_param": request_param,
        "request_value": request_value,
        "filter_scope": raw.get("filter_scope") or "primary",
        "available": raw.get("available") is True,
        "readiness": str(raw.get("readiness") or ""),
    }


def evaluate_category_request_support(item):
    normalized = normalize_category_item(item)
    ctype = normalized["category_type"]
    param = normalized["request_param"]
    if ctype == "all" or (not param and not normalized["request_value"]):
        return {
            "supported": True,
            "discoverGender": "",
            "discoverGameId": None,
            "normalized": normalized,
            "message": "",
        }
    if ctype == "gender" and param == "gender" and normalized["request_value"]:
        return {
            "supported": True,
            "discoverGender": str(normalized["request_value"]).lower(),
            "discoverGameId": None,
            "normalized": normalized,
            "message": "",
        }
    value = str(normalized["request_value"] or "")
    if (
        (ctype == "content" or param == "game_id")
        and param == "game_id"
        and value.isdigit()
    ):
        return {
            "supported": True,
            "discoverGender": None,
            "discoverGameId": value,
            "normalized": normalized,
            "message": "",
        }
    return {
        "supported": False,
        "discoverGender": None,
        "discoverGameId": None,
        "normalized": normalized,
        "message": "This category filter is not wired yet",
    }


def apply_category_request(selection):
    sel = selection or {}
    synthetic = {
        "canonical_key": sel.get("selectedCategoryKey") or sel.get("canonical_key") or "all",
        "category_type": sel.get("selectedCategoryType") or sel.get("category_type") or "all",
        "request_param": sel.get("selectedCategoryRequestParam", sel.get("request_param")),
        "request_value": sel.get("selectedCategoryRequestValue", sel.get("request_value")),
    }
    gate = evaluate_category_request_support(synthetic)
    if not gate["supported"]:
        return {"ok": False, "error": gate["message"], "gender": None, "game_id": None}
    if gate.get("discoverGender"):
        return {"ok": True, "gender": gate["discoverGender"], "game_id": ""}
    if gate.get("discoverGameId"):
        return {"ok": True, "gender": "", "game_id": gate["discoverGameId"]}
    return {"ok": True, "gender": "", "game_id": ""}


def should_apply_categories_response(request_seq, latest_seq, request_source, current_source):
    if request_seq != latest_seq:
        return False
    req = str(request_source or "").strip().lower()
    cur = str(current_source or "").strip().lower()
    return bool(req) and req == cur


def safe_all_fallback_items():
    return [{
        "canonical_key": "all",
        "canonical_category": "all",
        "display_label": "All",
        "category_type": "all",
        "request_param": None,
        "request_value": None,
        "filter_scope": "primary",
        "available": True,
        "readiness": "verified",
    }]


class DiscoverCategoriesLogicTests(unittest.TestCase):
    def test_chaturbate_female_sends_gender_female(self):
        item = {
            "canonical_key": "female",
            "category_type": "gender",
            "request_param": "gender",
            "request_value": "female",
            "available": True,
            "readiness": "verified",
        }
        applied = apply_category_request({
            "selectedCategoryKey": "female",
            "selectedCategoryType": "gender",
            "selectedCategoryRequestParam": "gender",
            "selectedCategoryRequestValue": "female",
        })
        self.assertTrue(applied["ok"])
        self.assertEqual("female", applied["gender"])
        self.assertTrue(evaluate_category_request_support(item)["supported"])

    def test_all_does_not_send_gender(self):
        applied = apply_category_request({
            "selectedCategoryKey": "all",
            "selectedCategoryType": "all",
            "selectedCategoryRequestParam": None,
            "selectedCategoryRequestValue": None,
        })
        self.assertTrue(applied["ok"])
        self.assertEqual("", applied["gender"])

    def test_content_asmr_must_not_send_gender_asmr(self):
        applied = apply_category_request({
            "selectedCategoryKey": "asmr",
            "selectedCategoryType": "content",
            "selectedCategoryRequestParam": "category",
            "selectedCategoryRequestValue": "asmr",
        })
        self.assertFalse(applied["ok"])
        self.assertIsNone(applied["gender"])
        self.assertNotEqual("asmr", applied.get("gender"))

    def test_twitch_game_id_sends_game_id_not_gender(self):
        applied = apply_category_request({
            "selectedCategoryKey": "game:33214",
            "selectedCategoryType": "content",
            "selectedCategoryRequestParam": "game_id",
            "selectedCategoryRequestValue": "33214",
        })
        self.assertTrue(applied["ok"])
        self.assertEqual("", applied["gender"])
        self.assertEqual("33214", applied["game_id"])

    def test_language_english_must_not_send_gender_english(self):
        applied = apply_category_request({
            "selectedCategoryKey": "english",
            "selectedCategoryType": "language",
            "selectedCategoryRequestParam": "language",
            "selectedCategoryRequestValue": "english",
        })
        self.assertFalse(applied["ok"])
        self.assertIsNone(applied["gender"])

    def test_unsupported_category_type_blocked(self):
        gate = evaluate_category_request_support({
            "canonical_key": "asmr",
            "category_type": "content",
            "request_param": "category",
            "request_value": "Just Chatting",
        })
        self.assertFalse(gate["supported"])
        self.assertIn("not wired yet", gate["message"])

    def test_unavailable_categories_never_rendered(self):
        payload = {
            "categories": [
                {"canonical_category": "all", "available": True, "readiness": "verified"},
            ],
            "unavailable_categories": [
                {"canonical_category": "male", "available": True, "readiness": "verified"},
            ],
        }
        formal = filter_formal_categories_from_payload(payload)
        self.assertEqual(["all"], [
            c.get("canonical_key") or c.get("canonical_category") for c in formal
        ])

    def test_api_failure_safe_all_only(self):
        fallback = safe_all_fallback_items()
        self.assertEqual(["all"], [c["canonical_key"] for c in fallback])
        self.assertIsNone(fallback[0]["request_param"])

    def test_stale_response_ignored_on_fast_source_switch(self):
        self.assertFalse(should_apply_categories_response(1, 2, "twitch", "chaturbate"))
        self.assertTrue(should_apply_categories_response(2, 2, "chaturbate", "chaturbate"))


class DiscoverCategoryApiMappingTests(unittest.TestCase):
    def test_catalog_items_include_request_mapping(self):
        from app.discover_category_catalog import SCHEMA_VERSION, build_categories_payload

        self.assertEqual("categories-request-v1", SCHEMA_VERSION)
        chaturbate = build_categories_payload("chaturbate")
        female = next(c for c in chaturbate["categories"] if c["canonical_category"] == "female")
        self.assertEqual("female", female["canonical_key"])
        self.assertEqual("gender", female["category_type"])
        self.assertEqual("gender", female["request_param"])
        self.assertEqual("female", female["request_value"])
        self.assertEqual("primary", female["filter_scope"])

        all_item = next(c for c in chaturbate["categories"] if c["canonical_category"] == "all")
        self.assertIsNone(all_item["request_param"])
        self.assertIsNone(all_item["request_value"])
        self.assertEqual("all", all_item["category_type"])

    def test_future_content_mapping_is_not_gender(self):
        from app.discover_category_catalog import _request_mapping_for

        mapping = _request_mapping_for("content", "asmr")
        self.assertEqual("category", mapping["request_param"])
        self.assertEqual("asmr", mapping["request_value"])
        self.assertNotEqual("gender", mapping["request_param"])

        twitch_game = _request_mapping_for("content", "game:33214")
        self.assertEqual("game_id", twitch_game["request_param"])
        self.assertEqual("33214", twitch_game["request_value"])
        self.assertEqual("content", twitch_game["filter_scope"])
        self.assertNotEqual("gender", twitch_game["request_param"])

        lang = _request_mapping_for("language", "english")
        self.assertEqual("language", lang["request_param"])
        self.assertNotEqual("gender", lang["request_param"])


class DiscoverDynamicCategoriesStaticTests(unittest.TestCase):
    def test_html_uses_category_filters_not_fixed_five_keys(self):
        html = (ROOT / "static" / "discover.html").read_text()
        self.assertIn('id="categoryFilters"', html)
        self.assertNotIn('onclick="setGender(', html)
        self.assertNotIn('data-gender="female"', html)
        self.assertIn("discover_categories.js?v=9", html)
        self.assertIn("discover.js?v=72", html)
        self.assertNotIn("discover.js?v=23", html)
        self.assertNotIn("discover.js?v=25", html)

    def test_discover_js_uses_generic_category_state(self):
        js = (ROOT / "static" / "discover.js").read_text()
        helpers = (ROOT / "static" / "discover_categories.js").read_text()

        self.assertIn("selectedCategoryKey", js)
        self.assertIn("selectedCategoryType", js)
        self.assertIn("selectedCategoryRequestParam", js)
        self.assertIn("selectedCategoryRequestValue", js)
        self.assertIn("selectedSecondaryFilters", js)
        self.assertIn("function setCategory", js)
        self.assertIn("function applyCategoryRequestToDiscover", js)
        self.assertIn("evaluateCategoryRequestSupport", helpers)
        self.assertIn("applyCategoryRequest", helpers)
        self.assertIn("selectDefaultCategory", helpers)
        self.assertIn("This category filter is not wired yet", helpers)
        self.assertIn("Never silently map content/language/tag into gender", helpers)
        self.assertIn("AbortController", js)
        self.assertIn("resetDiscoverListState", js)
        # Compat wrapper may remain but must not be the only path.
        self.assertIn("function setGender", js)
        self.assertIn("setCategory(", js)


if __name__ == "__main__":
    unittest.main()

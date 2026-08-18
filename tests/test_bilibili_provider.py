"""Bilibili live provider + parent-area categories tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.discover_category_catalog import (
    bilibili_content_category_item,
    build_categories_payload,
    is_formal_deliverable_category,
)
from app.discover_gender_capabilities import unsupported_reason
from app.providers.bilibili import BilibiliProvider
from app.providers.registry import create_provider_registry
from app.services.bilibili_categories import (
    build_bilibili_area_name_index,
    dedupe_bilibili_parent_areas,
    list_bilibili_parent_areas,
    normalize_bilibili_parent_area_id,
    reset_bilibili_category_cache_for_tests,
    resolve_bilibili_area_by_name,
)


class BilibiliCategoriesServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_bilibili_category_cache_for_tests()

    def test_normalize_parent_area_id(self):
        self.assertEqual(normalize_bilibili_parent_area_id("9"), "9")
        self.assertIsNone(normalize_bilibili_parent_area_id("abc"))
        self.assertIsNone(normalize_bilibili_parent_area_id(""))

    def test_build_area_name_index_prefers_child_over_parent(self):
        rows = build_bilibili_area_name_index(
            [
                {
                    "id": 2,
                    "name": "网游",
                    "list": [
                        {"id": 86, "name": "英雄联盟", "parent_id": 2},
                        {"id": 87, "name": "守望先锋", "parent_id": 2},
                    ],
                }
            ]
        )
        names = [row["name"] for row in rows]
        self.assertEqual(names.count("英雄联盟"), 1)
        self.assertEqual(names.count("网游"), 1)
        self.assertLess(names.index("英雄联盟"), names.index("网游"))
        lol = next(row for row in rows if row["name"] == "英雄联盟")
        self.assertEqual(lol["parent_area_id"], "2")
        self.assertEqual(lol["area_id"], "86")
        self.assertEqual(lol["match_kind"], "child")

    async def test_resolve_area_by_name_maps_lol_tag(self):
        async def fake_fetch():
            return [
                {
                    "id": 2,
                    "name": "网游",
                    "list": [{"id": 86, "name": "英雄联盟", "parent_id": 2}],
                }
            ]

        resolved = await resolve_bilibili_area_by_name("英雄联盟", area_fetcher=fake_fetch)
        self.assertEqual(resolved["parent_area_id"], "2")
        self.assertEqual(resolved["area_id"], "86")
        parent = await resolve_bilibili_area_by_name("网游", area_fetcher=fake_fetch)
        self.assertEqual(parent["parent_area_id"], "2")
        self.assertEqual(parent["area_id"], "0")

    def test_dedupe_parent_areas(self):
        rows = dedupe_bilibili_parent_areas(
            [
                {"id": "9", "name": "虚拟主播"},
                {"id": "9", "name": "dup"},
                {"id": "1", "name": "娱乐"},
                {"id": "x", "name": "bad"},
            ]
        )
        self.assertEqual(
            rows,
            [
                {"parent_area_id": "9", "name": "虚拟主播"},
                {"parent_area_id": "1", "name": "娱乐"},
            ],
        )

    async def test_list_parent_areas_uses_fetcher(self):
        areas = await list_bilibili_parent_areas(
            force_refresh=True,
            area_fetcher=AsyncMock(
                return_value=[
                    {"id": 9, "name": "虚拟主播", "list": []},
                    {"id": 2, "name": "网游", "list": []},
                ]
            ),
        )
        self.assertEqual(
            [a["name"] for a in areas],
            ["虚拟主播", "网游"],
        )


class BilibiliCatalogTests(unittest.TestCase):
    def test_formal_parent_area_item(self):
        item = bilibili_content_category_item("9", "虚拟主播")
        self.assertTrue(is_formal_deliverable_category(item))
        self.assertEqual(item["request_param"], "parent_area_id")
        self.assertEqual(item["request_value"], "9")
        self.assertEqual(item["canonical_key"], "parent_area:9")

    def test_payload_injects_parent_areas_not_gender(self):
        payload = build_categories_payload(
            "bilibili",
            bilibili_areas=[
                {"parent_area_id": "9", "name": "虚拟主播"},
                {"parent_area_id": "1", "name": "娱乐"},
            ],
        )
        keys = [c["canonical_key"] for c in payload["categories"]]
        self.assertEqual(keys[0], "parent_area:9")
        self.assertNotIn("all", keys)
        self.assertIn("parent_area:9", keys)
        self.assertIn("parent_area:1", keys)
        self.assertNotIn("female", keys)
        self.assertEqual(unsupported_reason("bilibili", "female"), "gender_not_supported_by_provider")

    def test_payload_pins_vtuber_when_areas_missing(self):
        payload = build_categories_payload("bilibili", bilibili_areas=[])
        keys = [c["canonical_key"] for c in payload["categories"]]
        self.assertEqual(["parent_area:9"], keys)
        self.assertEqual("Virtual streamers", payload["categories"][0]["display_label"])
        self.assertNotIn("all", keys)


class BilibiliProviderStaticContractTests(unittest.TestCase):
    def test_get_room_list_uses_page_not_page_no(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "providers" / "bilibili.py").read_text(
            encoding="utf-8"
        )
        # Regression: page_no is ignored by getRoomList and returns the same
        # first page forever, which capped Discover at ~24 rooms.
        self.assertIn('"page": str(max(1, int(page_no)))', source)
        self.assertNotIn('"page_no": str(max(1, int(page_no)))', source)


class BilibiliProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = BilibiliProvider(
            "bilibili",
            "Bilibili",
            "https://live.bilibili.com/{username}",
            ("live.bilibili.com",),
            None,
        )

    def test_room_model_stable_id_and_url(self):
        model = self.provider._room_model(
            {
                "roomid": 22632424,
                "uid": 672353429,
                "title": "hello",
                "uname": "贝拉kira",
                "online": 100,
                "face": "https://example.com/face.jpg",
                "user_cover": "https://example.com/c.jpg",
                "parent_id": 9,
                "parent_name": "虚拟主播",
                "area_id": 371,
                "area_name": "虚拟日常",
            }
        )
        self.assertEqual(model["username"], "22632424")
        self.assertEqual(model["display_name"], "贝拉kira")
        self.assertEqual(model["user_id"], "672353429")
        self.assertEqual(model["viewers"], 0)
        self.assertIsNone(model["followers"])
        self.assertEqual(model["profile_image_url"], "https://example.com/face.jpg")
        self.assertEqual(model["thumbnail"], "https://example.com/c.jpg")
        self.assertEqual(model["id"], "bilibili:22632424")
        self.assertEqual(model["channel_url"], "https://live.bilibili.com/22632424")
        self.assertEqual(model["parent_area_id"], "9")

    def test_room_model_prefers_long_room_id_not_short(self):
        model = self.provider._room_model(
            {
                "roomid": 7734200,
                "uid": 50329118,
                "uname": "哔哩哔哩英雄联盟赛事",
                "online": 100,
                "face": "https://example.com/face.jpg",
                "user_cover": "https://example.com/c.jpg",
                "link": "/6",
            }
        )
        self.assertEqual(model["username"], "7734200")
        self.assertEqual(model["short_id"], "6")
        self.assertEqual(model["room_id"], "7734200")
        self.assertEqual(model["display_name"], "哔哩哔哩英雄联盟赛事")
        self.assertEqual(model["id"], "bilibili:7734200")
        self.assertEqual(model["channel_url"], "https://live.bilibili.com/7734200")
        self.assertEqual(model["profile_image_url"], "https://example.com/face.jpg")
        self.assertNotEqual(model["profile_image_url"], model["thumbnail"])

    def test_room_model_ignores_long_url_as_short_id(self):
        model = self.provider._room_model(
            {
                "roomid": 7734200,
                "uid": 1,
                "uname": "a",
                "face": "https://example.com/f.jpg",
                "url": "https://live.bilibili.com/7734200",
            }
        )
        self.assertEqual(model["username"], "7734200")
        self.assertEqual(model["short_id"], "")
        self.assertEqual(model["profile_image_url"], "https://example.com/f.jpg")

    async def test_resolve_watch_meta_matches_card_fields(self):
        async def fake_fetch_json(url, **kwargs):
            if "getInfoByRoom" in url:
                return {
                    "code": 0,
                    "data": {
                        "room_info": {
                            "room_id": 24678311,
                            "short_id": 0,
                            "uid": 50329118,
                            "online": 775235,
                            "live_status": 1,
                            "title": "match",
                            "user_cover": "https://example.com/cover.jpg",
                        },
                        "watched_show": {
                            "switch": True,
                            "num": 103297,
                            "text_large": "10.3万人看过",
                        },
                        "room_rank_info": {
                            "user_rank_entry": {
                                "user_contribution_rank_entry": {
                                    "count": 31389,
                                    "count_text": "3万+",
                                }
                            }
                        },
                        "anchor_info": {
                            "base_info": {
                                "uid": 50329118,
                                "uname": "示例主播",
                                "face": "https://example.com/face.jpg",
                            },
                            "relation_info": {"attention": 999},
                        },
                    },
                }
            raise AssertionError(url)

        with patch.object(self.provider, "_fetch_json", AsyncMock(side_effect=fake_fetch_json)):
            meta = await self.provider.resolve_watch_meta("24678311")
        self.assertEqual(meta["username"], "24678311")
        self.assertEqual(meta["displayName"], "示例主播")
        self.assertEqual(meta["profileImageUrl"], "https://example.com/face.jpg")
        self.assertEqual(meta["viewers"], 31389)
        self.assertEqual(meta["followers"], 999)
        self.assertTrue(meta["isOnline"])

    def test_viewers_prefer_room_audience_over_heat_and_watched(self):
        model = self.provider._room_model(
            {
                "roomid": 24678311,
                "uid": 2,
                "uname": "a",
                "online": 775235,
                "watched_show": {"switch": True, "num": 103297, "text_large": "10.3万人看过"},
                "room_rank_info": {
                    "user_rank_entry": {
                        "user_contribution_rank_entry": {
                            "count": 31389,
                            "count_text": "3万+",
                        }
                    }
                },
                "face": "https://example.com/f.jpg",
            }
        )
        self.assertEqual(model["viewers"], 31389)

    def test_viewers_ignore_online_heat_without_room_audience(self):
        model = self.provider._room_model(
            {
                "roomid": 6650029,
                "uid": 2,
                "uname": "a",
                "online": 2077145,
                "watched_show": {"switch": False, "num": 2053931, "text_large": "205.3万人气"},
                "face": "https://example.com/f.jpg",
            }
        )
        self.assertEqual(model["viewers"], 0)

    def test_room_model_maps_attention_followers(self):
        model = self.provider._room_model(
            {
                "roomid": 1,
                "uid": 2,
                "uname": "a",
                "online": 9,
                "attention": 12345,
                "face": "https://example.com/f.jpg",
            }
        )
        self.assertEqual(model["followers"], 12345)
        self.assertEqual(model["profile_image_url"], "https://example.com/f.jpg")

    def test_clean_text_strips_search_highlight(self):
        self.assertEqual(
            self.provider._clean_text('<em class="keyword">waywardzz</em>'),
            "waywardzz",
        )
        self.assertEqual(
            self.provider._absolute_url("//i0.hdslb.com/bfs/face/a.jpg"),
            "https://i0.hdslb.com/bfs/face/a.jpg",
        )

    def test_normalize_search_prefers_live_room(self):
        rows = self.provider._normalize_search_result(
            {
                "live_room": [
                    {
                        "roomid": 1919216529,
                        "uid": 139006416,
                        "uname": "waywardzz",
                        "title": "我来了",
                        "online": 89933,
                        "live_status": 1,
                        "cover": "//i0.hdslb.com/bfs/live/a.jpg",
                        "uface": "//i2.hdslb.com/bfs/face/b.jpg",
                    }
                ],
                "live_user": [
                    {
                        "roomid": 1919216529,
                        "uid": 139006416,
                        "uname": '<em class="keyword">waywardzz</em>',
                        "is_live": True,
                        "uface": "//i2.hdslb.com/bfs/face/b.jpg",
                    },
                    {
                        "roomid": 2,
                        "uid": 3,
                        "uname": "offline",
                        "is_live": False,
                    },
                ],
            }
        )
        self.assertEqual(len(rows), 1)
        model = self.provider._room_model(rows[0])
        self.assertEqual(model["display_name"], "waywardzz")
        self.assertEqual(model["viewers"], 0)
        self.assertTrue(model["thumbnail"].startswith("https://"))
        self.assertNotIn("<em", model["display_name"])

    async def test_fetch_search_uses_bili_user_and_room_info(self):
        user_payload = {
            "code": 0,
            "data": {
                "result": [
                    {
                        "mid": 139006416,
                        "uname": "waywardzz",
                        "fans": 10,
                        "upic": "//i2.hdslb.com/bfs/face/a.jpg",
                        "is_live": 1,
                        "room_id": 1919216529,
                    },
                    {
                        "mid": 299013902,
                        "uname": "炫神_",
                        "fans": 100,
                        "upic": "//i2.hdslb.com/bfs/face/b.jpg",
                        "is_live": 0,
                        "room_id": 14709735,
                    },
                ]
            },
        }

        async def fake_fetch_json(url, **kwargs):
            if "search/type" in url:
                return user_payload
            if "getRoomInfoOld" in url:
                params = kwargs.get("params") or {}
                mid = str(params.get("mid") or "")
                if mid == "299013902":
                    return {
                        "code": 0,
                        "data": {
                            "liveStatus": 0,
                            "title": "休息中",
                            "cover": "https://example.com/offline.jpg",
                            "online": 0,
                            "roomid": 14709735,
                        },
                    }
                return {
                    "code": 0,
                    "data": {
                        "liveStatus": 1,
                        "title": "我来了",
                        "cover": "https://example.com/cover.jpg",
                        "online": 123,
                        "roomid": 1919216529,
                    },
                }
            if "Room/get_info" in url:
                params = kwargs.get("params") or {}
                rid = str(params.get("room_id") or "")
                if rid == "14709735":
                    return {
                        "code": 0,
                        "data": {
                            "room_id": 14709735,
                            "short_id": 0,
                            "parent_area_id": 9,
                            "parent_area_name": "虚拟主播",
                            "area_id": 744,
                            "area_name": "虚拟日常",
                        },
                    }
                return {
                    "code": 0,
                    "data": {
                        "room_id": 1919216529,
                        "short_id": 0,
                        "parent_area_id": 2,
                        "parent_area_name": "网游",
                        "area_id": 86,
                        "area_name": "英雄联盟",
                    },
                }
            raise AssertionError(url)

        with patch.object(self.provider, "_fetch_json", AsyncMock(side_effect=fake_fetch_json)):
            rows = await self.provider._fetch_search(keyword="炫神_", page=1)
        self.assertEqual(len(rows), 2)
        live = self.provider._room_model(rows[0])
        offline = self.provider._room_model(rows[1])
        self.assertEqual(live["display_name"], "waywardzz")
        self.assertTrue(live["is_online"])
        self.assertEqual(live["parent_area_id"], "2")
        self.assertEqual(offline["display_name"], "炫神_")
        self.assertFalse(offline["is_online"])
        self.assertEqual(offline["room_status"], "offline")
        self.assertEqual(offline["username"], "14709735")
        self.assertEqual(offline["parent_area_id"], "9")
        self.assertEqual(offline["profile_image_url"], "https://i2.hdslb.com/bfs/face/b.jpg")

    async def test_search_respects_selected_parent_area(self):
        """Every Bilibili category search stays inside that parent_area_id."""
        with patch.object(
            self.provider,
            "_fetch_search",
            AsyncMock(
                return_value=[
                    {
                        "roomid": "111",
                        "uid": "1",
                        "uname": "虚拟ti",
                        "is_live": True,
                        "live_status": 1,
                        "online": 10,
                        "area_v2_parent_id": "9",
                        "area_v2_parent_name": "虚拟主播",
                        "face": "https://example.com/a.jpg",
                    },
                    {
                        "roomid": "222",
                        "uid": "2",
                        "uname": "网游ti",
                        "is_live": True,
                        "live_status": 1,
                        "online": 20,
                        "area_v2_parent_id": "2",
                        "area_v2_parent_name": "网游",
                        "face": "https://example.com/b.jpg",
                    },
                ]
            ),
        ), patch.object(
            self.provider,
            "_search_catalogue_fallback",
            AsyncMock(return_value=[]),
        ), patch.object(
            self.provider,
            "_enrich_models",
            AsyncMock(side_effect=lambda models: models),
        ):
            result = await self.provider.list_live_models(
                page=1, limit=10, search="ti", parent_area_id="9"
            )
        names = [m["display_name"] for m in result["models"]]
        self.assertEqual(["虚拟ti"], names)
        self.assertNotIn("网游ti", names)

    def test_search_keyword_variants_strip_trailing(self):
        self.assertEqual(
            self.provider._search_keyword_variants("炫神_"),
            ["炫神_", "炫神"],
        )

    async def test_search_falls_back_to_catalogue(self):
        with patch.object(
            self.provider,
            "_fetch_search",
            AsyncMock(side_effect=RuntimeError("412")),
        ), patch.object(
            self.provider,
            "_search_catalogue_fallback",
            AsyncMock(
                return_value=[
                    {
                        "username": "21144080",
                        "user_id": "1",
                        "display_name": "哔哩哔哩王者荣耀赛事",
                        "source_type": "bilibili",
                        "is_online": True,
                        "room_status": "public",
                        "viewers": 9,
                        "followers": None,
                        "thumbnail": "https://example.com/c.jpg",
                        "profile_image_url": "https://example.com/f.jpg",
                        "parent_area_id": "2",
                        "id": "bilibili:21144080",
                    }
                ]
            ),
        ) as fallback, patch.object(
            self.provider,
            "_enrich_models",
            AsyncMock(side_effect=lambda models: models),
        ):
            result = await self.provider.list_live_models(
                page=1,
                limit=5,
                search="哔哩哔哩王者荣耀赛事",
                parent_area_id="2",
            )
        self.assertEqual(result["provider_status"], "ok")
        self.assertEqual(result["models"][0]["display_name"], "哔哩哔哩王者荣耀赛事")
        fallback.assert_awaited_once()
        self.assertEqual(fallback.await_args.kwargs.get("parent_area_id"), "2")

    async def test_enrich_models_fills_followers(self):
        models = [
            {
                "username": "1",
                "user_id": "11",
                "display_name": "a",
                "followers": None,
                "profile_image_url": "https://example.com/f.jpg",
            }
        ]
        with patch.object(self.provider, "_follower_total", AsyncMock(return_value=42)), patch.object(
            self.provider,
            "_info_by_room",
            AsyncMock(return_value=None),
        ):
            out = await self.provider._enrich_models(models)
        self.assertEqual(out[0]["followers"], 42)

    async def test_enrich_models_replaces_online_heat_with_room_audience(self):
        models = [
            {
                "username": "24678311",
                "room_id": "24678311",
                "user_id": "22",
                "is_online": True,
                "viewers": 775235,
                "followers": 10,
            }
        ]

        async def fake_info(_room_key):
            return {
                "room_info": {"room_id": 24678311, "online": 775235, "live_status": 1},
                "watched_show": {"switch": True, "num": 103297},
                "room_rank_info": {
                    "user_rank_entry": {
                        "user_contribution_rank_entry": {
                            "count": 31389,
                            "count_text": "3万+",
                        }
                    }
                },
            }

        with patch.object(self.provider, "_info_by_room", AsyncMock(side_effect=fake_info)), patch.object(
            self.provider,
            "_online_gold_audience",
            AsyncMock(return_value=None),
        ):
            out = await self.provider._enrich_models(models)
        self.assertEqual(out[0]["viewers"], 31389)

    async def test_enrich_models_uses_gold_rank_when_room_audience_missing(self):
        models = [
            {
                "username": "6650029",
                "room_id": "6650029",
                "user_id": "22",
                "is_online": True,
                "viewers": 2077145,
                "followers": 10,
            }
        ]

        async def fake_info(_room_key):
            return {
                "room_info": {"room_id": 6650029, "uid": 22, "online": 2077145, "live_status": 1},
                "room_rank_info": {"user_rank_entry": {"user_contribution_rank_entry": None}},
                "watched_show": {"num": 2053931, "text_large": "205.3万人气"},
            }

        with patch.object(self.provider, "_info_by_room", AsyncMock(side_effect=fake_info)), patch.object(
            self.provider,
            "_online_gold_audience",
            AsyncMock(return_value=1234),
        ):
            out = await self.provider._enrich_models(models)
        self.assertEqual(out[0]["viewers"], 1234)

    async def test_list_live_catalogue_paginates_unique(self):
        page1 = [
            {
                "roomid": 1,
                "uid": 11,
                "uname": "a",
                "title": "t1",
                "online": 10,
                "parent_id": 9,
                "parent_name": "虚拟主播",
                "area_id": 1,
                "area_name": "x",
            },
            {
                "roomid": 2,
                "uid": 22,
                "uname": "b",
                "title": "t2",
                "online": 9,
                "parent_id": 9,
                "parent_name": "虚拟主播",
                "area_id": 1,
                "area_name": "x",
            },
        ]
        page2 = [
            {
                "roomid": 2,
                "uid": 22,
                "uname": "b",
                "title": "t2",
                "online": 9,
                "parent_id": 9,
                "parent_name": "虚拟主播",
                "area_id": 1,
                "area_name": "x",
            },
            {
                "roomid": 3,
                "uid": 33,
                "uname": "c",
                "title": "t3",
                "online": 8,
                "parent_id": 9,
                "parent_name": "虚拟主播",
                "area_id": 1,
                "area_name": "x",
            },
        ]
        with patch.object(
            self.provider,
            "_fetch_room_page",
            AsyncMock(side_effect=[page1, page2, []]),
        ), patch.object(
            self.provider,
            "_follower_total",
            AsyncMock(return_value=None),
        ), patch.object(
            self.provider,
            "_info_by_room",
            AsyncMock(return_value=None),
        ):
            first = await self.provider.list_live_models(page=1, limit=2, parent_area_id="9")
            second = await self.provider.list_live_models(page=2, limit=2, parent_area_id="9")
        self.assertEqual([m["username"] for m in first["models"]], ["1", "2"])
        self.assertEqual([m["username"] for m in second["models"]], ["3"])
        self.assertEqual(first["provider_status"], "ok")

    async def test_all_catalogue_walks_parent_areas_for_depth(self):
        """parent_area_id=0 pages are identical upstream; walk real parents instead."""
        parent_a = [
            {
                "roomid": 1,
                "uid": 11,
                "uname": "a1",
                "title": "t",
                "online": 10,
                "parent_id": 1,
                "parent_name": "娱乐",
                "area_id": 10,
                "area_name": "聊天室",
            },
            {
                "roomid": 2,
                "uid": 22,
                "uname": "a2",
                "title": "t",
                "online": 9,
                "parent_id": 1,
                "parent_name": "娱乐",
                "area_id": 10,
                "area_name": "聊天室",
            },
        ]
        parent_b = [
            {
                "roomid": 3,
                "uid": 33,
                "uname": "b1",
                "title": "t",
                "online": 8,
                "parent_id": 2,
                "parent_name": "网游",
                "area_id": 86,
                "area_name": "英雄联盟",
            },
            {
                "roomid": 4,
                "uid": 44,
                "uname": "b2",
                "title": "t",
                "online": 7,
                "parent_id": 2,
                "parent_name": "网游",
                "area_id": 86,
                "area_name": "英雄联盟",
            },
        ]

        async def fake_fetch(*, page_no, page_size, parent_area_id, area_id="0"):
            if parent_area_id == "1" and page_no == 1:
                return parent_a
            if parent_area_id == "1":
                return []  # exhaust 娱乐
            if parent_area_id == "2" and page_no == 1:
                return parent_b
            return []

        with patch(
            "app.providers.bilibili.list_bilibili_parent_areas",
            AsyncMock(
                return_value=[
                    {"parent_area_id": "1", "name": "娱乐"},
                    {"parent_area_id": "2", "name": "网游"},
                ]
            ),
        ), patch.object(
            self.provider, "_fetch_room_page", AsyncMock(side_effect=fake_fetch)
        ), patch.object(
            self.provider, "_follower_total", AsyncMock(return_value=None)
        ), patch.object(
            self.provider, "_info_by_room", AsyncMock(return_value=None)
        ):
            first = await self.provider.list_live_models(page=1, limit=2)
            second = await self.provider.list_live_models(page=2, limit=2)

        self.assertEqual([m["username"] for m in first["models"]], ["1", "2"])
        self.assertEqual([m["username"] for m in second["models"]], ["3", "4"])
        self.assertGreaterEqual(int(second["total_pages"]), 2)
        self.assertTrue(int(second["total"]) >= 4)

    async def test_list_live_models_resolves_tag_to_child_area(self):
        rooms = [
            {
                "roomid": 101,
                "uid": 1,
                "uname": "lol1",
                "title": "ranked",
                "online": 100,
                "parent_id": 2,
                "parent_name": "网游",
                "area_id": 86,
                "area_name": "英雄联盟",
            },
            {
                "roomid": 102,
                "uid": 2,
                "uname": "lol2",
                "title": "aram",
                "online": 90,
                "parent_id": 2,
                "parent_name": "网游",
                "area_id": 86,
                "area_name": "英雄联盟",
            },
        ]
        fetch = AsyncMock(return_value=rooms)

        async def fake_resolve(name, area_fetcher=None):
            self.assertEqual(name, "英雄联盟")
            return {
                "name": "英雄联盟",
                "parent_area_id": "2",
                "area_id": "86",
                "match_kind": "child",
            }

        with patch(
            "app.providers.bilibili.resolve_bilibili_area_by_name",
            AsyncMock(side_effect=fake_resolve),
        ), patch.object(self.provider, "_fetch_room_page", fetch), patch.object(
            self.provider,
            "_follower_total",
            AsyncMock(return_value=None),
        ), patch.object(
            self.provider,
            "_info_by_room",
            AsyncMock(return_value=None),
        ):
            result = await self.provider.list_live_models(
                page=1, limit=24, tags=["英雄联盟"]
            )
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["models"][0]["tags"], ["网游", "英雄联盟"])
        fetch.assert_awaited()
        kwargs = fetch.await_args.kwargs
        self.assertEqual(kwargs["parent_area_id"], "2")
        self.assertEqual(kwargs["area_id"], "86")

    async def test_registry_includes_bilibili(self):
        registry = create_provider_registry(db=None)
        self.assertIn("bilibili", registry.source_types())
        caps = registry.get("bilibili").capabilities
        self.assertTrue(caps.can_discover)
        self.assertTrue(caps.can_stream)
        self.assertTrue(caps.can_record)
        self.assertTrue(caps.can_login)
        self.assertTrue(caps.can_follow)
        self.assertTrue(caps.can_sync_following)
        self.assertFalse(caps.can_password_login)


class _MemorySessionStore:
    def __init__(self):
        self.state = {}

    async def get(self, source_type):
        return dict(self.state.get(source_type) or {})

    async def save(
        self,
        source_type,
        username=None,
        is_logged_in=False,
        cookies=None,
        local_storage=None,
        last_error=None,
    ):
        from app.providers.sessions import ProviderSessionStore

        self.state[source_type] = {
            "username": username,
            "is_logged_in": is_logged_in,
            "cookies": cookies or [],
            "localStorage": local_storage or [],
            "last_error": last_error,
            "cookie_header": ProviderSessionStore.cookies_to_header(cookies or []),
        }

    async def clear(self, source_type):
        self.state.pop(source_type, None)

    async def cookie_header(self, source_type):
        from app.providers.sessions import ProviderSessionStore

        return ProviderSessionStore.cookies_to_header(
            (await self.get(source_type)).get("cookies")
        )


def _bili_provider():
    return BilibiliProvider(
        "bilibili",
        "Bilibili",
        "https://live.bilibili.com/{username}",
        ("live.bilibili.com",),
        _MemorySessionStore(),
    )


class BilibiliFollowingTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_login_is_session_only(self):
        provider = _bili_provider()
        result = await provider.login("user", "pass")
        self.assertFalse(result["success"])
        self.assertIn("SESSDATA", result["error"])

    async def test_import_session_requires_sessdata(self):
        provider = _bili_provider()
        result = await provider.import_session(cookie_header="bili_jct=csrf")
        self.assertFalse(result["success"])
        self.assertIn("SESSDATA", result["error"])

    async def test_sync_following_uses_live_following_list(self):
        provider = _bili_provider()
        await provider.session_store.save(
            "bilibili",
            username="me",
            is_logged_in=True,
            cookies=[
                {"name": "SESSDATA", "value": "sess"},
                {"name": "bili_jct", "value": "csrf"},
            ],
        )
        with patch.object(
            provider,
            "_bili_nav",
            AsyncMock(return_value={"mid": "99", "uname": "me", "isLogin": True}),
        ), patch.object(
            provider,
            "_auth_request_json",
            AsyncMock(
                return_value={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "roomid": 24678311,
                                "uid": 123,
                                "uname": "Anchor",
                                "face": "https://i0.hdslb.com/face.jpg",
                                "live_status": 1,
                                "online": 88,
                            }
                        ],
                        "totalPage": 1,
                    },
                }
            ),
        ):
            items = await provider.sync_following()
        self.assertEqual(1, len(items))
        self.assertEqual("24678311", items[0]["username"])
        self.assertEqual("Anchor", items[0]["display_name"])
        self.assertTrue(items[0]["is_online"])

    async def test_follow_posts_relation_modify(self):
        provider = _bili_provider()
        await provider.session_store.save(
            "bilibili",
            username="me",
            is_logged_in=True,
            cookies=[
                {"name": "SESSDATA", "value": "sess"},
                {"name": "bili_jct", "value": "csrf"},
            ],
        )
        posted = {}

        async def fake_auth(method, url, **kwargs):
            posted["method"] = method
            posted["url"] = url
            posted["data"] = kwargs.get("data")
            return {"code": 0}

        with patch.object(
            provider,
            "_bili_uid_for_room",
            AsyncMock(return_value="123"),
        ), patch.object(provider, "_auth_request_json", fake_auth):
            result = await provider.follow("24678311")
        self.assertTrue(result["success"])
        self.assertEqual("POST", posted["method"])
        self.assertIn("relation/modify", posted["url"])
        self.assertEqual("1", posted["data"]["act"])
        self.assertEqual("123", posted["data"]["fid"])
        self.assertEqual("csrf", posted["data"]["csrf"])


if __name__ == "__main__":
    unittest.main()

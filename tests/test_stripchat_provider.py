"""Stripchat C1 unique-pool / gender routing unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.providers.stripchat import StripchatProvider


def _model(uid, username, viewers=10, gender="female", **extra):
    row = {
        "username": username,
        "status": "public",
        "isOnline": True,
        "gender": gender,
        "genderGroup": {"female": "F", "male": "M", "trans": "T", "maleFemale": "F"}.get(gender, "F"),
        "viewersCount": viewers,
        "id": uid,
    }
    row.update(extra)
    return row


def _pad(models, width=24):
    out = list(models)
    base = 1000 + int(models[0]["id"]) * 100
    while len(out) < width:
        i = len(out)
        out.append(_model(base + i, f"pad_{base}_{i}", viewers=1))
    return {"models": out, "totalCount": 1000}


def _provider() -> StripchatProvider:
    return StripchatProvider(
        "stripchat",
        "Stripchat",
        "https://stripchat.com/{username}",
        ("stripchat.com",),
    )


class StripchatProviderC1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_pagination_overlap_budget_and_gender(self):
        provider = _provider()
        base_models = [_model(i, f"a{i}", 100 - i) for i in range(1, 25)]
        full = {"models": base_models, "totalCount": 999}
        api = AsyncMock(side_effect=[full, full, full, full])
        with patch.object(provider, "_stripchat_api_json", api):
            p1 = await provider.list_live_models(page=1, limit=24, gender="female", search="", tags=[])
            p2 = await provider.list_live_models(page=2, limit=24, gender="female", search="", tags=[])
        self.assertEqual(24, len(p1["models"]))
        self.assertEqual(len(p1["models"]), len({m["username"] for m in p1["models"]}))
        self.assertEqual(0, len(p2["models"]))
        self.assertFalse(p2["total_pages"] > p2["page"])
        self.assertEqual(3, api.await_count)
        self.assertEqual([0, 24, 48], [c.kwargs["params"]["offset"] for c in api.await_args_list])

        provider = _provider()
        shared = [_model(1, "s1", 100), _model(2, "s2", 90)]
        w1 = _pad(shared + [_model(10 + i, f"n{i}", 80 - i) for i in range(22)])
        w2_models = shared + [_model(200 + i, f"m{i}", 50 - i) for i in range(22)]
        w2 = {"models": w2_models, "totalCount": 1000}
        w3 = {"models": [_model(300 + i, f"p{i}", 40 - i) for i in range(24)], "totalCount": 1000}
        w4 = {"models": [_model(400 + i, f"q{i}", 10) for i in range(24)], "totalCount": 1000}
        api = AsyncMock(side_effect=[w1, w2, w3, w4])
        gets_before = []
        with patch.object(provider, "_stripchat_api_json", api):
            page_one = await provider.list_live_models(page=1, limit=24, gender=None, search="", tags=[])
            gets_before.append(api.await_count)
            page_two = await provider.list_live_models(page=2, limit=24, gender=None, search="", tags=[])
            gets_before.append(api.await_count - gets_before[0])
            page_three = await provider.list_live_models(page=3, limit=24, gender=None, search="", tags=[])
            gets_before.append(api.await_count - gets_before[0] - gets_before[1])
        self.assertTrue(all(g <= 3 for g in gets_before))
        offsets = [c.kwargs["params"]["offset"] for c in api.await_args_list]
        self.assertEqual(offsets, sorted(set(offsets)))
        n1 = [m["username"] for m in page_one["models"]]
        n2 = [m["username"] for m in page_two["models"]]
        n3 = [m["username"] for m in page_three["models"]]
        self.assertEqual(24, len(n1), n1)
        self.assertEqual(24, len(n2), n2)
        self.assertEqual(24, len(n3), n3)
        self.assertEqual(set(), set(n1) & set(n2))
        self.assertEqual(set(), set(n1) & set(n3))
        self.assertEqual(set(), set(n2) & set(n3))

        provider = _provider()
        calls = []

        async def _gendered(method, path, params=None, **kwargs):
            calls.append(dict(params or {}))
            tag = (params or {}).get("primaryTag")
            if tag == "men":
                return _pad([_model(201, "man1", 11, gender="male")])
            if tag == "trans":
                return _pad([_model(301, "ts1", 12, gender="trans")])
            if tag == "couples":
                models = [
                    _model(401, "cpl1", 13, gender="females", genderGroup="F", broadcastGender="group")
                ]
                while len(models) < 24:
                    i = len(models)
                    models.append(
                        _model(401 + i, f"cpl_pad_{i}", 1, gender="females", genderGroup="F", broadcastGender="group")
                    )
                return {"models": models, "totalCount": 24}
            return _pad([_model(101, "girl1", 14, gender="female")])

        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_gendered)):
            male = await provider.list_live_models(page=1, limit=24, gender="male", search="", tags=[])
            trans = await provider.list_live_models(page=1, limit=24, gender="trans", search="", tags=[])
            couple = await provider.list_live_models(page=1, limit=24, gender="couple", search="", tags=[])
            female = await provider.list_live_models(page=1, limit=24, gender="female", search="", tags=[])
            all_cat = await provider.list_live_models(page=1, limit=24, gender=None, search="", tags=[])

        self.assertEqual(["men", "trans", "couples", "girls", "girls"], [c["primaryTag"] for c in calls])
        self.assertIn("man1", {m["username"] for m in male["models"]})
        self.assertIn("ts1", {m["username"] for m in trans["models"]})
        self.assertNotIn("man1", {m["username"] for m in female["models"]})
        self.assertIn("couple", next(m["tags"] for m in couple["models"] if m["username"] == "cpl1"))
        self.assertEqual(24, len(all_cat["models"]))
        pools = getattr(provider, "_stripchat_unique_pools", {})
        girl_keys = [key for key in pools if key[0] == "girls"]
        self.assertEqual(2, len(girl_keys))
        self.assertEqual({"", "female"}, {key[1] for key in girl_keys})

        provider = _provider()
        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertIsNone(
                await provider._stripchat_list_live_models_api(
                    page=1, limit=24, gender=None, search="", tags=[]
                )
            )

    async def test_catalog_viewers_scans_past_top_150(self):
        provider = _provider()
        target = _model(125562367, "Miu1_girl", viewers=703)

        async def _pages(method, path, params=None, **kwargs):
            self.assertEqual("/models", path)
            offset = int((params or {}).get("offset") or 0)
            if offset < 150:
                return {"models": [_model(offset + i, f"pad_{offset}_{i}", 10) for i in range(50)]}
            if offset == 150:
                return {"models": [target] + [_model(900 + i, f"tail_{i}", 5) for i in range(49)]}
            return {"models": []}

        api = AsyncMock(side_effect=_pages)
        with patch.object(provider, "_stripchat_api_json", api):
            viewers = await provider._stripchat_catalog_viewers(
                "Miu1_girl",
                model_id="125562367",
                model=target,
            )
        self.assertEqual(703, viewers)
        offsets = [c.kwargs["params"]["offset"] for c in api.await_args_list]
        self.assertEqual([0, 50, 100, 150], offsets)

    async def test_resolve_watch_meta_backfills_catalog_viewers(self):
        provider = _provider()
        cam = {
            "cam": {"streamName": "125562367"},
            "user": {
                "user": {
                    "id": 125562367,
                    "username": "Miu1_girl",
                    "isOnline": True,
                    "status": "public",
                    "gender": "female",
                    "favoritedCount": 244945,
                    "avatarUrl": "https://static-proxy.strpst.com/avatars/miu-full",
                }
            },
        }

        async def _api(method, path, params=None, **kwargs):
            if path.endswith("/cam"):
                return cam
            self.assertEqual("/models", path)
            offset = int((params or {}).get("offset") or 0)
            if offset == 0:
                return {"models": [_model(1, "pad_a", 10) for _ in range(50)]}
            if offset == 50:
                return {"models": [_model(125562367, "Miu1_girl", 688)]}
            return {"models": []}

        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_api)):
            meta = await provider.resolve_watch_meta("Miu1_girl")
        self.assertTrue(meta["isOnline"])
        self.assertEqual(688, meta["viewers"])
        self.assertEqual("Miu1_girl", meta["username"])
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/miu-full",
            meta["profileImageUrl"],
        )

    def test_offline_thumbnail_prefers_preview_over_dead_snapshot(self):
        provider = _provider()
        offline = _model(
            89673378,
            "xxxnba",
            viewers=0,
            isOnline=False,
            isLive=False,
            status="off",
            snapshotTimestamp=1785596370,
            previewUrlThumbBig="https://static-proxy.strpst.com/previews/xxx-big",
            previewUrlThumbSmall="https://static-proxy.strpst.com/previews/xxx-small",
            avatarUrl="https://static-proxy.strpst.com/avatars/xxx-full",
            avatarUrlThumb="https://static-proxy.strpst.com/avatars/xxx-thumb",
        )
        item = provider._stripchat_model_item(offline)
        self.assertEqual(
            "https://static-proxy.strpst.com/previews/xxx-big",
            item["thumbnail"],
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/xxx-full",
            item["profile_image_url"],
        )
        self.assertFalse(item["is_online"])
        self.assertEqual("offline", item["room_status"])

        online = dict(offline, isOnline=True, isLive=True, status="public")
        live_item = provider._stripchat_model_item(online)
        self.assertEqual(
            "https://img.doppiocdn.net/snapshot/89673378/1785596370",
            live_item["thumbnail"],
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/xxx-full",
            live_item["profile_image_url"],
        )

    def test_catalogue_doppiocdn_avatar_rewritten_to_static_proxy(self):
        """Discover catalogue returns doppiocdn avatars that 404; Media/Watch use static-proxy."""
        provider = _provider()
        catalog = _model(
            73697527,
            "lunagirl13",
            viewers=100,
            snapshotTimestamp=1785636930,
            avatarUrl="https://img.doppiocdn.net/avatars/c/c/e/ccec547a4b4c6a83eefc56185de92200-full",
            avatarUrlThumb="https://img.doppiocdn.net/avatars/c/c/e/ccec547a4b4c6a83eefc56185de92200-thumb",
            previewUrlThumbBig="https://img.doppiocdn.net/previews/8/5/2/85273cc71f248324ac0336ae80075736-thumb-big",
        )
        item = provider._stripchat_model_item(catalog)
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/c/c/e/ccec547a4b4c6a83eefc56185de92200-full",
            item["profile_image_url"],
        )
        # Live cover snapshots stay on doppiocdn (those still 200).
        self.assertEqual(
            "https://img.doppiocdn.net/snapshot/73697527/1785636930",
            item["thumbnail"],
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/previews/8/5/2/85273cc71f248324ac0336ae80075736-thumb-big",
            provider._stripchat_abs_media_url(catalog["previewUrlThumbBig"]),
        )

    def test_missing_avatar_does_not_use_live_snapshot(self):
        """No uploaded face photo → empty profile_image_url (letter avatar in UI)."""
        provider = _provider()
        model = _model(
            99112233,
            "djjfnfczks",
            viewers=42,
            snapshotTimestamp=1785636930,
            previewUrlThumbBig="https://static-proxy.strpst.com/previews/djj-big",
        )
        item = provider._stripchat_model_item(model)
        self.assertEqual(
            "https://img.doppiocdn.net/snapshot/99112233/1785636930",
            item["thumbnail"],
        )
        self.assertEqual("", item["profile_image_url"])

    def test_status_off_forces_offline_even_when_isonline_true(self):
        provider = _provider()
        stale = _model(
            125562367,
            "Miu1_girl",
            viewers=0,
            isOnline=True,
            isLive=True,
            status="off",
            snapshotTimestamp=1785626640,
            previewUrlThumbBig="https://static-proxy.strpst.com/previews/miu-big",
            avatarUrl="https://static-proxy.strpst.com/avatars/miu-full",
        )
        item = provider._stripchat_model_item(stale)
        self.assertFalse(item["is_online"])
        self.assertEqual("offline", item["room_status"])
        self.assertEqual(0, item["viewers"])
        self.assertEqual(
            "https://static-proxy.strpst.com/previews/miu-big",
            item["thumbnail"],
        )

    async def test_search_backfills_catalog_viewers_when_cam_omits_count(self):
        provider = _provider()
        cam = {
            "cam": {"streamName": "125562367"},
            "user": {
                "user": {
                    "id": 125562367,
                    "username": "Miu1_girl",
                    "isOnline": True,
                    "status": "public",
                    "gender": "female",
                    "favoritedCount": 244945,
                }
            },
        }

        async def _api(method, path, params=None, **kwargs):
            if path.endswith("/cam"):
                return cam
            self.assertEqual("/models", path)
            offset = int((params or {}).get("offset") or 0)
            if offset == 150:
                return {"models": [_model(125562367, "Miu1_girl", 688)]}
            return {"models": [_model(offset + i, f"pad_{offset}_{i}", 3) for i in range(50)]}

        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_api)):
            with patch.object(
                provider, "_stripchat_search_models_html", AsyncMock(return_value=[])
            ):
                payload = await provider.list_live_models(page=1, limit=12, search="Miu1_girl")
        self.assertEqual(1, len(payload["models"]))
        self.assertEqual(688, payload["models"][0]["viewers"])

    async def test_private_search_skips_catalog_viewer_scan(self):
        provider = _provider()
        cam = {
            "cam": {"streamName": "125562367", "groupShowUsersCount": 1},
            "user": {
                "user": {
                    "id": 125562367,
                    "username": "Miu1_girl",
                    "isOnline": True,
                    "status": "private",
                    "gender": "female",
                    "favoritedCount": 245050,
                }
            },
        }
        api = AsyncMock(side_effect=lambda method, path, params=None, **kwargs: cam)
        with patch.object(provider, "_stripchat_api_json", api):
            with patch.object(
                provider, "_stripchat_search_models_html", AsyncMock(return_value=[])
            ):
                with patch.object(
                    provider,
                    "_stripchat_catalog_viewers",
                    AsyncMock(return_value=999),
                ) as catalog:
                    payload = await provider.list_live_models(page=1, limit=12, search="Miu1_girl")
        self.assertEqual(1, len(payload["models"]))
        item = payload["models"][0]
        self.assertEqual("private", item["room_status"])
        self.assertTrue(item["is_online"])
        self.assertEqual(0, item["viewers"])
        catalog.assert_not_awaited()

    def test_private_cam_lock_with_stale_public_status(self):
        """Guest cam is locked in private while user.status can still say public."""
        provider = _provider()
        payload = {
            "cam": {
                "streamName": "125562367",
                "isCamAvailable": False,
                "isCamActive": False,
                "privateMode": True,
            },
            "user": {
                "user": {
                    "id": 125562367,
                    "username": "Miu1_girl",
                    "isOnline": True,
                    "isLive": True,
                    "status": "public",
                    "gender": "female",
                }
            },
        }
        model = provider._stripchat_profile_model(payload)
        item = provider._stripchat_model_item(model)
        self.assertTrue(item["is_online"])
        self.assertEqual("private", item["room_status"])
        self.assertEqual(0, item["viewers"])

    def test_group_show_status_is_private_even_when_flags_offline(self):
        provider = _provider()
        payload = {
            "cam": {
                "streamName": "99",
                "isCamAvailable": False,
                "groupShowAnnouncement": {"topic": "tip menu"},
            },
            "user": {
                "user": {
                    "id": 99,
                    "username": "group_model",
                    "isOnline": False,
                    "isLive": False,
                    "status": "groupShow",
                }
            },
        }
        model = provider._stripchat_profile_model(payload)
        item = provider._stripchat_model_item(model)
        self.assertTrue(item["is_online"])
        self.assertEqual("groupshow", item["room_status"])

    async def test_resolve_watch_meta_private_not_live_zero(self):
        provider = _provider()
        cam = {
            "cam": {
                "streamName": "125562367",
                "isCamAvailable": False,
                "isCamActive": False,
                "show": "private",
            },
            "user": {
                "user": {
                    "id": 125562367,
                    "username": "Miu1_girl",
                    "isOnline": True,
                    "status": "public",
                    "gender": "female",
                }
            },
        }

        async def _api(method, path, params=None, **kwargs):
            if path.endswith("/cam"):
                return cam
            raise AssertionError(f"unexpected catalogue fetch for private: {path}")

        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_api)):
            meta = await provider.resolve_watch_meta("Miu1_girl")
        self.assertTrue(meta["isOnline"])
        self.assertEqual("private", meta["roomStatus"])
        self.assertEqual(0, meta["viewers"])

    def test_parse_search_models_keeps_contiguous_id_contains(self):
        provider = _provider()
        html = """
        <section class="ModelThumbGrid SearchPage__models">
          <a data-model-id="1" href="/yyydgda">
            <svg data-testid="model-list-item-live"></svg>
            <img src="https://img.doppiocdn.org/snapshot/1/1"/>
            <img alt="yyydgda's Live Chat Room"/>
          </a>
          <a data-model-id="2" href="/yyydgegyeggsywg">
            <img alt="yyydgegyeggsywg's Offline Chat Room"
                 src="https://static-proxy.strpst.com/previews/x-thumb-small"/>
          </a>
          <a data-model-id="3" href="/CindyyyDolll"></a>
        </section>
        """
        items = provider._stripchat_parse_search_models(html, "yyydg")
        self.assertEqual(
            [("yyydgda", True), ("yyydgegyeggsywg", False)],
            [(i["username"], i["is_online"]) for i in items],
        )

    async def test_search_includes_substring_usernames_from_html_without_bulk_hydrate(self):
        provider = _provider()

        async def _api(method, path, params=None, **kwargs):
            # Exact needle missing; HTML cards must still surface without cam hydrate.
            raise Exception(f"unexpected {path}")

        html_items = [
            {
                "username": "yyydgda",
                "display_name": "yyydgda",
                "is_online": True,
                "room_status": "public",
                "viewers": 0,
                "tags": ["public"],
                "thumbnail": "https://img.doppiocdn.org/snapshot/1/1",
                "profile_image_url": "",
                "source_type": "stripchat",
                "channel_url": "https://stripchat.com/yyydgda",
            },
            {
                "username": "Evelyn_Evyyy",
                "display_name": "Evelyn_Evyyy",
                "is_online": True,
                "room_status": "public",
                "viewers": 0,
                "tags": ["public"],
                "thumbnail": "",
                "profile_image_url": "",
                "source_type": "stripchat",
                "channel_url": "https://stripchat.com/Evelyn_Evyyy",
            },
        ]
        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_api)):
            with patch.object(
                provider,
                "_stripchat_search_models_html",
                AsyncMock(return_value=html_items),
            ):
                with patch.object(
                    provider,
                    "_stripchat_enrich_search_page",
                    AsyncMock(side_effect=lambda items, **kwargs: items),
                ):
                    payload = await provider.list_live_models(page=1, limit=12, search="yyy")
        names = [m["username"] for m in payload["models"]]
        self.assertEqual(["yyydgda", "Evelyn_Evyyy"], names)

    async def test_short_search_falls_back_to_catalogue_contains(self):
        provider = _provider()

        async def _api(method, path, params=None, **kwargs):
            offset = int((params or {}).get("offset") or 0)
            if offset == 0:
                return {
                    "models": [
                        _model(1, "alpha", 10),
                        _model(2, "yyydgda", 55),
                        _model(3, "beta", 8),
                    ],
                    "totalCount": 100,
                }
            return {"models": [], "totalCount": 100}

        with patch.object(provider, "_stripchat_api_json", AsyncMock(side_effect=_api)):
            with patch.object(
                provider, "_stripchat_search_models_html", AsyncMock(return_value=[])
            ):
                with patch.object(
                    provider,
                    "_stripchat_enrich_search_page",
                    AsyncMock(side_effect=lambda items, **kwargs: items),
                ):
                    payload = await provider.list_live_models(page=1, limit=12, search="yy")
        self.assertEqual(["yyydgda"], [m["username"] for m in payload["models"]])
        self.assertEqual(55, payload["models"][0]["viewers"])

    async def test_enrich_search_page_refreshes_cover_and_viewers(self):
        provider = _provider()
        html_item = {
            "username": "yyydgda",
            "display_name": "yyydgda",
            "is_online": True,
            "room_status": "public",
            "viewers": 0,
            "tags": ["public"],
            "thumbnail": "https://img.doppiocdn.org/snapshot/1/stale",
            "profile_image_url": "",
            "source_type": "stripchat",
            "channel_url": "https://stripchat.com/yyydgda",
        }
        cam_item = {
            "username": "yyydgda",
            "display_name": "yyydgda",
            "is_online": True,
            "room_status": "public",
            "viewers": 0,
            "tags": ["public"],
            "thumbnail": "https://img.doppiocdn.net/snapshot/1/fresh",
            "profile_image_url": "https://static-proxy.strpst.com/avatars/face",
            "source_type": "stripchat",
            "channel_url": "https://stripchat.com/yyydgda",
        }
        with patch.object(
            provider, "_stripchat_hydrate_search_username", AsyncMock(return_value=cam_item)
        ):
            with patch.object(
                provider,
                "_stripchat_catalog_viewers_bulk",
                AsyncMock(return_value={"yyydgda": 903}),
            ):
                out = await provider._stripchat_enrich_search_page(
                    [html_item], needle="yyy", gender=None, tags=[]
                )
        self.assertEqual(1, len(out))
        self.assertEqual("https://img.doppiocdn.net/snapshot/1/fresh", out[0]["thumbnail"])
        self.assertEqual("https://static-proxy.strpst.com/avatars/face", out[0]["profile_image_url"])
        self.assertEqual(903, out[0]["viewers"])

    def test_best_variant_picks_highest_height(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480\n"
            "480p.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
            "720p.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
            "1080p.m3u8\n"
        )
        url = StripchatProvider._stripchat_variant_url_for_height(
            "https://edge.example/master.m3u8",
            master,
            None,
        )
        self.assertTrue(str(url).endswith("1080p.m3u8"))
        capped = StripchatProvider._stripchat_variant_url_for_height(
            "https://edge.example/master.m3u8",
            master,
            720,
        )
        self.assertTrue(str(capped).endswith("720p.m3u8"))


if __name__ == "__main__":
    unittest.main()

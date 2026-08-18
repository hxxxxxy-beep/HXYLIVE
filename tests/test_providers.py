import unittest
from unittest.mock import AsyncMock, patch

from app.providers.base import ProviderAuthError
from app.providers.builtin import ChaturbateProvider
from app.providers.registry import create_provider_registry


class _DummyDB:
    pass


class ProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_registry_includes_supported_sources(self):
        registry = create_provider_registry(_DummyDB())

        self.assertEqual({"twitch", "chaturbate", "bilibili", "stripchat"}, registry.source_types())
        self.assertEqual(
            {"twitch", "chaturbate", "bilibili", "stripchat"},
            {
                provider.source_type
                for provider in registry.all()
                if provider.capabilities.can_discover
            },
        )
        self.assertFalse(registry.has("onlyfans"))
        self.assertFalse(registry.has("fansly"))
        self.assertFalse(registry.has("manyvids"))
        self.assertTrue(registry.has("stripchat"))
        self.assertFalse(registry.has("cam4"))
        self.assertTrue(registry.get("chaturbate").capabilities.can_login)
        self.assertTrue(registry.get("chaturbate").capabilities.can_sync_following)
        self.assertTrue(registry.get("twitch").capabilities.can_login)
        self.assertTrue(registry.get("twitch").capabilities.can_follow)
        self.assertTrue(registry.get("twitch").capabilities.can_sync_following)
        self.assertFalse(registry.get("twitch").capabilities.can_password_login)
        self.assertTrue(registry.get("twitch").capabilities.can_discover)
        self.assertTrue(registry.get("twitch").capabilities.can_stream)
        self.assertTrue(registry.get("twitch").capabilities.can_record)
        stripchat = registry.get("stripchat")
        self.assertTrue(stripchat.capabilities.can_discover)
        self.assertTrue(stripchat.capabilities.can_stream)
        self.assertTrue(stripchat.capabilities.can_follow)
        self.assertTrue(stripchat.capabilities.can_sync_following)
        self.assertTrue(stripchat.capabilities.can_login)
        self.assertTrue(stripchat.capabilities.can_password_login)
        bilibili = registry.get("bilibili")
        self.assertTrue(bilibili.capabilities.can_login)
        self.assertTrue(bilibili.capabilities.can_follow)
        self.assertTrue(bilibili.capabilities.can_sync_following)
        self.assertFalse(bilibili.capabilities.can_password_login)


class BuiltinProviderDiscoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_chaturbate_resolve_stream_carries_llhls_video_index(self):
        provider = ChaturbateProvider()

        with (
            patch(
                "app.resolvers.chaturbate.resolve_m3u8_async",
                AsyncMock(return_value="https://edge.example.test/live/llhls.m3u8"),
            ) as resolve,
            patch(
                "app.resolvers.chaturbate.resolve_llhls_master_playlist",
                AsyncMock(return_value={
                    "video_stream_index": 3,
                    "text": "#EXTM3U\n",
                    "base_url": "https://edge.example.test/live/llhls.m3u8",
                    "content_type": "application/vnd.apple.mpegurl",
                }),
            ) as pick_index,
        ):
            stream = await provider.resolve_stream("alice", max_height=None)

        self.assertEqual("https://edge.example.test/live/llhls.m3u8", stream.url)
        self.assertEqual(3, stream.ffmpeg_video_stream_index)
        self.assertEqual("#EXTM3U\n", stream.hls_playlist_text)
        resolve.assert_awaited_once_with("alice", max_height=None)
        pick_index.assert_awaited_once()

    async def test_chaturbate_discover_filters_generic_kwargs(self):
        class FakeAPI:
            def __init__(self):
                self.kwargs = None

            async def get_live_models(self, **kwargs):
                self.kwargs = kwargs
                return {"models": [{"username": "alice"}]}

        api = FakeAPI()
        provider = ChaturbateProvider(api=api)

        result = await provider.list_live_models(
            page=2,
            limit=7,
            gender="female",
            search="ali",
            tags=["french"],
            allow_browser=True,
        )

        self.assertEqual({"page": 2, "limit": 7, "gender": "female", "search": "ali", "tag": "french"}, api.kwargs)
        self.assertEqual("chaturbate", result["models"][0]["source_type"])

    async def test_chaturbate_search_includes_offline_exact_username(self):
        class FakeAPI:
            async def get_live_models(self, **kwargs):
                return {
                    "models": [
                        {"username": "ashleyalban_fan", "is_online": True},
                        {"username": "otherlive", "is_online": True},
                    ],
                    "total": 2,
                }

            async def lookup_username(self, username):
                self.lookup = username
                return {
                    "username": username,
                    "display_name": username,
                    "is_online": False,
                    "room_status": "offline",
                    "viewers": 0,
                    "thumbnail": f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg",
                    "followers": None,
                }

        api = FakeAPI()
        provider = ChaturbateProvider(api=api)
        result = await provider.list_live_models(page=1, limit=24, search="AshleyAlban")
        self.assertEqual("ashleyalban", api.lookup)
        self.assertEqual("ashleyalban", result["models"][0]["username"])
        self.assertFalse(result["models"][0]["is_online"])
        self.assertEqual("offline", result["models"][0]["room_status"])
        # Unrelated roomlist rows are dropped; partial username matches are kept.
        self.assertEqual(
            ["ashleyalban", "ashleyalban_fan"],
            [item["username"] for item in result["models"]],
        )

    async def test_chaturbate_search_miss_clears_global_roomlist_total(self):
        class FakeAPI:
            async def get_live_models(self, **kwargs):
                # Roomlist keywords often return zero rooms but still advertise
                # the unfiltered inventory total.
                return {"models": [], "total": 500, "total_pages": 21, "page": 1, "limit": 24}

            async def lookup_username(self, username):
                return None

        provider = ChaturbateProvider(api=FakeAPI())
        result = await provider.list_live_models(page=1, limit=24, search="mazzanti")
        self.assertEqual([], result["models"])
        self.assertEqual(0, result["total"])
        self.assertEqual(1, result["total_pages"])

    async def test_chaturbate_password_exact_hit_does_not_paginate_global_total(self):
        class FakeAPI:
            async def get_live_models(self, **kwargs):
                return {"models": [], "total": 500, "total_pages": 21, "page": 1, "limit": 24}

            async def lookup_username(self, username):
                return {
                    "username": username,
                    "display_name": username,
                    "is_online": True,
                    "room_status": "password_protected",
                    "viewers": 0,
                    "thumbnail": f"https://thumb.live.mmcdn.com/riw/{username}.jpg",
                }

        provider = ChaturbateProvider(api=FakeAPI())
        result = await provider.list_live_models(page=1, limit=24, search="mazzanti_")
        self.assertEqual(["mazzanti_"], [m["username"] for m in result["models"]])
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["total_pages"])

    async def test_chaturbate_search_tries_trailing_underscore_username(self):
        class FakeAPI:
            def __init__(self):
                self.lookups = []

            async def get_live_models(self, **kwargs):
                return {"models": [], "total": 0, "total_pages": 1, "page": 1, "limit": 24}

            async def lookup_username(self, username):
                self.lookups.append(username)
                if username == "mazzanti_":
                    return {
                        "username": "mazzanti_",
                        "display_name": "mazzanti_",
                        "is_online": True,
                        "room_status": "password_protected",
                        "viewers": 0,
                    }
                return None

        api = FakeAPI()
        provider = ChaturbateProvider(api=api)
        result = await provider.list_live_models(page=1, limit=24, search="mazzanti")
        self.assertEqual(["mazzanti", "mazzanti_"], api.lookups)
        self.assertEqual(["mazzanti_"], [m["username"] for m in result["models"]])
        self.assertEqual("password_protected", result["models"][0]["room_status"])

    async def test_chaturbate_resolve_watch_meta_uses_summary_profile_image(self):
        class FakeAPI:
            async def lookup_username(self, username):
                return {
                    "username": username,
                    "display_name": username,
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 88,
                    "followers": 1200,
                    "thumbnail": f"https://thumb.live.mmcdn.com/riw/{username}.jpg",
                    "profile_image_url": "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
                    "channel_url": f"https://chaturbate.com/{username}/",
                }

        provider = ChaturbateProvider(api=FakeAPI())
        meta = await provider.resolve_watch_meta("AnitaSweetness")
        self.assertTrue(meta["isOnline"])
        self.assertEqual(88, meta["viewers"])
        self.assertEqual(1200, meta["followers"])
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            meta["profileImageUrl"],
        )
        self.assertEqual(
            "https://thumb.live.mmcdn.com/riw/AnitaSweetness.jpg",
            meta["thumbnail"],
        )

    async def test_chaturbate_resolve_watch_meta_backfills_zero_viewers(self):
        class FakeAPI:
            async def lookup_username(self, username):
                return {
                    "username": username,
                    "display_name": username,
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 0,
                    "followers": 10,
                    "thumbnail": f"https://thumb.live.mmcdn.com/riw/{username}.jpg",
                    "profile_image_url": "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
                    "channel_url": f"https://chaturbate.com/{username}/",
                }

            async def get_live_models(self, **kwargs):
                return {
                    "models": [{
                        "username": kwargs["search"],
                        "viewers": 144,
                        "room_status": "public",
                        "tags": ["french"],
                    }]
                }

        provider = ChaturbateProvider(api=FakeAPI())
        meta = await provider.resolve_watch_meta("nancy_a1")
        self.assertTrue(meta["isOnline"])
        self.assertEqual(144, meta["viewers"])

    async def test_chaturbate_status_supplements_tags_and_viewers_from_discover(self):
        class FakeAPI:
            async def check_status(self, username):
                return {
                    "is_online": True,
                    "viewers": 0,
                    "room_status": "public",
                    "hls_source": "https://cdn.example/live.m3u8",
                    "tags": [],
                }

            async def get_live_models(self, **kwargs):
                return {
                    "models": [
                        {
                            "username": kwargs["search"],
                            "viewers": 321,
                            "tags": ["French", "Cosplay"],
                            "thumbnail": "https://example.test/thumb.jpg",
                            "room_status": "public",
                        }
                    ]
                }

        provider = ChaturbateProvider(api=FakeAPI())

        status = await provider.check_status("alice")

        self.assertTrue(status.is_online)
        self.assertEqual(321, status.viewers)
        self.assertEqual(["French", "Cosplay"], status.tags)
        self.assertEqual("https://example.test/thumb.jpg", status.thumbnail)

    async def test_chaturbate_account_actions_require_verified_session(self):
        class FakeAuth:
            def get_status(self):
                return {"isLoggedIn": False, "username": "tester"}

            def get_cookies(self):
                return {"sessionid": "expired"}

        class FakeAPI:
            def __init__(self):
                self.get_followed_models = AsyncMock(return_value=[])
                self.follow_model = AsyncMock(return_value=True)
                self.unfollow_model = AsyncMock(return_value=True)
                self.is_following = AsyncMock(return_value=True)

        api = FakeAPI()
        provider = ChaturbateProvider(api=api, auth=FakeAuth())

        with self.assertRaises(ProviderAuthError):
            await provider.sync_following()
        with self.assertRaises(ProviderAuthError):
            await provider.follow("alice")
        with self.assertRaises(ProviderAuthError):
            await provider.unfollow("alice")

        self.assertFalse(await provider.is_following("alice"))
        api.get_followed_models.assert_not_awaited()
        api.follow_model.assert_not_awaited()
        api.unfollow_model.assert_not_awaited()
        api.is_following.assert_not_awaited()

    async def test_chaturbate_import_session_requires_sessionid_cookie(self):
        class FakeAuth:
            pass

        provider = ChaturbateProvider(auth=FakeAuth())

        result = await provider.import_session(cookie_header="csrftoken=csrf")

        self.assertFalse(result["success"])
        self.assertIn("sessionid", result["error"])


if __name__ == "__main__":
    unittest.main()

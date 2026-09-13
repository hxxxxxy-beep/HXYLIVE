import os
import unittest
from unittest.mock import AsyncMock, patch

from app.providers.youtube import (
    YouTubeProvider,
    is_youtube_channel_id,
    is_youtube_video_id,
    normalize_youtube_discover_identity,
    youtube_username_from_url,
)
from app.services.youtube_categories import (
    DEFAULT_YOUTUBE_LIVE_QUERY,
    curated_youtube_video_categories,
    normalize_youtube_live_query,
    youtube_discover_tags,
    youtube_live_query_q,
    youtube_video_category_title,
)


def _provider(**env):
    defaults = {"YOUTUBE_API_KEY": "test-key", "YOUTUBE_LIVE_QUERY": "asmr"}
    defaults.update(env)
    with patch.dict(os.environ, defaults, clear=False):
        provider = YouTubeProvider(
            "youtube",
            "YouTube",
            "https://www.youtube.com/@{username}/live",
            ("youtube.com", "youtu.be"),
        )
    provider.api_key = defaults["YOUTUBE_API_KEY"]
    provider.live_query = defaults["YOUTUBE_LIVE_QUERY"]
    return provider


def _search_item(video_id, channel_id, title, channel_title):
    return {
        "id": {"videoId": video_id},
        "snippet": {
            "channelId": channel_id,
            "channelTitle": channel_title,
            "title": title,
            "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}},
            "liveBroadcastContent": "live",
        },
    }


def _video(video_id, channel_id, title, viewers=12, started="2026-08-18T00:00:00Z", tags=None):
    snippet = {
        "channelId": channel_id,
        "channelTitle": "Streamer",
        "title": title,
        "categoryId": "20",
        "liveBroadcastContent": "live",
        "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}},
    }
    if tags is not None:
        snippet["tags"] = list(tags)
    return {
        "id": video_id,
        "snippet": snippet,
        "liveStreamingDetails": {
            "actualStartTime": started,
            "concurrentViewers": str(viewers),
        },
        "statistics": {"viewCount": "999"},
    }


def _channel(channel_id, title, handle, subscribers="1000"):
    return {
        "id": channel_id,
        "snippet": {
            "title": title,
            "customUrl": f"@{handle}",
            "thumbnails": {"high": {"url": f"https://yt3.googleusercontent.com/{handle}.jpg"}},
        },
        "statistics": {"subscriberCount": subscribers, "hiddenSubscriberCount": False},
    }


class YouTubeParseTests(unittest.TestCase):
    def test_url_identities(self):
        self.assertEqual(
            "mkbhd",
            youtube_username_from_url("https://www.youtube.com/@mkbhd/live"),
        )
        self.assertEqual(
            "UCsXVk37bltHxD1rDPwtNM8Q",
            youtube_username_from_url("https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q"),
        )
        self.assertEqual(
            "dQw4w9wgxcQ",
            youtube_username_from_url("https://www.youtube.com/watch?v=dQw4w9wgxcQ"),
        )
        self.assertEqual(
            "dQw4w9wgxcQ",
            youtube_username_from_url("https://youtu.be/dQw4w9wgxcQ"),
        )
        self.assertIsNone(youtube_username_from_url("https://twitch.tv/alice"))

    def test_channel_and_video_id_helpers(self):
        self.assertTrue(is_youtube_channel_id("UCsXVk37bltHxD1rDPwtNM8Q"))
        self.assertFalse(is_youtube_channel_id("mkbhd"))
        self.assertTrue(is_youtube_video_id("dQw4w9wgxcQ"))
        self.assertFalse(is_youtube_video_id("MrBeastLive"))

    def test_canonical_url_shapes(self):
        provider = _provider()
        self.assertEqual(
            "https://www.youtube.com/@mkbhd/live",
            provider.canonical_url("mkbhd"),
        )
        self.assertEqual(
            "https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q/live",
            provider.canonical_url("UCsXVk37bltHxD1rDPwtNM8Q"),
        )
        self.assertEqual(
            "https://www.youtube.com/watch?v=dQw4w9wgxcQ",
            provider.canonical_url("dQw4w9wgxcQ"),
        )

    def test_discover_identity_from_handle_and_url(self):
        self.assertEqual(
            "AngelicLofiASMR",
            normalize_youtube_discover_identity("AngelicLofiASMR"),
        )
        self.assertEqual(
            "AngelicLofiASMR",
            normalize_youtube_discover_identity("@AngelicLofiASMR"),
        )
        self.assertEqual(
            "AngelicLofiASMR",
            normalize_youtube_discover_identity(
                "https://www.youtube.com/@AngelicLofiASMR"
            ),
        )
        self.assertEqual(
            "AngelicLofiASMR",
            normalize_youtube_discover_identity("www.youtube.com/@AngelicLofiASMR"),
        )
        self.assertIsNone(normalize_youtube_discover_identity("yawning ASMR"))
        self.assertIsNone(
            normalize_youtube_discover_identity(
                "https://www.youtube.com/watch?v=dQw4w9wgxcQ"
            )
        )

    def test_curated_categories(self):
        rows = curated_youtube_video_categories()
        self.assertEqual([], rows)
        self.assertEqual("asmr", DEFAULT_YOUTUBE_LIVE_QUERY)
        self.assertIsNone(normalize_youtube_live_query("ASMR"))
        self.assertIsNone(youtube_live_query_q("asmr"))
        self.assertIsNone(normalize_youtube_live_query("not-a-query"))
        self.assertIsNone(normalize_youtube_live_query("gaming"))

    def test_official_category_and_snippet_tags(self):
        self.assertEqual("Gaming", youtube_video_category_title("20"))
        self.assertEqual("Music", youtube_video_category_title("10"))
        self.assertIsNone(youtube_video_category_title("999"))
        self.assertEqual(
            ["Gaming", "ASMR", "whisper"],
            youtube_discover_tags(category_id="20", snippet_tags=["ASMR", "whisper", "gaming"]),
        )
        self.assertEqual([], youtube_discover_tags(category_id="999", snippet_tags=None))

class YouTubeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_reports_auth_required(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = YouTubeProvider(
                "youtube",
                "YouTube",
                "https://www.youtube.com/@{username}/live",
                ("youtube.com",),
            )
        result = await provider.list_live_models(page=1, limit=24)
        self.assertEqual("auth_required", result["provider_status"])
        self.assertEqual([], result["models"])

    async def test_empty_search_does_not_browse_catalogue(self):
        provider = _provider()
        api = AsyncMock(side_effect=AssertionError("catalogue browse disabled"))
        with patch.object(provider, "_youtube_api_json", api):
            result = await provider.list_live_models(page=1, limit=24, live_query="asmr")
        self.assertEqual("empty", result["provider_status"])
        self.assertEqual([], result["models"])
        api.assert_not_called()

    async def test_handle_search_online_card(self):
        provider = _provider()
        channel_id = "UCsXVk37bltHxD1rDPwtNM8Q"
        search_payload = {
            "items": [_search_item("dQw4w9wgxcQ", channel_id, "Live now", "Kurzgesagt")],
            "nextPageToken": None,
        }
        videos_payload = {
            "items": [
                _video(
                    "dQw4w9wgxcQ",
                    channel_id,
                    "Live now",
                    viewers=321,
                    tags=["ASMR", "relax"],
                )
            ]
        }
        channels_payload = {"items": [_channel(channel_id, "Kurzgesagt", "kurzgesagt")]}

        async def _api(url, params):
            if "channels" in url:
                if params.get("forHandle"):
                    self.assertEqual("@kurzgesagt", params.get("forHandle"))
                return channels_payload
            if "search" in url:
                self.assertEqual(channel_id, params.get("channelId"))
                self.assertNotIn("q", params)
                return search_payload
            if "videos" in url:
                return videos_payload
            raise AssertionError(url)

        with patch.object(provider, "_youtube_api_json", AsyncMock(side_effect=_api)):
            result = await provider.list_live_models(
                page=1,
                limit=24,
                search="https://www.youtube.com/@kurzgesagt",
            )

        self.assertEqual("ok", result["provider_status"])
        self.assertEqual(1, len(result["models"]))
        model = result["models"][0]
        self.assertEqual("kurzgesagt", model["username"])
        self.assertEqual(321, model["viewers"])
        self.assertTrue(model["is_online"])
        self.assertEqual(channel_id, model["user_id"])

    async def test_handle_search_offline_when_not_live(self):
        provider = _provider()
        channel_id = "UCsXVk37bltHxD1rDPwtNM8Q"
        channel = _channel(channel_id, "Kurzgesagt", "kurzgesagt")

        async def _api(url, params):
            if "channels" in url:
                return {"items": [channel]}
            if "search" in url:
                return {"items": [], "nextPageToken": None}
            return {"items": []}

        with patch.object(provider, "_youtube_api_json", AsyncMock(side_effect=_api)):
            result = await provider.list_live_models(page=1, limit=24, search="@kurzgesagt")

        self.assertEqual(1, len(result["models"]))
        model = result["models"][0]
        self.assertEqual("kurzgesagt", model["username"])
        self.assertFalse(model["is_online"])
        self.assertEqual("offline", model["room_status"])
        self.assertEqual(1000, model["followers"])

    async def test_display_name_search_is_rejected(self):
        provider = _provider()
        api = AsyncMock(side_effect=AssertionError("display names are not searchable"))
        with patch.object(provider, "_youtube_api_json", api):
            result = await provider.list_live_models(
                page=1, limit=24, search="yawning ASMR"
            )
        self.assertEqual("empty", result["provider_status"])
        self.assertEqual([], result["models"])
        api.assert_not_called()

    async def test_login_is_rejected(self):
        provider = _provider()
        result = await provider.login("user", "pass")
        self.assertFalse(result["success"])
        self.assertIn("SAPISID", result["error"])

    async def test_import_session_requires_sapisid(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        result = await provider.import_session(cookie_header="SID=sid")
        self.assertFalse(result["success"])
        self.assertIn("SAPISID", result["error"])

    async def test_import_session_requires_session_continuity_cookies(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        result = await provider.import_session(cookie_header="SAPISID=secret; SID=sid")
        self.assertFalse(result["success"])
        self.assertIn("SIDCC", result["error"])

    async def test_import_session_verifies_logged_in_account(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        with patch.object(
            provider,
            "_youtube_current_user",
            AsyncMock(return_value={"username": "alice"}),
        ):
            result = await provider.import_session(
                cookie_header="SAPISID=secret; __Secure-3PSIDCC=cc"
            )
        self.assertTrue(result["success"])
        self.assertEqual("alice", result["username"])
        saved = await provider.session_store.get("youtube")
        self.assertTrue(saved["is_logged_in"])

    async def test_import_session_rejects_unauthenticated_cookies(self):
        from app.providers.base import ProviderAuthError

        provider = _provider()
        provider.session_store = _MemorySessionStore()
        with patch.object(
            provider,
            "_youtube_current_user",
            AsyncMock(side_effect=ProviderAuthError("Imported YouTube session is not logged in")),
        ):
            result = await provider.import_session(
                cookie_header="SAPISID=secret; __Secure-3PSIDCC=cc"
            )
        self.assertFalse(result["success"])
        saved = await provider.session_store.get("youtube")
        self.assertFalse(saved["is_logged_in"])

    def test_account_name_ignores_language_picker_titles(self):
        provider = _provider()
        payload = {
            "responseContext": {"mainAppWebResponseContext": {"loggedOut": True}},
            "actions": [{
                "openPopupAction": {
                    "popup": {
                        "multiPageMenuRenderer": {
                            "sections": [{
                                "multiPageMenuSectionRenderer": {
                                    "items": [{
                                        "compactLinkRenderer": {
                                            "title": {"simpleText": "Afrikaans"}
                                        }
                                    }]
                                }
                            }]
                        }
                    }
                }
            }],
        }
        self.assertTrue(provider._youtube_response_logged_out(payload))
        self.assertEqual("", provider._youtube_account_name_from_payload(payload))

    def test_account_name_uses_account_item_fields(self):
        provider = _provider()
        payload = {
            "actions": [{
                "openPopupAction": {
                    "popup": {
                        "multiPageMenuRenderer": {
                            "header": {
                                "activeAccountHeaderRenderer": {
                                    "accountName": {"simpleText": "Alice Example"},
                                    "channelHandle": {"simpleText": "@alice"},
                                }
                            }
                        }
                    }
                }
            }]
        }
        self.assertEqual("alice", provider._youtube_account_name_from_payload(payload))

    async def test_sync_following_uses_subscription_channel_ids(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        await provider.session_store.save(
            "youtube",
            username="alice",
            is_logged_in=True,
            cookies=[{"name": "SAPISID", "value": "secret"}],
        )
        channel_id = "UCsXVk37bltHxD1rDPwtNM8Q"
        with patch.object(
            provider,
            "_youtube_current_user",
            AsyncMock(return_value={"username": "alice"}),
        ), patch.object(
            provider,
            "_youtube_sync_subscription_ids",
            AsyncMock(return_value=[channel_id]),
        ), patch.object(
            provider,
            "_channels_by_id",
            AsyncMock(return_value={channel_id: _channel(channel_id, "Kurzgesagt", "kurzgesagt")}),
        ):
            items = await provider.sync_following()
        self.assertEqual(1, len(items))
        self.assertEqual("kurzgesagt", items[0]["username"])
        self.assertEqual(channel_id, items[0]["user_id"])

    def test_guide_subscription_ids(self):
        provider = _provider()
        payload = {
            "items": [
                {
                    "guideSubscriptionsSectionRenderer": {
                        "items": [
                            {
                                "guideEntryRenderer": {
                                    "navigationEndpoint": {
                                        "browseEndpoint": {"browseId": "UCsXVk37bltHxD1rDPwtNM8Q"}
                                    }
                                }
                            },
                            {
                                "guideCollapsibleEntryRenderer": {
                                    "expandableItems": [
                                        {
                                            "guideEntryRenderer": {
                                                "navigationEndpoint": {
                                                    "browseEndpoint": {
                                                        "browseId": "UCXuqSBlHAE6Xw-yeJA0Tunw"
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }
                            },
                            {
                                "guideEntryRenderer": {
                                    "navigationEndpoint": {
                                        "browseEndpoint": {"browseId": "FEchannels"}
                                    }
                                }
                            },
                        ]
                    }
                }
            ]
        }
        self.assertEqual(
            ["UCsXVk37bltHxD1rDPwtNM8Q", "UCXuqSBlHAE6Xw-yeJA0Tunw"],
            provider._youtube_guide_subscription_ids(payload),
        )

    def test_lockup_view_model_channel_ids(self):
        provider = _provider()
        payload = {
            "contents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "lockupViewModel": {
                                        "contentId": "UCsXVk37bltHxD1rDPwtNM8Q",
                                        "contentType": "LOCKUP_CONTENT_TYPE_CHANNEL",
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        self.assertEqual(
            ["UCsXVk37bltHxD1rDPwtNM8Q"],
            provider._youtube_channel_ids_from_payload(payload),
        )

    async def test_sync_following_skips_empty_subscription_list(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        with patch.object(
            provider,
            "_youtube_current_user",
            AsyncMock(return_value={"username": "alice"}),
        ), patch.object(
            provider,
            "_youtube_sync_subscription_ids",
            AsyncMock(return_value=[]),
        ):
            items = await provider.sync_following()
        self.assertEqual([], list(items))
        self.assertFalse(getattr(items, "trusted", True))
        self.assertIn("empty or unrecognized", getattr(items, "skipped_reason", ""))

    async def test_sync_following_keeps_ids_when_data_api_misses(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        channel_id = "UCsXVk37bltHxD1rDPwtNM8Q"
        with patch.object(
            provider,
            "_youtube_current_user",
            AsyncMock(return_value={"username": "alice"}),
        ), patch.object(
            provider,
            "_youtube_sync_subscription_ids",
            AsyncMock(return_value=[channel_id]),
        ), patch.object(
            provider,
            "_channels_by_id",
            AsyncMock(return_value={}),
        ):
            items = await provider.sync_following()
        self.assertEqual(1, len(items))
        self.assertEqual(channel_id, items[0]["username"])
        self.assertEqual(channel_id, items[0]["user_id"])

    async def test_sync_subscription_ids_use_guide_then_feed(self):
        provider = _provider()
        guide_id = "UCsXVk37bltHxD1rDPwtNM8Q"
        feed_id = "UCXuqSBlHAE6Xw-yeJA0Tunw"

        async def innertube(endpoint, body=None):
            if endpoint == "guide":
                return {
                    "items": [{
                        "guideSubscriptionsSectionRenderer": {
                            "items": [{
                                "guideEntryRenderer": {
                                    "navigationEndpoint": {
                                        "browseEndpoint": {"browseId": guide_id}
                                    }
                                }
                            }]
                        }
                    }]
                }
            return {
                "contents": {
                    "lockupViewModel": {"contentId": feed_id}
                }
            }

        with patch.object(provider, "_youtube_innertube_json", side_effect=innertube):
            ids = await provider._youtube_sync_subscription_ids()
        self.assertEqual([guide_id, feed_id], ids)


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

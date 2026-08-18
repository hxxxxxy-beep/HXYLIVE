import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.providers import twitch as twitch_mod
from app.providers.twitch import TwitchProvider


def _provider(**env):
    defaults = {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret", "TWITCH_GAME_ID": "509659"}
    defaults.update(env)
    with patch.dict(os.environ, defaults, clear=False):
        provider = TwitchProvider(
            "twitch",
            "Twitch",
            "https://www.twitch.tv/{username}",
            ("twitch.tv", "www.twitch.tv"),
        )
    provider.client_id = defaults["TWITCH_CLIENT_ID"]
    provider.client_secret = defaults["TWITCH_CLIENT_SECRET"]
    provider.game_id = defaults["TWITCH_GAME_ID"]
    return provider


def _stream(user_id: str, login: str, viewers: int = 10, **extra):
    row = {
        "user_id": user_id,
        "user_login": login,
        "user_name": login.title(),
        "type": "live",
        "viewer_count": viewers,
        "thumbnail_url": "https://img/{width}x{height}.jpg",
        "tags": [],
        "language": "en",
        "game_id": "509658",
        "game_name": "Just Chatting",
        "title": f"title-{login}",
        "started_at": "2026-07-29T00:00:00Z",
    }
    row.update(extra)
    return row


def _channel(
    user_id: str,
    login: str,
    *,
    display_name: str = "",
    is_live: bool = False,
    game_id: str = "509659",
    game_name: str = "ASMR",
    **extra,
):
    row = {
        "id": user_id,
        "broadcaster_login": login,
        "display_name": display_name or login,
        "is_live": is_live,
        "thumbnail_url": f"https://img/{login}.jpg",
        "title": "live" if is_live else "",
        "game_name": game_name,
        "game_id": game_id,
        "broadcaster_language": "en",
        "started_at": "2026-08-02T00:00:00Z" if is_live else "",
    }
    row.update(extra)
    return row


def _payload(streams, cursor=None):
    pagination = {"cursor": cursor} if cursor else {}
    return {"data": streams, "pagination": pagination}


class TwitchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_credentials_reports_auth_required(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = TwitchProvider(
                "twitch",
                "Twitch",
                "https://www.twitch.tv/{username}",
                ("twitch.tv", "www.twitch.tv"),
            )
            provider.client_id = ""
            provider.client_secret = ""
            result = await provider.list_live_models()

        self.assertEqual("auth_required", result["provider_status"])
        self.assertEqual([], result["models"])

    async def test_helix_streams_are_mapped_to_discover_models(self):
        provider = _provider()
        provider._helix_page = AsyncMock(
            return_value=_payload(
                [_stream("12345", "example", 321)]
                + [_stream(str(100 + i), f"pad{i}", 100 - i) for i in range(23)],
                cursor="next",
            )
        )
        provider._helix_users = AsyncMock(return_value=[{
            "id": "12345",
            "login": "example",
            "display_name": "Example",
            "profile_image_url": "https://img/avatar.jpg",
        }])
        provider._follower_total = AsyncMock(return_value=4567)

        result = await provider.list_live_models(page=1, limit=24)

        self.assertEqual("ok", result["provider_status"])
        example = next(m for m in result["models"] if m["username"] == "example")
        self.assertEqual("12345", example["id"])
        self.assertEqual("12345", example["user_id"])
        self.assertEqual(321, example["viewers"])
        self.assertEqual(4567, example["followers"])
        self.assertEqual("https://www.twitch.tv/example", example["channel_url"])
        self.assertEqual("https://img/440x248.jpg", example["thumbnail"])
        self.assertEqual("https://img/avatar.jpg", example["profile_image_url"])
        self.assertIn("Just Chatting", example["tags"])
        self.assertEqual(2, result["total_pages"])
        self.assertGreaterEqual(result["total"], 25)

    async def test_id_fallback_without_user_id(self):
        provider = _provider()
        stream = _stream("", "lonely", 1)
        stream["user_id"] = ""
        provider._helix_page = AsyncMock(return_value=_payload([stream]))
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

        result = await provider.list_live_models(page=1, limit=24)
        self.assertEqual("twitch:lonely", result["models"][0]["id"])

    async def test_search_uses_helix_search_channels(self):
        provider = _provider()
        provider._helix_search_channels = AsyncMock(return_value=_payload([]))
        provider._helix_page = AsyncMock(return_value=_payload([]))
        provider._helix_streams_by_user_ids = AsyncMock(return_value={})
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

        await provider.list_live_models(page=1, limit=10, search="Example")

        provider._helix_search_channels.assert_awaited_once_with(
            query="Example",
            first=100,
            after=None,
        )

    async def test_search_keeps_contiguous_display_name_contains(self):
        """Chinese display-name search: same-partition contains only."""
        provider = _provider()
        provider._helix_search_channels = AsyncMock(
            return_value=_payload(
                [
                    _channel("1", "xuanshen_main", display_name="炫神", is_live=True),
                    _channel("2", "xuanshen1", display_name="炫神1", is_live=False),
                    _channel(
                        "3",
                        "other",
                        display_name="别人",
                        is_live=True,
                        game_id="509659",
                        game_name="ASMR",
                    ),
                    _channel(
                        "4",
                        "xuanshen2",
                        display_name="炫神2",
                        is_live=True,
                        game_id="509658",
                        game_name="Just Chatting",
                    ),
                ]
            )
        )
        provider._helix_page = AsyncMock(return_value=_payload([]))
        provider._helix_streams_by_user_ids = AsyncMock(
            return_value={
                "1": _stream(
                    "1", "xuanshen_main", 500,
                    user_name="炫神", game_id="509659", game_name="ASMR",
                ),
            }
        )
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

        result = await provider.list_live_models(page=1, limit=10, search="炫神")
        names = [m["display_name"] for m in result["models"]]

        self.assertEqual(["炫神", "炫神1"], names)
        self.assertNotIn("炫神2", names)
        self.assertNotIn("别人", names)
        self.assertTrue(result["models"][0]["is_online"])
        self.assertEqual(500, result["models"][0]["viewers"])
        self.assertFalse(result["models"][1]["is_online"])

    async def test_search_respects_selected_game_partition(self):
        """Every Twitch category search stays inside that game_id."""
        provider = _provider()
        provider._helix_search_channels = AsyncMock(
            return_value=_payload(
                [
                    _channel(
                        "1", "asmrti", display_name="ASMR Ti", is_live=True,
                        game_id="509659", game_name="ASMR",
                    ),
                    _channel(
                        "2", "jcti", display_name="JC Ti", is_live=True,
                        game_id="509658", game_name="Just Chatting",
                    ),
                ]
            )
        )
        provider._helix_page = AsyncMock(return_value=_payload([]))
        provider._helix_streams_by_user_ids = AsyncMock(
            return_value={
                "2": _stream(
                    "2", "jcti", 100,
                    user_name="JC Ti", game_id="509658", game_name="Just Chatting",
                ),
            }
        )
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

        result = await provider.list_live_models(
            page=1, limit=10, search="ti", game_id="509658"
        )
        logins = [m["username"] for m in result["models"]]
        self.assertEqual(["jcti"], logins)
        self.assertNotIn("asmrti", logins)

    async def test_search_short_prefix_surfaces_compact_login(self):
        """ti → tiffy on page 1 inside the active partition."""
        provider = _provider()
        # Helix relevance often places tiffy past index 24; we must fetch 100.
        filler = [
            _channel(
                str(1000 + i),
                f"tizzzz{i:02d}longname",
                display_name=f"TiZzzz{i:02d}LongName",
                is_live=False,
            )
            for i in range(40)
        ]
        provider._helix_search_channels = AsyncMock(
            return_value=_payload(
                filler
                + [
                    _channel(
                        "10",
                        "timthetatmanvods",
                        display_name="TimTheTatmanVODs",
                        is_live=True,
                        game_id="509658",
                        game_name="Just Chatting",
                    ),
                    _channel("99", "tiffy", display_name="Tiffy", is_live=False),
                    _channel("11", "titanlol1", display_name="titanlol1", is_live=False),
                ]
            )
        )
        provider._helix_page = AsyncMock(return_value=_payload([]))
        provider._helix_streams_by_user_ids = AsyncMock(return_value={})
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

        result = await provider.list_live_models(page=1, limit=24, search="ti")
        logins = [m["username"] for m in result["models"]]

        self.assertIn("tiffy", logins)
        self.assertNotIn("timthetatmanvods", logins)
        self.assertLess(logins.index("tiffy"), logins.index("titanlol1"))

    async def test_search_returns_offline_twitch_user_in_partition(self):
        provider = _provider()
        provider._helix_search_channels = AsyncMock(
            return_value=_payload([
                _channel("99", "tiffy", display_name="Tiffy", is_live=False),
            ])
        )
        provider._helix_page = AsyncMock(return_value=_payload([]))
        provider._helix_streams_by_user_ids = AsyncMock(return_value={})
        provider._helix_users = AsyncMock(return_value=[{
            "id": "99",
            "login": "tiffy",
            "display_name": "Tiffy",
            "description": "Offline channel",
            "profile_image_url": "https://img/tiffy.jpg",
        }])
        provider._follower_total = AsyncMock(return_value=123)

        result = await provider.list_live_models(page=1, limit=10, search="tiffy")

        self.assertEqual("tiffy", result["models"][0]["username"])
        self.assertEqual("99", result["models"][0]["id"])
        self.assertFalse(result["models"][0]["is_online"])
        self.assertEqual("https://img/tiffy.jpg", result["models"][0]["profile_image_url"])
        self.assertEqual(123, result["models"][0]["followers"])

    def _stub_enrich(self, provider):
        provider._helix_users = AsyncMock(return_value=[])
        provider._follower_total = AsyncMock(return_value=None)

    async def test_cross_page_unique_zero_dups(self):
        provider = _provider()
        self._stub_enrich(provider)
        windows = [
            _payload([_stream(str(i), f"u{i}", 100 - i) for i in range(1, 25)], cursor="c1"),
            _payload([_stream(str(i), f"u{i}", 100 - i) for i in range(25, 49)], cursor="c2"),
            _payload([_stream(str(i), f"u{i}", 100 - i) for i in range(49, 73)], cursor=None),
        ]
        provider._helix_page = AsyncMock(side_effect=windows)

        pages = []
        for page in (1, 2, 3):
            result = await provider.list_live_models(page=page, limit=24)
            pages.append([m["id"] for m in result["models"]])
        all_ids = [i for page in pages for i in page]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(provider._helix_page.await_count, 3)

    async def test_overlapping_upstream_windows_dedupe(self):
        provider = _provider()
        self._stub_enrich(provider)
        # Window2 deliberately overlaps ids 20-24 from window1.
        windows = [
            _payload([_stream(str(i), f"u{i}") for i in range(1, 25)], cursor="c1"),
            _payload([_stream(str(i), f"u{i}") for i in range(20, 44)], cursor="c2"),
            _payload([_stream(str(i), f"u{i}") for i in range(44, 68)], cursor=None),
        ]
        provider._helix_page = AsyncMock(side_effect=windows)

        p1 = await provider.list_live_models(page=1, limit=24)
        p2 = await provider.list_live_models(page=2, limit=24)
        ids = [m["id"] for m in p1["models"]] + [m["id"] for m in p2["models"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(m["id"] for m in p2["models"]).isdisjoint(set(m["id"] for m in p1["models"])))

    async def test_max_three_helix_gets_per_call(self):
        provider = _provider()
        self._stub_enrich(provider)

        async def endless(*_a, **_k):
            return _payload([_stream("1", "same")], cursor="keep")

        provider._helix_page = AsyncMock(side_effect=endless)
        await provider.list_live_models(page=5, limit=24)
        self.assertLessEqual(provider._helix_page.await_count, 3)

    async def test_subsequent_pages_continue_saved_cursor(self):
        provider = _provider()
        self._stub_enrich(provider)
        calls = []

        async def capture(*, first, after=None, user_login=None, game_id=None, retry_auth=True):
            calls.append(after)
            if after is None:
                return _payload([_stream(str(i), f"a{i}") for i in range(24)], cursor="CUR1")
            if after == "CUR1":
                return _payload([_stream(str(i), f"b{i}") for i in range(24, 48)], cursor="CUR2")
            return _payload([_stream(str(i), f"c{i}") for i in range(48, 72)], cursor=None)

        provider._helix_page = AsyncMock(side_effect=capture)
        await provider.list_live_models(page=1, limit=24)
        await provider.list_live_models(page=2, limit=24)
        self.assertEqual(calls[0], None)
        self.assertEqual(calls[1], "CUR1")
        # Must not restart from None on page2.
        self.assertNotEqual(calls[1], None)

    async def test_cold_pool_page3_budgeted_backfill(self):
        provider = _provider()
        self._stub_enrich(provider)
        windows = [
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1"),
            _payload([_stream(str(i), f"u{i}") for i in range(24, 48)], cursor="c2"),
            _payload([_stream(str(i), f"u{i}") for i in range(48, 72)], cursor="c3"),
        ]
        provider._helix_page = AsyncMock(side_effect=windows)
        result = await provider.list_live_models(page=3, limit=24)
        self.assertEqual(24, len(result["models"]))
        self.assertEqual(3, provider._helix_page.await_count)
        self.assertEqual({str(i) for i in range(48, 72)}, {m["id"] for m in result["models"]})

    async def test_same_page_stable_within_ttl(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1")
        )
        a = await provider.list_live_models(page=1, limit=24)
        b = await provider.list_live_models(page=1, limit=24)
        self.assertEqual([m["id"] for m in a["models"]], [m["id"] for m in b["models"]])
        self.assertEqual(1, provider._helix_page.await_count)

    async def test_first_zero_unique_window_does_not_exhaust(self):
        provider = _provider()
        self._stub_enrich(provider)
        # Fill page1, then a fully-overlapping window (0 new), then new uniques.
        windows = [
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1"),
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c2"),  # zero new
            _payload([_stream(str(i), f"u{i}") for i in range(24, 48)], cursor="c3"),
        ]
        provider._helix_page = AsyncMock(side_effect=windows)
        await provider.list_live_models(page=1, limit=24)
        # Force need for more via page2
        result = await provider.list_live_models(page=2, limit=24)
        state = next(iter(provider._twitch_unique_pools.values()))
        self.assertFalse(state["exhausted"])
        self.assertEqual(24, len(result["models"]))
        self.assertTrue(result["total_pages"] > 2 or result["total"] > 24)

    async def test_two_zero_unique_windows_exhaust(self):
        provider = _provider()
        self._stub_enrich(provider)
        windows = [
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1"),
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c2"),
            _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c3"),
        ]
        provider._helix_page = AsyncMock(side_effect=windows)
        await provider.list_live_models(page=1, limit=24)
        await provider.list_live_models(page=2, limit=24)
        state = next(iter(provider._twitch_unique_pools.values()))
        self.assertTrue(state["exhausted"])

    async def test_no_next_cursor_terminates(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream(str(i), f"u{i}") for i in range(10)], cursor=None)
        )
        result = await provider.list_live_models(page=1, limit=24)
        self.assertEqual(1, result["total_pages"])
        self.assertEqual(10, result["total"])
        state = next(iter(provider._twitch_unique_pools.values()))
        self.assertTrue(state["exhausted"])

    async def test_empty_page_not_exhausted_keeps_has_more(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            side_effect=[
                _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1"),
            ]
        )
        await provider.list_live_models(page=1, limit=24)
        state = next(iter(provider._twitch_unique_pools.values()))
        state["exhausted"] = False
        state["consecutive_zero_unique_windows"] = 0
        state["next_cursor"] = "c1"

        provider._helix_page = AsyncMock(return_value=_payload([], cursor="still"))
        with patch.object(twitch_mod, "_TWITCH_MAX_UPSTREAM_GETS_PER_CALL", 1):
            result = await provider.list_live_models(page=2, limit=24)
        self.assertEqual([], result["models"])
        self.assertFalse(next(iter(provider._twitch_unique_pools.values()))["exhausted"])
        self.assertEqual(3, result["total_pages"])
        self.assertEqual(1, provider._helix_page.await_count)
    async def test_empty_page_exhausted_stops(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream(str(i), f"u{i}") for i in range(5)], cursor=None)
        )
        await provider.list_live_models(page=1, limit=24)
        result = await provider.list_live_models(page=2, limit=24)
        self.assertEqual([], result["models"])
        self.assertEqual(1, result["total_pages"])
        self.assertTrue(next(iter(provider._twitch_unique_pools.values()))["exhausted"])

    async def test_ttl_expiry_creates_new_pool(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream("1", "a")], cursor=None)
        )
        await provider.list_live_models(page=1, limit=24)
        key = next(iter(provider._twitch_unique_pools))
        provider._twitch_unique_pools[key]["updated_at"] = time.monotonic() - 100
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream("2", "b")], cursor=None)
        )
        result = await provider.list_live_models(page=1, limit=24)
        self.assertEqual(["2"], [m["id"] for m in result["models"]])

    async def test_different_game_search_limit_isolate_pools(self):
        provider = _provider()
        self._stub_enrich(provider)
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream("1", "a")], cursor=None)
        )
        await provider.list_live_models(page=1, limit=24)
        provider.game_id = "other"
        provider._helix_page = AsyncMock(
            return_value=_payload([_stream("9", "z")], cursor=None)
        )
        await provider.list_live_models(page=1, limit=24)
        self.assertEqual(2, len(provider._twitch_unique_pools))

        provider2 = _provider()
        self._stub_enrich(provider2)
        provider2._helix_page = AsyncMock(
            return_value=_payload([_stream("1", "a")], cursor=None)
        )
        await provider2.list_live_models(page=1, limit=12)
        await provider2.list_live_models(page=1, limit=24)
        self.assertEqual(2, len(provider2._twitch_unique_pools))

    async def test_concurrent_requests_serialize_cursor(self):
        provider = _provider()
        self._stub_enrich(provider)
        after_values = []

        async def slow_page(*, first, after=None, user_login=None, game_id=None, retry_auth=True):
            after_values.append(after)
            await asyncio.sleep(0.01)
            if after is None:
                return _payload([_stream(str(i), f"a{i}") for i in range(24)], cursor="C1")
            return _payload([_stream(str(i), f"b{i}") for i in range(24, 48)], cursor=None)

        provider._helix_page = AsyncMock(side_effect=slow_page)
        await asyncio.gather(
            provider.list_live_models(page=1, limit=24),
            provider.list_live_models(page=2, limit=24),
        )
        # Second call must observe saved cursor, not both start at None racing.
        self.assertIn(None, after_values)
        self.assertIn("C1", after_values)

    async def test_pool_key_cap_32(self):
        provider = _provider()
        self._stub_enrich(provider)
        for i in range(40):
            provider.game_id = f"game-{i}"
            provider._helix_page = AsyncMock(
                return_value=_payload([_stream(str(i), f"u{i}")], cursor=None)
            )
            await provider.list_live_models(page=1, limit=24)
        self.assertLessEqual(len(provider._twitch_unique_pools), twitch_mod._TWITCH_UNIQUE_POOL_MAX_KEYS)

    async def test_error_with_partial_data_returns_accumulated(self):
        provider = _provider()
        self._stub_enrich(provider)

        async def fail_second(*, first, after=None, user_login=None, game_id=None, retry_auth=True):
            if after is None:
                return _payload([_stream(str(i), f"u{i}") for i in range(24)], cursor="c1")
            raise RuntimeError("helix down")

        provider._helix_page = AsyncMock(side_effect=fail_second)
        await provider.list_live_models(page=1, limit=24)
        result = await provider.list_live_models(page=2, limit=24)
        self.assertEqual("error", result["provider_status"])
        # Page2 empty because fill failed, but should not fake total_pages=1 exhaustion
        # unless we marked exhausted — implementation does not mark exhausted on error.
        state = next(iter(provider._twitch_unique_pools.values()))
        self.assertFalse(state["exhausted"])
        self.assertEqual(24, len(state["items"]))


class TwitchDiscoverCoherencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api import discover
        from app.discover_gender_capabilities import unsupported_reason

        class _Caps:
            can_discover = True
            can_stream = True
            can_record = True
            can_follow = False

        class _Prov:
            source_type = "twitch"
            display_name = "Twitch"
            capabilities = _Caps()

            def __init__(self, handler):
                self._handler = handler

            async def list_live_models(self, **kwargs):
                return await self._handler(**kwargs)

        class _Registry:
            def __init__(self, provider):
                self.provider = provider

            def all(self):
                return [self.provider]

            def has(self, source_type):
                return source_type == "twitch"

            def get(self, source_type):
                return self.provider

        self.discover = discover
        self.unsupported_reason = unsupported_reason
        self._Prov = _Prov
        self._Registry = _Registry

    async def asyncTearDown(self):
        self.discover.init(None, None, None)

    async def test_twitch_all_page_total_pages_has_more_coherent(self):
        async def handler(*, page=1, limit=24, search="", **kwargs):
            # Mimic pool provider contract: growing totals while has more.
            has_more = page < 6
            return {
                "models": [
                    {
                        "id": f"id-{page}-{i}",
                        "user_id": f"id-{page}-{i}",
                        "username": f"u{page}_{i}",
                        "is_online": True,
                        "room_status": "public",
                        "viewers": 10,
                        "tags": [],
                        "source_type": "twitch",
                    }
                    for i in range(limit)
                ],
                "total": page * limit + (1 if has_more else 0),
                "page": page,
                "limit": limit,
                "total_pages": page + 1 if has_more else page,
                "provider_status": "ok",
            }

        provider = self._Prov(handler)
        self.discover.init(None, None, self._Registry(provider))
        # Warm then later page must not freeze at 24/2.
        page1 = await self.discover.discover_models(
            page=1, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )
        page5 = await self.discover.discover_models(
            page=5, limit=24, source="twitch", gender=None, search=None, tags=None, sort="viewers"
        )
        self.assertTrue(page1["has_more"])
        self.assertTrue(page5["has_more"])
        self.assertGreater(page5["total_pages"], 5)
        self.assertEqual(24, len(page5["models"]))

    async def test_twitch_female_unsupported_contract(self):
        calls = []

        async def handler(**kwargs):
            calls.append(kwargs)
            return {"models": [{"username": "x"}], "total": 1, "page": 1, "limit": 24, "total_pages": 1}

        self.discover.init(None, None, self._Registry(self._Prov(handler)))
        result = await self.discover.discover_models(
            page=1, limit=24, source="twitch", gender="female", search=None, tags=None, sort="viewers"
        )
        self.assertFalse(result["supported"])
        self.assertTrue(result.get("unsupported_reason"))
        self.assertEqual([], result["models"])
        self.assertEqual(0, result["total"])
        self.assertFalse(result["has_more"])
        self.assertEqual([], calls)


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
        self.state[source_type] = {
            "username": username,
            "is_logged_in": is_logged_in,
            "cookies": cookies or [],
            "localStorage": local_storage or [],
            "last_error": last_error,
        }

    async def clear(self, source_type):
        self.state.pop(source_type, None)

    async def cookie_header(self, source_type):
        from app.providers.sessions import ProviderSessionStore

        return ProviderSessionStore.cookies_to_header(
            (await self.get(source_type)).get("cookies")
        )


class TwitchFollowingTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self):
        provider = _provider()
        provider.session_store = _MemorySessionStore()
        return provider

    async def test_password_login_is_session_only(self):
        provider = self._provider()
        result = await provider.login("user", "pass")
        self.assertFalse(result["success"])
        self.assertIn("auth-token", result["error"])

    async def test_import_session_requires_auth_token(self):
        provider = self._provider()
        result = await provider.import_session(cookie_header="login=alice")
        self.assertFalse(result["success"])
        self.assertIn("auth-token", result["error"])

    async def test_sync_following_uses_helix_followed_channels(self):
        provider = self._provider()
        await provider.session_store.save(
            "twitch",
            username="alice",
            is_logged_in=True,
            cookies=[{"name": "auth-token", "value": "oauth"}],
        )
        with patch.object(
            provider,
            "_twitch_current_user",
            AsyncMock(return_value={"id": "1", "login": "alice"}),
        ), patch.object(
            provider,
            "_user_helix_get",
            AsyncMock(
                return_value={
                    "data": [
                        {
                            "broadcaster_id": "9",
                            "broadcaster_login": "streamer",
                            "broadcaster_name": "Streamer",
                        }
                    ],
                    "pagination": {},
                }
            ),
        ), patch.object(
            provider,
            "_hydrate_followed_live_status",
            AsyncMock(side_effect=lambda items: items),
        ):
            items = await provider.sync_following()
        self.assertEqual(1, len(items))
        self.assertEqual("streamer", items[0]["username"])
        self.assertEqual("Streamer", items[0]["display_name"])

    async def test_follow_uses_gql_mutation(self):
        provider = self._provider()
        await provider.session_store.save(
            "twitch",
            username="alice",
            is_logged_in=True,
            cookies=[{"name": "auth-token", "value": "oauth"}],
        )
        gql = AsyncMock(return_value={"followUser": {"follow": {"user": {"id": "9"}}}})
        with patch.object(
            provider,
            "_twitch_user_id",
            AsyncMock(return_value="9"),
        ), patch.object(provider, "_twitch_gql", gql):
            result = await provider.follow("streamer")
        self.assertTrue(result["success"])
        self.assertIn("followUser", gql.await_args.args[0])
        self.assertEqual({"id": "9"}, gql.await_args.args[1])


if __name__ == "__main__":
    unittest.main()

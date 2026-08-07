import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import chaturbate_api as chaturbate_api_mod
from app.services.chaturbate_api import ChaturbateAPI, _FakeResponse
from app.services.chaturbate_auth import ChaturbateAuthService


class _FakeAuth:
    def get_user_agent(self):
        return "HXYLIVE-Test"

    def get_cookies(self):
        return {}


class _CookieAuth(_FakeAuth):
    def get_cookies(self):
        return {"sessionid": "abc", "csrftoken": "csrf"}


class _UnverifiedCookieAuth(_CookieAuth):
    def get_status(self):
        return {"isLoggedIn": False, "hasCookies": True}


class _MutableCookieAuth(_FakeAuth):
    def __init__(self):
        self._cookies = {"sessionid": "real-session", "csrftoken": "real-csrf"}
        self._user_agent = "HXYLIVE-Test"

    def get_cookies(self):
        return dict(self._cookies)

    def get_user_agent(self):
        return self._user_agent


class _FakeFlareSolverr:
    def __init__(self):
        self.urls = []

    async def solve_challenge(self, url):
        self.urls.append(url)
        return {
            "cookies": {"cf_clearance": "solved"},
            "user_agent": "Solved-UA",
        }


class _ResponseCookie:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class _AuthLoginResponse:
    def __init__(self, status=302, cookies=None):
        self.status = status
        self.cookies = {
            key: _ResponseCookie(key, value)
            for key, value in (cookies or {}).items()
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""


class _AuthLoginSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        return self.response


def _json_response(payload):
    return _FakeResponse(
        200,
        json.dumps(payload).encode("utf-8"),
        {},
        "application/json",
    )


def _followed_room(username, *, online=True, is_following=True):
    return {
        "username": username,
        "display_name": username.title(),
        "current_show": "public" if online else "offline",
        "num_users": "42" if online else "0",
        "img": f"//thumb.example.test/{username}.jpg",
        "tags": ["French"],
        "is_following": is_following,
    }


def _empty_followed_response():
    return _json_response({"rooms": [], "total_count": 0})


class _RoomlistAPI(ChaturbateAPI):
    def __init__(self, response, scrape_response=None):
        super().__init__(_FakeAuth())
        self.response = response
        self.scrape_response = scrape_response or {
            "models": [],
            "total": 0,
            "page": 1,
            "limit": 24,
            "total_pages": 1,
        }
        self.requests = []
        self.scraped = False

    async def _rate_limit(self):
        return None

    async def _request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "kwargs": kwargs})
        # Avatar enrichment hits chatvideocontext; keep roomlist fixtures isolated.
        if "chatvideocontext" in str(url or ""):
            return _FakeResponse(404, b"missing", {}, "text/html")
        return self.response

    async def _scrape_live_models(self, page, limit, gender, search):
        self.scraped = True
        result = dict(self.scrape_response)
        result["page"] = page
        result["limit"] = limit
        return result


class _FollowedAPI(ChaturbateAPI):
    def __init__(self, responses, flaresolverr=None):
        super().__init__(_CookieAuth(), flaresolverr=flaresolverr)
        self.responses = list(responses)
        self.requests = []

    async def _rate_limit(self):
        return None

    async def _request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "kwargs": kwargs})
        return self.responses.pop(0)


class ChaturbateRoomlistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chaturbate_api_mod._PROFILE_IMAGE_CACHE.clear()

    async def test_followed_sync_requires_a_verified_session_not_only_a_cookie(self):
        api = ChaturbateAPI(_UnverifiedCookieAuth())
        api._fetch_followed_roomlist_api = AsyncMock()

        result = await api.get_followed_models()

        self.assertFalse(result.trusted)
        self.assertFalse(result.authoritative)
        self.assertIn("not verified", result.skipped_reason)
        api._fetch_followed_roomlist_api.assert_not_awaited()

    async def test_native_login_redirect_requires_verified_session(self):
        auth = ChaturbateAuthService(db=object())
        auth._extract_csrf_token = AsyncMock(return_value=("csrf", {"csrftoken": "csrf"}))

        async def reject_session():
            self.assertEqual("issued-session", auth._cookies.get("sessionid"))
            auth._last_validation_error = "Chaturbate session validation redirected to login"
            return False

        auth._validate_session = AsyncMock(side_effect=reject_session)
        auth._save_state = AsyncMock()
        auth._save_error = AsyncMock()
        session = _AuthLoginSession(_AuthLoginResponse(cookies={"sessionid": "issued-session"}))

        with patch(
            "app.services.chaturbate_auth.aiohttp_client_session",
            return_value=session,
        ):
            result = await auth.login("alice", "secret")

        self.assertFalse(result["success"])
        self.assertFalse(auth._is_logged_in)
        self.assertEqual({}, auth._cookies)
        auth._save_state.assert_not_awaited()
        auth._save_error.assert_awaited_once()

    async def test_native_login_persists_only_after_session_validation(self):
        auth = ChaturbateAuthService(db=object())
        auth._extract_csrf_token = AsyncMock(return_value=("csrf", {"csrftoken": "csrf"}))
        auth._validate_session = AsyncMock(return_value=True)
        auth._save_state = AsyncMock()
        auth._save_error = AsyncMock()
        session = _AuthLoginSession(_AuthLoginResponse(cookies={"sessionid": "verified-session"}))

        with patch(
            "app.services.chaturbate_auth.aiohttp_client_session",
            return_value=session,
        ):
            result = await auth.login("alice", "secret")

        self.assertTrue(result["success"])
        self.assertTrue(auth._is_logged_in)
        self.assertEqual("verified-session", auth._cookies["sessionid"])
        auth._save_state.assert_awaited_once_with("alice", "secret")
        auth._save_error.assert_not_awaited()

    async def test_flaresolverr_cookie_merge_preserves_authenticated_session(self):
        auth = _MutableCookieAuth()
        api = ChaturbateAPI(auth)
        headers = {"User-Agent": auth.get_user_agent()}

        applied = api._apply_flaresolverr_solution(headers, {
            "cookies": {
                "sessionid": "anonymous-session",
                "csrftoken": "anonymous-csrf",
                "cf_clearance": "solved",
                "__cf_bm": "bot-token",
            },
            "user_agent": "Solved-UA",
        })

        self.assertTrue(applied)
        self.assertEqual("real-session", auth._cookies["sessionid"])
        self.assertEqual("real-csrf", auth._cookies["csrftoken"])
        self.assertEqual("solved", auth._cookies["cf_clearance"])
        self.assertEqual("bot-token", auth._cookies["__cf_bm"])
        self.assertEqual("Solved-UA", headers["User-Agent"])
        self.assertIn("sessionid=real-session", headers["Cookie"])
        self.assertNotIn("anonymous-session", headers["Cookie"])

    async def test_auth_flaresolverr_cookie_merge_preserves_authenticated_session(self):
        auth = ChaturbateAuthService(db=object())
        cookies = {"sessionid": "real-session", "csrftoken": "real-csrf"}
        headers = {}

        applied = auth._apply_flaresolverr_solution(headers, cookies, {
            "cookies": {
                "sessionid": "anonymous-session",
                "csrftoken": "anonymous-csrf",
                "cf_clearance": "solved",
            },
            "user_agent": "Solved-UA",
        })

        self.assertTrue(applied)
        self.assertEqual("real-session", cookies["sessionid"])
        self.assertEqual("real-csrf", cookies["csrftoken"])
        self.assertEqual("solved", cookies["cf_clearance"])
        self.assertEqual("Solved-UA", headers["User-Agent"])
        self.assertIn("sessionid=real-session", headers["Cookie"])
        self.assertNotIn("anonymous-session", headers["Cookie"])

    async def test_roomlist_uses_ajax_json_headers(self):
        api = _RoomlistAPI(_json_response({"rooms": [{"username": "alice"}], "total_count": 1}))

        await api.get_live_models(page=1, limit=24)

        headers = api.requests[0]["headers"]
        self.assertEqual("application/json", headers["Accept"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertEqual("https://chaturbate.com/", headers["Referer"])
        self.assertFalse(api.requests[0]["kwargs"].get("allow_redirects", True))

    async def test_roomlist_parses_current_chaturbate_fields(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {
                    "username": "alice",
                    "display_age": "19",
                    "gender": "f",
                    "current_show": "public",
                    "num_users": "42",
                    "num_followers": "1234",
                    "room_subject": "Goal text",
                    "tags": ["French", "Cosplay"],
                    "img": "//thumb.live.mmcdn.com/riw/alice.jpg",
                }
            ],
            "total_count": "25",
        }))

        result = await api.get_live_models(page=2, limit=10)

        self.assertFalse(api.scraped)
        self.assertEqual(25, result["total"])
        self.assertEqual(3, result["total_pages"])
        self.assertEqual(1, len(result["models"]))
        model = result["models"][0]
        self.assertEqual("alice", model["username"])
        self.assertEqual("alice", model["display_name"])
        self.assertEqual(19, model["age"])
        self.assertEqual(42, model["viewers"])
        self.assertEqual(1234, model["followers"])
        self.assertEqual("Goal text", model["subject"])
        self.assertEqual("https://thumb.live.mmcdn.com/riw/alice.jpg", model["thumbnail"])
        # Roomlist `img` is the live cover; circular avatar stays empty until
        # summary enrichment (Media/Watch use summary_card_image).
        self.assertEqual("", model["profile_image_url"])
        self.assertEqual(["French", "Cosplay"], model["tags"])
        self.assertEqual("public", model["room_status"])

    async def test_roomlist_normalizes_c_and_s_gender_codes_from_api_samples(self):
        # genders=c returns room.gender "c"; genders=t returns room.gender "s".
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {"username": "couple_room", "gender": "c", "current_show": "public", "num_users": 10},
                {"username": "trans_room", "gender": "s", "current_show": "public", "num_users": 9},
                {"username": "female_room", "gender": "f", "current_show": "public", "num_users": 8},
                {"username": "male_room", "gender": "m", "current_show": "public", "num_users": 7},
            ],
            "total_count": 4,
        }))

        result = await api.get_live_models(page=1, limit=24)
        by_name = {item["username"]: item for item in result["models"]}

        self.assertEqual("couple", by_name["couple_room"]["gender"])
        self.assertEqual("trans", by_name["trans_room"]["gender"])
        self.assertEqual("f", by_name["female_room"]["gender"])
        self.assertEqual("m", by_name["male_room"]["gender"])
        self.assertEqual("couple", ChaturbateAPI._normalize_response_gender("c"))
        self.assertEqual("trans", ChaturbateAPI._normalize_response_gender("s"))
        self.assertEqual("f", ChaturbateAPI._normalize_response_gender("f"))

    async def test_non_json_roomlist_response_falls_back(self):
        fallback = {
            "models": [{"username": "fallback"}],
            "total": 1,
            "page": 1,
            "limit": 24,
            "total_pages": 1,
        }
        api = _RoomlistAPI(
            _FakeResponse(200, b"<html>challenge</html>", {}, "text/html"),
            scrape_response=fallback,
        )

        result = await api.get_live_models(page=1, limit=12)

        self.assertTrue(api.scraped)
        self.assertEqual([{"username": "fallback"}], result["models"])
        self.assertEqual(12, result["limit"])

    async def test_malformed_room_items_are_skipped(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                "not-a-room",
                {"display_name": "missing username"},
                {"username": "valid"},
            ],
            "total_count": 3,
        }))

        result = await api.get_live_models(page=1, limit=10)

        self.assertEqual(["valid"], [item["username"] for item in result["models"]])

    async def test_followed_models_prefers_roomlist_and_combines_online_offline(self):
        api = _FollowedAPI([
            _json_response({
                "rooms": [_followed_room("alice", online=True)],
                "total_count": 1,
            }),
            _json_response({
                "rooms": [_followed_room("bella", online=False)],
                "total_count": 1,
            }),
        ])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertTrue(result.authoritative)
        self.assertEqual(["alice", "bella"], [item["username"] for item in result])
        self.assertTrue(result[0]["is_online"])
        self.assertFalse(result[1]["is_online"])
        self.assertEqual("https://thumb.example.test/alice.jpg", result[0]["thumbnail_url"])
        self.assertEqual(2, len(api.requests))
        self.assertIn("follow=true", api.requests[0]["url"])
        self.assertNotIn("offline=true", api.requests[0]["url"])
        self.assertIn("offline=true", api.requests[1]["url"])
        self.assertTrue(all("/api/ts/roomlist/" in request["url"] for request in api.requests))
        self.assertEqual("application/json", api.requests[0]["headers"]["Accept"])
        self.assertEqual("XMLHttpRequest", api.requests[0]["headers"]["X-Requested-With"])

    async def test_followed_roomlist_paginates_complete_snapshot(self):
        first_page = [_followed_room(f"model_{index}") for index in range(90)]
        api = _FollowedAPI([
            _json_response({"rooms": first_page, "total_count": 91}),
            _json_response({"rooms": [_followed_room("model_90")], "total_count": 91}),
            _empty_followed_response(),
        ])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertEqual(91, len(result))
        self.assertIn("offset=90", api.requests[1]["url"])

    async def test_followed_roomlist_rejects_false_is_following_below_safety_cap(self):
        api = _FollowedAPI([
            _json_response({
                "rooms": [_followed_room("global_model", is_following=False)],
                "total_count": 1,
            }),
        ])

        result = await api._fetch_followed_roomlist_api(api._get_headers())

        self.assertFalse(result.trusted)
        self.assertFalse(result.authoritative)
        self.assertIn("ignored", result.skipped_reason)

    async def test_followed_roomlist_rejects_missing_is_following(self):
        room = _followed_room("ambiguous")
        room.pop("is_following")
        api = _FollowedAPI([_json_response({"rooms": [room], "total_count": 1})])

        result = await api._fetch_followed_roomlist_api(api._get_headers())

        self.assertFalse(result.trusted)
        self.assertIn("ignored", result.skipped_reason)

    async def test_followed_roomlist_rejects_missing_or_excessive_total(self):
        api = _FollowedAPI([_json_response({"rooms": [_followed_room("alice")]})])
        missing = await api._fetch_followed_roomlist_api(api._get_headers())
        self.assertFalse(missing.trusted)
        self.assertIn("total", missing.skipped_reason)

        import app.services.chaturbate_api as cb_api

        original = cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS
        cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS = 2
        try:
            api = _FollowedAPI([_json_response({"rooms": [], "total_count": 3})])
            excessive = await api._fetch_followed_roomlist_api(api._get_headers())
        finally:
            cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS = original
        self.assertFalse(excessive.trusted)
        self.assertIn("safety limit", excessive.skipped_reason)

    async def test_followed_roomlist_rejects_premature_empty_page(self):
        first_page = [_followed_room(f"model_{index}") for index in range(90)]
        api = _FollowedAPI([
            _json_response({"rooms": first_page, "total_count": 91}),
            _json_response({"rooms": [], "total_count": 91}),
        ])

        result = await api._fetch_followed_roomlist_api(api._get_headers())

        self.assertFalse(result.trusted)
        self.assertIn("ended before", result.skipped_reason)

    async def test_followed_models_falls_back_to_non_authoritative_html(self):
        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <li class="room_list_room" data-room="_alice_">
            <a href="/_alice_/"><img data-src="//thumb.example.test/alice.jpg" alt="Alice"></a>
            <span class="cams">1,234</span>
          </li>
        </body></html>
        """
        api = _FollowedAPI([
            _json_response({
                "rooms": [_followed_room("global_model", is_following=False)],
                "total_count": 1,
            }),
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
        ])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertFalse(result.authoritative)
        self.assertEqual("_alice_", result[0]["username"])
        self.assertEqual(1234, result[0]["viewers"])
        self.assertEqual("https://thumb.example.test/alice.jpg", result[0]["thumbnail_url"])
        self.assertIn("/api/ts/roomlist/", api.requests[0]["url"])
        self.assertEqual("https://chaturbate.com/followed-cams/", api.requests[1]["url"])

    async def test_followed_models_parse_embedded_json_html_fallback(self):
        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <script type="application/json">
            {"props":{"followed":[
              {"username":"_alice_","display_name":"Alice","is_online":true,
               "viewers":"123","thumbnail_url":"//thumb.example.test/alice.jpg",
               "room_status":"public","tags":["French"]}
            ]}}
          </script>
        </body></html>
        """
        api = _FollowedAPI([
            _json_response({"rooms": [_followed_room("global", is_following=False)], "total_count": 1}),
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
        ])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertFalse(result.authoritative)
        self.assertEqual("_alice_", result[0]["username"])
        self.assertEqual("Alice", result[0]["display_name"])
        self.assertEqual(123, result[0]["viewers"])
        self.assertEqual(["French"], result[0]["tags"])

    async def test_followed_models_reject_paginated_html_fallback(self):
        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <li class="room_list_room" data-room="global_model">
            <img src="//thumb.example.test/global.jpg">
          </li>
          <a href="/followed-cams/?page=2">Next</a>
        </body></html>
        """
        api = _FollowedAPI([
            _json_response({"rooms": [_followed_room("global", is_following=False)], "total_count": 1}),
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
        ])

        result = await api.get_followed_models()

        self.assertFalse(result.trusted)
        self.assertEqual([], list(result))
        self.assertIn("pagination", result.skipped_reason)

    async def test_followed_models_skip_when_api_and_html_redirect_to_login(self):
        api = _FollowedAPI([
            _FakeResponse(302, b"", {"Location": "/auth/login/"}, "text/html"),
            _FakeResponse(302, b"", {"Location": "/auth/login/"}, "text/html"),
        ])

        result = await api.get_followed_models()

        self.assertFalse(result.trusted)
        self.assertEqual([], list(result))
        self.assertIn("redirected", result.skipped_reason)

    async def test_followed_html_fallback_skips_when_safety_limit_is_exceeded(self):
        import app.services.chaturbate_api as cb_api

        html = """
        <a href="/followed-cams/">Followed Cams</a>
        <li class="room_list_room" data-room="alice"><img src="//thumb/a.jpg"></li>
        <li class="room_list_room" data-room="bella"><img src="//thumb/b.jpg"></li>
        """
        original = cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS
        cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS = 1
        try:
            api = _FollowedAPI([
                _json_response({"rooms": [_followed_room("global", is_following=False)], "total_count": 1}),
                _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
            ])
            result = await api.get_followed_models()
        finally:
            cb_api.HXYLIVE_MAX_FOLLOW_SYNC_ITEMS = original

        self.assertFalse(result.trusted)
        self.assertIn("safety limit", result.skipped_reason)

    async def test_lookup_username_returns_offline_room(self):
        payload = {
            "broadcaster_username": "ashleyalban",
            "room_status": "offline",
            "room_title": "Ashleyalban's room",
            "num_viewers": 0,
            "hls_source": "",
            "age": 27,
            "broadcaster_gender": "female",
            "summary_card_image": "https://s3pv.highwebmedia.com/uploads/photos/example.jpg",
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(return_value=_json_response(payload))
        model = await api.lookup_username("AshleyAlban")
        self.assertEqual("ashleyalban", model["username"])
        self.assertFalse(model["is_online"])
        self.assertEqual("offline", model["room_status"])
        self.assertEqual(27, model["age"])
        self.assertEqual(payload["summary_card_image"], model["thumbnail"])
        self.assertEqual(model["thumbnail"], model["profile_image_url"])

    async def test_lookup_username_online_prefers_summary_for_profile_image(self):
        payload = {
            "broadcaster_username": "anitasweetness",
            "room_status": "public",
            "room_title": "live",
            "num_users": 120,
            "hls_source": "https://edge.test/live.m3u8",
            "summary_card_image": "https://s3pv.highwebmedia.com/uploads/photos/anita.jpg",
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(return_value=_json_response(payload))
        model = await api.lookup_username("anitasweetness")
        self.assertTrue(model["is_online"])
        self.assertEqual(120, model["viewers"])
        self.assertEqual(
            "https://thumb.live.mmcdn.com/riw/anitasweetness.jpg",
            model["thumbnail"],
        )
        self.assertEqual(payload["summary_card_image"], model["profile_image_url"])

    async def test_lookup_username_prefers_num_users_over_zero_num_viewers(self):
        payload = {
            "broadcaster_username": "nancy_a1",
            "room_status": "public",
            "room_title": "live",
            "num_viewers": 0,
            "num_users": 87,
            "hls_source": "https://edge.test/live.m3u8",
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(return_value=_json_response(payload))
        model = await api.lookup_username("nancy_a1")
        self.assertTrue(model["is_online"])
        self.assertEqual(87, model["viewers"])

    async def test_lookup_username_normalizes_hyphenated_display_name(self):
        payload = {
            "broadcaster_username": "nancy_a1",
            "room_status": "public",
            "num_users": 12,
            "hls_source": "https://edge.test/live.m3u8",
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(return_value=_json_response(payload))
        model = await api.lookup_username("Nancy-A1")
        self.assertIsNotNone(model)
        self.assertEqual("nancy_a1", model["username"])
        self.assertEqual(12, model["viewers"])
        api._request.assert_awaited()
        called_url = api._request.await_args.args[1]
        self.assertIn("/nancy_a1/", called_url)

    async def test_roomlist_enriches_profile_image_from_summary_card(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {
                    "username": "anita",
                    "current_show": "public",
                    "num_users": "10",
                    "img": "//thumb.live.mmcdn.com/riw/anita.jpg",
                    "summary_card_image": "https://s3pv.highwebmedia.com/uploads/photos/anita.jpg",
                }
            ],
            "total_count": 1,
        }))
        result = await api.get_live_models(page=1, limit=1)
        model = result["models"][0]
        self.assertEqual("https://thumb.live.mmcdn.com/riw/anita.jpg", model["thumbnail"])
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/anita.jpg",
            model["profile_image_url"],
        )

    async def test_roomlist_applies_cached_profile_image_without_blocking_lookup(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {
                    "username": "alice",
                    "current_show": "public",
                    "num_users": "3",
                    "img": "//thumb.live.mmcdn.com/riw/alice.jpg",
                }
            ],
            "total_count": 1,
        }))
        chaturbate_api_mod._PROFILE_IMAGE_CACHE["alice"] = (
            __import__("time").monotonic() + 60,
            "https://s3pv.highwebmedia.com/uploads/photos/alice.jpg",
        )
        api.lookup_username = AsyncMock()
        result = await api.get_live_models(page=1, limit=1)
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/alice.jpg",
            result["models"][0]["profile_image_url"],
        )
        api.lookup_username.assert_not_awaited()

    async def test_resolve_profile_images_uses_chatvideocontext_summary(self):
        api = ChaturbateAPI(_FakeAuth())
        api._chatvideocontext_summary_image = AsyncMock(
            return_value="https://s3pv.highwebmedia.com/uploads/photos/alice.jpg"
        )
        images = await api.resolve_profile_images(["Alice", "alice", "bad name"])
        self.assertEqual(
            {"alice": "https://s3pv.highwebmedia.com/uploads/photos/alice.jpg"},
            images,
        )
        api._chatvideocontext_summary_image.assert_awaited_once_with("alice")

    async def test_roomlist_waits_for_page_profile_images_like_media(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {
                    "username": "alice",
                    "current_show": "public",
                    "num_users": "3",
                    "img": "//thumb.live.mmcdn.com/riw/alice.jpg",
                },
                {
                    "username": "betty",
                    "current_show": "public",
                    "num_users": "2",
                    "img": "//thumb.live.mmcdn.com/riw/betty.jpg",
                },
            ],
            "total_count": 2,
        }))
        api.resolve_profile_images = AsyncMock(return_value={
            "alice": "https://s3pv.highwebmedia.com/uploads/photos/alice.jpg",
            "betty": "https://s3pv.highwebmedia.com/uploads/photos/betty.jpg",
        })
        result = await api.get_live_models(page=1, limit=2)
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/alice.jpg",
            result["models"][0]["profile_image_url"],
        )
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/betty.jpg",
            result["models"][1]["profile_image_url"],
        )
        api.resolve_profile_images.assert_awaited()

    async def test_lookup_username_missing_returns_none(self):
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(return_value=_FakeResponse(404, b"missing", {}, "text/html"))
        self.assertIsNone(await api.lookup_username("nonexistentuserzzz999"))

    async def test_lookup_username_password_room_is_exact_search_hit(self):
        payload = {
            "status": 401,
            "detail": "This room requires a password.",
            "code": "unauthorized",
            "ts_context": None,
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(
            return_value=_FakeResponse(
                401,
                __import__("json").dumps(payload).encode(),
                {"Content-Type": "application/json"},
                "application/json",
            )
        )
        api._follower_count = AsyncMock(return_value=None)
        model = await api.lookup_username("mazzanti_")
        self.assertEqual("mazzanti_", model["username"])
        self.assertTrue(model["is_online"])
        self.assertEqual("password_protected", model["room_status"])
        self.assertEqual("https://chaturbate.com/mazzanti_/", model["channel_url"])

    async def test_check_status_password_room_is_private_online(self):
        payload = {
            "status": 401,
            "detail": "This room requires a password.",
            "code": "unauthorized",
            "ts_context": None,
        }
        api = ChaturbateAPI(_FakeAuth())
        api._request = AsyncMock(
            return_value=_FakeResponse(
                401,
                __import__("json").dumps(payload).encode(),
                {"Content-Type": "application/json"},
                "application/json",
            )
        )
        status = await api.check_status("mazzanti_")
        self.assertTrue(status["is_online"])
        self.assertEqual("password_protected", status["room_status"])
        self.assertIsNone(status["hls_source"])


if __name__ == "__main__":
    unittest.main()

import unittest

from app.api import following
from app.providers.base import ProviderCapabilities
from app.services.chaturbate_api import FollowedSyncResult


class _Provider:
    def __init__(self, source_type, items, can_login=False, can_sync_following=False):
        self.source_type = source_type
        self.display_name = source_type.upper()
        self.capabilities = ProviderCapabilities(
            can_login=can_login,
            can_follow=True,
            can_sync_following=can_sync_following,
        )
        self.items = items

    async def sync_following(self):
        return self.items


class _DiscoverProvider(_Provider):
    def __init__(self, source_type, statuses):
        super().__init__(source_type, [])
        self.capabilities = ProviderCapabilities(can_discover=True)
        self.statuses = statuses

    async def list_live_models(self, page=1, limit=1, search="", **kwargs):
        item = self.statuses.get(search)
        return {"models": [item] if item else []}


class _Registry:
    def __init__(self, providers):
        self.providers = providers

    def all(self):
        return self.providers


class _DB:
    def __init__(self):
        self.upserts = []
        self.removed = []
        self.reconciled = False

    async def upsert_followed_model(self, **kwargs):
        self.upserts.append(kwargs)

    async def remove_unfollowed(self, current_usernames, source_type="chaturbate"):
        self.removed.append((set(current_usernames), source_type))

    async def reconcile_model_sources_from_followed(self):
        self.reconciled = True
        return 0


class _FollowingDB(_DB):
    def __init__(self):
        super().__init__()
        self.followed = [
            {
                "username": "alice",
                "display_name": "Alice",
                "is_online": True,
                "viewers": 12,
                "source_type": "chaturbate",
                "room_status": "public",
            },
            {
                "username": "bella",
                "display_name": "Bella",
                "is_online": False,
                "viewers": 0,
                "source_type": "twitch",
                "room_status": "offline",
            },
        ]
        self.models = [{"username": "alice", "is_recording": True, "source_type": "chaturbate"}]
        self.sessions = {
            "chaturbate": {
                "username": "tester",
                "is_logged_in": 1,
                "credential_username": None,
                "credential_password": None,
                "credentials_updated_at": None,
                "session_cookies": '[{"name":"sessionid","value":"abc"}]',
                "local_storage": None,
                "last_error": None,
            }
        }

    async def get_all_followed(self):
        return [dict(item) for item in self.followed]

    async def get_all_models(self):
        return [dict(item) for item in self.models]

    async def get_provider_session(self, source_type):
        row = self.sessions.get(source_type)
        return dict(row) if row else None

    async def get_all_media_profiles(self):
        return []


class _ChaturbateApi:
    def __init__(self, images):
        self.images = images
        self.requested = None

    async def resolve_profile_images(self, usernames):
        self.requested = list(usernames)
        return dict(self.images)


class _StripchatFaceProvider(_Provider):
    def __init__(self, faces):
        super().__init__("stripchat", [], can_sync_following=True)
        self.capabilities = ProviderCapabilities(
            can_login=True,
            can_follow=True,
            can_sync_following=True,
            can_discover=True,
        )
        self.faces = faces

    async def list_live_models(self, page=1, limit=1, search="", **kwargs):
        face = self.faces.get(search)
        if not face:
            return {"models": []}
        return {
            "models": [{
                "username": search,
                "is_online": False,
                "viewers": 0,
                "room_status": "offline",
                "profile_image_url": face,
            }]
        }


class FollowingSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        following.init(None, None, None, None)

    async def test_legacy_sync_calls_remote_for_sync_capable_providers(self):
        db = _DB()
        registry = _Registry([
            _Provider("chaturbate", [
                {"username": "alice", "display_name": "Alice", "is_online": True, "viewers": 12},
            ], can_login=True, can_sync_following=True),
            _Provider("twitch", [
                {"username": "bella", "display_name": "Bella", "is_online": False, "viewers": 0},
            ], can_sync_following=False),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(1, result["synced"])
        self.assertFalse(result["localOnly"])
        self.assertEqual(["alice"], [item["username"] for item in db.upserts])
        self.assertEqual([
            ({"alice"}, "chaturbate"),
        ], db.removed)
        self.assertTrue(all(item["authoritative"] for item in result["results"]))
        self.assertTrue(db.reconciled)

    async def test_provider_without_remote_sync_is_left_local_only(self):
        db = _DB()
        registry = _Registry([_Provider("twitch", [], can_sync_following=False)])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(0, result["synced"])
        self.assertTrue(result["localOnly"])
        self.assertEqual([], db.upserts)
        self.assertEqual([], db.removed)

    async def test_untrusted_remote_sync_does_not_mutate_followed_cache(self):
        db = _DB()
        registry = _Registry([
            _Provider(
                "chaturbate",
                FollowedSyncResult([], trusted=False, skipped_reason="login page"),
                can_login=True,
                can_sync_following=True,
            ),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(0, result["synced"])
        self.assertEqual([], db.upserts)
        self.assertEqual([], db.removed)
        self.assertFalse(result["results"][0]["trusted"])
        self.assertFalse(result["results"][0]["authoritative"])
        self.assertEqual("login page", result["results"][0]["skippedReason"])

    async def test_non_authoritative_remote_sync_upserts_without_removing_cache(self):
        db = _DB()
        registry = _Registry([
            _Provider(
                "chaturbate",
                FollowedSyncResult(
                    [{"username": "alice", "display_name": "Alice", "is_online": True}],
                    authoritative=False,
                ),
                can_login=True,
                can_sync_following=True,
            ),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(["alice"], [item["username"] for item in db.upserts])
        self.assertEqual([], db.removed)
        self.assertTrue(result["results"][0]["trusted"])
        self.assertFalse(result["results"][0]["authoritative"])

    async def test_get_following_includes_provider_summaries(self):
        db = _FollowingDB()
        registry = _Registry([
            _Provider("chaturbate", [], can_login=True, can_sync_following=True),
            _Provider("twitch", []),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertEqual(2, len(result["models"]))
        self.assertEqual({"chaturbate": True, "twitch": False}, result["perSource"])
        self.assertEqual({"chaturbate", "twitch"}, set(result["byProvider"].keys()))
        summaries = {item["sourceType"]: item for item in result["providers"]}
        self.assertEqual({"chaturbate", "twitch"}, set(summaries))
        self.assertEqual(1, summaries["chaturbate"]["totalCount"])
        self.assertEqual(1, summaries["twitch"]["totalCount"])
        self.assertFalse(summaries["twitch"]["capabilities"]["can_sync_following"])
        self.assertFalse(summaries["chaturbate"]["status"].get("accountDisabled", False))
        self.assertTrue(summaries["chaturbate"]["status"]["isLoggedIn"])

    async def test_get_following_refreshes_local_discoverable_provider_status(self):
        db = _FollowingDB()
        db.followed = [{
            "username": "cowi",
            "display_name": "cowi",
            "is_online": True,
            "viewers": 725,
            "source_type": "twitch",
            "room_status": "public",
        }]
        registry = _Registry([_DiscoverProvider("twitch", {
            "cowi": {
                "username": "cowi",
                "is_online": False,
                "viewers": 0,
                "room_status": "offline",
            },
        })])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertFalse(result["models"][0]["is_online"])
        self.assertEqual(0, result["models"][0]["viewers"])
        self.assertEqual("offline", result["models"][0]["room_status"])
        self.assertEqual(0, result["onlineCount"])

    async def test_get_following_enriches_sync_provider_face_photos(self):
        db = _FollowingDB()
        db.followed = [
            {
                "username": "xx_x_mg",
                "display_name": "xx_x_mg",
                "is_online": True,
                "viewers": 10,
                "source_type": "chaturbate",
                "room_status": "public",
                "thumbnail_url": "https://thumb.live.mmcdn.com/riw/xx_x_mg.jpg",
            },
            {
                "username": "Miu1_girl",
                "display_name": "Miu1_girl",
                "is_online": False,
                "viewers": 0,
                "source_type": "stripchat",
                "room_status": "offline",
            },
        ]
        db.models = []
        api = _ChaturbateApi({
            "xx_x_mg": "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
        })
        registry = _Registry([
            _Provider("chaturbate", [], can_login=True, can_sync_following=True),
            _StripchatFaceProvider({
                "Miu1_girl": "https://static-proxy.strpst.com/avatars/1/5/5/face-full",
            }),
        ])
        following.init(api, None, db, registry)

        result = await following.get_following()
        by_name = {item["username"]: item for item in result["models"]}

        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            by_name["xx_x_mg"]["profile_image_url"],
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/1/5/5/face-full",
            by_name["Miu1_girl"]["profile_image_url"],
        )
        self.assertIn("xx_x_mg", api.requested)
        persisted = {
            item["username"]: item.get("profile_image_url")
            for item in db.upserts
            if item.get("profile_image_url")
        }
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            persisted.get("xx_x_mg"),
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/1/5/5/face-full",
            persisted.get("Miu1_girl"),
        )

    async def test_get_following_sorts_models_by_viewers_across_providers(self):
        db = _FollowingDB()
        db.followed = [
            {
                "username": "low_cb",
                "is_online": True,
                "viewers": 12,
                "source_type": "chaturbate",
                "room_status": "public",
            },
            {
                "username": "top_twitch",
                "is_online": True,
                "viewers": 320,
                "source_type": "twitch",
                "room_status": "public",
            },
            {
                "username": "mid_cb",
                "is_online": True,
                "viewers": 88,
                "source_type": "chaturbate",
                "room_status": "public",
            },
            {
                "username": "offline_zero",
                "is_online": False,
                "viewers": 999,
                "source_type": "chaturbate",
                "room_status": "offline",
            },
        ]
        registry = _Registry([
            _Provider("chaturbate", []),
            _Provider("twitch", [], can_sync_following=False),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertEqual(
            ["top_twitch", "mid_cb", "low_cb", "offline_zero"],
            [item["username"] for item in result["models"]],
        )

    async def test_get_following_hides_sources_missing_from_registry(self):
        db = _FollowingDB()
        db.followed.append({
            "username": "removed_source_model",
            "is_online": True,
            "viewers": 500,
            "source_type": "removedsource",
            "room_status": "public",
        })
        registry = _Registry([
            _Provider("chaturbate", []),
            _Provider("twitch", []),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertNotIn("removed_source_model", [item["username"] for item in result["models"]])
        self.assertNotIn("removedsource", result["byProvider"])
        self.assertNotIn("removedsource", {item["sourceType"] for item in result["providers"]})

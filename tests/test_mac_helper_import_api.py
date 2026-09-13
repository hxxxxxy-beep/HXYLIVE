import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database
from app.providers.base import ProviderCapabilities


class _DummyImportProvider:
    display_name = "Twitch"
    capabilities = ProviderCapabilities(can_login=True)

    def __init__(self):
        self.calls = []

    async def import_session(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "username": kwargs.get("username") or "alice"}


class MacHelperChromeImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR
        self.tmpdir = tempfile.TemporaryDirectory()
        output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = output_dir
        app_main.db = Database(output_dir / "hxylive.db")
        await app_main.db.initialize()
        self.client = TestClient(app_main.app)
        app_main._mac_helper_snapshot.clear()
        app_main._mac_download_jobs.clear()
        app_main._mac_download_tokens.clear()
        app_main._mac_helper_commands.clear()
        app_main._mac_helper_thumbs.clear()
        app_main._mac_helper_import_results.clear()
        self.provider = _DummyImportProvider()

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        self.tmpdir.cleanup()

    def _prime_helper(self):
        primed = self.client.post(
            "/api/mac/helper/heartbeat",
            json={"localSessionId": "mac-session", "files": []},
        )
        self.assertEqual(primed.status_code, 200)

    async def test_queue_and_helper_import_session(self):
        self._prime_helper()
        with patch.object(app_main, "_provider_for", return_value=self.provider), \
             patch.object(app_main, "_schedule_following_sync"):
            queued = self.client.post(
                "/api/mac/helper/import-session",
                json={"localSessionId": "mac-session", "sourceType": "twitch"},
            )
            self.assertEqual(queued.status_code, 200)
            command_id = queued.json()["commandId"]
            self.assertEqual(queued.json()["type"], "import-session")

            pending = self.client.get("/api/mac/helper/import-session/" + command_id)
            self.assertEqual(pending.status_code, 200)
            self.assertEqual(pending.json()["status"], "queued")

            imported = self.client.post(
                "/api/mac/helper/provider-session",
                json={
                    "localSessionId": "mac-session",
                    "commandId": command_id,
                    "sourceType": "twitch",
                    "cookieHeader": "auth-token=oauth; login=alice",
                    "username": "alice",
                    "userAgent": "Chrome",
                },
            )
            self.assertEqual(imported.status_code, 200)
            self.assertTrue(imported.json()["success"])
            self.assertEqual(len(self.provider.calls), 1)
            self.assertEqual(self.provider.calls[0]["cookie_header"], "auth-token=oauth; login=alice")

            done = self.client.get("/api/mac/helper/import-session/" + command_id)
            self.assertEqual(done.json()["status"], "done")
            self.assertTrue(done.json()["success"])
            self.assertEqual(done.json()["username"], "alice")
            self.assertNotIn("cookieHeader", done.json())

    async def test_helper_missing_cookies_are_stored_without_import(self):
        self._prime_helper()
        with patch.object(app_main, "_provider_for", return_value=self.provider):
            queued = self.client.post(
                "/api/mac/helper/import-session",
                json={"localSessionId": "mac-session", "sourceType": "bilibili"},
            )
            command_id = queued.json()["commandId"]
            failed = self.client.post(
                "/api/mac/helper/provider-session",
                json={
                    "localSessionId": "mac-session",
                    "commandId": command_id,
                    "sourceType": "bilibili",
                    "success": False,
                    "error": "Log into Bilibili in Google Chrome",
                    "loginUrl": "https://www.bilibili.com",
                },
            )
        self.assertEqual(failed.status_code, 200)
        self.assertFalse(failed.json()["success"])
        self.assertEqual(self.provider.calls, [])
        result = self.client.get("/api/mac/helper/import-session/" + command_id).json()
        self.assertEqual(result["status"], "done")
        self.assertFalse(result["success"])
        self.assertEqual(result["loginUrl"], "https://www.bilibili.com")

    async def test_youtube_chrome_import_is_queued(self):
        self._prime_helper()
        queued = self.client.post(
            "/api/mac/helper/import-session",
            json={"localSessionId": "mac-session", "sourceType": "youtube"},
        )
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["type"], "import-session")
        self.assertTrue(queued.json().get("commandId"))

    async def test_provider_session_requires_live_helper(self):
        posted = self.client.post(
            "/api/mac/helper/provider-session",
            json={
                "localSessionId": "mac-session",
                "commandId": "missing",
                "sourceType": "twitch",
                "cookieHeader": "auth-token=oauth",
            },
        )
        self.assertEqual(posted.status_code, 503)

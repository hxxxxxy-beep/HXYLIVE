import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database
from app.services import recording_quota


class RecordingQuotaHelpersTests(unittest.TestCase):
    def test_cycle_bounds_start_on_the_13th(self):
        tz = ZoneInfo("UTC")
        after = datetime(2026, 3, 15, 12, 0, tzinfo=tz)
        start, end = recording_quota.quota_cycle_bounds(after, cycle_day=13, tz_name="UTC")
        self.assertEqual(start, datetime(2026, 3, 13, 0, 0, tzinfo=tz))
        self.assertEqual(end, datetime(2026, 4, 13, 0, 0, tzinfo=tz))

        before = datetime(2026, 3, 10, 12, 0, tzinfo=tz)
        start, end = recording_quota.quota_cycle_bounds(before, cycle_day=13, tz_name="UTC")
        self.assertEqual(start, datetime(2026, 2, 13, 0, 0, tzinfo=tz))
        self.assertEqual(end, datetime(2026, 3, 13, 0, 0, tzinfo=tz))

    def test_effective_quota_and_unlimited(self):
        self.assertEqual(recording_quota.effective_monthly_quota_gb(None, 100), 100)
        self.assertEqual(recording_quota.effective_monthly_quota_gb(50, 100), 50)
        self.assertEqual(recording_quota.gb_to_bytes(0), 0)
        self.assertFalse(recording_quota.is_quota_exceeded(10**12, 0))
        self.assertTrue(
            recording_quota.is_quota_exceeded(
                recording_quota.GB_BYTES,
                recording_quota.gb_to_bytes(1),
            )
        )

    def test_blocked_reason_priority(self):
        self.assertEqual(
            recording_quota.blocked_reason(recording_enabled=False, quota_exceeded=True),
            "global_pause",
        )
        self.assertEqual(
            recording_quota.blocked_reason(recording_enabled=True, quota_exceeded=True),
            "quota_exceeded",
        )
        self.assertIsNone(
            recording_quota.blocked_reason(recording_enabled=True, quota_exceeded=False)
        )


class RecordingQuotaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_db = app_main.db
        self._tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self._tmpdir.name) / "hxylive.db")
        await app_main.db.initialize()
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        app_main.db = self._original_db
        self._tmpdir.cleanup()

    async def test_recording_settings_expose_quota_defaults(self):
        response = self.client.get("/api/settings/recording")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["default_monthly_quota_gb"], 100)
        self.assertEqual(data["quota_cycle_day"], 13)
        self.assertIn("quota_cycle_start", data)
        self.assertIn("quota_cycle_end", data)

        updated = self.client.put(
            "/api/settings/recording",
            json={"default_monthly_quota_gb": 80, "quota_cycle_day": 13},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["default_monthly_quota_gb"], 80)

    async def test_quota_blocks_start_without_clearing_auto_record(self):
        username = "quota-model"
        await app_main.db.add_or_update_model(
            username,
            source_type="chaturbate",
            auto_record=True,
            monthly_quota_gb=1,
        )
        await app_main.db.upsert_media_profile_source(
            profile_username=username,
            source_type="chaturbate",
            channel_username=username,
            auto_record=True,
            monthly_quota_gb=1,
        )
        cycle_start, _ = await app_main._quota_cycle_window()
        await app_main.db.add_or_update_recording(
            username=username,
            recording_id="rec-quota-1",
            filename="quota.ts",
            file_path=str(Path(self._tmpdir.name) / "quota.ts"),
            file_size=recording_quota.GB_BYTES,
            created_at=cycle_start + 10,
        )

        blocked = self.client.post(
            "/api/start",
            json={"target": username, "person": username, "source_type": "chaturbate"},
        )
        self.assertEqual(blocked.status_code, 503)
        self.assertIn("Monthly recording quota", blocked.json()["detail"])

        model = await app_main.db.get_model(username, source_type="chaturbate")
        self.assertTrue(model["auto_record"])
        sources = await app_main.db.get_media_profile_sources(username)
        self.assertTrue(sources[0]["auto_record"])

        payload = await app_main._media_profile_payload(username)
        self.assertFalse(payload["recordingAllowed"])
        self.assertEqual(payload["recordingBlockedReason"], "quota_exceeded")
        self.assertTrue(payload["quotaExceeded"])
        self.assertEqual(payload["monthlyQuotaGbEffective"], 1)

    async def test_global_pause_marks_recording_not_allowed(self):
        username = "pause-quota-card"
        await app_main.db.upsert_media_profile(
            username,
            {"display_name": username},
        )
        await app_main.db.upsert_media_profile_source(
            profile_username=username,
            source_type="chaturbate",
            channel_username=username,
            auto_record=True,
            monthly_quota_gb=100,
        )
        await app_main.db.set_setting("recording_enabled", "false")
        payload = await app_main._media_profile_payload(username)
        self.assertTrue(payload["autoRecord"])
        self.assertFalse(payload["recordingAllowed"])
        self.assertEqual(payload["recordingBlockedReason"], "global_pause")

    async def test_enforce_quota_stops_running_session(self):
        username = "live-quota"
        await app_main.db.add_or_update_model(
            username,
            source_type="chaturbate",
            auto_record=True,
            monthly_quota_gb=1,
        )
        await app_main.db.upsert_media_profile_source(
            profile_username=username,
            source_type="chaturbate",
            channel_username=username,
            auto_record=True,
            monthly_quota_gb=1,
        )
        session = {
            "id": "sess-quota-1",
            "running": True,
            "person": username,
            "target": username,
            "bytes_written": recording_quota.GB_BYTES,
        }
        stopped = []

        def fake_stop(session_id, reason=""):
            stopped.append((session_id, reason))
            session["running"] = False
            return True

        with patch.object(app_main, "_all_recording_statuses", return_value=[session]), patch.object(
            app_main.manager, "stop_session", side_effect=fake_stop
        ):
            result = await app_main._enforce_quota_on_running_sessions()

        self.assertEqual(result, ["sess-quota-1"])
        self.assertEqual(stopped, [("sess-quota-1", "quota_exceeded")])
        model = await app_main.db.get_model(username, source_type="chaturbate")
        self.assertTrue(model["auto_record"])


class RecordingQuotaStaticTests(unittest.TestCase):
    def test_media_card_has_blocked_diagonal_mark(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "styles.css").read_text()
        js = (root / "static" / "media.js").read_text()
        self.assertIn(".media-profile-card.is-recording-blocked", css)
        self.assertIn(".media-settings-toggle-copy", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("defaultMonthlyQuotaGb", js)
        self.assertIn("monthlyQuotaGb", js)
        self.assertIn("hxylive:recording-enabled", js)


if __name__ == "__main__":
    unittest.main()

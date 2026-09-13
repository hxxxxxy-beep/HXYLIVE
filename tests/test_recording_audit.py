"""Tests for durable recording lifecycle audit + history tombstones."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.core.database import Database
from app.services import recording_audit


class RecordingAuditTests(unittest.TestCase):
    def test_emit_and_busy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording_audit.configure(root)
            recording_audit.emit("record_start", username="ella_lee15", path="/data/x.ts")
            recording_audit.mark_converting(
                username="ella_lee15",
                filename="clip.ts",
                path=root / "clip.ts",
                recording_id="ella_lee15_clip",
            )
            snap = recording_audit.refresh_busy_snapshot([
                {
                    "id": "abc",
                    "person": "ella_lee15",
                    "running": True,
                    "record_path": str(root / "live.ts"),
                    "bytes_written": 123,
                    "source_type": "chaturbate",
                }
            ])
            self.assertTrue(snap["busy"])
            self.assertEqual(1, snap["recordingCount"])
            self.assertEqual(1, snap["convertingCount"])
            log_path = recording_audit.lifecycle_log_path()
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            events = [json.loads(line)["event"] for line in lines]
            self.assertIn("record_start", events)
            self.assertIn("convert_start", events)
            busy_file = json.loads(recording_audit.busy_snapshot_path().read_text(encoding="utf-8"))
            self.assertTrue(busy_file["busy"])
            recording_audit.clear_converting(root / "clip.ts")
            snap2 = recording_audit.refresh_busy_snapshot([])
            self.assertFalse(snap2["busy"])

    def test_shutdown_snapshot_marks_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            recording_audit.configure(Path(tmp))
            snap = recording_audit.write_shutdown_snapshot([])
            self.assertTrue(snap.get("shutdown"))
            disk = json.loads(recording_audit.busy_snapshot_path().read_text(encoding="utf-8"))
            self.assertTrue(disk.get("shutdown"))

    def test_reconcile_orphans_after_restart_emits_path_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording_audit.configure(root)
            leftover = root / "records" / "yawn" / "clip.ts"
            leftover.parent.mkdir(parents=True)
            leftover.write_bytes(b"x" * 2048)
            recording_audit.refresh_busy_snapshot([
                {
                    "id": "deadbeef01",
                    "person": "yawn",
                    "running": True,
                    "record_path": str(leftover),
                    "bytes_written": 999,
                    "source_type": "twitch",
                }
            ])
            orphans = recording_audit.reconcile_orphans_after_restart(alive_session_ids=[])
            self.assertEqual(1, len(orphans))
            self.assertEqual("deadbeef01", orphans[0]["sessionId"])
            self.assertTrue(orphans[0]["pathExists"])
            self.assertEqual(2048, orphans[0]["pathBytes"])
            events = [
                json.loads(line)["event"]
                for line in recording_audit.lifecycle_log_path().read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("orphan_after_restart", events)

    def test_reconcile_skips_still_alive_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            recording_audit.configure(Path(tmp))
            recording_audit.refresh_busy_snapshot([
                {
                    "id": "alive123",
                    "person": "yawn",
                    "running": True,
                    "record_path": "/data/missing.ts",
                    "bytes_written": 1,
                }
            ])
            orphans = recording_audit.reconcile_orphans_after_restart(
                alive_session_ids=["alive123"]
            )
            self.assertEqual([], orphans)


class RecordingHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_and_delete_keeps_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.initialize()
            await db.add_or_update_recording(
                username="ella_lee15",
                filename="2026-09-08.ts",
                file_path="/data/records/ella_lee15/videos/record/2026-09-08.ts",
                file_size=14_000_000_000,
                recording_id="ella_lee15_20260908_test",
                duration_seconds=3600,
                mp4_path="/data/records/ella_lee15/videos/record/2026-09-08.mp4",
                mp4_size=13_000_000_000,
                is_converted=True,
            )
            ok = await db.archive_and_delete_recording_by_id(
                "ella_lee15_20260908_test",
                final_status="synced_purged",
                reason="mac_sync_purge",
            )
            self.assertTrue(ok)
            self.assertEqual([], await db.get_recordings("ella_lee15"))
            history = await db.get_recording_history("ella_lee15", limit=10)
            self.assertEqual(1, len(history))
            self.assertEqual("synced_purged", history[0]["final_status"])
            self.assertEqual("mac_sync_purge", history[0]["reason"])
            self.assertEqual("ella_lee15_20260908_test", history[0]["recording_id"])


class RecordingLifecycleAggregateTests(unittest.TestCase):
    def test_groups_success_path_into_one_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording_audit.configure(root)
            import time as _time
            now = int(_time.time()) - 3600
            lines = [
                {
                    "ts": "2026-09-10T01:00:00Z",
                    "ts_unix": now,
                    "event": "record_start",
                    "username": "ella",
                    "sessionId": "sess1",
                    "path": "/data/records/ella/clip.ts",
                    "sourceType": "twitch",
                },
                {
                    "ts": "2026-09-10T01:40:00Z",
                    "ts_unix": now + 2400,
                    "event": "record_stop",
                    "username": "ella",
                    "sessionId": "sess1",
                    "path": "/data/records/ella/clip.ts",
                    "bytesWritten": 850_000_000,
                    "elapsedSeconds": 2400,
                    "exitCode": 0,
                    "reason": "api_stop",
                },
                {
                    "ts": "2026-09-10T01:41:00Z",
                    "ts_unix": now + 2460,
                    "event": "convert_start",
                    "username": "ella",
                    "filename": "clip.ts",
                    "path": "/data/records/ella/clip.ts",
                    "recordingId": "ella_clip",
                },
                {
                    "ts": "2026-09-10T01:42:00Z",
                    "ts_unix": now + 2520,
                    "event": "convert_ok",
                    "username": "ella",
                    "filename": "clip.ts",
                    "recordingId": "ella_clip",
                    "path": "/data/records/ella/clip.mp4",
                    "mp4Size": 800_000_000,
                },
                {
                    "ts": "2026-09-10T01:42:01Z",
                    "ts_unix": now + 2521,
                    "event": "mp4_ready",
                    "username": "ella",
                    "path": "/data/records/ella/clip.mp4",
                    "recordingId": "ella_clip",
                },
            ]
            path = recording_audit.lifecycle_log_path()
            path.write_text(
                "\n".join(json.dumps(row) for row in lines) + "\n",
                encoding="utf-8",
            )
            payload = recording_audit.list_lifecycle_sessions(days=1, limit=20)
            self.assertEqual(1, payload["total"])
            sess = payload["sessions"][0]
            self.assertEqual("ok", sess["outcome"])
            self.assertEqual("Converted OK", sess["summary"])
            self.assertEqual("ella", sess["username"])
            self.assertEqual(5, sess["eventCount"])
            self.assertEqual("Started recording", sess["steps"][0]["text"])
            self.assertIn("raw", sess["steps"][0])

    def test_discarded_and_convert_fail_outcomes(self):
        events = [
            {
                "ts": "2026-09-10T02:00:00Z",
                "ts_unix": 100,
                "event": "record_start",
                "username": "bob",
                "sessionId": "s2",
                "path": "/data/bob/x.ts",
            },
            {
                "ts": "2026-09-10T02:00:05Z",
                "ts_unix": 105,
                "event": "record_stop",
                "username": "bob",
                "sessionId": "s2",
                "path": "/data/bob/x.ts",
                "bytesWritten": 0,
                "elapsedSeconds": 5,
                "reason": "ffmpeg_exit",
            },
            {
                "ts": "2026-09-10T02:00:06Z",
                "ts_unix": 106,
                "event": "record_discarded",
                "username": "bob",
                "sessionId": "s2",
                "path": "/data/bob/x.ts",
                "reason": "empty",
            },
            {
                "ts": "2026-09-10T03:00:00Z",
                "ts_unix": 200,
                "event": "record_start",
                "username": "cara",
                "sessionId": "s3",
                "path": "/data/cara/y.ts",
            },
            {
                "ts": "2026-09-10T03:10:00Z",
                "ts_unix": 800,
                "event": "record_stop",
                "username": "cara",
                "sessionId": "s3",
                "path": "/data/cara/y.ts",
                "bytesWritten": 10_000,
                "elapsedSeconds": 600,
                "reason": "api_stop",
            },
            {
                "ts": "2026-09-10T03:11:00Z",
                "ts_unix": 860,
                "event": "convert_fail",
                "username": "cara",
                "filename": "y.ts",
                "path": "/data/cara/y.ts",
                "attempt": 2,
            },
        ]
        payload = recording_audit.aggregate_lifecycle_sessions(events)
        by_user = {s["username"]: s for s in payload["sessions"]}
        self.assertEqual("discarded", by_user["bob"]["outcome"])
        self.assertIn("Empty fragment", by_user["bob"]["summary"])
        self.assertEqual("failed", by_user["cara"]["outcome"])
        self.assertIn("attempt 2", by_user["cara"]["summary"])


if __name__ == "__main__":
    unittest.main()

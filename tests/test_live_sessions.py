"""Live session ledger + media timeline coverage intervals."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from app.core.database import Database


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_observe_opens_and_closes_session(self):
        opened = await self.db.observe_live_status(
            "alice",
            is_online=True,
            source_type="chaturbate",
            profile_username="AliceCard",
            seen_at=1_700_000_000,
            detection="test",
        )
        self.assertIsNotNone(opened)
        self.assertIsNone(opened.get("ended_at"))
        self.assertEqual(opened.get("profile_username"), "AliceCard")

        closed = await self.db.observe_live_status(
            "alice",
            is_online=False,
            source_type="chaturbate",
            profile_username="AliceCard",
            seen_at=1_700_000_600,
            detection="test",
        )
        self.assertEqual(closed.get("ended_at"), 1_700_000_000)

        rows = await self.db.get_live_sessions_in_range(
            from_ts=1_699_999_000,
            to_ts=1_700_001_000,
            profile_usernames=["AliceCard"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["started_at"], 1_700_000_000)
        self.assertEqual(rows[0]["ended_at"], 1_700_000_000)

    async def test_gap_merge_reopens_brief_offline(self):
        await self.db.observe_live_status(
            "bob",
            is_online=True,
            source_type="twitch",
            seen_at=1_700_000_000,
        )
        await self.db.observe_live_status(
            "bob",
            is_online=False,
            source_type="twitch",
            seen_at=1_700_000_100,
        )
        merged = await self.db.observe_live_status(
            "bob",
            is_online=True,
            source_type="twitch",
            seen_at=1_700_000_200,
            gap_merge_seconds=300,
        )
        self.assertIsNone(merged.get("ended_at"))
        self.assertEqual(merged.get("started_at"), 1_700_000_000)

        rows = await self.db.get_live_sessions_in_range(
            from_ts=1_699_999_000,
            to_ts=1_700_001_000,
            profile_usernames=["bob"],
        )
        self.assertEqual(len(rows), 1)

    async def test_recording_intervals_include_history(self):
        await self.db.add_or_update_recording(
            username="carol",
            filename="a.mp4",
            file_path="/data/a.mp4",
            file_size=100,
            recording_id="carol_1",
            duration_seconds=3600,
            created_at=1_700_000_000,
        )
        await self.db.add_or_update_recording(
            username="carol",
            filename="old.ts",
            file_path="/data/old.ts",
            file_size=50,
            recording_id="carol_old",
            duration_seconds=1800,
            created_at=1_699_990_000,
        )
        await self.db.archive_and_delete_recording_by_id(
            "carol_old",
            final_status="purged",
            reason="test",
        )
        rows = await self.db.get_recording_intervals_in_range(
            from_ts=1_699_980_000,
            to_ts=1_700_010_000,
            usernames=["carol"],
        )
        locations = {row["location"] for row in rows}
        self.assertIn("vps", locations)
        self.assertIn("history", locations)


if __name__ == "__main__":
    unittest.main()

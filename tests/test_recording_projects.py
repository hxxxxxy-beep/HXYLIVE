"""Recording project helpers: parts, spans, gap merge, finalize."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.core.database import Database
from app.services import recording_projects as rp


class RecordingProjectHelpersTests(unittest.TestCase):
    def test_strip_part_suffix(self):
        base, label, num = rp.strip_part_suffix("2026-09-12_07-00-00_part002")
        self.assertEqual(base, "2026-09-12_07-00-00")
        self.assertEqual(label, "P2")
        self.assertEqual(num, 2)

        base2, label2, num2 = rp.strip_part_suffix("plain_clip")
        self.assertEqual(base2, "plain_clip")
        self.assertIsNone(label2)
        self.assertIsNone(num2)

        _, label3, num3 = rp.strip_part_suffix("x_PART010")
        self.assertEqual(label3, "P10")
        self.assertEqual(num3, 10)

    def test_parse_started_at_from_stem(self):
        ts = rp.parse_started_at_from_stem("2026-09-12_07-00-00_part002")
        expected = int(datetime(2026, 9, 12, 7, 0, 0).timestamp())
        self.assertEqual(ts, expected)

        compact = rp.parse_started_at_from_stem("alice_20260912_070000")
        self.assertEqual(compact, expected)

        fallback = rp.parse_started_at_from_stem("no-date-here", fallback=1_700_000_000)
        self.assertEqual(fallback, 1_700_000_000)

    def test_build_clip_structure(self):
        members = [
            {
                "filename": "2026-09-12_07-00-00_part001.mp4",
                "started_at": 100,
                "sort_index": 1,
            },
            {
                "filename": "2026-09-12_07-00-00_part002.mp4",
                "started_at": 110,
                "sort_index": 2,
            },
            {
                "filename": "2026-09-12_08-00-00.mp4",
                "started_at": 200,
                "sort_index": 0,
            },
        ]
        self.assertEqual(
            rp.build_clip_structure(members),
            "Clip 1 (P1, P2) · Clip 2 (P1)",
        )

    def test_format_span_line(self):
        self.assertEqual(rp.format_span_line(0, 0, 0), "")
        self.assertEqual(rp.format_span_line(100, 100, 65), "1m 5s")
        self.assertEqual(rp.format_span_line(100, 165, 65), "1m 5s")
        self.assertEqual(rp.format_span_line(100, 200, 65), "1m 5s / 1m 40s")
        self.assertEqual(rp.format_span_line(100, 160, 0), "1m")


class RecordingProjectAssignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.initialize()
        await self.db.set_setting("gap_buffer_minutes", "30")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_assign_fragment_gap_merge(self):
        t0 = 1_700_000_000
        first = await rp.assign_fragment_to_project(
            self.db,
            username="alice",
            filename="2026-09-12_07-00-00.mp4",
            item_id="item-a",
            started_at=t0,
            duration_seconds=600,
            size_bytes=1000,
        )
        project_id = first["project_id"]

        # Within 30m gap after first ended (t0+600): merge into same project.
        second = await rp.assign_fragment_to_project(
            self.db,
            username="alice",
            filename="2026-09-12_07-15-00.mp4",
            item_id="item-b",
            started_at=t0 + 600 + 900,
            duration_seconds=300,
            size_bytes=500,
        )
        self.assertEqual(second["project_id"], project_id)

        members = await self.db.list_project_members(project_id)
        self.assertEqual(len(members), 2)

        # Beyond gap: new project.
        third = await rp.assign_fragment_to_project(
            self.db,
            username="alice",
            filename="2026-09-12_09-00-00.mp4",
            item_id="item-c",
            started_at=t0 + 600 + 900 + 300 + 2000,
            duration_seconds=100,
            size_bytes=200,
        )
        self.assertNotEqual(third["project_id"], project_id)

        # Same session_key (_partNNN) joins even if timestamps look new.
        part = await rp.assign_fragment_to_project(
            self.db,
            username="alice",
            filename="2026-09-12_07-00-00_part002.mp4",
            item_id="item-d",
            started_at=t0 + 10,
            duration_seconds=60,
            size_bytes=50,
        )
        self.assertEqual(part["project_id"], project_id)

    async def test_finalize_ready_projects(self):
        t0 = 1_700_000_000
        project = await rp.assign_fragment_to_project(
            self.db,
            username="bob",
            filename="clip.mp4",
            item_id="item-bob",
            started_at=t0,
            duration_seconds=120,
            size_bytes=10,
        )
        project_id = project["project_id"]
        # Force known gap and ended_at for finalize math.
        await self.db.update_recording_project(
            project_id,
            status="open",
            ended_at=t0 + 120,
            gap_buffer_seconds=1800,
            ready_at=None,
        )

        not_yet = await rp.finalize_ready_projects(self.db, now=t0 + 120 + 100)
        self.assertEqual(not_yet, [])
        mid = await self.db.get_recording_project(project_id)
        self.assertEqual(mid["status"], "awaiting_gap")

        ready = await rp.finalize_ready_projects(self.db, now=t0 + 120 + 1800)
        self.assertEqual(ready, [project_id])
        done = await self.db.get_recording_project(project_id)
        self.assertEqual(done["status"], "ready")
        self.assertEqual(done["ready_at"], t0 + 120 + 1800)

    async def test_mac_path_helpers_in_module(self):
        self.assertIn(".projects/", rp.mac_project_relative_dir("bob", "pid"))
        self.assertTrue(rp.mac_final_relative_path("bob", 1_700_000_000, 1_700_003_600).endswith(".mp4"))


if __name__ == "__main__":
    unittest.main()

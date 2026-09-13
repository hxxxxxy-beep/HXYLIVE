"""Download queue scheduler and concat path naming tests."""
import unittest

from app.services import download_queue as dq
from app.services import recording_projects as rp
from app.services.concat_queue import ConcatQueue


class DownloadQueueSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.q = dq.DownloadQueue()

    def test_sort_append_batch_ready_before_open(self):
        entries = [
            {
                "itemId": "open-1",
                "projectId": "p-open",
                "projectStatus": "open",
                "projectStartedAt": 100,
                "startedAt": 100,
                "sortIndex": 0,
                "filename": "a.mp4",
            },
            {
                "itemId": "ready-1",
                "projectId": "p-ready",
                "projectStatus": "ready",
                "projectStartedAt": 200,
                "startedAt": 200,
                "sortIndex": 0,
                "filename": "b.mp4",
            },
            {
                "itemId": "ready-2",
                "projectId": "p-ready",
                "projectStatus": "ready",
                "projectStartedAt": 200,
                "startedAt": 210,
                "sortIndex": 2,
                "filename": "c_part002.mp4",
            },
        ]
        ordered = dq.sort_append_batch(entries)
        self.assertEqual([e["itemId"] for e in ordered], ["ready-1", "ready-2", "open-1"])

    def test_append_skips_duplicates(self):
        self.q.append_batch([
            {"itemId": "a", "projectId": "p1", "projectStatus": "ready", "filename": "a.mp4"},
            {"itemId": "b", "projectId": "p1", "projectStatus": "ready", "filename": "b.mp4"},
        ])
        again = self.q.append_batch([
            {"itemId": "a", "projectId": "p1", "projectStatus": "ready", "filename": "a.mp4"},
            {"itemId": "c", "projectId": "p1", "projectStatus": "ready", "filename": "c.mp4"},
        ])
        self.assertEqual(again["addedCount"], 1)
        self.assertEqual(len(again["skipped"]), 1)
        grouped = self.q.list_grouped()
        self.assertEqual(grouped["taskCount"], 3)

    def test_scheduler_respects_project_and_file_concurrency(self):
        # Three projects, two files each.
        batch = []
        for pi in range(3):
            for fi in range(2):
                batch.append({
                    "itemId": f"p{pi}-f{fi}",
                    "projectId": f"proj-{pi}",
                    "projectStatus": "ready",
                    "projectStartedAt": pi,
                    "startedAt": fi,
                    "sortIndex": fi,
                    "filename": f"f{fi}.mp4",
                    "sizeBytes": 100,
                })
        self.q.append_batch(batch)

        claimed = self.q.claim_next(project_concurrency=2, file_concurrency=3)
        self.assertEqual(len(claimed), 3)
        project_ids = {c["projectId"] for c in claimed}
        self.assertLessEqual(len(project_ids), 2)

        # Active projects still fill first when more slots open.
        claimed2 = self.q.claim_next(project_concurrency=2, file_concurrency=4)
        # One more file slot available (4-3=1)
        self.assertEqual(len(claimed2), 1)
        self.assertIn(claimed2[0]["projectId"], project_ids)

    def test_paused_project_does_not_occupy_slot(self):
        self.q.append_batch([
            {"itemId": "a1", "projectId": "pa", "projectStatus": "ready", "filename": "a1.mp4"},
            {"itemId": "a2", "projectId": "pa", "projectStatus": "ready", "filename": "a2.mp4"},
            {"itemId": "b1", "projectId": "pb", "projectStatus": "ready", "filename": "b1.mp4"},
        ])
        first = self.q.claim_next(project_concurrency=1, file_concurrency=2)
        self.assertEqual(len(first), 2)
        self.assertEqual({c["projectId"] for c in first}, {"pa"})

        self.q.pause(project_ids=["pa"])
        # Paused project frees the only project slot for pb.
        nxt = self.q.claim_next(project_concurrency=1, file_concurrency=2)
        self.assertEqual(len(nxt), 1)
        self.assertEqual(nxt[0]["projectId"], "pb")

    def test_normalize_concurrency_bounds(self):
        self.assertEqual(dq.normalize_project_concurrency(0), 1)
        self.assertEqual(dq.normalize_project_concurrency(99), 4)
        self.assertEqual(dq.normalize_file_concurrency(1), 2)
        self.assertEqual(dq.normalize_file_concurrency(99), 12)


class ConcatPathNamingTests(unittest.TestCase):
    def test_mac_project_relative_dir(self):
        path = rp.mac_project_relative_dir("alice(chaturbate)", "20260912_070000__alice__abc123")
        self.assertEqual(path, "alice(chaturbate)/.projects/20260912_070000__alice__abc123")

    def test_mac_final_relative_path(self):
        started = int(__import__("datetime").datetime(2026, 9, 12, 7, 0, 0).timestamp())
        ended = int(__import__("datetime").datetime(2026, 9, 12, 8, 30, 15).timestamp())
        path = rp.mac_final_relative_path("alice(chaturbate)", started, ended)
        self.assertEqual(
            path,
            "alice(chaturbate)/2026-09-12 07-00-00_to_08-30-15.mp4",
        )

    def test_concat_queue_serial_claim(self):
        q = ConcatQueue()
        q.enqueue([
            {"projectId": "p1", "memberPaths": ["a/1.mp4"], "finalRelativePath": "a/out.mp4"},
            {"projectId": "p2", "memberPaths": ["b/1.mp4"], "finalRelativePath": "b/out.mp4"},
        ])
        first = q.claim_next()
        self.assertIsNotNone(first)
        self.assertEqual(first["projectId"], "p1")
        second = q.claim_next()
        self.assertIsNone(second)  # concurrency 1
        q.report(project_id="p1", status="done")
        third = q.claim_next()
        self.assertEqual(third["projectId"], "p2")


if __name__ == "__main__":
    unittest.main()

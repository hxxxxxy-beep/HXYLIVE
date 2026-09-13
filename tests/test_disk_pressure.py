"""Tests for dual-disk storage gates and staging promotion."""
import tempfile
import unittest
from pathlib import Path

from app.tasks.disk_pressure import (
    DEFAULT_LIBRARY_STOP_GB,
    DEFAULT_STAGING_STOP_GB,
    GB,
    normalize_library_stop_gb,
    normalize_staging_stop_gb,
    promote_closed_staging_files,
    promote_staging_file_to_library,
    staging_has_recording_files,
    storage_gate_decision,
)


class NormalizeThresholdTests(unittest.TestCase):
    def test_staging_defaults_and_clamps(self):
        self.assertEqual(normalize_staging_stop_gb(None), DEFAULT_STAGING_STOP_GB)
        self.assertEqual(normalize_staging_stop_gb(1), 2)
        self.assertEqual(normalize_staging_stop_gb(99), 20)
        self.assertEqual(normalize_staging_stop_gb(5), 5)

    def test_library_defaults_and_clamps(self):
        self.assertEqual(normalize_library_stop_gb(None), DEFAULT_LIBRARY_STOP_GB)
        self.assertEqual(normalize_library_stop_gb(10), 50)
        self.assertEqual(normalize_library_stop_gb(999), 500)
        self.assertEqual(normalize_library_stop_gb(100), 100)


class StorageGateDecisionTests(unittest.TestCase):
    def test_library_blocks_below_threshold(self):
        decision = storage_gate_decision(
            library_free_bytes=99 * GB,
            staging_free_bytes=40 * GB,
            library_stop_bytes=100 * GB,
            staging_stop_bytes=5 * GB,
            library_blocked=False,
            staging_blocked=False,
            staging_has_files=False,
        )
        self.assertTrue(decision.library_blocked)
        self.assertTrue(decision.pause_convert)
        self.assertFalse(decision.staging_blocked)

    def test_library_clears_when_space_recovers_without_auto_resume(self):
        decision = storage_gate_decision(
            library_free_bytes=120 * GB,
            staging_free_bytes=40 * GB,
            library_stop_bytes=100 * GB,
            staging_stop_bytes=5 * GB,
            library_blocked=True,
            staging_blocked=False,
            staging_has_files=False,
        )
        self.assertFalse(decision.library_blocked)

    def test_staging_blocks_below_threshold(self):
        decision = storage_gate_decision(
            library_free_bytes=500 * GB,
            staging_free_bytes=4 * GB,
            library_stop_bytes=100 * GB,
            staging_stop_bytes=5 * GB,
            library_blocked=False,
            staging_blocked=False,
            staging_has_files=True,
        )
        self.assertTrue(decision.staging_blocked)

    def test_staging_clears_only_when_empty(self):
        still = storage_gate_decision(
            library_free_bytes=500 * GB,
            staging_free_bytes=40 * GB,
            library_stop_bytes=100 * GB,
            staging_stop_bytes=5 * GB,
            library_blocked=False,
            staging_blocked=True,
            staging_has_files=True,
        )
        self.assertTrue(still.staging_blocked)
        cleared = storage_gate_decision(
            library_free_bytes=500 * GB,
            staging_free_bytes=40 * GB,
            library_stop_bytes=100 * GB,
            staging_stop_bytes=5 * GB,
            library_blocked=False,
            staging_blocked=True,
            staging_has_files=False,
        )
        self.assertFalse(cleared.staging_blocked)


class StagingPromoteTests(unittest.TestCase):
    def test_promotes_closed_file_and_skips_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging" / "records"
            library = root / "library" / "records"
            active = staging / "alice" / "videos" / "record"
            active.mkdir(parents=True)
            closed = staging / "bob" / "videos" / "record"
            closed.mkdir(parents=True)
            active_file = active / "live.ts"
            closed_file = closed / "done.ts"
            active_file.write_bytes(b"active")
            closed_file.write_bytes(b"closed")

            moved = promote_closed_staging_files(
                staging_records_root=staging,
                library_records_root=library,
                active_path_keys={str(active_file.resolve())},
            )
            self.assertEqual(len(moved), 1)
            self.assertTrue((library / "bob" / "videos" / "record" / "done.ts").exists())
            self.assertTrue(active_file.exists())
            self.assertFalse(closed_file.exists())
            self.assertTrue(staging_has_recording_files(staging))

            promote_staging_file_to_library(
                active_file,
                staging_records_root=staging,
                library_records_root=library,
            )
            self.assertFalse(staging_has_recording_files(staging))


if __name__ == "__main__":
    unittest.main()

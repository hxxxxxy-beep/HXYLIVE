import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_helper_module():
    path = Path(__file__).resolve().parents[1] / "mac-helper" / "hxylive_mac_helper.py"
    spec = importlib.util.spec_from_file_location("hxylive_mac_helper", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper_mod = _load_helper_module()


class MacHelperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.video_dir = root / "videos"
        self.support_dir = root / "support"
        self.video_dir.mkdir()
        self.support_dir.mkdir()
        self.helper = helper_mod.Helper(
            self.video_dir,
            "http://example.test",
            "",
            support_dir=self.support_dir,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discards_v1_dispatch_time_ledger(self):
        self.helper.ledger_path.write_text(
            json.dumps([{
                "recordingId": "rec_pending",
                "filename": "hxylive-rec_pending__clip.mp4",
                "size": 999,
            }]),
            encoding="utf-8",
        )
        self.assertEqual(self.helper._load_ledger(), [])
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"], [])
        self.assertEqual(scanned["scanSource"], "disk")

    def test_readable_empty_folder_does_not_use_ledger_ghosts(self):
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_gone",
            "filename": "hxylive-rec_gone__clip.mp4",
            "size": 12,
        }])
        # Only an in-progress Chrome download — must not count as present.
        (self.video_dir / "Unconfirmed 123.crdownload").write_bytes(b"partial")
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"], [])
        self.assertEqual(scanned["scanSource"], "disk")
        self.assertEqual(self.helper._load_ledger(), [])

    def test_ledger_fallback_only_when_folder_unreadable(self):
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_done",
            "filename": "hxylive-rec_done__clip.mp4",
            "size": 12,
        }])
        with patch.object(self.helper, "_file_entries_from_disk", return_value=([], False)):
            scanned = self.helper.scan()
        self.assertEqual(scanned["scanSource"], "ledger")
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_done")

    def test_scan_confirms_disk_files_into_v2_ledger(self):
        name = "hxylive-rec_clip__clip.mp4"
        path = self.video_dir / name
        path.write_bytes(b"0123456789ab")

        scanned = self.helper.scan()
        self.assertEqual(len(scanned["files"]), 1)
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_clip")
        self.assertEqual(scanned["files"][0]["size"], 12)
        self.assertEqual(scanned["scanSource"], "disk")

        payload = json.loads(self.helper.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["files"][0]["recordingId"], "rec_clip")

        # Readable folder after delete must clear presence (not keep ledger ghosts).
        path.unlink()
        cleared = self.helper.scan()
        self.assertEqual(cleared["files"], [])
        self.assertEqual(cleared["scanSource"], "disk")
        self.assertEqual(self.helper._load_ledger(), [])

    def test_relocate_moves_chrome_file_into_streamer_folder(self):
        chrome_dir = Path(self.tmpdir.name) / "chrome-downloads"
        chrome_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(chrome_dir)
        chrome_name = "hxylive-rec_clip__pending.mp4"
        source = chrome_dir / chrome_name
        source.write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": chrome_name,
            "relativePath": "model/2026-08-02.mp4",
            "size": 12,
        }
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper._relocate_one(item)
        dest = self.video_dir / "model" / "2026-08-02.mp4"
        self.assertFalse(source.exists())
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"0123456789ab")
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_clip")
        self.assertEqual(scanned["files"][0]["filename"], "model/2026-08-02.mp4")

    def test_wait_for_filed_returns_after_relocate(self):
        chrome_dir = Path(self.tmpdir.name) / "chrome-downloads"
        chrome_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(chrome_dir)
        chrome_name = "hxylive-rec_clip__pending.mp4"
        (chrome_dir / chrome_name).write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": chrome_name,
            "relativePath": "model/2026-08-02.mp4",
            "size": 12,
        }
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            waiter = threading.Thread(target=self.helper._relocate_one, args=(item,))
            waiter.start()
            result = self.helper.wait_for_filed(["rec_clip"], 2.0)
            waiter.join(timeout=2.0)
        self.assertIn("rec_clip", result["filed"])
        self.assertEqual(result["remaining"], [])

    def test_relocate_picks_up_chrome_download_outside_video_dir(self):
        chrome_dir = Path(self.tmpdir.name) / "chrome-downloads"
        chrome_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(chrome_dir)
        chrome_name = "hxylive-rec_clip__pending.mp4"
        source = chrome_dir / chrome_name
        source.write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": chrome_name,
            "relativePath": "model/2026-08-02.mp4",
            "size": 12,
        }
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper._relocate_one(item)
        dest = self.video_dir / "model" / "2026-08-02.mp4"
        self.assertFalse(source.exists())
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"0123456789ab")

    def test_download_watch_dirs_include_chrome_override(self):
        chrome_dir = Path(self.tmpdir.name) / "chrome-downloads"
        chrome_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(chrome_dir)
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            watched = self.helper._download_watch_dirs()
        self.assertEqual(watched, [chrome_dir.resolve()])
        self.assertNotIn(self.video_dir.resolve(), watched)

    def test_download_watch_dirs_are_chrome_prefs_only(self):
        chrome_dir = Path(self.tmpdir.name) / "chrome-downloads"
        chrome_dir.mkdir()
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[chrome_dir]):
            watched = self.helper._download_watch_dirs()
        self.assertEqual(watched, [chrome_dir.resolve()])
        self.assertNotIn(self.video_dir.resolve(), watched)
        self.assertNotIn((Path.home() / "Downloads").resolve(), watched)

    def test_download_watch_dirs_empty_when_chrome_prefs_empty(self):
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            watched = self.helper._download_watch_dirs()
        self.assertEqual(watched, [])
        self.assertNotIn(self.video_dir.resolve(), watched)

    def test_open_local_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            self.helper.open_local(relative="../outside.mp4")
        with self.assertRaises(ValueError):
            self.helper.open_local(relative="/tmp/evil.mp4")

    def test_open_local_opens_resolved_file(self):
        target = self.video_dir / "model" / "2026-08-02.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        with patch("subprocess.run") as run:
            result = self.helper.open_local(relative="model/2026-08-02.mp4")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["relativePath"], "model/2026-08-02.mp4")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/open", str(target.resolve())])

    def test_open_local_by_recording_id(self):
        target = self.video_dir / "model" / "2026-08-02.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        self.helper.upsert_ledger_entry("rec_clip", "model/2026-08-02.mp4", 12)
        # Flat legacy name so scan attaches recordingId from filename prefix too.
        legacy = self.video_dir / "hxylive-rec_clip__pending.mp4"
        legacy.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=None):
            with patch.object(self.helper, "_probe_resolution", return_value=None):
                with patch("subprocess.run") as run:
                    result = self.helper.open_local(recording_id="rec_clip")
        self.assertEqual(result["status"], "ok")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/open", str(legacy.resolve())])

    def test_delete_local_removes_file_and_ledger(self):
        target = self.video_dir / "model" / "2026-08-02.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        self.helper.upsert_ledger_entry("rec_clip", "model/2026-08-02.mp4", 12)

        result = self.helper.delete_local(recording_id="rec_clip")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["relativePath"], "model/2026-08-02.mp4")
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertEqual(self.helper._load_ledger(), [])

    def test_scan_includes_duration_when_probe_succeeds(self):
        path = self.video_dir / "model" / "2026-08-02_153045.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=125):
            with patch.object(self.helper, "_probe_resolution", return_value=None):
                scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["durationSeconds"], 125)

    def test_scan_includes_resolution_when_probe_succeeds(self):
        path = self.video_dir / "model" / "2026-08-02_153045.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=None):
            with patch.object(self.helper, "_probe_resolution", return_value="1920x1080"):
                scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["resolution"], "1920x1080")

    def test_probe_resolution_reads_mdls_attrs_separately(self):
        path = self.video_dir / "clip.mp4"
        path.write_bytes(b"0123456789ab")

        def fake_mdls(_path, name):
            return {"kMDItemPixelWidth": 1280, "kMDItemPixelHeight": 720}.get(name)

        with patch.object(self.helper, "_mdls_raw_number", side_effect=fake_mdls):
            self.assertEqual(self.helper._probe_resolution(path), "1280x720")

    def test_ensure_thumbnail_uses_cache(self):
        target = self.video_dir / "model" / "2026-08-02_153045.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        cache_dir = self.support_dir / "thumbnails"
        cache_dir.mkdir(parents=True)

        def fake_qlmanage(cmd, capture_output=True, text=True, check=False):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            produced = out_dir / (target.name + ".png")
            produced.write_bytes(b"png-bytes")

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        with patch("subprocess.run", side_effect=fake_qlmanage):
            first = self.helper.ensure_thumbnail(relative="model/2026-08-02_153045.mp4")
            second = self.helper.ensure_thumbnail(relative="model/2026-08-02_153045.mp4")
        self.assertTrue(first.is_file())
        self.assertEqual(first.read_bytes(), b"png-bytes")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

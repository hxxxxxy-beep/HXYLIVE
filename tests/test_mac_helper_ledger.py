import importlib.util
import errno
import json
import struct
import sys
import tempfile
import threading
import time
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

    def test_scan_reuses_cache_within_ttl(self):
        path = self.video_dir / "model" / "clip.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        first = self.helper.scan()
        self.assertEqual(first["scanSource"], "disk")
        self.assertEqual(len(first["files"]), 1)
        second = self.helper.scan()
        self.assertEqual(second["scanSource"], "cache")
        self.assertEqual(len(second["files"]), 1)
        forced = self.helper.scan(force=True)
        self.assertEqual(forced["scanSource"], "disk")

    def test_scan_skips_motrix_in_progress_with_aria2_sidecar(self):
        profile = self.video_dir / "model(chaturbate)"
        profile.mkdir()
        partial = profile / "2026-09-05 12-00-00.mp4"
        partial.write_bytes(b"partial-bytes")
        Path(str(partial) + ".aria2").write_bytes(b"control")
        finished = profile / "2026-09-04 12-00-00.mp4"
        finished.write_bytes(b"0123456789ab")
        scanned = self.helper.scan()
        names = [entry["filename"] for entry in scanned["files"]]
        self.assertEqual(names, ["model(chaturbate)/2026-09-04 12-00-00.mp4"])
        self.assertEqual(scanned["scanSource"], "disk")

    def test_scan_ignores_root_staging_videos(self):
        staging = self.video_dir / "2026-09-05 12-00-00.mp4"
        staging.write_bytes(b"partial-or-staged")
        Path(str(staging) + ".aria2").write_bytes(b"control")
        profile = self.video_dir / "model(chaturbate)"
        profile.mkdir()
        finished = profile / "2026-09-04 12-00-00.mp4"
        finished.write_bytes(b"0123456789ab")
        scanned = self.helper.scan()
        names = [entry["filename"] for entry in scanned["files"]]
        self.assertEqual(names, ["model(chaturbate)/2026-09-04 12-00-00.mp4"])
        self.assertEqual(scanned["scanSource"], "disk")

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

    def test_missing_folder_does_not_use_ledger_ghosts(self):
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_ext",
            "filename": "model/2026-08-02 12-00-00.mp4",
            "size": 12,
        }])
        self.video_dir.rmdir()
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"], [])
        self.assertEqual(scanned["scanSource"], "disk")
        ledger = self.helper._load_ledger()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["recordingId"], "rec_ext")

    def test_missing_folder_keeps_ledger_mapping_after_remount(self):
        relative = "model/2026-08-02 12-00-00.mp4"
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_ext",
            "filename": relative,
            "size": 12,
        }])
        self.video_dir.rmdir()
        self.assertEqual(self.helper.scan()["files"], [])

        dest = self.video_dir / relative
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"0123456789ab")
        scanned = self.helper.scan(force=True)
        self.assertEqual(scanned["scanSource"], "disk")
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_ext")
        self.assertEqual(scanned["files"][0]["filename"], relative)

    def test_unmounted_volume_does_not_create_stub_or_ghosts(self):
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_ext",
            "filename": "model/clip.mp4",
            "size": 12,
        }])
        missing_vol = Path("/Volumes/HxyliveMissingTestVol")
        self.helper.video_dir_arg = str(missing_vol / "HXYLIVE")
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"], [])
        self.assertEqual(scanned["scanSource"], "disk")
        self.assertFalse(missing_vol.exists())
        self.assertEqual(self.helper._load_ledger()[0]["recordingId"], "rec_ext")

    def test_leftover_volumes_stub_is_treated_as_unmounted(self):
        clip = self.video_dir / "clip.mp4"
        clip.write_bytes(b"0123456789ab")
        self.helper.remember_confirmed_files([{
            "recordingId": "rec_ext",
            "filename": "clip.mp4",
            "size": 12,
        }])
        with patch.object(self.helper, "_volume_root", return_value=self.video_dir):
            with patch("os.path.ismount", return_value=False):
                scanned = self.helper.scan()
        self.assertEqual(scanned["files"], [])
        self.assertEqual(scanned["scanSource"], "disk")
        self.assertEqual(self.helper._load_ledger()[0]["recordingId"], "rec_ext")

    def test_scan_confirms_disk_files_into_v2_ledger(self):
        name = "hxylive-rec_clip__clip.mp4"
        path = self.video_dir / "model" / name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")

        scanned = self.helper.scan()
        self.assertEqual(len(scanned["files"]), 1)
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_clip")
        self.assertEqual(scanned["files"][0]["filename"], f"model/{name}")
        self.assertEqual(scanned["files"][0]["size"], 12)
        self.assertEqual(scanned["scanSource"], "disk")

        payload = json.loads(self.helper.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["files"][0]["recordingId"], "rec_clip")

        # Readable folder after delete must clear presence (not keep ledger ghosts).
        path.unlink()
        cleared = self.helper.scan(force=True)
        self.assertEqual(cleared["files"], [])
        self.assertEqual(cleared["scanSource"], "disk")
        self.assertEqual(self.helper._load_ledger(), [])

    def test_scan_keeps_motrix_date_named_ledger_across_rescan(self):
        """Date-renamed Motrix files must not lose recordingId when another file is scanned."""
        dated = self.video_dir / "tiffy(Twitch)" / "2026-09-05 02-30-08.mp4"
        dated.parent.mkdir(parents=True)
        dated.write_bytes(b"0123456789ab")
        other = self.video_dir / "other(chaturbate)" / "hxylive-rec_other__other.mp4"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"xxxxxxxxxxxx")
        self.helper.upsert_ledger_entry(
            "rec_tiffy",
            "tiffy(Twitch)/2026-09-05 02-30-08.mp4",
            12,
        )
        with patch.object(self.helper, "_probe_duration_seconds", return_value=None):
            with patch.object(self.helper, "_probe_resolution", return_value=None):
                scanned = self.helper.scan()
        by_name = {entry["filename"]: entry for entry in scanned["files"]}
        self.assertEqual(
            by_name["tiffy(Twitch)/2026-09-05 02-30-08.mp4"]["recordingId"],
            "rec_tiffy",
        )
        ledger_ids = {entry["recordingId"] for entry in self.helper._load_ledger()}
        self.assertIn("rec_tiffy", ledger_ids)
        self.assertIn("rec_other", ledger_ids)

    def test_attach_recording_ids_by_unique_ledger_size(self):
        self.helper.upsert_ledger_entry("rec_tiffy", "old/name.mp4", 42)
        files = [{"recordingId": None, "filename": "tiffy/2026-09-05.mp4", "size": 42}]
        attached = self.helper._attach_recording_ids(files)
        self.assertEqual(attached[0]["recordingId"], "rec_tiffy")

    def test_probe_duration_reads_mp4_mvhd(self):
        # Minimal ftyp + moov/mvhd (version 0, timescale 1000, duration 125000 → 125s).
        mvhd = (
            b"\x00"  # version
            + b"\x00\x00\x00"  # flags
            + b"\x00\x00\x00\x00"  # creation
            + b"\x00\x00\x00\x00"  # modification
            + b"\x00\x00\x03\xe8"  # timescale 1000
            + b"\x00\x01\xe8\x48"  # duration 125000
        )
        mvhd_box = struct.pack(">I4s", 8 + len(mvhd), b"mvhd") + mvhd
        moov_box = struct.pack(">I4s", 8 + len(mvhd_box), b"moov") + mvhd_box
        ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\x00\x00\x00\x00"
        path = self.video_dir / "clip.mp4"
        path.write_bytes(ftyp + moov_box)
        self.assertEqual(self.helper._probe_duration_mp4(path), 125)
        with patch.object(self.helper, "_mdls_raw_number", return_value=None):
            with patch("subprocess.run") as run:
                # First call is mdls duration; force failure so mvhd path is used.
                run.return_value = type(
                    "Result",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": ""},
                )()
                self.assertEqual(self.helper._probe_duration_seconds(path), 125)

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
            with patch.object(self.helper, "_push_folder_snapshot"):
                self.helper._relocate_one(item)
        dest = self.video_dir / "model" / "2026-08-02.mp4"
        self.assertFalse(source.exists())
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"0123456789ab")
        scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["recordingId"], "rec_clip")
        self.assertEqual(scanned["files"][0]["filename"], "model/2026-08-02.mp4")

    def test_move_file_cross_volume_uses_copy2_and_deletes_source(self):
        staging = Path(self.tmpdir.name) / "staging"
        staging.mkdir()
        source = staging / "hxylive-rec_x__pending.mp4"
        source.write_bytes(b"cross-volume-bytes")
        dest = self.video_dir / "model" / "filed.mp4"
        dest.parent.mkdir(parents=True)

        def exdev_replace(self_path, target):
            raise OSError(errno.EXDEV, "Cross-device link")

        with patch.object(Path, "replace", exdev_replace):
            with patch("subprocess.run") as run:
                self.helper._move_file(source, dest)
                run.assert_not_called()
        self.assertFalse(source.exists())
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"cross-volume-bytes")

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
            with patch.object(self.helper, "_push_folder_snapshot"):
                waiter = threading.Thread(
                    target=self.helper._relocate_one,
                    args=(item,),
                )
                waiter.start()
                result = self.helper.wait_for_filed(["rec_clip"], 2.0)
                waiter.join(timeout=2.0)
        self.assertIn("rec_clip", result["filed"])
        self.assertEqual(result["remaining"], [])

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
            run.return_value = type("Result", (), {"returncode": 0})()
            result = self.helper.open_local(relative="model/2026-08-02.mp4")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["relativePath"], "model/2026-08-02.mp4")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0][:3],
            ["/usr/bin/open", "-a", "IINA"],
        )
        self.assertEqual(run.call_args.args[0][3], str(target.resolve()))

    def test_open_local_falls_back_when_iina_missing(self):
        target = self.video_dir / "model" / "2026-08-02.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        with patch("subprocess.run") as run:
            run.side_effect = [
                type("Result", (), {"returncode": 1})(),
                type("Result", (), {"returncode": 0})(),
            ]
            result = self.helper.open_local(relative="model/2026-08-02.mp4")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["/usr/bin/open", "-a", "IINA"])
        self.assertEqual(run.call_args_list[1].args[0][:2], ["/usr/bin/open", str(target.resolve())])

    def test_open_local_by_recording_id(self):
        target = self.video_dir / "model" / "2026-08-02.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789ab")
        self.helper.upsert_ledger_entry("rec_clip", "model/2026-08-02.mp4", 12)
        with patch.object(self.helper, "_probe_duration_seconds", return_value=None):
            with patch.object(self.helper, "_probe_resolution", return_value=None):
                with patch.object(self.helper, "_probe_fps", return_value=None):
                    with patch.object(self.helper, "_probe_bitrate", return_value=None):
                        with patch("subprocess.run") as run:
                            run.return_value = type("Result", (), {"returncode": 0})()
                            result = self.helper.open_local(recording_id="rec_clip")
        self.assertEqual(result["status"], "ok")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/open", "-a", "IINA"])
        self.assertEqual(run.call_args.args[0][3], str(target.resolve()))

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

    def test_rename_streamer_folder_updates_files_and_ledger(self):
        src = self.video_dir / "1883358196(bilibili)" / "2026-09-05 20-24-23.mp4"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"0123456789ab")
        self.helper.upsert_ledger_entry(
            "rec_bili",
            "1883358196(bilibili)/2026-09-05 20-24-23.mp4",
            12,
        )
        result = self.helper.rename_streamer_folder(
            "1883358196(bilibili)",
            "示例主播(bilibili)",
        )
        self.assertEqual(result["movedFiles"], 1)
        dst = self.video_dir / "示例主播(bilibili)" / "2026-09-05 20-24-23.mp4"
        self.assertTrue(dst.exists())
        self.assertFalse(src.parent.exists())
        ledger = self.helper._load_ledger()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(
            ledger[0]["filename"],
            "示例主播(bilibili)/2026-09-05 20-24-23.mp4",
        )

    def test_scan_includes_duration_when_probe_succeeds(self):
        path = self.video_dir / "model" / "2026-08-02_153045.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=125):
            with patch.object(self.helper, "_probe_resolution", return_value=None):
                with patch.object(self.helper, "_probe_fps", return_value=None):
                    with patch.object(self.helper, "_probe_bitrate", return_value=None):
                        scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["durationSeconds"], 125)

    def test_scan_includes_resolution_when_probe_succeeds(self):
        path = self.video_dir / "model" / "2026-08-02_153045.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=None):
            with patch.object(self.helper, "_probe_resolution", return_value="1920x1080"):
                with patch.object(self.helper, "_probe_fps", return_value=None):
                    with patch.object(self.helper, "_probe_bitrate", return_value=None):
                        scanned = self.helper.scan()
        self.assertEqual(scanned["files"][0]["resolution"], "1920x1080")

    def test_scan_includes_fps_and_bitrate_when_probe_succeeds(self):
        path = self.video_dir / "model" / "2026-08-02_153045.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789ab")
        with patch.object(self.helper, "_probe_duration_seconds", return_value=125):
            with patch.object(self.helper, "_probe_resolution", return_value="1280x720"):
                with patch.object(self.helper, "_probe_fps", return_value=30.0):
                    with patch.object(self.helper, "_probe_bitrate", return_value=4_200_000):
                        scanned = self.helper.scan()
        entry = scanned["files"][0]
        self.assertEqual(entry["resolution"], "1280x720")
        self.assertEqual(entry["fps"], 30.0)
        self.assertEqual(entry["bitrate"], 4_200_000)

    def test_probe_resolution_reads_mdls_attrs_separately(self):
        path = self.video_dir / "clip.mp4"
        path.write_bytes(b"0123456789ab")

        def fake_mdls(_path, name):
            return {"kMDItemPixelWidth": 1280, "kMDItemPixelHeight": 720}.get(name)

        with patch.object(self.helper, "_mdls_raw_number", side_effect=fake_mdls):
            self.assertEqual(self.helper._probe_resolution(path), "1280x720")

    def test_parse_frame_rate_supports_fraction(self):
        self.assertEqual(self.helper._parse_frame_rate("30/1"), 30.0)
        self.assertEqual(self.helper._parse_frame_rate("30000/1001"), 29.97)
        self.assertIsNone(self.helper._parse_frame_rate("0/0"))

    def test_probe_fps_mp4_reads_stts_delta(self):
        def box(typ: bytes, payload: bytes) -> bytes:
            return struct.pack(">I4s", 8 + len(payload), typ) + payload

        mdhd = bytes([0, 0, 0, 0]) + b"\x00" * 8 + struct.pack(">II", 30, 900) + b"\x00\x00\x00\x00"
        hdlr = bytes([0, 0, 0, 0]) + b"\x00" * 4 + b"vide" + b"\x00" * 12
        stts = box(b"stts", bytes([0, 0, 0, 0]) + struct.pack(">III", 1, 900, 1))
        mdia = box(
            b"mdia",
            box(b"mdhd", mdhd) + box(b"hdlr", hdlr) + box(b"minf", box(b"stbl", stts)),
        )
        path = self.video_dir / "fps-clip.mp4"
        path.write_bytes(box(b"ftyp", b"isom" + b"\x00" * 4) + box(b"moov", box(b"trak", mdia)))
        self.assertEqual(self.helper._probe_fps_mp4(path), 30.0)

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

    def test_opener_uses_proxy_only_when_reachable(self):
        self.helper.proxy_url = "http://127.0.0.1:7897"

        def proxied_http(opener):
            for handler in opener.handlers:
                proxies = getattr(handler, "proxies", None)
                if isinstance(proxies, dict) and proxies.get("http"):
                    return proxies.get("http")
            return None

        with patch.object(self.helper, "_proxy_is_reachable", return_value=True):
            self.assertEqual(proxied_http(self.helper._opener()), "http://127.0.0.1:7897")
        with patch.object(self.helper, "_proxy_is_reachable", return_value=False):
            self.assertIsNone(proxied_http(self.helper._opener()))

    def test_heartbeat_claims_pending_jobs(self):
        opened = []

        class ImmediateThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

        def fake_vps(method, path, payload=None, timeout=20):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/api/mac/helper/heartbeat")
            self.assertEqual(payload["localSessionId"], self.helper.session_id)
            return {
                "pendingJobs": [{
                    "jobId": "job1",
                    "items": [{"url": "http://example.test/file", "downloadFilename": "a.mp4"}],
                }]
            }

        with patch.object(self.helper, "_vps_json", side_effect=fake_vps), \
             patch.object(self.helper, "open_downloads", side_effect=lambda *args: opened.append(args)), \
             patch.object(helper_mod.threading, "Thread", ImmediateThread):
            self.helper._push_heartbeat_and_run_jobs()
        self.assertEqual(opened, [("job1", [{
            "url": "http://example.test/file",
            "downloadFilename": "a.mp4",
        }], "chrome")])

    def test_heartbeat_passes_motrix_method(self):
        opened = []

        class ImmediateThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

        def fake_vps(method, path, payload=None, timeout=20):
            return {
                "pendingJobs": [{
                    "jobId": "job-motrix",
                    "method": "motrix",
                    "autoSync": True,
                    "items": [{"url": "http://example.test/file", "downloadFilename": "b.mp4"}],
                }],
                "macAutoSync": False,
            }

        with patch.object(self.helper, "_vps_json", side_effect=fake_vps), \
             patch.object(self.helper, "open_downloads", side_effect=lambda *args: opened.append(args)), \
             patch.object(helper_mod.threading, "Thread", ImmediateThread):
            self.helper._push_heartbeat_and_run_jobs()
        self.assertEqual(opened[0][0], "job-motrix")
        self.assertEqual(opened[0][2], "motrix")
        # Auto Sync flag on the job is ignored; only jobId/items/method are passed.
        self.assertEqual(len(opened[0]), 3)

    def test_motrix_download_dirs_include_config_dir(self):
        motrix_dir = Path(self.tmpdir.name) / "motrix-downloads"
        motrix_dir.mkdir()
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        with patch.object(self.helper, "_motrix_config", return_value={"dir": str(motrix_dir)}), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            watched = self.helper._download_watch_dirs("motrix")
        self.assertIn(motrix_dir.resolve(), watched)
        self.assertIn(download_dir.resolve(), watched)
        self.assertEqual(watched[0], download_dir.resolve())
        self.assertNotIn(self.video_dir.resolve(), watched)

    def test_send_to_motrix_uses_rpc(self):
        calls = []
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"jsonrpc":"2.0","id":"1","result":"gid"}'

        def fake_urlopen(req, timeout=5):
            calls.append(json.loads(req.data.decode("utf-8")))
            return FakeResponse()

        with patch.object(self.helper, "_motrix_config", return_value={"dir": str(Path(self.tmpdir.name)), "rpc-secret": ""}), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.helper._send_to_motrix(
                "http://example.test/a.mp4",
                "a.mp4",
                "model/2026-09-05 02-30-08.mp4",
            )
        add_calls = [c for c in calls if c["method"] == "aria2.addUri"]
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(add_calls[0]["params"][0], ["http://example.test/a.mp4"])
        opts = add_calls[0]["params"][1]
        self.assertEqual(opts["out"], "2026-09-05 02-30-08.mp4")
        self.assertEqual(opts["dir"], str(download_dir.resolve()))
        self.assertEqual(opts["pause"], "false")
        self.assertEqual(opts["split"], "64")
        self.assertEqual(opts["max-connection-per-server"], "64")
        self.assertTrue((self.video_dir / "model").is_dir())

    def test_send_to_motrix_tracks_gid_for_progress(self):
        calls = []
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"jsonrpc":"2.0","id":"1","result":"gidmanual01"}'

        def fake_urlopen(req, timeout=5):
            calls.append(json.loads(req.data.decode("utf-8")))
            return FakeResponse()

        with patch.object(self.helper, "_motrix_config", return_value={"dir": str(Path(self.tmpdir.name)), "rpc-secret": ""}), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.helper._send_to_motrix(
                "http://example.test/a.mp4",
                "a.mp4",
                "model/2026-09-05 02-30-08.mp4",
            )
        self.assertIn("gidmanual01", self.helper._motrix_gids)
        dest = str((download_dir / "2026-09-05 02-30-08.mp4").resolve())
        self.assertIn(dest, self.helper._motrix_paths)

    def test_repeated_error_log_collapses_duplicates(self):
        prints = []

        def fake_print(*args, **kwargs):
            prints.append(args[0] if args else "")

        with patch("builtins.print", side_effect=fake_print), \
             patch.object(helper_mod, "TRANSIENT_ERROR_LOG_INTERVAL_SECONDS", 60.0):
            self.helper._log_repeated("vps-sync", "VPS sync failed: timeout('x')")
            self.helper._log_repeated("vps-sync", "VPS sync failed: timeout('x')")
            self.helper._log_repeated("vps-sync", "VPS sync failed: timeout('x')")
            self.helper._clear_repeated("vps-sync")
        self.assertEqual(prints[0], "[mac-helper] VPS sync failed: timeout('x')")
        self.assertTrue(any("repeated 2x; recovered" in line for line in prints))

    def test_motrix_relocate_moves_from_mac_staging(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        staging = download_dir / "final.mp4"
        staging.write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": "hxylive-rec_clip__pending.mp4",
            "relativePath": "model/final.mp4",
            "size": 12,
        }
        notified = []
        with patch.object(self.helper, "notify_filed", side_effect=lambda *a: notified.append(a)), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]), \
             patch.object(self.helper, "_wait_for_download_file", return_value=staging):
            self.helper._relocate_one(item, "motrix")
        dest = self.video_dir / "model" / "final.mp4"
        self.assertTrue(dest.is_file())
        self.assertFalse(staging.exists())
        self.assertEqual(notified, [("rec_clip", "model/final.mp4", 12)])

    def test_pending_relocate_survives_restart_resume(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": "hxylive-rec_clip__pending.mp4",
            "relativePath": "model/final.mp4",
            "size": 12,
        }
        self.helper._enqueue_pending_relocate(item, "motrix")
        self.assertEqual(len(self.helper._load_pending_relocates()), 1)
        staging = download_dir / "final.mp4"
        staging.write_bytes(b"0123456789ab")
        notified = []
        with patch.object(self.helper, "notify_filed", side_effect=lambda *a: notified.append(a)), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper._resume_pending_relocates()
        dest = self.video_dir / "model" / "final.mp4"
        self.assertTrue(dest.is_file())
        self.assertFalse(staging.exists())
        self.assertEqual(self.helper._load_pending_relocates(), [])
        self.assertEqual(notified, [("rec_clip", "model/final.mp4", 12)])

    def test_pending_sweep_moves_ready_without_waiting_on_incomplete(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        ready = download_dir / "ready.mp4"
        ready.write_bytes(b"0123456789ab")
        partial = download_dir / "partial.mp4"
        partial.write_bytes(b"0123456789")
        Path(str(partial) + ".aria2").write_bytes(b"ctrl")
        ready_item = {
            "recordingId": "rec_ready",
            "downloadFilename": "hxylive-rec_ready__pending.mp4",
            "relativePath": "model/ready.mp4",
            "size": 12,
        }
        partial_item = {
            "recordingId": "rec_partial",
            "downloadFilename": "hxylive-rec_partial__pending.mp4",
            "relativePath": "model/partial.mp4",
            "size": 100,
        }
        # Incomplete first in the queue — must not block the ready file behind it.
        self.helper._enqueue_pending_relocate(partial_item, "motrix")
        self.helper._enqueue_pending_relocate(ready_item, "motrix")
        notified = []
        with patch.object(self.helper, "notify_filed", side_effect=lambda *a: notified.append(a)), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            filed = self.helper._sweep_pending_relocates()
        self.assertEqual(filed, 1)
        self.assertTrue((self.video_dir / "model" / "ready.mp4").is_file())
        self.assertFalse(ready.exists())
        self.assertTrue(partial.exists())
        pending = self.helper._load_pending_relocates()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["relativePath"], "model/partial.mp4")
        self.assertEqual(notified, [("rec_ready", "model/ready.mp4", 12)])

    def test_pending_sweep_drops_stale_missing_sources(self):
        item = {
            "recordingId": "rec_gone",
            "downloadFilename": "hxylive-rec_gone__pending.mp4",
            "relativePath": "model/gone.mp4",
            "size": 12,
        }
        self.helper._enqueue_pending_relocate(item, "motrix")
        with self.helper._pending_relocate_lock:
            rows = self.helper._load_pending_relocates()
            rows[0]["enqueuedAt"] = int(time.time()) - (6 * 60 * 60) - 10
            self.helper._save_pending_relocates(rows)
        with patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper._sweep_pending_relocates()
        self.assertEqual(self.helper._load_pending_relocates(), [])

    def test_open_downloads_skips_motrix_readd_when_staging_ready(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        staging = download_dir / "final.mp4"
        staging.write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": "hxylive-rec_clip__pending.mp4",
            "relativePath": "model/final.mp4",
            "size": 12,
            "url": "https://example.test/final.mp4",
        }
        sent = []
        notified = []
        with patch.object(self.helper, "_send_to_motrix", side_effect=lambda *a, **k: sent.append(a)), \
             patch.object(self.helper, "notify_filed", side_effect=lambda *a: notified.append(a)), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper.open_downloads("job1", [item], method="motrix")
        self.assertEqual(sent, [])
        dest = self.video_dir / "model" / "final.mp4"
        self.assertTrue(dest.is_file())
        self.assertFalse(staging.exists())
        self.assertEqual(self.helper._load_pending_relocates(), [])
        self.assertEqual(notified, [("rec_clip", "model/final.mp4", 12)])

    def test_motrix_relocate_skips_move_when_already_in_library(self):
        dest = self.video_dir / "model" / "final.mp4"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"0123456789ab")
        item = {
            "recordingId": "rec_clip",
            "downloadFilename": "hxylive-rec_clip__pending.mp4",
            "relativePath": "model/final.mp4",
            "size": 12,
        }
        notified = []
        with patch.object(self.helper, "notify_filed", side_effect=lambda *a: notified.append(a)), \
             patch.object(self.helper, "_move_file") as move_mock:
            self.helper._relocate_one(item, "motrix")
        move_mock.assert_not_called()
        self.assertEqual(notified, [("rec_clip", "model/final.mp4", 12)])

    def test_heartbeat_and_command_poll_run_open_commands(self):
        opened = []

        def fake_vps(method, path, payload=None, timeout=20):
            if method == "GET":
                self.assertIn("/api/mac/helper/commands?", path)
                return {
                    "pendingCommands": [{
                        "commandId": "cmd1",
                        "type": "open",
                        "relativePath": "model/clip.mp4",
                        "recordingId": "rec_clip",
                        "reveal": True,
                    }]
                }
            return {"pendingJobs": [], "pendingCommands": [{
                "commandId": "cmd2",
                "type": "open",
                "relativePath": "model/other.mp4",
                "recordingId": "",
                "reveal": False,
            }]}

        with patch.object(self.helper, "_vps_json", side_effect=fake_vps), \
             patch.object(self.helper, "open_local", side_effect=lambda **kwargs: opened.append(kwargs) or {
                 "status": "ok",
                 "relativePath": kwargs.get("relative") or "x",
                 "reveal": kwargs.get("reveal"),
             }):
            self.helper._claim_and_run_commands()
            self.helper._push_heartbeat_and_run_jobs()
        self.assertEqual(opened, [
            {"relative": "model/clip.mp4", "recording_id": "rec_clip", "reveal": True},
            {"relative": "model/other.mp4", "recording_id": "", "reveal": False},
        ])

    def test_thumb_command_uploads_cover(self):
        uploaded = []

        def fake_vps(method, path, payload=None, timeout=20):
            uploaded.append((method, path, payload))
            return {}

        png = self.video_dir / "clip.mp4"
        png.write_bytes(b"video")
        thumb = self.support_dir / "thumbnails" / "fake.png"
        thumb.parent.mkdir(parents=True)
        thumb.write_bytes(b"png-bytes")

        with patch.object(self.helper, "_vps_json", side_effect=fake_vps), \
             patch.object(self.helper, "ensure_thumbnail", return_value=thumb), \
             patch.object(self.helper, "_resolve_local_media", return_value=png):
            result = self.helper.upload_thumbnail(relative="clip.mp4", recording_id="rec_clip")
        self.assertEqual(result["relativePath"], "clip.mp4")
        self.assertEqual(uploaded[0][0], "POST")
        self.assertEqual(uploaded[0][1], "/api/mac/helper/thumb")
        self.assertEqual(uploaded[0][2]["recordingId"], "rec_clip")
        self.assertTrue(uploaded[0][2]["imageBase64"])

    def test_pending_incomplete_does_not_probe_library(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        partial = download_dir / "partial.mp4"
        partial.write_bytes(b"0123456789")
        Path(str(partial) + ".aria2").write_bytes(b"ctrl")
        item = {
            "recordingId": "rec_partial",
            "downloadFilename": "hxylive-rec_partial__pending.mp4",
            "relativePath": "model/partial.mp4",
            "size": 100,
        }
        self.helper._enqueue_pending_relocate(item, "motrix")
        with patch.object(self.helper, "_library_dest_for_item") as library_mock, \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            filed = self.helper._sweep_pending_relocates()
        self.assertEqual(filed, 0)
        library_mock.assert_not_called()
        self.assertEqual(len(self.helper._load_pending_relocates()), 1)
        self.assertEqual(
            self.helper._pending_relocate_wait_seconds(),
            helper_mod.PENDING_RELOCATE_IDLE_POLL_SECONDS,
        )

    def test_motrix_send_reuses_existing_task(self):
        download_dir = Path(self.tmpdir.name) / "mac-downloads"
        download_dir.mkdir()
        self.helper.chrome_download_dir_arg = str(download_dir)
        dest = str(download_dir / "final.mp4")
        calls = []

        def fake_rpc(method, *params):
            calls.append(method)
            if method in {"aria2.tellActive", "aria2.tellWaiting"}:
                return [{
                    "gid": "gid-existing",
                    "status": "paused",
                    "completedLength": "50",
                    "totalLength": "100",
                    "files": [{"path": dest}],
                }]
            raise AssertionError(method)

        with patch.object(self.helper, "_motrix_rpc", side_effect=fake_rpc), \
             patch.object(self.helper, "_chrome_preference_download_dirs", return_value=[]):
            self.helper._send_to_motrix(
                "https://example.test/a.mp4",
                "hxylive-rec__pending.mp4",
                "model/final.mp4",
                recording_id="rec",
                item_id="item",
            )
        self.assertNotIn("aria2.addUri", calls)
        self.assertIn("gid-existing", self.helper._motrix_gids)
        tracked = {str(Path(p).resolve()) for p in self.helper._motrix_paths}
        self.assertIn(str(Path(dest).resolve()), tracked)

    def test_motrix_collapse_duplicate_paths_keeps_best_progress(self):
        dest = str(Path(self.tmpdir.name) / "same.mp4")
        removed = []

        def fake_rpc(method, *params):
            if method == "aria2.tellActive":
                return []
            if method == "aria2.tellWaiting":
                return [
                    {
                        "gid": "gid-low",
                        "status": "paused",
                        "completedLength": "10",
                        "files": [{"path": dest}],
                    },
                    {
                        "gid": "gid-high",
                        "status": "paused",
                        "completedLength": "90",
                        "files": [{"path": dest}],
                    },
                ]
            if method in {"aria2.forceRemove", "aria2.remove", "aria2.removeDownloadResult"}:
                removed.append(params[0])
                return "ok"
            raise AssertionError(method)

        with patch.object(self.helper, "_motrix_rpc", side_effect=fake_rpc):
            collapsed = self.helper._motrix_collapse_duplicate_paths()
        self.assertEqual(collapsed, 1)
        self.assertIn("gid-low", removed)
        self.assertIn("gid-high", self.helper._motrix_gids)
        self.assertNotIn("gid-low", self.helper._motrix_gids)


class MacHelperHttpsProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.video_dir = root / "videos"
        self.support_dir = root / "support"
        self.video_dir.mkdir()
        self.support_dir.mkdir()
        self.helper = helper_mod.Helper(
            self.video_dir,
            "http://203.0.113.10:8080",
            "http://127.0.0.1:7897",
            support_dir=self.support_dir,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_http_origin_uses_proxy_when_reachable(self):
        with patch.object(self.helper, "_proxy_is_reachable", return_value=True):
            self.assertTrue(self.helper._want_proxy())
            for handler in self.helper._opener().handlers:
                proxies = getattr(handler, "proxies", None)
                if isinstance(proxies, dict):
                    self.assertEqual(proxies.get("http"), "http://127.0.0.1:7897")
                    self.assertEqual(proxies.get("https"), "http://127.0.0.1:7897")

    def test_http_origin_falls_back_when_proxy_fails(self):
        http_helper = helper_mod.Helper(
            self.video_dir,
            "http://203.0.113.10:8080",
            "http://127.0.0.1:7897",
            support_dir=self.support_dir,
        )
        calls = []

        def fake_once(method, url, parsed, body, headers, timeout, use_proxy):
            calls.append(use_proxy)
            if use_proxy:
                raise TimeoutError("timed out")
            return {"ok": True}

        with patch.object(http_helper, "_proxy_is_reachable", return_value=True), \
             patch.object(http_helper, "_vps_json_once", side_effect=fake_once):
            result = http_helper._vps_json("GET", "/api/version")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()

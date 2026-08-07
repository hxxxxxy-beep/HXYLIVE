import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database


def mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


class MediaLibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR
        self.original_profile_images_dir = app_main.PROFILE_IMAGES_DIR
        self.original_range_chunk_size = app_main.RECORDING_RANGE_CHUNK_SIZE

        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.PROFILE_IMAGES_DIR = self.output_dir / "profile-images"
        app_main.db = Database(self.output_dir / "streamrec.db")
        await app_main.db.initialize()

        self.records_dir = self.output_dir / "records" / "model"
        self.records_dir.mkdir(parents=True)
        self.video = self.records_dir / "clip.mp4"
        self.video.write_bytes(b"0123456789")
        self.video_thumb = self.output_dir / "thumbnails" / "model" / "clip.jpg"
        self.video_thumb.parent.mkdir(parents=True, exist_ok=True)
        self.video_thumb.write_bytes(b"thumb")
        self.photo = self.records_dir / "photo.jpg"
        self.photo.write_bytes(b"\xff\xd8\xff\xe0photo")
        self.ts_file = self.records_dir / "raw.ts"
        self.ts_file.write_bytes(b"ts should stay out of media")
        self.empty_dir = self.output_dir / "records" / "empty_model"
        self.empty_dir.mkdir(parents=True)
        old = time.time() - 120
        os.utime(self.video, (old, old))
        os.utime(self.photo, (old + 10, old + 10))
        os.utime(self.ts_file, (old + 20, old + 20))

        await app_main.db.add_or_update_recording(
            username="model",
            filename="clip.mp4",
            file_path=str(self.video),
            file_size=self.video.stat().st_size,
            recording_id="rec_clip",
            duration_seconds=12,
            thumbnail_path=str(self.video_thumb),
            is_converted=True,
            media_kind="recording",
            created_at=int(old),
        )

        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        app_main.PROFILE_IMAGES_DIR = self.original_profile_images_dir
        app_main.RECORDING_RANGE_CHUNK_SIZE = self.original_range_chunk_size
        self.tmpdir.cleanup()

    async def test_lists_videos_and_photos_from_records_folder(self):
        response = self.client.get("/api/media-library")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["stats"]["total"], 2)
        self.assertEqual(data["stats"]["videos"], 1)
        self.assertEqual(data["stats"]["images"], 1)
        self.assertEqual(data["profiles"][0]["username"], "model")
        self.assertEqual(data["profiles"][0]["latestTitle"], "photo")
        self.assertEqual(
            data["profiles"][0]["latestRecordingCover"],
            "/api/recording-thumbnail/model/clip.jpg",
        )
        self.assertEqual(data["profiles"][0]["profileImageUrl"], "")
        self.assertNotIn("thumbnail", data["profiles"][0])
        profiles = {profile["username"]: profile for profile in data["profiles"]}
        self.assertIn("empty_model", profiles)
        self.assertEqual(profiles["empty_model"]["total"], 0)
        self.assertTrue(profiles["empty_model"]["folderExists"])

        items = {item["filename"]: item for item in data["items"]}
        self.assertEqual(items["clip.mp4"]["type"], "video")
        self.assertTrue(items["clip.mp4"]["isRecording"])
        self.assertEqual(items["clip.mp4"]["duration"], 12)
        self.assertEqual(items["photo.jpg"]["type"], "image")
        self.assertEqual(items["photo.jpg"]["thumbnail"], items["photo.jpg"]["url"])
        self.assertNotIn("raw.ts", items)

        storage = data["storage"]
        self.assertNotIn("status", storage)
        self.assertNotIn("message", storage)
        self.assertGreaterEqual(storage["recordingFolderBytes"], 0)
        self.assertTrue(storage["recordingFolderFormatted"])
        self.assertGreaterEqual(storage["processingBytes"], 0)
        self.assertTrue(storage["processingFormatted"])
        self.assertGreaterEqual(storage["untrackedBytes"], 0)
        self.assertTrue(storage["untrackedFormatted"])
        self.assertGreater(storage["diskUsedBytes"], 0)
        self.assertTrue(storage["diskUsedFormatted"])
        self.assertGreater(storage["diskFreeBytes"], 0)
        self.assertTrue(storage["diskFreeFormatted"])
        self.assertGreaterEqual(storage["diskUsedPercent"], 0)
        self.assertGreaterEqual(storage["diskFreePercent"], 0)

    async def test_media_profiles_include_last_seen_online_at_from_followed(self):
        await app_main.db.upsert_followed_model(
            username="model",
            display_name="Model",
            is_online=True,
            viewers=12,
            source_type="chaturbate",
        )
        followed = await app_main.db.get_followed_model("model", "chaturbate")
        self.assertIsNotNone(followed)
        expected = int(followed["last_seen_online_at"])
        self.assertGreater(expected, 0)

        await app_main.db.upsert_followed_model(
            username="model",
            display_name="Model",
            is_online=False,
            viewers=0,
            source_type="chaturbate",
        )

        response = self.client.get("/api/media-library?metadata=lazy&limit=50")
        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "model"
        )
        self.assertEqual(profile["lastSeenOnlineAt"], expected)
        self.assertEqual(profile["last_seen_online_at"], expected)
        self.assertEqual(profile["lastLiveAt"], expected)
        self.assertFalse(profile["isOnline"])

    async def test_media_profiles_last_live_falls_back_to_latest_video(self):
        response = self.client.get("/api/media-library?metadata=lazy&limit=50")
        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "model"
        )
        candidates = [
            int(value)
            for value in (profile.get("latestVideoAt"), profile.get("latestAt"))
            if value
        ]
        self.assertTrue(candidates)
        self.assertEqual(profile["lastLiveAt"], max(candidates))

    async def test_media_profiles_last_live_prefers_newer_video_over_stale_seen(self):
        stale_seen = int(time.time()) - 8 * 3600
        await app_main.db.upsert_followed_model(
            username="model",
            display_name="Model",
            is_online=True,
            viewers=3,
            source_type="chaturbate",
        )
        # Force an older last-seen while a newer recording exists.
        async with app_main.db._connect() as conn:
            await conn.execute(
                "UPDATE followed_models SET last_seen_online_at = ? WHERE username = ?",
                (stale_seen, "model"),
            )
            await conn.commit()
        await app_main.db.upsert_followed_model(
            username="model",
            display_name="Model",
            is_online=False,
            viewers=0,
            source_type="chaturbate",
        )

        response = self.client.get("/api/media-library?metadata=lazy&limit=50")
        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "model"
        )
        latest_media = max(
            int(value)
            for value in (profile.get("latestVideoAt"), profile.get("latestAt"))
            if value
        )
        self.assertGreater(latest_media, stale_seen)
        self.assertEqual(profile["lastSeenOnlineAt"], stale_seen)
        self.assertEqual(profile["lastLiveAt"], latest_media)

    async def test_note_last_seen_online_from_monitor_without_media_page(self):
        """Tracked-model online polls bump last-seen even if Media UI is closed."""
        await app_main.db.add_or_update_model(
            username="model",
            display_name="Model",
            auto_record=True,
            source_type="chaturbate",
        )
        await app_main.db.upsert_followed_model(
            username="model",
            display_name="Model",
            is_online=False,
            viewers=0,
            source_type="chaturbate",
        )
        async with app_main.db._connect() as conn:
            await conn.execute(
                "UPDATE followed_models SET last_seen_online_at = NULL WHERE username = ?",
                ("model",),
            )
            await conn.commit()

        before = int(time.time()) - 1
        await app_main.db.update_model_status(
            username="model",
            is_online=True,
            viewers=12,
            source_type="chaturbate",
        )
        profile = await app_main.db.get_media_profile("model")
        followed = await app_main.db.get_followed_model("model", "chaturbate")
        self.assertIsNotNone(profile)
        self.assertGreaterEqual(int(profile["last_seen_online_at"]), before)
        self.assertGreaterEqual(int(followed["last_seen_online_at"]), before)

        # Recording-end style bump (start + duration) advances monotonically.
        end_at = int(profile["last_seen_online_at"]) + 3600
        await app_main.db.note_last_seen_online(
            "model",
            seen_at=end_at,
            source_type="chaturbate",
        )
        profile = await app_main.db.get_media_profile("model")
        followed = await app_main.db.get_followed_model("model", "chaturbate")
        self.assertEqual(int(profile["last_seen_online_at"]), end_at)
        self.assertEqual(int(followed["last_seen_online_at"]), end_at)

        # Older stamps must not rewind last-seen.
        await app_main.db.note_last_seen_online(
            "model",
            seen_at=end_at - 10_000,
            source_type="chaturbate",
        )
        profile = await app_main.db.get_media_profile("model")
        self.assertEqual(int(profile["last_seen_online_at"]), end_at)

        response = self.client.get("/api/media-library?metadata=lazy&limit=50")
        card = next(
            item for item in response.json()["profiles"] if item["username"] == "model"
        )
        self.assertEqual(card["lastSeenOnlineAt"], end_at)
        self.assertEqual(card["lastLiveAt"], end_at)

    async def test_mac_snapshot_and_download_job(self):
        library = self.client.get("/api/media-library?kind=video").json()
        item = next(entry for entry in library["items"] if entry["filename"] == "clip.mp4")

        snapshot = self.client.post(
            "/api/mac/sync-snapshot",
            json={
                "localSessionId": "mac-session",
                "files": [{
                    "recordingId": item["recordingId"],
                    "filename": "anything.mp4",
                    "size": item["size"],
                }],
            },
        )
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["statuses"][item["id"]], "synced")

        created = self.client.post(
            "/api/mac/download-jobs",
            json={"localSessionId": "mac-session", "itemIds": [item["id"]]},
        )
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["jobId"]
        wrong_session = self.client.get(
            f"/api/mac/download-jobs/{job_id}?localSessionId=other"
        )
        self.assertEqual(wrong_session.status_code, 403)

        job = self.client.get(
            f"/api/mac/download-jobs/{job_id}?localSessionId=mac-session"
        )
        self.assertEqual(job.status_code, 200)
        entry = job.json()["items"][0]
        self.assertTrue(entry["downloadFilename"].startswith("hxylive-rec_clip__"))
        self.assertTrue(entry["filename"].startswith("hxylive-rec_clip__"))
        self.assertTrue(entry["relativePath"].startswith("model/"))
        self.assertIn("/", entry["relativePath"])
        # Standard Mac names: model/YYYY-MM-DD HH-MM-SS.ext
        self.assertRegex(
            entry["relativePath"],
            r"^model/\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.mp4$",
        )

        authorized = self.client.get(entry["url"])
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(
            authorized.headers["x-accel-redirect"],
            "/_protected_recordings/model/clip.mp4",
        )
        self.assertIn("attachment", authorized.headers["content-disposition"])
        self.assertIn("hxylive-rec_clip__", authorized.headers["content-disposition"])

    async def test_mac_download_claim_and_token_are_public_when_password_enabled(self):
        library = self.client.get("/api/media-library?kind=video").json()
        item = next(entry for entry in library["items"] if entry["filename"] == "clip.mp4")

        original_password = app_main.PASSWORD
        original_sessions = set(app_main.active_sessions)
        app_main.PASSWORD = "secret"
        app_main.active_sessions.clear()
        try:
            create_unauth = self.client.post(
                "/api/mac/download-jobs",
                json={"localSessionId": "mac-session", "itemIds": [item["id"]]},
            )
            self.assertEqual(create_unauth.status_code, 401)

            app_main.active_sessions.add("authed")
            created = self.client.post(
                "/api/mac/download-jobs",
                json={"localSessionId": "mac-session", "itemIds": [item["id"]]},
                cookies={"session_token": "authed"},
            )
            self.assertEqual(created.status_code, 200)
            job_id = created.json()["jobId"]
            app_main.active_sessions.clear()

            job = self.client.get(
                f"/api/mac/download-jobs/{job_id}?localSessionId=mac-session"
            )
            self.assertEqual(job.status_code, 200)
            entry = job.json()["items"][0]
            authorized = self.client.get(entry["url"])
            self.assertEqual(authorized.status_code, 200)
            self.assertIn("attachment", authorized.headers["content-disposition"])
        finally:
            app_main.PASSWORD = original_password
            app_main.active_sessions.clear()
            app_main.active_sessions.update(original_sessions)

    async def test_mac_snapshot_marks_different_size_incomplete(self):
        library = self.client.get("/api/media-library?kind=video").json()
        item = next(entry for entry in library["items"] if entry["filename"] == "clip.mp4")
        snapshot = self.client.post(
            "/api/mac/sync-snapshot",
            json={
                "localSessionId": "mac-session",
                "files": [{
                    "recordingId": item["recordingId"],
                    "filename": "clip.mp4",
                    "size": item["size"] - 1,
                }],
            },
        )
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["statuses"][item["id"]], "incomplete")

    async def test_batch_deletes_selected_videos_in_one_request(self):
        second = self.records_dir / "second.mp4"
        second.write_bytes(b"second-video")
        old = time.time() - 120
        os.utime(second, (old, old))

        library = self.client.get("/api/media-library?kind=video").json()
        item_ids = [
            entry["id"]
            for entry in library["items"]
            if entry["filename"] in {"clip.mp4", "second.mp4"}
        ]
        self.assertEqual(len(item_ids), 2)

        response = self.client.post(
            "/api/media-library/batch-delete",
            json={"itemIds": item_ids},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deletedCount"], 2)
        self.assertFalse(self.video.exists())
        self.assertFalse(second.exists())

    async def test_indexes_manual_video_with_duration_and_thumbnail(self):
        manual = self.records_dir / "manual_import.mp4"
        manual.write_bytes(b"manual video")
        old = time.time() - 120
        os.utime(manual, (old, old))
        thumb = self.output_dir / "thumbnails" / "model" / "manual_import.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=61)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            response = self.client.get("/api/media-library?kind=video&search=manual_import")

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["filename"], "manual_import.mp4")
        self.assertTrue(item["isImported"])
        self.assertEqual(item["duration"], 61)
        self.assertEqual(item["durationStr"], "1m01s")
        self.assertEqual(item["thumbnail"], "/api/recording-thumbnail/model/manual_import.jpg")
        self.assertEqual(item["createdAt"], 1704164645)

        recs = await app_main.db.get_recordings("model")
        indexed = next(rec for rec in recs if rec["filename"] == "manual_import.mp4")
        self.assertEqual(indexed["media_kind"], "import")
        self.assertEqual(indexed["duration_seconds"], 61)
        self.assertEqual(indexed["thumbnail_path"], str(thumb))
        self.assertEqual(indexed["created_at"], 1704164645)

    async def test_lazy_media_library_listing_does_not_probe_manual_video(self):
        manual = self.empty_dir / "lazy_manual.mp4"
        manual.write_bytes(b"manual video")
        old = time.time() - 120
        os.utime(manual, (old, old))

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=61)) as duration_mock,
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)) as created_mock,
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value="thumb")) as thumb_mock,
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, manual, None)),
            ) as convert_mock,
        ):
            response = self.client.get("/api/media-library?metadata=lazy&kind=video&search=lazy_manual")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["filename"], "lazy_manual.mp4")
        self.assertEqual(item["duration"], 0)
        self.assertTrue(item["recordingId"])
        self.assertTrue(item["url"].startswith("/streams/library/empty_model/"))
        self.assertFalse(item["isImported"])
        self.assertFalse(item["isRecording"])
        duration_mock.assert_not_awaited()
        created_mock.assert_not_awaited()
        thumb_mock.assert_not_awaited()
        convert_mock.assert_not_awaited()
        self.assertEqual([], await app_main.db.get_recordings("empty_model"))

    async def test_filters_media_library(self):
        response = self.client.get("/api/media-library?kind=image&search=photo")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["filename"], "photo.jpg")

    async def test_marks_media_library_video_as_watched(self):
        unwatched_before = self.client.get("/api/media-library?kind=video&watched=unwatched")
        self.assertEqual(unwatched_before.status_code, 200)
        self.assertEqual(unwatched_before.json()["total"], 1)

        position = self.client.post(
            "/api/playback-position/rec_clip",
            json={"username": "model", "position": 11, "duration": 12},
        )
        self.assertEqual(position.status_code, 200)
        self.assertTrue(position.json()["isWatched"])

        response = self.client.get("/api/media-library?kind=video")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]

        self.assertEqual(item["recordingId"], "rec_clip")
        self.assertEqual(item["playbackProgress"], 92)
        self.assertTrue(item["isWatched"])
        self.assertIsNotNone(item["watchedAt"])

        unwatched_after = self.client.get("/api/media-library?kind=video&watched=unwatched")
        self.assertEqual(unwatched_after.status_code, 200)
        self.assertEqual(unwatched_after.json()["total"], 0)

        watched_only = self.client.get("/api/media-library?kind=video&watched=watched")
        self.assertEqual(watched_only.status_code, 200)
        self.assertEqual(watched_only.json()["total"], 1)

        replay = self.client.post(
            "/api/playback-position/rec_clip",
            json={"username": "model", "position": 1, "duration": 12},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["isWatched"])

    async def test_streams_media_files_securely(self):
        video = self.client.get(
            "/streams/library/model/clip.mp4",
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(video.status_code, 206)
        self.assertEqual(video.content, b"0123")

        photo = self.client.get("/streams/library/model/photo.jpg")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.content, b"\xff\xd8\xff\xe0photo")
        self.assertTrue(photo.headers["content-type"].startswith("image/jpeg"))

        traversal = self.client.get("/streams/library/model/%2E%2E/secret.jpg")
        self.assertIn(traversal.status_code, {400, 404})

        ts_media = self.client.get("/streams/library/model/raw.ts")
        self.assertEqual(ts_media.status_code, 400)

        delete_ts = self.client.delete("/api/media-library/model/raw.ts")
        self.assertEqual(delete_ts.status_code, 400)
        self.assertTrue(self.ts_file.exists())

    async def test_thumbnail_route_rejects_encoded_profile_traversal(self):
        secret = self.output_dir / "secret.jpg"
        secret.write_bytes(b"internal secret")

        response = self.client.get("/api/recording-thumbnail/%2E%2E/secret.jpg")

        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(b"internal secret", response.content)

    async def test_recording_delete_rejects_encoded_profile_traversal(self):
        target = self.output_dir / "target.mp4"
        target.write_bytes(b"must remain")

        response = self.client.delete("/api/recordings/%2E%2E/target.mp4")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(target.exists())
        self.assertEqual(b"must remain", target.read_bytes())

    async def test_recording_delete_does_not_trust_database_paths_outside_profile(self):
        target = self.output_dir / "target.mp4"
        target.write_bytes(b"must remain")
        await app_main.db.add_or_update_recording(
            username="model",
            filename="target.mp4",
            file_path=str(target),
            file_size=target.stat().st_size,
            recording_id="rec_outside_profile",
            media_kind="recording",
        )

        response = self.client.delete("/api/recordings/model/target.mp4")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(target.exists())
        self.assertEqual(b"must remain", target.read_bytes())

    async def test_initial_open_range_includes_large_mp4_metadata(self):
        app_main.RECORDING_RANGE_CHUNK_SIZE = 64
        ftyp = (32).to_bytes(4, "big") + b"ftyp" + b"isom" + (b"\0" * 20)
        moov = (96).to_bytes(4, "big") + b"moov" + (b"\0" * 88)
        mdat = (72).to_bytes(4, "big") + b"mdat" + (b"1" * 64)
        large_metadata = self.records_dir / "large_metadata.mp4"
        large_metadata.write_bytes(ftyp + moov + mdat)

        response = self.client.head(
            "/streams/library/model/large_metadata.mp4",
            headers={"Range": "bytes=0-"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-127/200")
        self.assertEqual(response.headers["content-length"], "128")

    async def test_web_upload_endpoint_removed_and_direct_image_files_are_listed(self):
        upload = self.client.post(
            "/api/media-profiles/empty_model/uploads",
            content=b"\xff\xd8\xff\xe0portrait",
        )
        self.assertIn(upload.status_code, {404, 405})

        portrait = self.empty_dir / "portrait.jpg"
        portrait.write_bytes(b"\xff\xd8\xff\xe0portrait")
        raw_ts = self.empty_dir / "raw.ts"
        raw_ts.write_bytes(b"transport stream")

        listing = self.client.get("/api/media-library?username=empty_model&kind=image")
        self.assertEqual(listing.status_code, 200)
        items = {item["filename"]: item for item in listing.json()["items"]}
        self.assertEqual("image", items["portrait.jpg"]["type"])
        self.assertNotIn("raw.ts", items)

    async def test_direct_mp4_file_indexes_import_record(self):
        media_file = self.empty_dir / "uploaded.mp4"
        media_file.write_bytes(b"video bytes")
        old = time.time() - 120
        os.utime(media_file, (old, old))
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=33)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        self.assertEqual(listing.status_code, 200)
        item = listing.json()["items"][0]
        self.assertEqual("uploaded.mp4", item["filename"])
        self.assertEqual("video", item["type"])
        self.assertTrue(item["url"].startswith("/streams/media/"))

        recs = await app_main.db.get_recordings("empty_model")
        self.assertEqual(1, len(recs))
        self.assertEqual("import", recs[0]["media_kind"])
        self.assertEqual("uploaded.mp4", recs[0]["filename"])
        self.assertEqual(33, recs[0]["duration_seconds"])

        self.assertEqual(recs[0]["recording_id"], item["recordingId"])
        self.assertTrue(item["browserPlayable"])

    async def test_non_faststart_mp4_file_creates_playable_copy(self):
        media_file = self.empty_dir / "slow_start.mp4"
        media_file.write_bytes(
            mp4_box(b"ftyp", b"isom0000")
            + mp4_box(b"mdat", b"1" * 16)
            + mp4_box(b"moov", b"0" * 16)
        )
        old = time.time() - 120
        os.utime(media_file, (old, old))
        converted = self.output_dir / "media_imports" / "empty_model" / "converted.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4 copy")
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ) as convert_mock,
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        convert_mock.assert_awaited_once()
        rec = (await app_main.db.get_recordings("empty_model"))[0]
        self.assertEqual(str(converted), rec["playable_path"])
        self.assertEqual(str(converted), rec["mp4_path"])

        item = listing.json()["items"][0]
        self.assertTrue(item["url"].startswith("/streams/media/"))
        self.assertTrue(item["browserPlayable"])

    async def test_direct_mkv_file_creates_playable_mp4_copy(self):
        media_file = self.empty_dir / "bonus.mkv"
        media_file.write_bytes(b"mkv bytes")
        old = time.time() - 120
        os.utime(media_file, (old, old))
        converted = self.output_dir / "media_imports" / "empty_model" / "converted.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4 copy")
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ),
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        rec = (await app_main.db.get_recordings("empty_model"))[0]
        self.assertTrue(rec["file_path"].endswith("/records/empty_model/bonus.mkv"))
        self.assertEqual(str(converted), rec["playable_path"])
        self.assertEqual(str(converted), rec["mp4_path"])

        item = listing.json()["items"][0]
        self.assertTrue(item["url"].startswith("/streams/media/"))
        self.assertTrue(item["browserPlayable"])

    async def test_recordings_api_streams_nested_record_path_by_id(self):
        nested_dir = self.output_dir / "records" / "model" / "videos" / "record"
        nested_dir.mkdir(parents=True)
        nested = nested_dir / "nested.ts"
        nested.write_bytes(b"nested-recording")
        await app_main.db.add_or_update_recording(
            username="model",
            filename="nested.ts",
            file_path=str(nested),
            file_size=nested.stat().st_size,
            recording_id="rec_nested",
            duration_seconds=12,
            is_converted=False,
            media_kind="recording",
            created_at=int(time.time()) - 120,
        )

        listing = self.client.get("/api/recordings/model?show_ts=true")
        self.assertEqual(listing.status_code, 200)
        items = {item["recordingId"]: item for item in listing.json()["recordings"]}
        self.assertEqual("/streams/recordings/rec_nested", items["rec_nested"]["url"])

        stream = self.client.get(
            "/streams/recordings/rec_nested",
            headers={"Range": "bytes=0-5"},
        )
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"nested")

        legacy_url = self.client.get(
            "/streams/records/model/nested.ts",
            headers={"Range": "bytes=0-5"},
        )
        self.assertEqual(legacy_url.status_code, 206)
        self.assertEqual(legacy_url.content, b"nested")

    async def test_browser_webm_recording_is_visible_while_raw_ts_is_opt_in(self):
        browser_dir = self.output_dir / "records" / "browser_model"
        browser_dir.mkdir(parents=True)
        webm = browser_dir / "browser_capture.webm"
        webm.write_bytes(b"browser capture")
        await app_main.db.add_or_update_recording(
            username="browser_model",
            filename=webm.name,
            file_path=str(webm),
            file_size=webm.stat().st_size,
            recording_id="browser_capture",
            duration_seconds=12,
            is_converted=False,
            media_kind="recording",
            created_at=int(time.time()) - 120,
        )
        raw_ts = browser_dir / "raw_capture.ts"
        raw_ts.write_bytes(b"raw transport stream")
        await app_main.db.add_or_update_recording(
            username="browser_model",
            filename=raw_ts.name,
            file_path=str(raw_ts),
            file_size=raw_ts.stat().st_size,
            recording_id="raw_capture",
            duration_seconds=12,
            is_converted=False,
            media_kind="recording",
            created_at=int(time.time()) - 119,
        )

        default_listing = self.client.get("/api/recordings/browser_model")
        self.assertEqual(default_listing.status_code, 200)
        self.assertEqual(
            [item["filename"] for item in default_listing.json()["recordings"]],
            ["browser_capture.webm"],
        )

        flat_listing = self.client.get("/api/all-recordings?username=browser_model")
        self.assertEqual(flat_listing.status_code, 200)
        self.assertEqual(
            [item["filename"] for item in flat_listing.json()["recordings"]],
            ["browser_capture.webm"],
        )

        grouped = self.client.get("/api/recordings-by-model")
        self.assertEqual(grouped.status_code, 200)
        browser_group = next(
            item for item in grouped.json()["models"] if item["username"] == "browser_model"
        )
        self.assertEqual(browser_group["recordingCount"], 1)

        visible = self.client.get("/api/recordings/browser_model?show_ts=true")
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(
            [item["filename"] for item in visible.json()["recordings"]],
            ["raw_capture.ts", "browser_capture.webm"],
        )

    async def test_recording_groups_keep_provider_identity_for_distinct_record_paths(self):
        models = [
            ("chaturbate", "shared/chaturbate"),
            ("twitch", "shared/twitch"),
        ]
        for index, (source_type, record_path) in enumerate(models, start=1):
            await app_main.db.add_or_update_model(
                username="shared",
                source_type=source_type,
                auto_record=True,
                record_path=record_path,
            )
            media_dir = self.output_dir / "records" / record_path
            media_dir.mkdir(parents=True)
            media_path = media_dir / f"capture_{source_type}.mp4"
            media_path.write_bytes(source_type.encode("utf-8"))
            await app_main.db.add_or_update_recording(
                username="shared",
                filename=media_path.name,
                file_path=str(media_path),
                file_size=media_path.stat().st_size,
                recording_id=f"shared_{source_type}",
                duration_seconds=index * 10,
                mp4_path=str(media_path),
                mp4_size=media_path.stat().st_size,
                is_converted=True,
                media_kind="recording",
                created_at=1704164645 + index,
            )

        response = self.client.get("/api/recordings-by-model")
        self.assertEqual(response.status_code, 200)
        shared = {
            item["sourceType"]: item
            for item in response.json()["models"]
            if item["username"] == "shared"
        }
        self.assertEqual(set(shared), {"chaturbate", "twitch"})
        self.assertEqual(shared["chaturbate"]["recordingCount"], 1)
        self.assertEqual(shared["twitch"]["recordingCount"], 1)
        self.assertEqual(shared["chaturbate"]["totalDuration"], 10)
        self.assertEqual(shared["twitch"]["totalDuration"], 20)

    async def test_ambiguous_legacy_recording_group_uses_deterministic_provider(self):
        shared_record_path = "shared/videos/record"
        for source_type in ("twitch", "chaturbate"):
            await app_main.db.add_or_update_model(
                username="shared",
                source_type=source_type,
                auto_record=True,
                record_path=shared_record_path,
            )
        media_dir = self.output_dir / "records" / shared_record_path
        media_dir.mkdir(parents=True)
        media_path = media_dir / "legacy.mp4"
        media_path.write_bytes(b"legacy")
        await app_main.db.add_or_update_recording(
            username="shared",
            filename=media_path.name,
            file_path=str(media_path),
            file_size=media_path.stat().st_size,
            recording_id="shared_legacy",
            duration_seconds=30,
            mp4_path=str(media_path),
            mp4_size=media_path.stat().st_size,
            is_converted=True,
            media_kind="recording",
            created_at=1704164645,
        )

        response = self.client.get("/api/recordings-by-model")
        self.assertEqual(response.status_code, 200)
        shared = {
            item["sourceType"]: item
            for item in response.json()["models"]
            if item["username"] == "shared"
        }
        self.assertEqual(shared["chaturbate"]["recordingCount"], 1)
        self.assertEqual(shared["twitch"]["recordingCount"], 0)

    async def test_recording_group_prefers_most_specific_nested_provider_path(self):
        await app_main.db.add_or_update_model(
            username="shared",
            source_type="chaturbate",
            auto_record=True,
            record_path="shared",
        )
        await app_main.db.add_or_update_model(
            username="shared",
            source_type="twitch",
            auto_record=True,
            record_path="shared/twitch",
        )
        media_dir = self.output_dir / "records/shared/twitch"
        media_dir.mkdir(parents=True)
        media_path = media_dir / "nested.mp4"
        media_path.write_bytes(b"twitch")
        await app_main.db.add_or_update_recording(
            username="shared",
            filename=media_path.name,
            file_path=str(media_path),
            file_size=media_path.stat().st_size,
            recording_id="shared_nested",
            duration_seconds=15,
            mp4_path=str(media_path),
            mp4_size=media_path.stat().st_size,
            is_converted=True,
            media_kind="recording",
        )

        response = self.client.get("/api/recordings-by-model")

        self.assertEqual(response.status_code, 200)
        shared = {
            item["sourceType"]: item
            for item in response.json()["models"]
            if item["username"] == "shared"
        }
        self.assertEqual(shared["twitch"]["recordingCount"], 1)
        self.assertEqual(shared["chaturbate"]["recordingCount"], 0)

    async def test_deletes_photo_and_indexed_video(self):
        photo = self.client.delete("/api/media-library/model/photo.jpg")
        self.assertEqual(photo.status_code, 200)
        self.assertFalse(self.photo.exists())

        photo_listing = self.client.get("/api/media-library?kind=image")
        self.assertEqual(photo_listing.status_code, 200)
        self.assertEqual(photo_listing.json()["total"], 0)

        video = self.client.delete("/api/media-library/model/clip.mp4")
        self.assertEqual(video.status_code, 200)
        self.assertFalse(self.video.exists())
        self.assertEqual(await app_main.db.get_recordings("model"), [])

        missing = self.client.delete("/api/media-library/model/clip.mp4")
        self.assertEqual(missing.status_code, 404)

    async def test_deleting_final_mp4_removes_ts_and_conversion_temporary_file(self):
        final_mp4 = self.records_dir / "session.mp4"
        source_ts = self.records_dir / "session.ts"
        temporary_mp4 = self.records_dir / ".session.abcdef.tmp.mp4"
        final_mp4.write_bytes(b"final")
        source_ts.write_bytes(b"source")
        temporary_mp4.write_bytes(b"temporary")
        await app_main.db.add_or_update_recording(
            username="model",
            filename="session.ts",
            file_path=str(source_ts),
            file_size=source_ts.stat().st_size,
            recording_id="rec_session",
            duration_seconds=10,
            mp4_path=str(final_mp4),
            mp4_size=final_mp4.stat().st_size,
            is_converted=True,
            media_kind="recording",
        )

        response = self.client.delete("/api/media-library/model/session.mp4")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(final_mp4.exists())
        self.assertFalse(source_ts.exists())
        self.assertFalse(temporary_mp4.exists())
        self.assertIsNone(await app_main.db.get_recording_by_id("rec_session"))

    async def test_updates_profile_metadata_and_stream_settings(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "Empty Model",
                "firstName": "Empty",
                "lastName": "Model",
                "birthDate": "1999-04-03",
                "age": 25,
                "country": "Canada",
                "socialUrls": ["https://social.example/empty"],
                "streamUrls": ["https://stream.example/empty"],
                "recordQuality": "720p",
                "retentionDays": 14,
                "autoRecord": True,
                "sourceType": "chaturbate",
            },
        )
        self.assertEqual(response.status_code, 200)

        profile = self.client.get("/api/media-profiles/empty_model")
        self.assertEqual(profile.status_code, 200)
        data = profile.json()
        self.assertEqual(data["displayName"], "Empty Model")
        self.assertEqual(data["firstName"], "Empty")
        self.assertEqual(data["birthDate"], "1999-04-03")
        self.assertEqual(data["birth_date"], "1999-04-03")
        self.assertEqual(data["age"], 25)
        self.assertEqual(data["country"], "Canada")
        self.assertEqual(data["socialUrls"], ["https://social.example/empty"])
        self.assertEqual(data["streamUrls"], ["https://stream.example/empty"])
        self.assertEqual(data["recordQuality"], "720p")
        self.assertEqual(data["retentionDays"], 14)
        self.assertTrue(data["autoRecord"])

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertEqual(profiles["empty_model"]["displayName"], "Empty Model")
        self.assertEqual(profiles["empty_model"]["recordQuality"], "720p")
        self.assertEqual(profiles["empty_model"]["streamSources"][0]["channelUsername"], "empty_model")

    async def test_updates_profile_with_multiple_stream_sources(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "Multi Source",
                "streamSources": [
                    {
                        "sourceType": "chaturbate",
                        "channelUsername": "empty_one",
                        "channelUrl": "https://chaturbate.com/empty_one/",
                        "recordQuality": "1080p",
                        "retentionDays": 7,
                        "autoRecord": True,
                    },
                    {
                        "sourceType": "chaturbate",
                        "channelUsername": "empty_two",
                        "recordQuality": "720p",
                        "retentionDays": 0,
                        "autoRecord": True,
                    },
                    {
                        "sourceType": "twitch",
                        "channelUsername": "empty_twitch",
                        "channelUrl": "https://www.twitch.tv/empty_twitch",
                        "recordQuality": "best",
                        "retentionDays": 30,
                        "autoRecord": False,
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(len(profile["streamSources"]), 3)
        sources = {item["channelUsername"]: item for item in profile["streamSources"]}
        self.assertEqual(sources["empty_one"]["recordPath"], "empty_model/videos/record")
        self.assertEqual(sources["empty_two"]["retentionDays"], 0)

        self.assertIsNotNone(await app_main.db.get_model("empty_one", source_type="chaturbate"))
        self.assertIsNotNone(await app_main.db.get_model("empty_two", source_type="chaturbate"))
        self.assertIsNotNone(await app_main.db.get_model("empty_twitch", source_type="twitch"))

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertEqual(len(profiles["empty_model"]["streamSources"]), 3)

    async def test_links_live_to_existing_media_profile(self):
        create = self.client.put(
            "/api/media-profiles/empty_model",
            json={"displayName": "Existing Profile", "streamSources": []},
        )
        self.assertEqual(create.status_code, 200)

        response = self.client.post(
            "/api/media-profiles/link-live",
            json={
                "profileUsername": "empty_model",
                "liveUsername": "channel_one",
                "sourceType": "chaturbate",
                "channelUrl": "https://chaturbate.com/channel_one/",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["source"]["channelUsername"], "channel_one")
        self.assertFalse(data["source"]["autoRecord"])
        self.assertEqual(data["profile"]["streamSources"][0]["channelUsername"], "channel_one")
        self.assertIn("https://chaturbate.com/channel_one/", data["profile"]["streamUrls"])
        model = await app_main.db.get_model("channel_one", source_type="chaturbate")
        self.assertIsNotNone(model)
        self.assertFalse(model["auto_record"])

    async def test_profile_recording_requires_explicit_enable_and_can_be_paused(self):
        create = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "Controlled Recording",
                "streamSources": [{
                    "sourceType": "chaturbate",
                    "channelUsername": "controlled_live",
                    "autoRecord": False,
                }],
            },
        )
        self.assertEqual(create.status_code, 200)
        self.assertFalse(create.json()["profile"]["autoRecord"])

        enabled = self.client.patch(
            "/api/media-profiles/empty_model/auto-record",
            json={"autoRecord": True},
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["autoRecord"])
        source = (await app_main.db.get_media_profile_sources("empty_model"))[0]
        model = await app_main.db.get_model("controlled_live", source_type="chaturbate")
        self.assertTrue(source["auto_record"])
        self.assertTrue(model["auto_record"])

        paused = self.client.patch(
            "/api/media-profiles/empty_model/auto-record",
            json={"autoRecord": False},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(paused.json()["autoRecord"])
        source = (await app_main.db.get_media_profile_sources("empty_model"))[0]
        model = await app_main.db.get_model("controlled_live", source_type="chaturbate")
        self.assertFalse(source["auto_record"])
        self.assertFalse(model["auto_record"])
        # Toggle payload may include a base profile, but live avatar fields stay
        # optional so the Media UI can keep the already-enriched card state.
        profile_payload = paused.json()["profile"]
        self.assertIn("autoRecord", profile_payload)
        self.assertIn("streamSources", profile_payload)

    async def test_links_live_and_creates_media_profile(self):
        response = self.client.post(
            "/api/media-profiles/link-live",
            json={
                "createProfile": True,
                "profileUsername": "brand_new",
                "displayName": "Brand New",
                "liveUsername": "brand_new_live",
                "sourceType": "twitch",
            },
        )
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["username"], "brand_new")
        self.assertEqual(profile["displayName"], "Brand New")
        self.assertEqual(profile["streamSources"][0]["sourceType"], "twitch")
        self.assertEqual(profile["streamSources"][0]["channelUsername"], "brand_new_live")
        self.assertEqual(profile["streamSources"][0]["recordPath"], "brand_new/videos/record")

    async def test_profile_source_prefers_provider_from_channel_url_over_default_chaturbate(self):
        response = self.client.put(
            "/api/media-profiles/strip_profile",
            json={
                "displayName": "Strip Profile",
                "sourceType": "chaturbate",
                "streamSources": [
                    {
                        "sourceType": "chaturbate",
                        "channelUrl": "https://www.twitch.tv/aaa/",
                        "recordQuality": "best",
                        "retentionDays": 30,
                        "autoRecord": False,
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        source = response.json()["profile"]["streamSources"][0]
        self.assertEqual("twitch", source["sourceType"])
        self.assertEqual("aaa", source["channelUsername"])
        self.assertEqual("https://www.twitch.tv/aaa/", source["channelUrl"])
        self.assertIsNotNone(await app_main.db.get_model("aaa", source_type="twitch"))
        self.assertIsNone(await app_main.db.get_model("aaa", source_type="chaturbate"))

    def test_media_live_profile_image_skips_offline_stripchat_snapshot(self):
        offline_item = {
            "profile_image_url": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
            "thumbnail": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
        }
        self.assertEqual(
            "",
            app_main._media_live_profile_image_url(
                offline_item, source_type="stripchat", is_online=False
            ),
        )
        offline_with_avatar = {
            "profile_image_url": "https://static-proxy.strpst.com/avatars/xxx-full",
            "thumbnail": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
        }
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/xxx-full",
            app_main._media_live_profile_image_url(
                offline_with_avatar, source_type="stripchat", is_online=False
            ),
        )
        online_item = {
            "thumbnail": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
        }
        # Live snapshots are covers — empty so UI can show a letter avatar.
        self.assertEqual(
            "",
            app_main._media_live_profile_image_url(
                online_item, source_type="stripchat", is_online=True
            ),
        )
        snapshot_as_avatar = {
            "profile_image_url": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
            "thumbnail": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
        }
        self.assertEqual(
            "",
            app_main._media_live_profile_image_url(
                snapshot_as_avatar, source_type="stripchat", is_online=True
            ),
        )

    def test_media_live_profile_image_prefers_chaturbate_summary_over_riw(self):
        item = {
            "profile_image_url": "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            "thumbnail": "https://thumb.live.mmcdn.com/riw/anita.jpg",
        }
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            app_main._media_live_profile_image_url(
                item, source_type="chaturbate", is_online=True
            ),
        )
        riw_only = {
            "profile_image_url": "https://thumb.live.mmcdn.com/riw/anita.jpg",
            "thumbnail": "https://thumb.live.mmcdn.com/riw/anita.jpg",
        }
        # Webcam covers only — empty so Watch/Media can show a letter avatar.
        self.assertEqual(
            "",
            app_main._media_live_profile_image_url(
                riw_only, source_type="chaturbate", is_online=True
            ),
        )

    async def test_live_meta_keeps_stored_avatar_over_offline_stripchat_snapshot(self):
        await app_main.db.upsert_media_profile(
            "xxxnba",
            {
                "display_name": "xxxnba",
                "profile_image_url": "https://static-proxy.strpst.com/avatars/xxx-full",
            },
        )
        await app_main.db.upsert_media_profile_source(
            profile_username="xxxnba",
            source_type="stripchat",
            channel_username="xxxnba",
            channel_url="https://stripchat.com/xxxnba",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="xxxnba/videos/record",
        )
        (self.output_dir / "records" / "xxxnba").mkdir(parents=True, exist_ok=True)

        async def fake_live_meta(source_type, username):
            if source_type == "stripchat" and username == "xxxnba":
                return {
                    "isOnline": False,
                    "viewers": 0,
                    "profileImageUrl": "https://img.doppiocdn.net/snapshot/89673378/1785598410",
                    "channelUrl": "https://stripchat.com/xxxnba",
                }
            return {}

        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            response = self.client.get("/api/media-library?live=true&metadata=lazy&limit=50")

        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "xxxnba"
        )
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/xxx-full",
            profile["profileImageUrl"],
        )
        self.assertFalse(profile["isOnline"])

    async def test_live_meta_keeps_stored_chaturbate_summary_over_riw(self):
        await app_main.db.upsert_media_profile(
            "ameliabiers",
            {
                "display_name": "ameliabiers",
                "profile_image_url": "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            },
        )
        await app_main.db.upsert_media_profile_source(
            profile_username="ameliabiers",
            source_type="chaturbate",
            channel_username="ameliabiers",
            channel_url="https://chaturbate.com/ameliabiers/",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="ameliabiers/videos/record",
        )
        (self.output_dir / "records" / "ameliabiers").mkdir(parents=True, exist_ok=True)

        async def fake_live_meta(source_type, username):
            if source_type == "chaturbate" and username == "ameliabiers":
                return {
                    "isOnline": True,
                    "viewers": 40,
                    "profileImageUrl": "https://thumb.live.mmcdn.com/riw/ameliabiers.jpg",
                    "channelUrl": "https://chaturbate.com/ameliabiers/",
                }
            return {}

        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            response = self.client.get("/api/media-library?live=true&metadata=lazy&limit=50")

        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "ameliabiers"
        )
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/face.jpg",
            profile["profileImageUrl"],
        )
        self.assertTrue(profile["isOnline"])

    async def test_media_live_meta_uses_chaturbate_resolve_watch_meta(self):
        (self.output_dir / "records" / "cutieeeeva").mkdir(parents=True, exist_ok=True)
        await app_main.db.upsert_media_profile("cutieeeeva", {"display_name": "cutieeeeva"})
        await app_main.db.upsert_media_profile_source(
            profile_username="cutieeeeva",
            source_type="chaturbate",
            channel_username="cutieeeeva",
            channel_url="https://chaturbate.com/cutieeeeva/",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="cutieeeeva/videos/record",
        )

        class FakeProvider:
            source_type = "chaturbate"

            async def resolve_watch_meta(self, username):
                return {
                    "isOnline": False,
                    "viewers": 0,
                    "followers": 10,
                    "channelUrl": f"https://chaturbate.com/{username}/",
                    "profileImageUrl": "https://s3pv.highwebmedia.com/uploads/photos/cutie.jpg",
                    "displayName": username,
                    "username": username,
                    "roomStatus": "offline",
                }

        app_main._media_profile_live_cache.clear()
        with patch.object(app_main, "_provider_for", return_value=FakeProvider()):
            meta = await app_main._media_profile_live_card_meta("chaturbate", "cutieeeeva")
        self.assertEqual(
            "https://s3pv.highwebmedia.com/uploads/photos/cutie.jpg",
            meta["profileImageUrl"],
        )
        self.assertFalse(meta["isOnline"])

    async def test_media_live_meta_uses_stripchat_resolve_watch_meta(self):
        (self.output_dir / "records" / "Miu1_girl").mkdir(parents=True, exist_ok=True)
        await app_main.db.upsert_media_profile("Miu1_girl", {"display_name": "Miu1_girl"})
        await app_main.db.upsert_media_profile_source(
            profile_username="Miu1_girl",
            source_type="stripchat",
            channel_username="Miu1_girl",
            channel_url="https://stripchat.com/Miu1_girl",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="Miu1_girl/videos/record",
        )

        class FakeProvider:
            source_type = "stripchat"

            async def resolve_watch_meta(self, username):
                return {
                    "isOnline": True,
                    "viewers": 688,
                    "followers": None,
                    "channelUrl": f"https://stripchat.com/{username}",
                    "profileImageUrl": "https://static-proxy.strpst.com/avatars/miu-full",
                    "displayName": username,
                    "username": username,
                    "roomStatus": "public",
                }

        app_main._media_profile_live_cache.clear()
        with patch.object(app_main, "_provider_for", return_value=FakeProvider()):
            meta = await app_main._media_profile_live_card_meta("stripchat", "Miu1_girl")
        self.assertTrue(meta["isOnline"])
        self.assertEqual(688, meta["viewers"])
        self.assertEqual(
            "https://static-proxy.strpst.com/avatars/miu-full",
            meta["profileImageUrl"],
        )

    async def test_media_live_meta_force_refresh_bypasses_memory_cache(self):
        class FakeProvider:
            source_type = "chaturbate"

            def __init__(self):
                self.calls = 0

            async def resolve_watch_meta(self, username):
                self.calls += 1
                return {
                    "isOnline": self.calls > 1,
                    "viewers": 25 if self.calls > 1 else 0,
                    "username": username,
                    "roomStatus": "public" if self.calls > 1 else "offline",
                }

        provider = FakeProvider()
        app_main._media_profile_live_cache.clear()
        with patch.object(app_main, "_provider_for", return_value=provider):
            first = await app_main._media_profile_live_card_meta("chaturbate", "fresh_model")
            cached = await app_main._media_profile_live_card_meta("chaturbate", "fresh_model")
            refreshed = await app_main._media_profile_live_card_meta(
                "chaturbate", "fresh_model", force_refresh=True
            )

        self.assertFalse(first["isOnline"])
        self.assertFalse(cached["isOnline"])
        self.assertTrue(refreshed["isOnline"])
        self.assertEqual(2, provider.calls)

    async def test_live_meta_promotes_bilibili_uname_over_numeric_room_id(self):
        room_id = "1883358196"
        (self.output_dir / "records" / room_id).mkdir(parents=True, exist_ok=True)
        await app_main.db.upsert_media_profile(room_id, {"display_name": room_id})
        await app_main.db.upsert_media_profile_source(
            profile_username=room_id,
            source_type="bilibili",
            channel_username=room_id,
            channel_url=f"https://live.bilibili.com/{room_id}",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path=f"{room_id}/videos/record",
        )
        await app_main.db.add_or_update_model(
            username=room_id,
            display_name=room_id,
            source_type="bilibili",
        )

        async def fake_live_meta(source_type, username):
            if source_type == "bilibili" and username == room_id:
                return {
                    "isOnline": True,
                    "viewers": 100,
                    "displayName": "CS-advent",
                    "channelUrl": f"https://live.bilibili.com/{room_id}",
                }
            return {}

        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            response = self.client.get("/api/media-library?live=true&metadata=lazy&limit=50")

        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == room_id
        )
        self.assertEqual("bilibili", profile["sourceType"])
        self.assertEqual("CS-advent", profile["displayName"])
        self.assertEqual(room_id, profile["username"])
        stored = await app_main.db.get_media_profile(room_id)
        self.assertEqual("CS-advent", (stored or {}).get("display_name"))

    async def test_live_meta_exposes_stripchat_private_room_status(self):
        await app_main.db.upsert_media_profile("Miu1_girl", {"display_name": "Miu1_girl"})
        await app_main.db.upsert_media_profile_source(
            profile_username="Miu1_girl",
            source_type="stripchat",
            channel_username="Miu1_girl",
            channel_url="https://stripchat.com/Miu1_girl",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="Miu1_girl/videos/record",
        )
        (self.output_dir / "records" / "Miu1_girl").mkdir(parents=True, exist_ok=True)

        async def fake_live_meta(source_type, username):
            if source_type == "stripchat" and username == "Miu1_girl":
                return {
                    "isOnline": True,
                    "viewers": 0,
                    "roomStatus": "private",
                    "channelUrl": "https://stripchat.com/Miu1_girl",
                    "profileImageUrl": "https://static-proxy.strpst.com/avatars/miu-full",
                }
            return {}

        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            response = self.client.get("/api/media-library?live=true&metadata=lazy&limit=50")

        self.assertEqual(response.status_code, 200)
        profile = next(
            item for item in response.json()["profiles"] if item["username"] == "Miu1_girl"
        )
        self.assertTrue(profile["isOnline"])
        self.assertEqual(0, profile["viewers"])
        self.assertEqual("private", profile["roomStatus"])

    async def test_live_meta_does_not_shadow_username_filter_and_hide_videos(self):
        """live=true must not overwrite the username query param via loop locals."""
        other_dir = self.output_dir / "records" / "other_model"
        other_dir.mkdir(parents=True)
        other_video = other_dir / "other.mp4"
        other_video.write_bytes(b"other-video")
        old = time.time() - 60
        os.utime(other_video, (old, old))

        async def fake_live_meta(source_type, username):
            # Return displayName for every profile so the buggy loop would keep
            # reassigning the request-scoped `username` local on each iteration.
            return {
                "isOnline": False,
                "viewers": 0,
                "displayName": f"{username}-live",
                "channelUrl": f"https://chaturbate.com/{username}",
            }

        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            response = self.client.get(
                "/api/media-library?kind=video&metadata=lazy&live=true&limit=1000"
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        filenames = sorted(item["filename"] for item in data["items"])
        self.assertEqual(filenames, ["clip.mp4", "other.mp4"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["libraryStats"]["videos"], 2)

        # Explicit username filter must still work under live=true.
        with patch.object(app_main, "_media_profile_live_card_meta", side_effect=fake_live_meta):
            filtered = self.client.get(
                "/api/media-library?kind=video&metadata=lazy&live=true&username=model&limit=1000"
            )
        self.assertEqual(filtered.status_code, 200)
        filtered_names = [item["filename"] for item in filtered.json()["items"]]
        self.assertEqual(filtered_names, ["clip.mp4"])

    async def test_stripchat_source_rebuilds_mismatched_chaturbate_channel_url(self):
        response = self.client.put(
            "/api/media-profiles/Miu1_girl",
            json={
                "displayName": "Miu1_girl",
                "streamSources": [
                    {
                        "sourceType": "stripchat",
                        "channelUsername": "Miu1_girl",
                        "channelUrl": "https://chaturbate.com/Miu1_girl/",
                        "recordQuality": "best",
                        "retentionDays": 30,
                        "autoRecord": False,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        source = response.json()["profile"]["streamSources"][0]
        self.assertEqual("stripchat", source["sourceType"])
        self.assertEqual("https://stripchat.com/Miu1_girl", source["channelUrl"])

        # Stale DB rows (source_type stripchat + chaturbate host) must self-heal on read.
        await app_main.db.upsert_media_profile_source(
            profile_username="Miu1_girl",
            source_type="stripchat",
            channel_username="Miu1_girl",
            channel_url="https://chaturbate.com/Miu1_girl/",
            auto_record=False,
            record_quality="best",
            retention_days=30,
            record_path="Miu1_girl/videos/record",
        )
        healed = app_main._profile_source_response(
            (await app_main.db.get_media_profile_sources("Miu1_girl"))[0]
        )
        self.assertEqual("https://stripchat.com/Miu1_girl", healed["channelUrl"])

    async def test_resolves_and_serves_dedicated_profile_image(self):
        async def fake_download(username, image_url):
            self.assertEqual(username, "empty_model")
            self.assertEqual(image_url, "https://images.example/empty.jpg")
            app_main.PROFILE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            image_path = app_main.PROFILE_IMAGES_DIR / "empty_model.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0profile")
            return {
                "path": str(image_path),
                "size": image_path.stat().st_size,
                "contentType": "image/jpeg",
            }

        with (
            patch.object(
                app_main,
                "_resolve_profile_image_from_babepedia",
                new=AsyncMock(return_value={
                    "imageUrl": "https://images.example/empty.jpg",
                    "sourceUrl": "https://www.babepedia.com/babe/Empty_Model",
                }),
            ),
            patch.object(app_main, "_download_profile_image", new=AsyncMock(side_effect=fake_download)),
        ):
            response = self.client.post(
                "/api/media-profiles/empty_model/profile-image/resolve",
                json={"query": "Empty Model"},
            )

        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertTrue(profile["profileImageUrl"].startswith("/api/media-profiles/empty_model/profile-image?v="))
        self.assertEqual(profile["profileImageSourceUrl"], "https://www.babepedia.com/babe/Empty_Model")

        image = self.client.get("/api/media-profiles/empty_model/profile-image")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, b"\xff\xd8\xff\xe0profile")
        self.assertTrue(image.headers["content-type"].startswith("image/jpeg"))

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertTrue(profiles["empty_model"]["profileImageUrl"].startswith("/api/media-profiles/empty_model/profile-image?v="))
        self.assertEqual(profiles["empty_model"]["profileImageSourceUrl"], "https://www.babepedia.com/babe/Empty_Model")

    async def test_profile_image_resolver_rejects_private_network_urls(self):
        for image_url in (
            "http://127.0.0.1:8080/secret.jpg",
            "http://[::1]/secret.jpg",
            "http://[::ffff:127.0.0.1]/secret.jpg",
        ):
            with self.subTest(image_url=image_url):
                response = self.client.post(
                    "/api/media-profiles/empty_model/profile-image/resolve",
                    json={"profileImageUrl": image_url},
                )
                self.assertEqual(response.status_code, 400)

        self.assertFalse(app_main.PROFILE_IMAGES_DIR.exists())

    async def test_profile_image_dns_resolver_rejects_private_answers(self):
        class FakeResolver:
            async def resolve(self, host, port, family):
                return [{"host": "192.168.40.59", "port": port}]

            async def close(self):
                return None

        resolver = app_main._PublicAddressResolver.__new__(
            app_main._PublicAddressResolver
        )
        resolver._resolver = FakeResolver()

        with self.assertRaisesRegex(OSError, "non-public"):
            await resolver.resolve("images.example.test", 443)

    async def test_deletes_profile_folder_metadata_and_recordings(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "To Delete",
                "recordQuality": "best",
                "retentionDays": 30,
                "autoRecord": False,
                "sourceType": "chaturbate",
            },
        )
        self.assertEqual(response.status_code, 200)

        delete = self.client.delete("/api/media-profiles/empty_model")
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(self.empty_dir.exists())
        self.assertIsNone(await app_main.db.get_media_profile("empty_model"))
        self.assertIsNone(await app_main.db.get_model("empty_model"))

        missing = self.client.get("/api/media-profiles/empty_model")
        self.assertEqual(missing.status_code, 404)

    async def test_repairs_media_profile_truncated_edge_underscores(self):
        profile_dir = self.output_dir / "records" / "_edgeuser_"
        profile_dir.mkdir(parents=True)
        await app_main.db.upsert_media_profile("edgeuser", {"display_name": "Edge User"})
        await app_main.db.add_or_update_model("_edgeuser_", source_type="chaturbate")

        response = self.client.get("/api/media-library")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await app_main.db.get_media_profile("edgeuser"))
        repaired = await app_main.db.get_media_profile("_edgeuser_")
        self.assertIsNotNone(repaired)
        self.assertEqual("Edge User", repaired["display_name"])
        profiles = {item["username"]: item for item in response.json()["profiles"]}
        self.assertIn("_edgeuser_", profiles)
        self.assertNotIn("edgeuser", profiles)


if __name__ == "__main__":
    unittest.main()

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.tasks.monitor import _thumbnail_looks_empty, generate_recording_thumbnail


class RecordingThumbnailTests(unittest.IsolatedAsyncioTestCase):
    def test_thumbnail_looks_empty_for_tiny_or_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.jpg"
            tiny = root / "tiny.jpg"
            ok = root / "ok.jpg"
            tiny.write_bytes(b"x" * 100)
            ok.write_bytes(b"x" * 4000)
            self.assertTrue(_thumbnail_looks_empty(missing))
            self.assertTrue(_thumbnail_looks_empty(tiny))
            self.assertFalse(_thumbnail_looks_empty(ok))

    async def test_generate_recording_thumbnail_picks_largest_non_empty_seek(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.ts"
            source.write_bytes(b"ts")
            sizes = {
                "00:01:00": 800,
                "00:00:30": 1200,
                "00:00:10": 5000,
                "00:00:03": 900,
                "00:00:00": 700,
            }

            async def fake_exec(*args, **kwargs):
                out = Path(args[-1])
                seek = None
                argv = list(args)
                if "-ss" in argv:
                    seek = argv[argv.index("-ss") + 1]
                out.write_bytes(b"j" * sizes.get(seek, 100))

                class Proc:
                    returncode = 0

                    def kill(self):
                        return None

                return Proc()

            with (
                patch("app.tasks.monitor.asyncio.create_subprocess_exec", side_effect=fake_exec),
                patch("app.tasks.monitor.wait_with_timeout", new=AsyncMock(return_value=None)),
            ):
                result = await generate_recording_thumbnail(
                    source,
                    root,
                    "21728563",
                    ffmpeg_path="ffmpeg",
                )

            self.assertEqual(result, str(root / "thumbnails" / "21728563" / "clip.jpg"))
            self.assertEqual((root / "thumbnails" / "21728563" / "clip.jpg").stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main()

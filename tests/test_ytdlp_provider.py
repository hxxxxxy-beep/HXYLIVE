import unittest

from app.providers.ytdlp import YtDlpProvider
from app.providers.base import ProviderOfflineError


def _provider() -> YtDlpProvider:
    return YtDlpProvider(
        "bilibili",
        "Bilibili",
        "https://live.bilibili.com/{username}",
        ("live.bilibili.com",),
    )


class YtDlpSelectMediaUrlTests(unittest.TestCase):
    def test_skips_top_level_flv_and_picks_hls(self):
        info = {
            "url": "https://cdn.example/live/best.flv?qn=400",
            "protocol": "https",
            "ext": "flv",
            "vcodec": "avc",
            "height": 1080,
            "tbr": 4000,
            "formats": [
                {
                    "url": "https://cdn.example/live/hevc.flv",
                    "protocol": "https",
                    "vcodec": "hevc",
                    "height": 1080,
                    "tbr": 3000,
                },
                {
                    "url": "https://cdn.example/live/avc/index.m3u8",
                    "protocol": "m3u8_native",
                    "vcodec": "avc",
                    "height": 720,
                    "tbr": 2500,
                },
                {
                    "url": "https://cdn.example/live/hevc/index.m3u8",
                    "protocol": "m3u8_native",
                    "vcodec": "hevc",
                    "height": 1080,
                    "tbr": 2800,
                },
            ],
        }
        url, _headers = _provider()._select_media_url(info, None)
        self.assertEqual(url, "https://cdn.example/live/avc/index.m3u8")

    def test_prefers_avc_hls_when_heights_missing(self):
        # Large Bilibili rooms often omit height/tbr on every format.
        info = {
            "url": "https://cdn.example/live/best.flv",
            "protocol": "https",
            "ext": "flv",
            "vcodec": "avc",
            "formats": [
                {
                    "url": "https://cdn.example/a.m3u8",
                    "protocol": "m3u8_native",
                    "vcodec": "avc",
                    "format_id": "hls-avc",
                },
                {
                    "url": "https://cdn.example/h.m3u8",
                    "protocol": "m3u8_native",
                    "vcodec": "hevc",
                    "format_id": "hls-hevc",
                },
            ],
        }
        url, _headers = _provider()._select_media_url(info, None)
        self.assertEqual(url, "https://cdn.example/a.m3u8")

    def test_raises_when_only_flv_available(self):
        info = {
            "url": "https://cdn.example/live/best.flv",
            "protocol": "https",
            "formats": [
                {"url": "https://cdn.example/live/a.flv", "protocol": "https"},
            ],
        }
        with self.assertRaises(ProviderOfflineError):
            _provider()._select_media_url(info, None)


if __name__ == "__main__":
    unittest.main()

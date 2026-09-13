import unittest

from app.recording_names import (
    format_identity_with_source,
    normalize_source_marker,
    parse_identity_with_source,
    recording_base_name,
)


class RecordingIdentityMarkerTests(unittest.TestCase):
    def test_format_and_parse_round_trip(self):
        stamped = format_identity_with_source("Nancy-A1", "stripchat")
        self.assertEqual("Nancy-A1(stripchat)", stamped)
        name, source = parse_identity_with_source(stamped)
        self.assertEqual("Nancy-A1", name)
        self.assertEqual("stripchat", source)

    def test_parse_folder_and_filename_stems(self):
        self.assertEqual(
            ("model", "twitch"),
            parse_identity_with_source("model(twitch)/2026-08-02 21-21-42.mp4"),
        )
        self.assertEqual(
            ("model", "chaturbate"),
            parse_identity_with_source("model(chaturbate)_20260527-123456"),
        )
        self.assertEqual(
            ("model", None),
            parse_identity_with_source("model"),
        )

    def test_recording_base_name_stamps_known_source(self):
        self.assertEqual(
            "model(stripchat)_20260527-123456",
            recording_base_name(
                "model",
                "20260527_123456",
                "abcdef1234",
                "username_timestamp",
                source_type="stripchat",
            ),
        )
        self.assertEqual(
            "20260527_123456_abcdef(youtube)",
            recording_base_name(
                "model",
                "20260527_123456",
                "abcdef1234",
                "timestamp",
                source_type="youtube",
            ),
        )

    def test_recording_base_name_omits_marker_without_source(self):
        self.assertEqual(
            "model_20260527-123456",
            recording_base_name(
                "model",
                "20260527_123456",
                "abcdef1234",
                "username_timestamp",
            ),
        )

    def test_normalize_source_marker_aliases(self):
        self.assertEqual("youtube", normalize_source_marker("yt"))
        self.assertEqual("chaturbate", normalize_source_marker("CB"))
        self.assertIsNone(normalize_source_marker("cam4"))

    def test_safe_filename_keeps_unicode_display_names(self):
        from app.recording_names import safe_filename_part

        self.assertEqual("示例主播", safe_filename_part("示例主播"))
        stamped = format_identity_with_source("示例主播", "bilibili")
        self.assertEqual("示例主播(bilibili)", stamped)
        name, source = parse_identity_with_source(stamped)
        self.assertEqual("示例主播", name)
        self.assertEqual("bilibili", source)


if __name__ == "__main__":
    unittest.main()

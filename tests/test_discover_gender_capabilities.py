import unittest

from app.discover_gender_capabilities import (
    all_unsupported_combos,
    is_gender_supported,
    unsupported_reason,
    uses_finite_local_filter,
)


class DiscoverGenderCapabilitiesTests(unittest.TestCase):
    def test_twitch_gender_filters_are_unsupported(self):
        combos = all_unsupported_combos()
        self.assertEqual(4, len(combos))
        self.assertEqual(
            {("twitch", "female"), ("twitch", "male"), ("twitch", "trans"), ("twitch", "couple")},
            {(s, g) for s, g, _ in combos},
        )

    def test_all_is_supported_for_twitch_and_chaturbate(self):
        for source in ("twitch", "chaturbate"):
            self.assertTrue(is_gender_supported(source, None))
            self.assertTrue(is_gender_supported(source, ""))
            self.assertTrue(is_gender_supported(source, "all"))

    def test_chaturbate_gender_filters_are_supported(self):
        for gender in ("female", "male", "trans", "couple"):
            self.assertTrue(is_gender_supported("chaturbate", gender))
            self.assertIsNone(unsupported_reason("chaturbate", gender))

    def test_twitch_gender_filters_are_unsupported(self):
        for gender in ("female", "male", "trans", "couple"):
            self.assertFalse(is_gender_supported("twitch", gender))
            self.assertIsNotNone(unsupported_reason("twitch", gender))

    def test_finite_local_filter_sources_is_empty(self):
        self.assertFalse(uses_finite_local_filter("livejasmin"))
        self.assertFalse(uses_finite_local_filter("chaturbate"))
        self.assertFalse(uses_finite_local_filter("twitch"))


if __name__ == "__main__":
    unittest.main()

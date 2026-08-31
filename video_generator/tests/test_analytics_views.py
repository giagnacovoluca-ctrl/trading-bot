import unittest

from analizza_risultati import parse_view_count


class AnalyticsViewParsingTest(unittest.TestCase):
    def test_parses_compact_tiktok_counts(self):
        self.assertEqual(parse_view_count("1.2K"), 1200)
        self.assertEqual(parse_view_count("3,5M"), 3_500_000)
        self.assertEqual(parse_view_count("842"), 842)


if __name__ == "__main__":
    unittest.main()

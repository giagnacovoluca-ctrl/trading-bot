import unittest

from agente_tiktok import parse_quality_result


class QualityParserTest(unittest.TestCase):
    def test_parses_score_and_diagnosis(self):
        score, report = parse_quality_result("SCORE: 8/10\nMOTIVAZIONE: Hook forte\nPROBLEMI: - nessuno")
        self.assertEqual(score, 8)
        self.assertIn("Hook forte", report)

    def test_clamps_invalid_score_and_uses_safe_default(self):
        self.assertEqual(parse_quality_result("SCORE: 18\nMOTIVAZIONE: no")[0], 10)
        self.assertEqual(parse_quality_result("risposta senza score")[0], 5)


if __name__ == "__main__": unittest.main()

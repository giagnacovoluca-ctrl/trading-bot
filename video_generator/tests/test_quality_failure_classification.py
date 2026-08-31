import unittest

from agente_tiktok import parse_quality_result


class QualityFailureClassificationTest(unittest.TestCase):
    def test_missing_score_gets_safe_default_for_manual_review(self):
        score, report = parse_quality_result("Validazione fallita: servizio temporaneamente non disponibile")
        self.assertEqual(score, 5)
        self.assertEqual(report, "")


if __name__ == "__main__": unittest.main()

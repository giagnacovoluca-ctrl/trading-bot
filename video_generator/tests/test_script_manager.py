import unittest

from modules.script_manager import parse_script


class ScriptManagerTest(unittest.TestCase):
    def test_extracts_only_spoken_text_from_agy_format(self):
        parsed = parse_script(
            """TITOLO: Un titolo
TESTO:
ATTO 1 — HOOK (15-20 parole): La luce cambia il sonno.
ATTO 2 — CONTESTO (20 parole): Questo conta ogni sera.
FONTE_NOTIZIA: Nature
EBOOK_FILE: esempio.docx
"""
        )
        self.assertIn("La luce cambia il sonno", parsed.full_text)
        self.assertIn("Questo conta ogni sera", parsed.full_text)
        self.assertNotIn("TITOLO", parsed.full_text)
        self.assertNotIn("FONTE_NOTIZIA", parsed.full_text)
        self.assertNotIn("ATTO 1", parsed.full_text)

    def test_keeps_plain_legacy_script(self):
        parsed = parse_script("Prima frase.\nSeconda frase.")
        self.assertEqual(parsed.full_text, "Prima frase.\nSeconda frase.")


if __name__ == "__main__":
    unittest.main()

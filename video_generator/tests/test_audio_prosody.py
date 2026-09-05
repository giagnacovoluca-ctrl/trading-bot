import unittest

from modules.audio_generator import sanitize_tts_segment


class AudioProsodyTest(unittest.TestCase):
    def test_removes_spoken_punctuation_but_keeps_words(self):
        cleaned = sanitize_tts_segment("Perché succede? È importante: osserva, poi scegli.")
        self.assertNotIn("?", cleaned)
        self.assertNotIn(".", cleaned)
        self.assertNotIn(":", cleaned)
        self.assertIn("Perché succede", cleaned)
        self.assertNotIn("punto", cleaned.lower())


if __name__ == "__main__":
    unittest.main()

import unittest

from agente_cosciamente import valida_contenuto


class ConsciaMenteContentTest(unittest.TestCase):
    def valid_slides(self):
        return [
            {"slide_number": index, "overlay_text": f"Testo breve numero {index}"}
            for index in range(1, 6)
        ]

    def test_normalizes_valid_content(self):
        result = valida_contenuto(
            {"slides": self.valid_slides(), "caption": "Una caption valida"}
        )
        self.assertEqual(len(result["slides"]), 5)
        self.assertEqual(result["slides"][0]["slide_number"], 1)

    def test_rejects_too_few_slides(self):
        with self.assertRaisesRegex(ValueError, "da 5 a 6"):
            valida_contenuto({"slides": self.valid_slides()[:4], "caption": "ok"})

    def test_rejects_excessive_overlay_text(self):
        slides = self.valid_slides()
        slides[2]["overlay_text"] = "parola " * 21
        with self.assertRaisesRegex(ValueError, "20 parole"):
            valida_contenuto({"slides": slides, "caption": "ok"})


if __name__ == "__main__":
    unittest.main()

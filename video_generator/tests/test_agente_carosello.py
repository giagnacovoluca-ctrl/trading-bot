import unittest

from agente_carosello import valida_output


class CarouselValidationTest(unittest.TestCase):
    def slides(self):
        return [
            {"slide_number": index, "overlay_text": f"Testo breve {index}"}
            for index in range(1, 6)
        ]

    def test_accepts_carousel(self):
        result = valida_output(
            {"slides": self.slides(), "caption": "Caption valida"}, "carosello"
        )
        self.assertEqual(len(result["slides"]), 5)

    def test_rejects_long_slide(self):
        slides = self.slides()
        slides[0]["overlay_text"] = "parola " * 21
        with self.assertRaisesRegex(ValueError, "overlay_text"):
            valida_output({"slides": slides, "caption": "ok"}, "carosello")

    def test_accepts_aesthetic(self):
        result = valida_output(
            {"testo_schermo": "Una frase breve", "caption": "Caption"}, "aesthetic"
        )
        self.assertEqual(result["testo_schermo"], "Una frase breve")


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import agente_carosello
from agente_carosello import AESTHETIC_CTA_PREVIEW, genera_contenuto, valida_output


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

    def test_promo_carousel_requires_exact_cta_in_slide_and_caption(self):
        cta = "Leggi l'anteprima gratuita dal link in bio."
        slides = [
            {"slide_number": index, "overlay_text": f"Concetto utile numero {index}"}
            for index in range(1, 5)
        ]
        slides.append({"slide_number": 5, "overlay_text": cta})
        result = valida_output(
            {"slides": slides, "caption": f"Approfondisci il tema. {cta}"},
            "carosello",
            cta,
        )
        self.assertEqual(result["slides"][-1]["overlay_text"], cta)

        with self.assertRaisesRegex(ValueError, "ultima slide"):
            valida_output(
                {"slides": [*slides[:-1], {"slide_number": 5, "overlay_text": "Vai al profilo"}], "caption": f"Approfondisci. {cta}"},
                "carosello",
                cta,
            )

    def test_rejects_long_slide(self):
        slides = self.slides()
        slides[0]["overlay_text"] = "parola " * 21
        with self.assertRaisesRegex(ValueError, "overlay_text"):
            valida_output({"slides": slides, "caption": "ok"}, "carosello")

    def test_accepts_aesthetic(self):
        caption = ("Osservare un pensiero crea spazio tra ciò che accade e la reazione automatica. " * 6)
        caption += AESTHETIC_CTA_PREVIEW
        result = valida_output(
            {
                "testo_schermo": "I pensieri perdono forza quando smetti di combatterli",
                "caption": caption,
            },
            "aesthetic",
            AESTHETIC_CTA_PREVIEW,
        )
        self.assertEqual(
            result["testo_schermo"],
            "I pensieri perdono forza quando smetti di combatterli",
        )

    def test_rejects_aesthetic_caption_that_is_too_short(self):
        with self.assertRaisesRegex(ValueError, "caption aesthetic"):
            valida_output(
                {
                    "testo_schermo": "I pensieri perdono forza quando smetti di combatterli",
                    "caption": f"Una caption breve. {AESTHETIC_CTA_PREVIEW}",
                },
                "aesthetic",
                AESTHETIC_CTA_PREVIEW,
            )

    def test_rejects_repetitive_elitist_aesthetic_copy(self):
        caption = ("Il vero lusso consiste nel dominare ogni risposta. " * 9) + AESTHETIC_CTA_PREVIEW
        with self.assertRaisesRegex(ValueError, "vecchio stile"):
            valida_output(
                {
                    "testo_schermo": "Ogni scelta rivela quanto domini davvero la tua mente",
                    "caption": caption,
                },
                "aesthetic",
                AESTHETIC_CTA_PREVIEW,
            )

    def test_aesthetic_prompt_uses_data_driven_constraints(self):
        with patch.object(
            agente_carosello, "scegli_ebook_aesthetic", return_value="Meditazione per Chiunque"
        ), patch.object(
            agente_carosello,
            "_recent_aesthetic_hooks",
            return_value=["Non devi spegnere i pensieri"],
        ):
            prompt, category, format_type, topic, expected_cta = genera_contenuto("aesthetic")

        self.assertEqual(category, "nervo_vago")
        self.assertEqual(format_type, "aesthetic")
        self.assertEqual(topic, "Meditazione per Chiunque")
        self.assertIn("7-12 parole", prompt)
        self.assertIn("450-900 caratteri", prompt)
        self.assertIn("Non devi spegnere i pensieri", prompt)
        self.assertEqual(expected_cta, AESTHETIC_CTA_PREVIEW)
        self.assertIn(AESTHETIC_CTA_PREVIEW, prompt)

    def test_recent_topic_is_penalized_during_selection(self):
        recent = [
            {
                "platform": "instagram",
                "mode": "aesthetic",
                "topic": "Meditazione per Chiunque",
            }
        ]
        with patch.object(
            agente_carosello, "_recent_aesthetic_entries", return_value=recent
        ), patch.object(
            agente_carosello.random,
            "choices",
            return_value=["Attiva il Nervo Vago"],
        ) as choices:
            selected = agente_carosello.scegli_ebook_aesthetic()

        weights = choices.call_args.kwargs["weights"]
        meditation_index = agente_carosello.EBOOKS.index("Meditazione per Chiunque")
        integrators_index = agente_carosello.EBOOKS.index("Integratori Naturali")
        self.assertEqual(selected, "Attiva il Nervo Vago")
        self.assertLess(weights[meditation_index], weights[integrators_index])


if __name__ == "__main__":
    unittest.main()

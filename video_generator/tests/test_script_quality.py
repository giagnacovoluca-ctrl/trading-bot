import unittest

from modules.script_quality import extract_metadata, validate_publication_text, validate_script


VALID_METADATA = {
    "FONTE_NOTIZIA": "Nature",
    "FATTO_CENTRALE": "La luce serale può influenzare il ritmo circadiano.",
    "TIPO_EVIDENZA": "revisione scientifica",
    "LIMITE_EVIDENZA": "Non dimostra che sia l'unica causa del sonno scarso.",
    "ANGOLO_NARRATIVO": "Un gesto serale sottovalutato",
}


class ScriptQualityTest(unittest.TestCase):
    def test_extracts_editorial_metadata(self):
        script = "\n".join(f"{key}: {value}" for key, value in VALID_METADATA.items())
        self.assertEqual(extract_metadata(script)["TIPO_EVIDENZA"], "revisione scientifica")

    def test_accepts_balanced_script(self):
        text = (
            "Il sonno non dipende solo dalle ore. "
            "La luce serale può ritardare il segnale che prepara il riposo. "
            "Una revisione scientifica osserva un'associazione, ma non dimostra che sia l'unica causa. "
            "Prova a ridurre la luminosità e osserva la tua risposta per una settimana. "
            "Salva il video per ricordarti l'esperimento. " * 3
        )
        report = validate_script("La luce e il sonno", text, VALID_METADATA)
        self.assertTrue(report.ok)

    def test_rejects_absolute_health_claim(self):
        text = "Questo metodo cura l'ansia e garantisce risultati. Commenta e prova subito. " * 8
        report = validate_script("Una prova pratica", text, VALID_METADATA)
        self.assertFalse(report.ok)
        self.assertIn("promessa sanitaria", " ".join(report.issues))

    def test_rejects_missing_evidence_contract(self):
        text = "Un fatto interessante da capire. Salva il video e leggilo con calma. " * 10
        report = validate_script("Un fatto interessante", text, {"FONTE_NOTIZIA": "Nature"})
        self.assertFalse(report.ok)
        self.assertTrue(any("metadato" in issue for issue in report.issues))

    def test_rejects_misinformation_style_absolutes(self):
        issues = validate_publication_text(
            "Questa è la prova definitiva del cambiamento climatico che non ti dicono."
        )
        self.assertTrue(any("assoluta" in issue for issue in issues))

    def test_rejects_unsourced_nuclear_bomb_analogy(self):
        issues = validate_publication_text(
            "Gli oceani assorbono l'energia di cinque esplosioni nucleari al secondo."
        )
        self.assertTrue(any("analogia quantitativa" in issue for issue in issues))

    def test_rejects_aggressive_contrarian_rhetoric(self):
        issues = validate_publication_text(
            "Ti hanno mentito: la matematica è rotta e tutti sbagliano."
        )
        self.assertTrue(any("contrariana aggressiva" in issue for issue in issues))

    def test_rejects_generic_scientific_source(self):
        metadata = dict(VALID_METADATA)
        metadata["FONTE_NOTIZIA"] = "Letteratura scientifica consolidata sulla termodinamica"
        text = (
            "Il clima si studia osservando molti segnali diversi. "
            "Le temperature atmosferiche cambiano in modo diverso con la quota. "
            "I dati vanno interpretati insieme e non come una singola dimostrazione. "
            "Condividi il video per discuterne con calma. " * 3
        )
        report = validate_script("Temperature e atmosfera", text, metadata)
        self.assertFalse(report.ok)
        self.assertTrue(any("fonte troppo generica" in issue for issue in report.issues))


if __name__ == "__main__":
    unittest.main()

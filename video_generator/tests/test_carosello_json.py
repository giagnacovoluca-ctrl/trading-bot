import unittest

from agente_carosello import estrai_json


class EstraiJsonTest(unittest.TestCase):
    def test_privilegia_oggetto_completo_rispetto_all_array_slides(self):
        risposta = '''ESITO: CORRETTO
JSON: {"slides": [{"overlay_text": "Test"}], "caption": "Caption"}'''

        self.assertEqual(
            estrai_json(risposta),
            '{"slides": [{"overlay_text": "Test"}], "caption": "Caption"}',
        )

    def test_estrae_oggetto_da_blocco_markdown(self):
        risposta = '''ESITO: APPROVATO
```json
{"slides": [], "caption": "Caption"}
```'''

        self.assertEqual(
            estrai_json(risposta),
            '{"slides": [], "caption": "Caption"}',
        )

    def test_mantiene_compatibilita_con_array(self):
        self.assertEqual(estrai_json('Risposta: [{"ok": true}]'), '[{"ok": true}]')


if __name__ == "__main__":
    unittest.main()

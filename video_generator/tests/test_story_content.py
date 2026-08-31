import unittest

from agente_story import validate_story_content


class StoryContentTest(unittest.TestCase):
    def test_accepts_useful_structured_story(self):
        story = validate_story_content({
            "titolo": "Perché perdi energia nel pomeriggio",
            "insight": "Una pausa breve e un bicchiere d'acqua possono aiutarti a osservare meglio stanchezza e concentrazione, senza trasformare ogni calo in un problema.",
            "azione": "Prima del prossimo caffè, fermati e controlla sete e respiro.",
        })
        self.assertIn("energia", story["titolo"])

    def test_rejects_empty_motivational_slogan(self):
        with self.assertRaises(ValueError):
            validate_story_content({"titolo": "Credi sempre in te", "insight": "Forza!", "azione": "Vai!"})

    def test_rejects_categorical_physiology(self):
        with self.assertRaises(ValueError):
            validate_story_content({
                "titolo": "Perché bere lentamente fa la differenza",
                "insight": "Bere tutto insieme non idrata efficacemente il corpo, mentre piccoli sorsi aiutano i tessuti ad assimilare meglio ogni liquido durante tutta la giornata.",
                "azione": "Metti una sveglia ogni ora e bevi esattamente due sorsi.",
            })


if __name__ == "__main__": unittest.main()

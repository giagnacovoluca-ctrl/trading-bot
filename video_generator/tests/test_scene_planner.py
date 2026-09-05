import unittest

from modules.scene_planner import create_scene_plan, scaled_scene_durations


class ScenePlannerTest(unittest.TestCase):
    def test_keeps_scene_order_and_classifies_question_without_tts_markup(self):
        plan = create_scene_plan(
            "Il sonno cambia davvero?",
            "sonno",
            "Perché la luce modifica la sera?\nIl cervello usa segnali ambientali.\nProva a osservare la tua routine.",
        )
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["intent"], "domanda_curiosa")
        self.assertEqual(plan[-1]["intent"], "cta_chiara")
        self.assertIn("luce", plan[0]["spoken_text"])

    def test_accepts_visual_plan_from_director(self):
        response = '[{"visual_prompt":"Italian bedroom at dusk, vertical, no text","pexels_query":"bedroom dusk","provider":"stock"}]'
        plan = create_scene_plan("Luce e sonno", "sonno", "La stanza diventa più buia.", lambda _: response)
        self.assertEqual(plan[0]["pexels_query"], "bedroom dusk")
        self.assertEqual(plan[0]["provider"], "stock")

    def test_scaled_durations_cover_full_audio(self):
        plan = [{"duration_weight": 1}, {"duration_weight": 3}]
        durations = scaled_scene_durations(plan, 20.0)
        self.assertEqual(durations, [5.0, 15.0])

    def test_long_act_is_split_into_short_visual_beats(self):
        text = (
            "Nel laboratorio, una ricercatrice osserva le cellule al microscopio, "
            "mentre il campione reagisce a una luce controllata e i dati cambiano sul monitor."
        )
        plan = create_scene_plan("Cellule e luce", "biologia", text)
        self.assertGreaterEqual(len(plan), 2)
        self.assertTrue(all(len(scene["spoken_text"].split()) <= 16 for scene in plan))
        self.assertIn("laboratorio", plan[0]["spoken_text"])


if __name__ == "__main__":
    unittest.main()

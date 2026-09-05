import datetime as dt
import unittest

from modules.content_tracking import append_tracking, choose_focus, create_campaign


class ContentTrackingTest(unittest.TestCase):
    def test_explicit_resource_overrides_script_keywords(self):
        campaign = create_campaign("instagram", "meditazione meditare", resource_id="acqua")
        self.assertEqual(campaign["focus"], "acqua")
        with self.assertRaises(ValueError):
            create_campaign("instagram", "test", resource_id="../admin")

    def test_focus_uses_relevant_landing(self):
        self.assertEqual(choose_focus("Tre esercizi per il nervo vago e lo stress"), "nervo-vago")
        self.assertEqual(choose_focus("Idratazione e acqua durante la giornata"), "acqua")
        self.assertEqual(choose_focus("Come valutare gli integratori"), "integratori")
        self.assertEqual(choose_focus("Calcola la tua numerologia di coppia"), "identita")
        self.assertEqual(choose_focus("Una curiosità sullo spazio"), "risorse")

    def test_campaign_is_short_stable_shape(self):
        campaign = create_campaign("tiktok", "nervo vago", "promo", dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertRegex(campaign["campaign_id"], r"^nervo-vago-tt-[a-f0-9]{8}$")
        self.assertTrue(campaign["tracking_url"].endswith(campaign["campaign_id"]))

    def test_caption_does_not_expose_non_clickable_url(self):
        campaign = create_campaign("instagram", "acqua", now=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        caption = append_tracking("Caption con link in bio", campaign)
        self.assertEqual(caption, "Caption con link in bio")
        self.assertNotIn("vercel.app", caption)


if __name__ == "__main__":
    unittest.main()

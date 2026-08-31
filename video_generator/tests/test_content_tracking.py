import datetime as dt
import unittest

from modules.content_tracking import append_tracking, choose_focus, create_campaign


class ContentTrackingTest(unittest.TestCase):
    def test_focus_uses_relevant_landing(self):
        self.assertEqual(choose_focus("Tre esercizi per il nervo vago e lo stress"), "stress")
        self.assertEqual(choose_focus("Idratazione e acqua durante la giornata"), "energia")
        self.assertEqual(choose_focus("Calcola la tua numerologia di coppia"), "identita")
        self.assertEqual(choose_focus("Una curiosità sullo spazio"), "risorse")

    def test_campaign_is_short_stable_shape(self):
        campaign = create_campaign("tiktok", "nervo vago", "promo", dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        self.assertRegex(campaign["campaign_id"], r"^stress-tt-[a-f0-9]{8}$")
        self.assertTrue(campaign["tracking_url"].endswith(campaign["campaign_id"]))

    def test_caption_does_not_expose_non_clickable_url(self):
        campaign = create_campaign("instagram", "acqua", now=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc))
        caption = append_tracking("Caption con link in bio", campaign)
        self.assertEqual(caption, "Caption con link in bio")
        self.assertNotIn("vercel.app", caption)


if __name__ == "__main__":
    unittest.main()

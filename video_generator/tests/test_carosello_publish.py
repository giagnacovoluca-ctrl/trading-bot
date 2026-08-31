import unittest
from pathlib import Path

from crea_carosello import build_instagram_reel_command


class CarouselPublishTest(unittest.TestCase):
    def test_instagram_uses_reel_uploader_with_rendered_mp4(self):
        command = build_instagram_reel_command(
            "/usr/bin/python3",
            Path("output/carosello_finale.mp4"),
            Path("scripts/script_carosello.txt"),
            "promo",
        )

        self.assertEqual(command[1], "step4_pubblica_ig_api.py")
        self.assertNotIn("step4_pubblica_ig_carousel_api.py", command)
        self.assertEqual(command[command.index("--video") + 1], "output/carosello_finale.mp4")
        self.assertEqual(command[command.index("--mode") + 1], "promo")


if __name__ == "__main__":
    unittest.main()

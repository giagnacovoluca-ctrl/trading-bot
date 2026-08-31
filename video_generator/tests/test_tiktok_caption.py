import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from step4_pubblica import genera_metadata_tiktok


class TikTokCaptionTest(unittest.TestCase):
    def test_standard_video_does_not_reuse_stale_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path("scripts").mkdir()
                Path("scripts/tiktok_caption.txt").write_text(
                    "Vecchia caption sull'email marketing", encoding="utf-8"
                )
                generated = subprocess.CompletedProcess(
                    ["agy"], 0, "Nuova caption coerente con la matematica", ""
                )
                with patch("subprocess.run", return_value=generated):
                    caption = genera_metadata_tiktok("Copione sulla matematica", "bastian")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(caption, "Nuova caption coerente con la matematica")

    def test_carousel_can_use_caption_from_its_current_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path("scripts").mkdir()
                Path("scripts/tiktok_caption.txt").write_text(
                    "Caption revisionata del carosello", encoding="utf-8"
                )
                caption = genera_metadata_tiktok(
                    "Testo carosello", "virale", use_existing_caption=True
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(caption, "Caption revisionata del carosello")


if __name__ == "__main__":
    unittest.main()

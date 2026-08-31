import unittest
from pathlib import Path


class QualityRetryScriptTest(unittest.TestCase):
    def test_all_video_wrappers_use_retry_orchestrator(self):
        root = Path(__file__).resolve().parents[1]
        for name, mode in (("video_virale.sh", "virale"), ("video_promo.sh", "promo"), ("video_bastian.sh", "bastian")):
            content = (root / name).read_text(encoding="utf-8")
            self.assertIn(f"./run_agent_until_publish.sh {mode}", content)

    def test_orchestrator_handles_quality_exit_code(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "run_agent_until_publish.sh").read_text(encoding="utf-8")
        self.assertIn("exit_code != 74 && exit_code != 75", content)
        self.assertIn("MAX_ATTEMPTS", content)


if __name__ == "__main__": unittest.main()

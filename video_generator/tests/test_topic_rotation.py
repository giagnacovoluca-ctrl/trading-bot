import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import feedback_loop
from rag_generator import EDITORIAL_FAMILIES, TOPIC_IDEAS


class TopicRotationTest(unittest.TestCase):
    def test_all_topics_belong_to_an_editorial_family(self):
        self.assertEqual(set(TOPIC_IDEAS), set(EDITORIAL_FAMILIES))

    def test_requested_editorial_families_are_present(self):
        expected = {
            "Alimentazione",
            "Persone e abitudini",
            "Storia culture e simboli",
            "Mente e comportamento",
            "Scienza e natura",
        }
        self.assertEqual(set(EDITORIAL_FAMILIES.values()), expected)

    def test_recent_history_contains_only_successful_publications(self):
        entries = [
            {"success": True, "topic": "A", "category": "Cat A"},
            {"success": False, "topic": "B", "category": "Cat B"},
            {"success": True, "topic": "C", "category": "Cat C"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "upload_log.json"
            log_path.write_text(
                "\n".join(json.dumps(entry) for entry in entries) + "\n",
                encoding="utf-8",
            )
            with patch.object(feedback_loop, "LOG_PATH", log_path):
                recent = feedback_loop.get_recent_published(30)

        self.assertEqual([entry["topic"] for entry in recent], ["A", "C"])

    def test_log_upload_persists_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "upload_log.json"
            with patch.object(feedback_loop, "LOG_PATH", log_path):
                feedback_loop.log_upload(
                    video_file="video.mp4",
                    hook_title="Hook",
                    category="Categoria",
                    mode="virale",
                    quality_score=8,
                    fonte="Fonte",
                    success=True,
                    topic="Argomento preciso",
                )
                entry = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(entry["topic"], "Argomento preciso")

    def test_updates_only_explicit_tiktok_entries_in_reverse_order(self):
        entries = [
            {"success": True, "platform": "tiktok", "topic": "vecchio", "views": 0},
            {"success": True, "platform": "instagram", "topic": "reel", "views": 0},
            {"success": True, "platform": "tiktok", "topic": "nuovo", "views": 0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "upload_log.json"
            log_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
            with patch.object(feedback_loop, "LOG_PATH", log_path):
                updated = feedback_loop.update_recent_tiktok_views([900, 300])
            saved = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(updated, 2)
        self.assertEqual(saved[2]["views"], 900)
        self.assertEqual(saved[0]["views"], 300)
        self.assertEqual(saved[1]["views"], 0)

    def test_leads_increase_weight_of_converting_category(self):
        entries = [
            {"success": True, "category": "stress", "campaign_id": "stress-tt-a1b2c3d4", "views": 100},
            {"success": True, "category": "energia", "campaign_id": "energia-tt-b1c2d3e4", "views": 100},
        ]
        leads = [{"attribution": {"utm_content": "stress-tt-a1b2c3d4"}}]
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "upload_log.json"
            leads_path = Path(tmp) / "leads.jsonl"
            log_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
            leads_path.write_text("\n".join(json.dumps(lead) for lead in leads) + "\n", encoding="utf-8")
            with patch.object(feedback_loop, "LOG_PATH", log_path), patch.object(feedback_loop, "LEADS_PATH", leads_path):
                weights = feedback_loop.get_topic_weights(["stress", "energia"])
        self.assertGreater(weights["stress"], weights["energia"])


if __name__ == "__main__":
    unittest.main()

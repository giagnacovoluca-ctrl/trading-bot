import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from modules.feedback_loop import update_tiktok_metrics

class MetricIdentityTest(unittest.TestCase):
    def test_matches_id_not_grid_order_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'uploads.jsonl'
            entries = [
                {'platform': 'tiktok', 'success': True, 'media_id': '123', 'views': 10},
                {'platform': 'tiktok', 'success': True, 'media_id': '', 'views': 20},
                {'platform': 'instagram', 'success': True, 'media_id': '123', 'views': 30},
            ]
            path.write_text(''.join(json.dumps(row) + '\n' for row in entries))
            with patch('modules.feedback_loop.LOG_PATH', path):
                self.assertEqual(update_tiktok_metrics({'456': 999, '123': 42}), 1)
            result = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['views'] for row in result], [42, 20, 30])

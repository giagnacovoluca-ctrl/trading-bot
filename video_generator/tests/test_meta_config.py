import os
import unittest
from unittest.mock import patch

from modules.meta_config import graph_url, graph_version


class MetaConfigTest(unittest.TestCase):
    def test_default_preserves_current_version(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(graph_version(), "v24.0")

    def test_accepts_configured_version(self):
        with patch.dict(os.environ, {"META_GRAPH_VERSION": "v24.0"}):
            self.assertEqual(graph_url("123/media"), "https://graph.facebook.com/v24.0/123/media")

    def test_rejects_invalid_version(self):
        with patch.dict(os.environ, {"META_GRAPH_VERSION": "latest/bad"}):
            with self.assertRaises(ValueError):
                graph_version()

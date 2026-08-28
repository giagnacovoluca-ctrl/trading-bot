import json
import tempfile
import unittest
from pathlib import Path

from modules.site_integration import MANIFEST_PREFIX, parse_generated_manifest


class GeneratedManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp_dir.name)
        (self.repository / "src/content/articles").mkdir(parents=True)
        (self.repository / "public/images").mkdir(parents=True)
        (self.repository / "src/content/articles/example.md").write_text("article")
        (self.repository / "public/images/example.jpg").write_bytes(b"image")

    def tearDown(self):
        self.temp_dir.cleanup()

    def output_for(self, manifest):
        return f"log\n{MANIFEST_PREFIX}{json.dumps(manifest)}\n"

    def test_accepts_existing_generated_files(self):
        result = parse_generated_manifest(
            self.output_for(
                ["src/content/articles/example.md", "public/images/example.jpg"]
            ),
            self.repository,
        )
        self.assertEqual(
            result,
            ["src/content/articles/example.md", "public/images/example.jpg"],
        )

    def test_rejects_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "non sicuro"):
            parse_generated_manifest(
                self.output_for(["src/content/articles/../../../.env"]),
                self.repository,
            )

    def test_rejects_files_outside_allowed_roots(self):
        (self.repository / "package.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "fuori dalle directory"):
            parse_generated_manifest(
                self.output_for(["package.json"]), self.repository
            )

    def test_rejects_missing_files(self):
        with self.assertRaises(FileNotFoundError):
            parse_generated_manifest(
                self.output_for(["public/images/missing.jpg"]), self.repository
            )


if __name__ == "__main__":
    unittest.main()

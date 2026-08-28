import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from modules.media_server import TemporaryMediaServer


class TemporaryMediaServerTest(unittest.TestCase):
    def test_serves_only_explicit_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "video.mp4"
            secret = root / ".env"
            media.write_bytes(b"media")
            secret.write_text("SECRET=value", encoding="utf-8")

            with TemporaryMediaServer([media], public_host="127.0.0.1") as server:
                with urllib.request.urlopen(server.url_for(media), timeout=2) as response:
                    self.assertEqual(response.read(), b"media")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{server.port}/.env", timeout=2
                    )
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()


if __name__ == "__main__":
    unittest.main()

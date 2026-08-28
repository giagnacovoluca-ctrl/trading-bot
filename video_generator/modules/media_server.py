"""Server HTTP temporaneo e isolato per consegnare media alle API Meta."""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class TemporaryMediaServer:
    """Serve esclusivamente copie dei file richiesti da una directory effimera."""

    def __init__(self, media_paths: list[Path], public_host: str | None = None):
        if not media_paths:
            raise ValueError("Nessun media da servire")
        # Fallback temporaneo mantiene compatibilità con la VPS esistente. Va
        # sostituito configurando PUBLIC_MEDIA_HOST nell'ambiente.
        self.public_host = public_host or os.getenv("PUBLIC_MEDIA_HOST", "141.94.79.16")

        self._temp_dir = Path(tempfile.mkdtemp(prefix="meta-media-"))
        self._names: dict[Path, str] = {}
        for index, source in enumerate(media_paths):
            source = source.resolve(strict=True)
            safe_name = f"{index:02d}_{source.name}"
            shutil.copy2(source, self._temp_dir / safe_name)
            self._names[source] = safe_name

        handler = functools.partial(_QuietHandler, directory=str(self._temp_dir))
        self._httpd = http.server.ThreadingHTTPServer(("", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url_for(self, media_path: Path) -> str:
        name = self._names[media_path.resolve(strict=True)]
        return f"http://{self.public_host}:{self.port}/{quote(name)}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

"""
Async-safe transport layer.
Batches events and sends them in background thread to minimize latency impact.
"""
from __future__ import annotations
import threading
import queue
import time
import logging
import httpx
from typing import Any

log = logging.getLogger("promptwatch.transport")


class Transport:
    def __init__(self, api_key: str, base_url: str, flush_interval: float = 2.0, batch_size: int = 20):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.flush_interval = flush_interval
        self.batch_size = batch_size

        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="promptwatch-transport")
        self._thread.start()

    def enqueue(self, event: dict):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            log.warning("PromptWatch event queue full — dropping event")

    def flush(self):
        """Drain queue synchronously."""
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._send(batch)

    def shutdown(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.flush()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(self.flush_interval)
            self.flush()

    def _send(self, events: list[dict]):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{self.base_url}/v1/events/batch",
                    json={"events": events},
                    headers={"X-API-Key": self.api_key},
                )
                if resp.status_code >= 400:
                    log.warning(f"PromptWatch ingest error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.debug(f"PromptWatch transport error: {e}")

"""
PromptWatch main client.
Usage:
    pw = PromptWatch(api_key="pw_...")
    openai_client = pw.wrap(openai.OpenAI())
"""
from __future__ import annotations
import os
import threading
import queue
import time
import logging
from typing import Any
from .transport import Transport
from .wrappers.openai import wrap_openai
from .wrappers.anthropic import wrap_anthropic

log = logging.getLogger("promptwatch")


class PromptWatch:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.promptwatch.io",
        flush_interval: float = 2.0,
        batch_size: int = 20,
        debug: bool = False,
    ):
        self.api_key = api_key or os.environ.get("PROMPTWATCH_API_KEY")
        if not self.api_key:
            raise ValueError("PromptWatch API key required. Set PROMPTWATCH_API_KEY env var or pass api_key=")

        if debug:
            logging.basicConfig(level=logging.DEBUG)
            log.setLevel(logging.DEBUG)

        self._transport = Transport(
            api_key=self.api_key,
            base_url=base_url,
            flush_interval=flush_interval,
            batch_size=batch_size,
        )
        self._transport.start()

    def wrap(self, client: Any) -> Any:
        """Wrap any supported LLM client to auto-track all calls."""
        class_name = type(client).__name__
        module_name = type(client).__module__

        if "openai" in module_name.lower() or class_name in ("OpenAI", "AsyncOpenAI"):
            return wrap_openai(client, self._transport)

        if "anthropic" in module_name.lower() or class_name in ("Anthropic", "AsyncAnthropic"):
            return wrap_anthropic(client, self._transport)

        raise ValueError(
            f"Unsupported client type: {class_name}. "
            "Supported: openai.OpenAI, openai.AsyncOpenAI, anthropic.Anthropic, anthropic.AsyncAnthropic"
        )

    def track(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        feature: str | None = None,
        user_id: str | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        metadata: dict | None = None,
    ):
        """Manually track an LLM call (for providers without a wrapper)."""
        self._transport.enqueue({
            "provider": provider,
            "model": model,
            "endpoint": "chat",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "feature": feature,
            "user_id": user_id,
            "latency_ms": latency_ms,
            "error": error,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "metadata": metadata,
        })

    def flush(self):
        """Force immediate flush of pending events."""
        self._transport.flush()

    def shutdown(self):
        """Flush remaining events and stop background thread."""
        self._transport.shutdown()

    def __del__(self):
        try:
            self._transport.shutdown()
        except Exception:
            pass

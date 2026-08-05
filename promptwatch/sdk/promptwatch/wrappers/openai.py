"""
OpenAI client wrapper.
Intercepts chat.completions.create and embeddings.create to track usage.
"""
from __future__ import annotations
import time
import wrapt
from typing import Any


def wrap_openai(client: Any, transport) -> Any:
    """Return a wrapped OpenAI client that auto-tracks all calls."""
    _patch_completions(client, transport)
    _patch_embeddings(client, transport)
    return client


def _patch_completions(client, transport):
    original_create = client.chat.completions.create

    @wrapt.decorator
    def _tracked_create(wrapped, instance, args, kwargs):
        feature = kwargs.pop("pw_feature", None) or _extract_header(kwargs, "pw-feature")
        user_id = kwargs.pop("pw_user", None) or _extract_header(kwargs, "pw-user")
        prompt_name = kwargs.pop("pw_prompt", None) or _extract_header(kwargs, "pw-prompt")
        prompt_version = kwargs.pop("pw_version", None) or _extract_header(kwargs, "pw-version")

        start = time.monotonic()
        error_msg = None
        response = None
        try:
            response = wrapped(*args, **kwargs)
            return response
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            model = kwargs.get("model", "unknown")

            prompt_tokens = 0
            completion_tokens = 0
            if response and hasattr(response, "usage") and response.usage:
                prompt_tokens = response.usage.prompt_tokens or 0
                completion_tokens = response.usage.completion_tokens or 0

            transport.enqueue({
                "provider": "openai",
                "model": model,
                "endpoint": "chat",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "error": error_msg,
                "feature": feature,
                "user_id": user_id,
                "prompt_name": prompt_name,
                "prompt_version": prompt_version,
            })

    client.chat.completions.create = _tracked_create(original_create)


def _patch_embeddings(client, transport):
    original_create = client.embeddings.create

    @wrapt.decorator
    def _tracked_embed(wrapped, instance, args, kwargs):
        start = time.monotonic()
        response = None
        error_msg = None
        try:
            response = wrapped(*args, **kwargs)
            return response
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            model = kwargs.get("model", "unknown")
            prompt_tokens = 0
            if response and hasattr(response, "usage") and response.usage:
                prompt_tokens = response.usage.prompt_tokens or 0

            transport.enqueue({
                "provider": "openai",
                "model": model,
                "endpoint": "embedding",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "latency_ms": latency_ms,
                "error": error_msg,
            })

    client.embeddings.create = _tracked_embed(original_create)


def _extract_header(kwargs: dict, header_name: str) -> str | None:
    headers = kwargs.get("extra_headers") or {}
    return headers.get(header_name)

"""Anthropic client wrapper — intercepts messages.create."""
from __future__ import annotations
import time
import wrapt
from typing import Any


def wrap_anthropic(client: Any, transport) -> Any:
    _patch_messages(client, transport)
    return client


def _patch_messages(client, transport):
    original_create = client.messages.create

    @wrapt.decorator
    def _tracked_create(wrapped, instance, args, kwargs):
        feature = kwargs.pop("pw_feature", None)
        user_id = kwargs.pop("pw_user", None)
        prompt_name = kwargs.pop("pw_prompt", None)
        prompt_version = kwargs.pop("pw_version", None)

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

            input_tokens = 0
            output_tokens = 0
            if response and hasattr(response, "usage") and response.usage:
                input_tokens = response.usage.input_tokens or 0
                output_tokens = response.usage.output_tokens or 0

            transport.enqueue({
                "provider": "anthropic",
                "model": model,
                "endpoint": "chat",
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "latency_ms": latency_ms,
                "error": error_msg,
                "feature": feature,
                "user_id": user_id,
                "prompt_name": prompt_name,
                "prompt_version": prompt_version,
            })

    client.messages.create = _tracked_create(original_create)

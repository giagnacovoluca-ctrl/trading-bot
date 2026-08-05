"""SDK unit tests — mocked transport, no network."""
import pytest
from unittest.mock import MagicMock, patch
from promptwatch.client import PromptWatch
from promptwatch.pricing import compute_cost


class FakeTransport:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)

    def start(self): pass
    def flush(self): pass
    def shutdown(self): pass


@pytest.fixture
def pw(monkeypatch):
    client = PromptWatch.__new__(PromptWatch)
    client.api_key = "pw_test"
    client._transport = FakeTransport()
    return client


def test_manual_track(pw):
    pw.track(
        provider="openai",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        feature="search",
        user_id="user_1",
    )
    assert len(pw._transport.events) == 1
    e = pw._transport.events[0]
    assert e["provider"] == "openai"
    assert e["prompt_tokens"] == 100
    assert e["feature"] == "search"


def test_pricing_openai():
    cost = compute_cost("openai", "gpt-4o", 1_000_000, 0)
    assert abs(cost - 2.50) < 0.001


def test_pricing_anthropic():
    cost = compute_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 0)
    assert abs(cost - 3.00) < 0.001


def test_pricing_unknown_model():
    cost = compute_cost("openai", "gpt-99-unknown", 1000, 1000)
    assert cost == 0.0


def test_pricing_prefix_match():
    cost = compute_cost("openai", "gpt-4o-mini-2024-07-18", 1_000_000, 0)
    assert cost > 0


def test_wrap_openai(pw):
    mock_client = MagicMock()
    mock_client.__class__.__module__ = "openai"
    mock_response = MagicMock()
    mock_response.usage.prompt_tokens = 50
    mock_response.usage.completion_tokens = 100
    mock_client.chat.completions.create.return_value = mock_response

    wrapped = pw.wrap(mock_client)
    result = wrapped.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        pw_feature="test_feature",
    )

    assert result == mock_response
    assert len(pw._transport.events) == 1
    e = pw._transport.events[0]
    assert e["feature"] == "test_feature"
    assert e["prompt_tokens"] == 50
    assert e["completion_tokens"] == 100


def test_wrap_anthropic(pw):
    mock_client = MagicMock()
    mock_client.__class__.__module__ = "anthropic"
    mock_response = MagicMock()
    mock_response.usage.input_tokens = 200
    mock_response.usage.output_tokens = 80
    mock_client.messages.create.return_value = mock_response

    wrapped = pw.wrap(mock_client)
    result = wrapped.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "hello"}],
        pw_feature="docs",
    )

    assert result == mock_response
    assert len(pw._transport.events) == 1
    e = pw._transport.events[0]
    assert e["provider"] == "anthropic"
    assert e["feature"] == "docs"
    assert e["prompt_tokens"] == 200


def test_error_tracked(pw):
    mock_client = MagicMock()
    mock_client.__class__.__module__ = "openai"
    mock_client.chat.completions.create.side_effect = Exception("Rate limited")

    wrapped = pw.wrap(mock_client)
    with pytest.raises(Exception, match="Rate limited"):
        wrapped.chat.completions.create(model="gpt-4o", messages=[])

    assert len(pw._transport.events) == 1
    assert "Rate limited" in pw._transport.events[0]["error"]

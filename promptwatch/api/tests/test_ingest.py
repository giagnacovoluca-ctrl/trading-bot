"""Integration test for ingest endpoint."""
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

from main import app


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ingest_invalid_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/events",
            json={"provider": "openai", "model": "gpt-4o", "prompt_tokens": 100, "completion_tokens": 50},
            headers={"X-API-Key": "pw_invalid_key"},
        )
    assert resp.status_code == 401

"""BountyCollector: aggregates results from all enabled platform adapters."""
from __future__ import annotations

import asyncio

from loguru import logger

from ..config.settings import get_settings
from ..core.models import Bounty
from .algora_adapter import AlgoraAdapter
from .github_adapter import GitHubAdapter
from .upwork_adapter import UpworkAdapter


class BountyCollector:
    def __init__(self):
        settings = get_settings()
        self._adapters = []
        if settings.get("scout.platforms.github.enabled", True):
            self._adapters.append(GitHubAdapter())
        if settings.get("scout.platforms.algora.enabled", False):
            self._adapters.append(AlgoraAdapter())
        if settings.get("scout.platforms.upwork.enabled", False):
            self._adapters.append(UpworkAdapter())

    async def collect(self) -> list[Bounty]:
        """Fetch from all adapters concurrently, merge, de-duplicate by id."""
        results = await asyncio.gather(
            *[adapter.fetch_bounties() for adapter in self._adapters],
            return_exceptions=True,
        )
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        bounties: list[Bounty] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Adapter error: {r}")
                continue
            for b in r:
                if b.id in seen_ids or (b.url and b.url in seen_urls):
                    continue
                seen_ids.add(b.id)
                if b.url:
                    seen_urls.add(b.url)
                bounties.append(b)
        logger.info(f"BountyCollector: {len(bounties)} unique bounties from {len(self._adapters)} platforms")
        return bounties

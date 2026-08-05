"""Algora adapter for bounty scouting via public GraphQL API."""
from __future__ import annotations

from datetime import datetime

import httpx
from loguru import logger

from ..core.interfaces import BountyAdapter
from ..core.models import Bounty, Platform


class AlgoraAdapter(BountyAdapter):
    """
    Algora adapter.
    TODO: Replace with official Algora API endpoint when available.
    Currently uses public GraphQL endpoint (https://console.algora.io/api/graphql).
    """

    GRAPHQL_URL = "https://algora.io/api/graphql"

    QUERY = """
    query ListBounties($first: Int) {
      bounties(first: $first, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes {
          id
          title
          body
          url
          reward { amount currency }
          issue {
            url createdAt updatedAt
            author { login }
            repository { url nameWithOwner }
          }
          status
          labels { nodes { name } }
        }
      }
    }
    """

    async def fetch_bounties(self) -> list[Bounty]:
        # TODO: add Authorization header when API key available
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    self.GRAPHQL_URL,
                    json={"query": self.QUERY, "variables": {"first": 100}},
                )
                resp.raise_for_status()
                data = resp.json()
                nodes = data.get("data", {}).get("bounties", {}).get("nodes", [])
                bounties = [self._parse(n) for n in nodes if n]
                logger.info(f"AlgoraAdapter: fetched {len(bounties)} bounties")
                return bounties
            except Exception as e:
                # Algora API may require auth — return empty, log warning
                logger.warning(f"AlgoraAdapter failed (may need auth): {e}")
                return []

    async def fetch_bounty_detail(self, bounty_id: str) -> Bounty:
        raise NotImplementedError("TODO: implement Algora detail fetch")

    def _parse(self, node: dict) -> Bounty:
        issue = node.get("issue") or {}
        repo = issue.get("repository") or {}
        reward = node.get("reward") or {}
        now = datetime.utcnow()
        created_raw = issue.get("createdAt") or now.isoformat()
        updated_raw = issue.get("updatedAt") or now.isoformat()
        return Bounty(
            id=f"algora_{node['id']}",
            platform=Platform.ALGORA,
            title=node.get("title", ""),
            body=node.get("body", "") or "",
            url=node.get("url", ""),
            repo_url=repo.get("url", ""),
            repo_name=repo.get("nameWithOwner", ""),
            payout_usd=float(reward.get("amount") or 0),
            currency=reward.get("currency", "USD"),
            labels=[lbl["name"] for lbl in (node.get("labels") or {}).get("nodes", [])],
            created_at=datetime.fromisoformat(created_raw.replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(updated_raw.replace("Z", "+00:00")),
            author=(issue.get("author") or {}).get("login", ""),
        )

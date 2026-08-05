"""GitHub Issues adapter for bounty scouting."""
from __future__ import annotations

import re
from datetime import datetime

import httpx
from loguru import logger

from ..config.settings import get_settings
from ..core.interfaces import BountyAdapter
from ..core.models import Bounty, Platform

BOUNTY_LABELS = ["bounty", "hacktoberfest", "help wanted", "good first issue"]


class GitHubAdapter(BountyAdapter):
    BASE_URL = "https://api.github.com"

    def __init__(self):
        settings = get_settings()
        self._token = settings.github_token
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._queries = settings.get("scout.platforms.github.search_queries", [
            "label:bounty state:open language:python",
        ])
        self._max_results = settings.get("scout.platforms.github.max_results_per_query", 30)

    async def fetch_bounties(self) -> list[Bounty]:
        if not self._token:
            logger.warning("GITHUB_TOKEN not set — skipping GitHub adapter")
            return []
        bounties: list[Bounty] = []
        seen_ids: set[str] = set()
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            for query in self._queries:
                try:
                    results = await self._search_issues(client, query)
                    for issue in results:
                        bid = f"github_{issue['id']}"
                        if bid in seen_ids:
                            continue
                        seen_ids.add(bid)
                        bounties.append(self._parse_issue(issue))
                except Exception as e:
                    logger.warning(f"GitHub query '{query}' failed: {e}")
        logger.info(f"GitHubAdapter: fetched {len(bounties)} unique bounties")
        return bounties

    async def fetch_bounty_detail(self, bounty_id: str) -> Bounty:
        # bounty_id format: github_{issue_node_id}
        raise NotImplementedError("TODO: implement GitHub detail fetch via issue number + repo")

    async def _search_issues(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        params = {
            "q": query,
            "per_page": min(self._max_results, 100),
            "sort": "created",
            "order": "desc",
        }
        resp = await client.get(f"{self.BASE_URL}/search/issues", params=params)
        resp.raise_for_status()
        return resp.json().get("items", [])

    def _parse_issue(self, issue: dict) -> Bounty:
        repo_url = issue.get("repository_url", "")
        parts = repo_url.split("/")
        repo_name = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else ""
        body = issue.get("body", "") or ""
        payout = self._extract_payout(body)
        return Bounty(
            id=f"github_{issue['id']}",
            platform=Platform.GITHUB,
            title=issue.get("title", ""),
            body=body,
            url=issue.get("html_url", ""),
            repo_url=repo_url.replace("api.github.com/repos", "github.com"),
            repo_name=repo_name,
            payout_usd=payout,
            labels=[lbl["name"] for lbl in issue.get("labels", [])],
            created_at=datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(issue["updated_at"].replace("Z", "+00:00")),
            author=issue.get("user", {}).get("login", ""),
        )

    def _extract_payout(self, text: str) -> float:
        """Extract USD payout from issue body using regex."""
        patterns = [
            r"\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)",
            r"(\d+(?:,\d{3})*(?:\.\d{2})?)\s*USD",
            r"bounty[:\s]+\$?\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return float(m.group(1).replace(",", ""))
        return 0.0

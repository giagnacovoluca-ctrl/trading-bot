"""Freelancer.com adapter — public REST API, no auth required.

Strategia: cattura solo job postati nelle ultime 6h con < 10 bid.
Questo è l'unico slot temporale dove la competizione non è ancora schiacciante.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from ..config.settings import get_settings
from ..core.interfaces import BountyAdapter
from ..core.models import Bounty, Platform

_API = "https://www.freelancer.com/api/projects/0.1/projects/active/"


class UpworkAdapter(BountyAdapter):
    """
    Nonostante il nome (mantenuto per retrocompatibilità), punta a Freelancer.com.
    Upwork ha rimosso il feed RSS. Freelancer espone API pubblica JSON.
    """

    async def fetch_bounties(self) -> list[Bounty]:
        settings = get_settings()
        if not settings.get("scout.platforms.upwork.enabled", False):
            return []

        queries: list[str] = settings.get("scout.platforms.upwork.rss_queries", [])
        max_bid = settings.get("scout.platforms.upwork.max_bid_count", 10)
        max_age_h = settings.get("scout.platforms.upwork.max_age_hours", 6)
        min_budget = settings.get("ranking.min_payout_usd", 80)

        seen: set[str] = set()
        bounties: list[Bounty] = []
        now_ts = datetime.now(timezone.utc).timestamp()

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for query in queries:
                try:
                    resp = await client.get(_API, params={
                        "full_description": "true",
                        "project_types[]": "fixed",
                        "sort_field": "time_submitted",
                        "reverse_sort": "true",
                        "limit": 50,
                        "query": query,
                        "min_avg_price": min_budget,
                    })
                    if resp.status_code != 200:
                        logger.debug(f"Freelancer API {query!r}: {resp.status_code}")
                        continue
                    projects = resp.json().get("result", {}).get("projects", [])
                    for p in projects:
                        age_h = (now_ts - p.get("time_submitted", 0)) / 3600
                        bids = p.get("bid_stats", {}).get("bid_count", 999)
                        if age_h > max_age_h or bids > max_bid:
                            continue
                        b = self._parse(p, age_h)
                        if b and b.id not in seen:
                            seen.add(b.id)
                            bounties.append(b)
                except Exception as e:
                    logger.debug(f"Freelancer [{query!r}] error: {e}")

        logger.info(f"FreelancerAdapter: {len(bounties)} job freschi (< {max_age_h}h, < {max_bid} bid)")
        return bounties

    async def fetch_bounty_detail(self, bounty_id: str) -> Bounty:
        raise NotImplementedError

    def _parse(self, p: dict, age_h: float) -> Bounty | None:
        try:
            pid = str(p.get("id", ""))
            title = p.get("title", "").strip()
            desc = p.get("description", "") or ""
            seo = p.get("seo_url") or pid
            url = f"https://www.freelancer.com/projects/{seo}"
            budget = p.get("budget", {})
            bmin = float(budget.get("minimum") or 0)
            bmax = float(budget.get("maximum") or bmin)
            payout = (bmin + bmax) / 2 if bmax else bmin

            ts = p.get("time_submitted", 0)
            posted_at = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)

            return Bounty(
                id=f"freelancer_{pid}",
                platform=Platform.UPWORK,  # riusa piattaforma Upwork per scoring
                title=title,
                body=desc,
                url=url,
                repo_url="",
                repo_name="",
                payout_usd=payout,
                created_at=posted_at,
                updated_at=posted_at,
            )
        except Exception as e:
            logger.debug(f"Freelancer parse error: {e}")
            return None

"""RankingEngine: applies filters and scorer, returns sorted bounty list."""
from __future__ import annotations

from loguru import logger

from ..config.settings import get_settings
from ..core.interfaces import ScorerBase
from ..core.models import Bounty, Platform, RankingResult
from .scorers.phase0_scorer import Phase0Scorer
from .scorers.phase1_scorer import Phase1Scorer
from .scorers.phase2_scorer import Phase2Scorer

_SCORERS: dict[int, type[ScorerBase]] = {
    0: Phase0Scorer,
    1: Phase1Scorer,
    2: Phase2Scorer,
}


class RankingEngine:
    def __init__(self, scorer: ScorerBase | None = None):
        settings = get_settings()
        phase = settings.get("ranking.phase", 0)
        self._scorer: ScorerBase = scorer or _SCORERS[phase]()
        self._min_ephh = settings.get("ranking.min_ephh_threshold", 10.0)
        self._min_payout = settings.get("ranking.min_payout_usd", 50)
        self._max_age_github = settings.get("ranking.max_issue_age_days", 180)
        upwork_max_hours = settings.get("ranking.upwork_max_age_hours", 48)
        self._max_age_upwork = upwork_max_hours / 24
        logger.info(f"RankingEngine using Phase {self._scorer.phase} scorer")

    def rank(self, bounties: list[Bounty]) -> list[tuple[Bounty, RankingResult]]:
        """Score and filter bounties, return sorted by EPHH descending."""
        settings = get_settings()
        max_attempts = settings.get("ranking.max_attempt_comments", 5)
        max_open_prs = settings.get("ranking.max_issue_prs_open", 2)
        max_closed_prs = settings.get("ranking.max_issue_prs_closed", 4)

        scored: list[tuple[Bounty, RankingResult]] = []

        for bounty in bounties:
            if not bounty.features:
                bounty.skip_reason = "features not extracted"
                continue

            f = bounty.features
            is_upwork = bounty.platform == Platform.UPWORK

            if is_upwork:
                # === FILTRI UPWORK ===
                if f.payout_usd < self._min_payout:
                    bounty.skip_reason = f"budget ${f.payout_usd:.0f} < ${self._min_payout} min"
                    continue
                if f.issue_age_days > self._max_age_upwork:
                    h = f.upwork_posted_hours_ago
                    bounty.skip_reason = f"job vecchio: {h:.0f}h fa (max {self._max_age_upwork*24:.0f}h)"
                    continue
                if f.upwork_experience_level == "entry" and f.payout_usd < 100:
                    bounty.skip_reason = "entry level + budget basso"
                    continue
            else:
                # === FILTRI GITHUB ===
                if f.has_merged_pr:
                    bounty.skip_reason = "PR già mergiata — bounty già pagata"
                    continue
                if f.n_attempt_comments > max_attempts:
                    bounty.skip_reason = f"bot farm: {f.n_attempt_comments} /attempt"
                    continue
                if f.n_issue_prs_open > max_open_prs:
                    bounty.skip_reason = f"saturata: {f.n_issue_prs_open} PR aperte"
                    continue
                if f.n_issue_prs_closed > max_closed_prs:
                    bounty.skip_reason = f"bloccata: {f.n_issue_prs_closed} PR chiuse"
                    continue
                if f.payout_usd < self._min_payout:
                    bounty.skip_reason = f"payout ${f.payout_usd:.0f} < ${self._min_payout} min"
                    continue
                if f.issue_age_days > self._max_age_github:
                    bounty.skip_reason = f"issue age {f.issue_age_days:.0f}d > {self._max_age_github}d max"
                    continue

            result = self._scorer.score(f)
            result.bounty_id = bounty.id

            if result.ephh < self._min_ephh:
                bounty.skip_reason = f"EPHH ${result.ephh:.1f} < ${self._min_ephh} threshold"
                continue

            scored.append((bounty, result))

        scored.sort(key=lambda x: x[1].ephh, reverse=True)
        for i, (b, r) in enumerate(scored):
            r.priority = i + 1

        logger.info(f"RankingEngine: {len(scored)}/{len(bounties)} bounties passed filters")
        return scored

    @property
    def scorer(self) -> ScorerBase:
        return self._scorer

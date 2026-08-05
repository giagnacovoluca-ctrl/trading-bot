"""Shared EPHH calculation mixin — same formula regardless of scorer phase."""
from __future__ import annotations

from abc import ABC

from ...config.settings import get_settings
from ...core.interfaces import ScorerBase
from ...core.models import BountyFeatures, RankingResult, TaskOutcome


class BaseScorerMixin(ScorerBase, ABC):
    """Provides _calc_ephh() and _build_result() for all scorer phases."""

    def _calc_ephh(
        self,
        p_correct: float,
        p_maintainer_accepts: float,
        payout: float,
        human_hours: float,
        api_cost: float = 0.5,
        claude_pro_amortized: float = 0.625,  # $100 / 160h
    ) -> float:
        """Core EPHH formula.

        EPHH = (P(correct) * P(maintainer_accepts) * payout - api_cost - claude_pro_amortized)
               / human_hours_predicted
        """
        if human_hours <= 0:
            return 0.0
        profit = p_correct * p_maintainer_accepts * payout - api_cost - claude_pro_amortized
        return profit / human_hours

    def _build_result(
        self,
        bounty_id: str,
        p_correct: float,
        p_maintainer_accepts: float,
        payout: float,
        human_hours: float,
        business_score: float,
        confidence: float,
    ) -> RankingResult:
        ephh = self._calc_ephh(p_correct, p_maintainer_accepts, payout, human_hours)
        return RankingResult(
            bounty_id=bounty_id,
            ephh=ephh,
            business_score=business_score,
            human_hours_predicted=human_hours,
            p_correct=p_correct,
            p_maintainer_accepts=p_maintainer_accepts,
            expected_profit_usd=p_correct * p_maintainer_accepts * payout,
            confidence=confidence,
            scorer_phase=self.phase,
        )

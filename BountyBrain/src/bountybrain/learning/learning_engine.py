"""Learning engine: records outcomes and decides when to upgrade scorer phase."""
from __future__ import annotations

from loguru import logger

from ..config.settings import get_settings
from ..core.interfaces import ScorerBase
from ..core.models import OutcomeStatus, TaskOutcome
from ..knowledge.knowledge_base import KnowledgeBase


class LearningEngine:
    """
    Tracks all task decisions and outcomes.
    Notifies the scorer and decides when to upgrade to the next phase.
    """

    def __init__(self, knowledge_base: KnowledgeBase, scorer: ScorerBase):
        self._kb = knowledge_base
        self._scorer = scorer
        settings = get_settings()
        self._min_phase1 = settings.get("learning.min_samples_phase1", 80)
        self._min_phase2 = settings.get("learning.min_samples_phase2", 300)
        self._retrain_every = settings.get("learning.retrain_every_n_tasks", 10)
        self._n_since_retrain = 0

    def record_outcome(self, outcome: TaskOutcome) -> None:
        """Persist outcome and potentially trigger scorer upgrade."""
        self._kb.record_outcome(outcome)
        self._scorer.update(outcome)
        self._n_since_retrain += 1
        if self._n_since_retrain >= self._retrain_every:
            self._maybe_upgrade_phase()
            self._n_since_retrain = 0

    def get_stats(self) -> dict:
        outcomes = self._kb.load_outcomes()
        if not outcomes:
            return {
                "total": 0,
                "merged": 0,
                "rejected": 0,
                "abandoned": 0,
                "merge_rate": 0.0,
                "avg_ephh": 0.0,
                "scorer_phase": self._scorer.phase,
                "next_phase_at": (
                    self._min_phase1 if self._scorer.phase < 1
                    else self._min_phase2 if self._scorer.phase < 2
                    else None
                ),
            }
        merged = [o for o in outcomes if o.get("status") == OutcomeStatus.MERGED]
        ephhs = [o["ephh_actual"] for o in merged if o.get("ephh_actual", 0) > 0]
        return {
            "total": len(outcomes),
            "merged": len(merged),
            "rejected": sum(1 for o in outcomes if o.get("status") == OutcomeStatus.REJECTED),
            "abandoned": sum(1 for o in outcomes if o.get("status") == OutcomeStatus.ABANDONED),
            "merge_rate": len(merged) / len(outcomes),
            "avg_ephh": sum(ephhs) / len(ephhs) if ephhs else 0.0,
            "scorer_phase": self._scorer.phase,
            "next_phase_at": (
                self._min_phase1 if self._scorer.phase < 1
                else self._min_phase2 if self._scorer.phase < 2
                else None
            ),
        }

    def _maybe_upgrade_phase(self) -> None:
        outcomes = self._kb.load_outcomes()
        n = len(outcomes)
        current_phase = self._scorer.phase

        if n >= self._min_phase2 and current_phase < 2:
            logger.info(
                f"LearningEngine: n={n} >= {self._min_phase2} — "
                "upgrading to Phase 2 (GBM). TODO: instantiate Phase2Scorer."
            )
            # TODO: instantiate Phase2Scorer(), retrain, persist config change

        elif n >= self._min_phase1 and current_phase < 1:
            logger.info(
                f"LearningEngine: n={n} >= {self._min_phase1} — "
                "upgrading to Phase 1 (Ridge). TODO: instantiate Phase1Scorer."
            )
            # TODO: instantiate Phase1Scorer(), retrain on all outcomes, persist config

        else:
            logger.debug(
                f"LearningEngine: n={n} outcomes — "
                f"need {self._min_phase1} for Phase 1, {self._min_phase2} for Phase 2"
            )

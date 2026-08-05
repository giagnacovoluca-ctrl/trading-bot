"""Phase 0 scorer — rule-based, no training data required."""
from __future__ import annotations

from ...core.models import BountyFeatures, Platform, RankingResult, TaskOutcome, TaskType
from .base_scorer import BaseScorerMixin

# Upwork prende il 20% sui primi $500 con un cliente
_UPWORK_FEE = 0.80


class Phase0Scorer(BaseScorerMixin):
    """Rule-based scorer. Works from day 1, collects data for Phase 1."""

    @property
    def phase(self) -> int:
        return 0

    def score(self, features: BountyFeatures) -> RankingResult:
        is_upwork = features.upwork_job_type != ""

        p_correct = self._estimate_p_correct(features)
        p_win = self._estimate_p_win(features, is_upwork)
        human_hours = self._estimate_human_hours(features, is_upwork)

        # Payout netto (Upwork trattiene 20%)
        net_payout = features.payout_usd * (_UPWORK_FEE if is_upwork else 1.0)
        business_score = min(100.0, (p_correct * p_win * net_payout / 200.0) * 100)
        confidence = self._calc_confidence(features, is_upwork)

        return self._build_result(
            bounty_id="",
            p_correct=p_correct,
            p_maintainer_accepts=p_win,
            payout=net_payout,
            human_hours=human_hours,
            business_score=business_score,
            confidence=confidence,
        )

    def update(self, outcome: TaskOutcome) -> None:
        pass

    def _estimate_p_correct(self, f: BountyFeatures) -> float:
        """Probabilità che Claude Code risolva correttamente."""
        score = 0.55

        task_boosts: dict[TaskType, float] = {
            TaskType.FIX_FAILING_TEST: +0.25,
            TaskType.FIX_BUG: +0.12,
            TaskType.ADD_FEATURE: +0.05,
            TaskType.PERFORMANCE: +0.02,
            TaskType.SECURITY: -0.05,
            TaskType.REFACTOR: -0.08,
            TaskType.DOCUMENTATION: +0.15,
            TaskType.UNKNOWN: -0.05,
        }
        score += task_boosts.get(f.task_type, 0)

        if f.issue_has_reproduction_steps:
            score += 0.08
        if f.issue_has_acceptance_criteria:
            score += 0.07
        if f.issue_body_length > 500:
            score += 0.05  # spec dettagliata

        # GitHub-specific
        if f.repo_has_tests:
            score += 0.10
        if f.repo_has_ci:
            score += 0.05
        if f.repo_has_devcontainer or f.repo_has_docker:
            score += 0.03

        # Penalità competizione GitHub
        if f.n_attempt_comments > 0:
            score -= 0.05 * min(f.n_attempt_comments, 4)
        if f.n_issue_prs_open > 0:
            score -= 0.08 * f.n_issue_prs_open
        if f.n_issue_prs_closed > 0:
            score -= 0.04 * min(f.n_issue_prs_closed, 3)

        # Upwork: entry level = requisiti più chiari
        if f.upwork_experience_level == "entry":
            score += 0.05
        elif f.upwork_experience_level == "expert":
            score -= 0.10

        return max(0.10, min(0.95, score))

    def _estimate_p_win(self, f: BountyFeatures, is_upwork: bool) -> float:
        """
        GitHub: P(maintainer_accepts PR)
        Upwork: P(cliente assegna job a noi)
        """
        if not is_upwork:
            # === GITHUB ===
            score = 0.60
            if f.maintainer_response_p50_days < 2:
                score += 0.15
            elif f.maintainer_response_p50_days < 7:
                score += 0.05
            elif f.maintainer_response_p50_days > 30:
                score -= 0.20
            if f.repo_has_contributing:
                score += 0.05
            if f.n_stale_prs > 5:
                score -= 0.15
            if f.issue_has_acceptance_criteria:
                score += 0.08
            return max(0.10, min(0.95, score))

        # === UPWORK ===
        # Competizione decresce rapidamente col tempo
        hours = f.upwork_posted_hours_ago
        if hours < 1:
            score = 0.65   # pochissimi competitor
        elif hours < 3:
            score = 0.50
        elif hours < 6:
            score = 0.35
        elif hours < 12:
            score = 0.20
        elif hours < 24:
            score = 0.12
        else:
            score = 0.05   # già saturato

        if f.upwork_experience_level == "expert":
            score *= 0.7   # meno competitori qualificati, ma anche meno probabilità
        if f.issue_has_acceptance_criteria:
            score += 0.05
        if f.payout_usd > 500:
            score *= 0.85  # budget alto → più competitor seri

        return max(0.03, min(0.90, score))

    def _estimate_human_hours(self, f: BountyFeatures, is_upwork: bool) -> float:
        """Ore umane per supervisionare Claude Code su questo task."""
        base: dict[TaskType, float] = {
            TaskType.FIX_FAILING_TEST: 0.5,
            TaskType.FIX_BUG: 1.5,
            TaskType.ADD_FEATURE: 2.5,
            TaskType.REFACTOR: 3.0,
            TaskType.DOCUMENTATION: 0.75,
            TaskType.SECURITY: 2.0,
            TaskType.PERFORMANCE: 2.0,
            TaskType.UNKNOWN: 2.0,
        }
        hours = base.get(f.task_type, 2.0)

        if is_upwork:
            # Upwork: aggiungi ~30min per bid + comunicazione cliente
            hours += 0.5
            if f.upwork_experience_level == "expert":
                hours *= 1.5
        else:
            # GitHub
            if f.repo_size_kb > 50_000:
                hours *= 1.5
            if not f.repo_has_tests:
                hours *= 1.3
            if not f.repo_has_docker and not f.repo_has_devcontainer:
                hours += 0.25
            if f.issue_age_days > 30:
                hours *= 1.2

        return round(max(0.1, hours), 2)

    def _calc_confidence(self, f: BountyFeatures, is_upwork: bool = False) -> float:
        if is_upwork:
            checks = [
                f.payout_usd > 0,
                f.task_type != TaskType.UNKNOWN,
                f.issue_body_length > 100,
                f.upwork_job_type != "",
                f.upwork_posted_hours_ago < 999,
            ]
        else:
            checks = [
                f.repo_stars > 0,
                f.task_type != TaskType.UNKNOWN,
                f.issue_body_length > 100,
                f.maintainer_response_p50_days != 7.0,
                f.payout_usd > 0,
            ]
        return round(sum(checks) / len(checks), 2)

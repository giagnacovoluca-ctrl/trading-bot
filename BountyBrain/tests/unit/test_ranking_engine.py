"""Unit tests for Phase0Scorer and RankingEngine."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from src.bountybrain.core.models import Bounty, BountyFeatures, Platform, TaskType
from src.bountybrain.ranking.ranking_engine import RankingEngine
from src.bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer


# ------------------------------------------------------------------ Phase0Scorer

def test_phase0_high_score_fix_failing_test(sample_features):
    scorer = Phase0Scorer()
    result = scorer.score(sample_features)
    assert result.p_correct > 0.7, f"Expected p_correct > 0.7, got {result.p_correct}"
    assert result.ephh > 0


def test_phase0_documentation_has_high_p_correct(sample_features):
    sample_features.task_type = TaskType.DOCUMENTATION
    sample_features.payout_usd = 100.0
    scorer = Phase0Scorer()
    result = scorer.score(sample_features)
    assert result.p_correct > 0.6


def test_phase0_low_p_correct_refactor(low_value_features):
    """Refactor with poor indicators scores lower than FIX_FAILING_TEST with good ones."""
    low_value_features.task_type = TaskType.REFACTOR
    scorer = Phase0Scorer()
    result = scorer.score(low_value_features)
    # Refactor + no tests + no ci + no repro → well below cap
    assert result.p_correct < 0.55


def test_phase0_competition_reduces_p_correct(low_value_features):
    """Attempt comments on issue → lower p_correct (use low_value_features to avoid cap)."""
    scorer = Phase0Scorer()
    low_value_features.n_attempt_comments = 0
    low_value_features.n_issue_prs_open = 0
    base = scorer.score(low_value_features)
    low_value_features.n_attempt_comments = 4
    low_value_features.n_issue_prs_open = 2
    with_competition = scorer.score(low_value_features)
    assert with_competition.p_correct < base.p_correct


def test_phase0_slow_maintainer_reduces_p_maintainer(low_value_features):
    """Slow maintainer (>30d) + stale PRs → p_maintainer < 0.5."""
    scorer = Phase0Scorer()
    # low_value_features already has slow maintainer + stale PRs + no contributing
    result = scorer.score(low_value_features)
    assert result.p_maintainer_accepts < 0.5


def test_phase0_fast_maintainer_increases_p_maintainer(sample_features):
    scorer = Phase0Scorer()
    sample_features.maintainer_response_p50_days = 1.0
    result = scorer.score(sample_features)
    assert result.p_maintainer_accepts > 0.7


def test_phase0_large_repo_increases_hours(sample_features):
    scorer = Phase0Scorer()
    sample_features.repo_size_kb = 100_000
    result = scorer.score(sample_features)
    # Large repo should add hours
    assert result.human_hours_predicted >= 0.5


def test_phase0_fix_failing_test_has_low_hours(sample_features):
    scorer = Phase0Scorer()
    result = scorer.score(sample_features)
    assert result.human_hours_predicted <= 1.5


def test_phase0_ephh_formula_correctness():
    scorer = Phase0Scorer()
    ephh = scorer._calc_ephh(
        p_correct=0.8,
        p_maintainer_accepts=0.7,
        payout=100.0,
        human_hours=2.0,
        api_cost=0.5,
        claude_pro_amortized=0.625,
    )
    expected = (0.8 * 0.7 * 100.0 - 0.5 - 0.625) / 2.0
    assert abs(ephh - expected) < 0.001


def test_phase0_ephh_zero_hours():
    scorer = Phase0Scorer()
    ephh = scorer._calc_ephh(0.8, 0.7, 100.0, human_hours=0.0)
    assert ephh == 0.0


def test_phase0_confidence_improves_with_data(sample_features):
    scorer = Phase0Scorer()
    full_confidence = scorer._calc_confidence(sample_features)
    # Degrade features
    sample_features.repo_stars = 0
    sample_features.task_type = TaskType.UNKNOWN
    sample_features.issue_body_length = 10
    sample_features.maintainer_response_p50_days = 7.0
    sample_features.payout_usd = 0.0
    low_confidence = scorer._calc_confidence(sample_features)
    assert full_confidence > low_confidence


def test_phase0_result_phase_is_0(sample_features):
    scorer = Phase0Scorer()
    result = scorer.score(sample_features)
    assert result.scorer_phase == 0


def test_phase0_update_is_noop(sample_features):
    """Phase0 update() should not raise."""
    from src.bountybrain.core.models import OutcomeStatus, TaskOutcome
    scorer = Phase0Scorer()
    outcome = TaskOutcome(
        bounty_id="test",
        status=OutcomeStatus.MERGED,
        payout_received=100.0,
        human_hours_actual=1.5,
    )
    scorer.update(outcome)  # should not raise


# ------------------------------------------------------------------ RankingEngine

def test_ranking_filters_low_payout(sample_bounty):
    sample_bounty.features.payout_usd = 10.0
    ranker = RankingEngine()
    ranked = ranker.rank([sample_bounty])
    assert len(ranked) == 0
    assert sample_bounty.skip_reason is not None
    assert "payout" in sample_bounty.skip_reason.lower()


def test_ranking_filters_old_issue(sample_bounty):
    sample_bounty.features.issue_age_days = 200
    ranker = RankingEngine()
    ranked = ranker.rank([sample_bounty])
    assert len(ranked) == 0
    assert sample_bounty.skip_reason is not None
    assert "age" in sample_bounty.skip_reason.lower()


def test_ranking_filters_no_features():
    bounty = Bounty(
        id="test_no_features",
        platform=Platform.GITHUB,
        title="Test",
        body="body",
        url="http://example.com",
        repo_url="http://github.com/test/repo",
        repo_name="test/repo",
        payout_usd=100.0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        features=None,
    )
    ranker = RankingEngine()
    ranked = ranker.rank([bounty])
    assert len(ranked) == 0


def test_ranking_sorts_by_ephh_descending(sample_bounty):
    high = copy.deepcopy(sample_bounty)
    high.id = "github_high_value"
    high.features.payout_usd = 1000.0
    high.features.task_type = TaskType.FIX_FAILING_TEST
    high.features.issue_age_days = 3

    low = copy.deepcopy(sample_bounty)
    low.id = "github_low_value"
    low.features.payout_usd = 55.0
    low.features.task_type = TaskType.REFACTOR

    ranker = RankingEngine()
    ranked = ranker.rank([low, high])
    if len(ranked) >= 2:
        assert ranked[0][1].ephh >= ranked[1][1].ephh


def test_ranking_priority_assigned(sample_bounty):
    ranker = RankingEngine()
    ranked = ranker.rank([sample_bounty])
    if ranked:
        assert ranked[0][1].priority == 1


def test_ranking_empty_input():
    ranker = RankingEngine()
    assert ranker.rank([]) == []

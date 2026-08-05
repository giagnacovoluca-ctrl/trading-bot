"""Integration tests: full pipeline without HTTP calls (mocked adapters)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bountybrain.core.models import Bounty, BountyFeatures, OutcomeStatus, Platform, TaskType
from src.bountybrain.knowledge.knowledge_base import KnowledgeBase
from src.bountybrain.learning.learning_engine import LearningEngine
from src.bountybrain.ranking.ranking_engine import RankingEngine
from src.bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer


def make_bounty(bounty_id: str, payout: float = 100.0,
                task_type: TaskType = TaskType.FIX_BUG) -> Bounty:
    features = BountyFeatures(
        issue_age_days=5,
        issue_has_reproduction_steps=True,
        issue_has_acceptance_criteria=True,
        issue_body_length=300,
        task_type=task_type,
        repo_stars=500,
        repo_has_tests=True,
        repo_has_ci=True,
        payout_usd=payout,
        maintainer_response_p50_days=2.0,
        n_open_prs=1,
    )
    return Bounty(
        id=bounty_id,
        platform=Platform.GITHUB,
        title=f"Test bounty {bounty_id}",
        body="Steps to reproduce:\n1. Do the thing\n\nExpected behavior: it works.",
        url=f"https://github.com/test/repo/issues/{bounty_id}",
        repo_url="https://github.com/test/repo",
        repo_name="test/repo",
        payout_usd=payout,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        features=features,
    )


def test_ranking_pipeline_end_to_end():
    """Full rank + filter pipeline produces sorted results."""
    bounties = [
        make_bounty("high", payout=300.0, task_type=TaskType.FIX_FAILING_TEST),
        make_bounty("mid", payout=150.0, task_type=TaskType.FIX_BUG),
        make_bounty("low", payout=100.0, task_type=TaskType.ADD_FEATURE),
    ]
    ranker = RankingEngine()
    ranked = ranker.rank(bounties)

    # All should pass filters (payout > 50, age < 60, EPHH above threshold)
    assert len(ranked) >= 2
    # Should be sorted by EPHH descending
    for i in range(len(ranked) - 1):
        assert ranked[i][1].ephh >= ranked[i + 1][1].ephh
    # Priority should start at 1
    assert ranked[0][1].priority == 1


def test_pipeline_with_learning(tmp_path):
    """Rank → record outcome → stats update."""
    kb = KnowledgeBase(storage_dir=str(tmp_path / "datasets"))
    scorer = Phase0Scorer()
    learning = LearningEngine(kb, scorer)
    ranker = RankingEngine()

    bounties = [make_bounty("b1", payout=200.0)]
    ranked = ranker.rank(bounties)
    assert len(ranked) == 1

    b, r = ranked[0]
    from src.bountybrain.core.models import TaskOutcome
    outcome = TaskOutcome(
        bounty_id=b.id,
        status=OutcomeStatus.MERGED,
        payout_received=200.0,
        human_hours_actual=1.5,
        ephh_actual=r.ephh,
    )
    learning.record_outcome(outcome)

    stats = learning.get_stats()
    assert stats["total"] == 1
    assert stats["merged"] == 1
    assert stats["merge_rate"] == 1.0


def test_skipped_bounties_have_skip_reason():
    b1 = make_bounty("too_cheap", payout=20.0)
    b2 = make_bounty("too_old", payout=100.0)
    b2.features.issue_age_days = 200

    ranker = RankingEngine()
    ranked = ranker.rank([b1, b2])

    assert len(ranked) == 0
    assert b1.skip_reason is not None
    assert b2.skip_reason is not None


def test_dashboard_api_smoke():
    """FastAPI app returns 200 on /health without any setup."""
    from fastapi.testclient import TestClient
    from src.bountybrain.dashboard.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dashboard_stats_endpoint():
    """GET /api/stats returns expected keys."""
    from fastapi.testclient import TestClient
    from src.bountybrain.dashboard.app import app

    client = TestClient(app)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_bounties" in data
    assert "merge_rate_pct" in data
    assert "avg_ephh" in data

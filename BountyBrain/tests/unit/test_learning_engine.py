"""Unit tests for LearningEngine and KnowledgeBase."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.bountybrain.core.models import OutcomeStatus, TaskOutcome
from src.bountybrain.knowledge.knowledge_base import KnowledgeBase
from src.bountybrain.learning.learning_engine import LearningEngine
from src.bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer


@pytest.fixture
def temp_kb(tmp_path) -> KnowledgeBase:
    return KnowledgeBase(storage_dir=str(tmp_path / "datasets"))


@pytest.fixture
def learning_engine(temp_kb) -> LearningEngine:
    scorer = Phase0Scorer()
    return LearningEngine(temp_kb, scorer)


def _make_outcome(bounty_id: str, status: OutcomeStatus = OutcomeStatus.MERGED,
                  payout: float = 100.0, hours: float = 1.5,
                  ephh: float = 60.0) -> TaskOutcome:
    return TaskOutcome(
        bounty_id=bounty_id,
        status=status,
        payout_received=payout,
        human_hours_actual=hours,
        ephh_actual=ephh,
        n_prompts=5,
        completed_at=datetime.utcnow(),
    )


# ------------------------------------------------------------------ KnowledgeBase

def test_kb_record_bounty_persists(temp_kb, sample_bounty):
    temp_kb.record_bounty(sample_bounty)
    loaded = temp_kb.load_bounties()
    assert len(loaded) == 1
    assert loaded[0]["bounty_id"] == sample_bounty.id


def test_kb_record_outcome_persists(temp_kb):
    outcome = _make_outcome("test_123")
    temp_kb.record_outcome(outcome)
    loaded = temp_kb.load_outcomes()
    assert len(loaded) == 1
    assert loaded[0]["bounty_id"] == "test_123"


def test_kb_multiple_outcomes(temp_kb):
    for i in range(5):
        temp_kb.record_outcome(_make_outcome(f"bounty_{i}"))
    assert len(temp_kb.load_outcomes()) == 5


def test_kb_get_outcome_found(temp_kb):
    temp_kb.record_outcome(_make_outcome("my_bounty"))
    result = temp_kb.get_outcome("my_bounty")
    assert result is not None
    assert result["bounty_id"] == "my_bounty"


def test_kb_get_outcome_not_found(temp_kb):
    result = temp_kb.get_outcome("nonexistent")
    assert result is None


def test_kb_stats_empty(temp_kb):
    stats = temp_kb.stats()
    assert stats["total_outcomes"] == 0
    assert stats["merge_rate"] == 0.0


def test_kb_stats_with_outcomes(temp_kb):
    temp_kb.record_outcome(_make_outcome("a", OutcomeStatus.MERGED, payout=100, ephh=60))
    temp_kb.record_outcome(_make_outcome("b", OutcomeStatus.REJECTED))
    stats = temp_kb.stats()
    assert stats["total_outcomes"] == 2
    assert stats["merged"] == 1
    assert stats["merge_rate"] == 0.5
    assert stats["avg_ephh"] == 60.0


def test_kb_record_pattern(temp_kb):
    temp_kb.record_pattern({"pattern_type": "fix_test", "description": "Test fix pattern"})
    patterns = temp_kb.load_patterns()
    assert len(patterns) == 1
    assert patterns[0]["pattern_type"] == "fix_test"
    assert "saved_at" in patterns[0]


# ------------------------------------------------------------------ LearningEngine

def test_learning_record_outcome(learning_engine, temp_kb):
    outcome = _make_outcome("bounty_1")
    learning_engine.record_outcome(outcome)
    assert len(temp_kb.load_outcomes()) == 1


def test_learning_stats_empty(learning_engine):
    stats = learning_engine.get_stats()
    assert stats["total"] == 0
    assert stats["merged"] == 0
    assert stats["merge_rate"] == 0.0
    assert stats["avg_ephh"] == 0.0
    assert stats["scorer_phase"] == 0


def test_learning_stats_with_merged(learning_engine):
    learning_engine.record_outcome(_make_outcome("a", OutcomeStatus.MERGED, ephh=80.0))
    learning_engine.record_outcome(_make_outcome("b", OutcomeStatus.MERGED, ephh=40.0))
    stats = learning_engine.get_stats()
    assert stats["merged"] == 2
    assert stats["total"] == 2
    assert stats["merge_rate"] == 1.0
    assert abs(stats["avg_ephh"] - 60.0) < 0.01


def test_learning_stats_rejected_counted(learning_engine):
    learning_engine.record_outcome(_make_outcome("a", OutcomeStatus.MERGED))
    learning_engine.record_outcome(_make_outcome("b", OutcomeStatus.REJECTED))
    stats = learning_engine.get_stats()
    assert stats["rejected"] == 1
    assert stats["total"] == 2


def test_learning_next_phase_info(learning_engine):
    stats = learning_engine.get_stats()
    # Phase 0 → next phase at min_samples_phase1 (default 80)
    assert stats["next_phase_at"] is not None
    assert stats["next_phase_at"] > 0

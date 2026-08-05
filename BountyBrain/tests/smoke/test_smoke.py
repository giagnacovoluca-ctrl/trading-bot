"""Smoke tests: verify all modules import and basic instantiation works."""
from __future__ import annotations


def test_imports_core():
    from src.bountybrain.core.models import (
        Bounty, BountyFeatures, OutcomeStatus, Platform, QualitativeScores,
        RankingResult, TaskOutcome, TaskType,
    )


def test_imports_config():
    from src.bountybrain.config.settings import Settings, get_settings, reset_settings


def test_imports_scout():
    from src.bountybrain.scout.algora_adapter import AlgoraAdapter
    from src.bountybrain.scout.collector import BountyCollector
    from src.bountybrain.scout.github_adapter import GitHubAdapter


def test_imports_extractor():
    from src.bountybrain.extractor.feature_extractor import FeatureExtractor


def test_imports_ranking():
    from src.bountybrain.ranking.ranking_engine import RankingEngine
    from src.bountybrain.ranking.scorers.base_scorer import BaseScorerMixin
    from src.bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer
    from src.bountybrain.ranking.scorers.phase1_scorer import Phase1Scorer
    from src.bountybrain.ranking.scorers.phase2_scorer import Phase2Scorer


def test_imports_knowledge():
    from src.bountybrain.knowledge.knowledge_base import KnowledgeBase
    from src.bountybrain.knowledge.similarity_engine import SimilarityEngine


def test_imports_learning():
    from src.bountybrain.learning.learning_engine import LearningEngine


def test_imports_environment():
    from src.bountybrain.environment.environment_builder import EnvironmentBuilder


def test_imports_context():
    from src.bountybrain.context.context_builder import ContextBuilder


def test_imports_analyzer():
    from src.bountybrain.analyzer.qualitative_analyzer import QualitativeAnalyzer


def test_imports_dashboard():
    from src.bountybrain.dashboard.app import app


def test_phase0_scorer_instantiation_and_run(sample_features):
    from src.bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer
    scorer = Phase0Scorer()
    assert scorer.phase == 0
    result = scorer.score(sample_features)
    assert result.ephh is not None
    assert 0 <= result.p_correct <= 1
    assert 0 <= result.p_maintainer_accepts <= 1
    assert result.human_hours_predicted > 0
    assert result.scorer_phase == 0


def test_phase1_falls_back_to_phase0(sample_features):
    from src.bountybrain.ranking.scorers.phase1_scorer import Phase1Scorer
    scorer = Phase1Scorer()
    assert scorer.phase == 1
    result = scorer.score(sample_features)
    assert result.ephh is not None
    assert result.scorer_phase == 1  # always marked as phase 1


def test_settings_load_without_env_file():
    from src.bountybrain.config.settings import Settings
    s = Settings()
    assert s is not None
    assert s.env in ("development", "production", "test")


def test_knowledge_base_instantiation(tmp_path):
    from src.bountybrain.knowledge.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(storage_dir=str(tmp_path))
    assert kb.load_bounties() == []
    assert kb.load_outcomes() == []
    assert kb.load_patterns() == []


def test_ranking_engine_instantiation():
    from src.bountybrain.ranking.ranking_engine import RankingEngine
    ranker = RankingEngine()
    assert ranker.scorer.phase == 0


def test_version():
    from src.bountybrain import __version__
    assert __version__ == "0.1.0"

"""Shared pytest fixtures."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.bountybrain.core.models import (
    Bounty,
    BountyFeatures,
    Platform,
    TaskType,
)


@pytest.fixture
def sample_features() -> BountyFeatures:
    return BountyFeatures(
        issue_age_days=10,
        issue_comment_count=3,
        issue_has_reproduction_steps=True,
        issue_has_acceptance_criteria=True,
        issue_body_length=500,
        task_type=TaskType.FIX_FAILING_TEST,
        repo_stars=1200,
        repo_forks=80,
        repo_size_kb=4500,
        repo_language="Python",
        repo_has_tests=True,
        repo_has_ci=True,
        repo_has_docker=False,
        repo_has_contributing=True,
        repo_has_devcontainer=False,
        repo_contributor_count=25,
        payout_usd=100.0,
        maintainer_response_p50_days=2.0,
        maintainer_merge_rate=0.75,
        n_open_prs=1,
        n_stale_prs=0,
    )


@pytest.fixture
def sample_bounty(sample_features: BountyFeatures) -> Bounty:
    return Bounty(
        id="github_test_123",
        platform=Platform.GITHUB,
        title="Fix failing test in auth module",
        body=(
            "Steps to reproduce:\n"
            "1. Run `pytest tests/test_auth.py`\n"
            "2. Observe failing assertion\n\n"
            "Expected behavior: all tests pass."
        ),
        url="https://github.com/test/repo/issues/1",
        repo_url="https://github.com/test/repo",
        repo_name="test/repo",
        payout_usd=100.0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        features=sample_features,
    )


@pytest.fixture
def high_value_features() -> BountyFeatures:
    """Features for a high-EPHH bounty."""
    return BountyFeatures(
        issue_age_days=5,
        issue_has_reproduction_steps=True,
        issue_has_acceptance_criteria=True,
        issue_body_length=800,
        task_type=TaskType.FIX_FAILING_TEST,
        repo_stars=5000,
        repo_has_tests=True,
        repo_has_ci=True,
        payout_usd=500.0,
        maintainer_response_p50_days=1.0,
        n_open_prs=0,
        n_stale_prs=0,
    )


@pytest.fixture
def low_value_features() -> BountyFeatures:
    """Features for a low-EPHH bounty (old issue, poor docs, refactor)."""
    return BountyFeatures(
        issue_age_days=55,
        issue_has_reproduction_steps=False,
        issue_has_acceptance_criteria=False,
        issue_body_length=30,
        task_type=TaskType.REFACTOR,
        repo_stars=10,
        repo_has_tests=False,
        repo_has_ci=False,
        payout_usd=60.0,
        maintainer_response_p50_days=45.0,
        n_open_prs=8,
        n_stale_prs=6,
    )

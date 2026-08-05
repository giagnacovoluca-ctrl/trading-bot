"""Unit tests for FeatureExtractor (no HTTP calls)."""
from __future__ import annotations

import pytest

from src.bountybrain.core.models import TaskType
from src.bountybrain.extractor.feature_extractor import FeatureExtractor


@pytest.fixture
def extractor() -> FeatureExtractor:
    return FeatureExtractor()


# ------------------------------------------------------------------ task type

def test_classify_fix_failing_test(extractor):
    result = extractor._classify_task_type("Fix failing test in auth module - tests are failing")
    assert result == TaskType.FIX_FAILING_TEST


def test_classify_bug(extractor):
    result = extractor._classify_task_type("Bug: login crashes on empty password")
    assert result == TaskType.FIX_BUG


def test_classify_feature(extractor):
    result = extractor._classify_task_type("Add support for dark mode in the UI")
    assert result == TaskType.ADD_FEATURE


def test_classify_refactor(extractor):
    result = extractor._classify_task_type("Refactor database connection pooling")
    assert result == TaskType.REFACTOR


def test_classify_documentation(extractor):
    result = extractor._classify_task_type("Update README with new docs for API v2")
    assert result == TaskType.DOCUMENTATION


def test_classify_security(extractor):
    result = extractor._classify_task_type("Fix XSS vulnerability in user input sanitization")
    assert result == TaskType.SECURITY


def test_classify_performance(extractor):
    result = extractor._classify_task_type("Optimize slow database query on the users table")
    assert result == TaskType.PERFORMANCE


def test_classify_unknown(extractor):
    result = extractor._classify_task_type("Something completely unrelated")
    assert result == TaskType.UNKNOWN


# ------------------------------------------------------------------ repro steps

def test_has_repro_with_steps(extractor):
    text = "Steps to reproduce:\n1. Click login\n2. Enter empty password\n3. Submit"
    assert extractor._has_repro(text) is True


def test_has_repro_with_backtick(extractor):
    text = "Run this command:\n```\npytest tests/\n```"
    assert extractor._has_repro(text) is True


def test_has_repro_false(extractor):
    text = "The button color is wrong. It should be blue."
    assert extractor._has_repro(text) is False


def test_has_repro_to_reproduce(extractor):
    text = "To reproduce the issue, follow these steps..."
    assert extractor._has_repro(text) is True


# ------------------------------------------------------------------ acceptance criteria

def test_has_acceptance_criteria_expected_behavior(extractor):
    text = "Expected behavior: the modal closes after clicking OK."
    assert extractor._has_acceptance_criteria(text) is True


def test_has_acceptance_criteria_section(extractor):
    text = "## Expected\n- Returns 200 OK\n- Body contains user object"
    assert extractor._has_acceptance_criteria(text) is True


def test_has_acceptance_criteria_should(extractor):
    text = "The function should: return a list of items sorted by date"
    assert extractor._has_acceptance_criteria(text) is True


def test_has_acceptance_criteria_false(extractor):
    text = "Just fix the thing, you know what to do."
    assert extractor._has_acceptance_criteria(text) is False


# ------------------------------------------------------------------ payout extraction

def test_extract_payout_dollar_sign(extractor):
    assert extractor._extract_payout("Bounty: $150 for fixing this bug") == 150.0


def test_extract_payout_usd_suffix(extractor):
    assert extractor._extract_payout("Reward: 200 USD") == 200.0


def test_extract_payout_bounty_keyword(extractor):
    assert extractor._extract_payout("bounty: $75") == 75.0


def test_extract_payout_with_comma(extractor):
    assert extractor._extract_payout("Prize: $1,500 USD") == 1500.0


def test_extract_payout_no_payout(extractor):
    assert extractor._extract_payout("No payout mentioned here") == 0.0


def test_extract_payout_decimal(extractor):
    assert extractor._extract_payout("$99.99 reward") == 99.99

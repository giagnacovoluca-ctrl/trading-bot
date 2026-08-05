"""Pydantic v2 domain models for BountyBrain."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    FIX_FAILING_TEST = "fix_failing_test"
    ADD_FEATURE = "add_feature"
    FIX_BUG = "fix_bug"
    REFACTOR = "refactor"
    DOCUMENTATION = "documentation"
    SECURITY = "security"
    PERFORMANCE = "performance"
    UNKNOWN = "unknown"


class Platform(str, Enum):
    GITHUB = "github"
    ALGORA = "algora"
    OPIRE = "opire"
    UPWORK = "upwork"


class OutcomeStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    MERGED = "merged"
    REJECTED = "rejected"
    ABANDONED = "abandoned"
    SKIPPED = "skipped"


class BountyFeatures(BaseModel):
    """Deterministic features extracted without LLM."""

    # Issue features
    issue_age_days: float = 0.0
    issue_comment_count: int = 0
    issue_has_reproduction_steps: bool = False
    issue_has_acceptance_criteria: bool = False
    issue_body_length: int = 0
    task_type: TaskType = TaskType.UNKNOWN

    # Repository features
    repo_stars: int = 0
    repo_forks: int = 0
    repo_size_kb: int = 0
    repo_language: str = ""
    repo_has_tests: bool = False
    repo_has_ci: bool = False
    repo_has_docker: bool = False
    repo_has_contributing: bool = False
    repo_has_devcontainer: bool = False
    repo_dependency_count: int = 0
    repo_contributor_count: int = 0
    repo_commit_frequency_30d: float = 0.0
    repo_open_issues_count: int = 0

    # Maintainer features
    maintainer_response_p50_days: float = 7.0
    maintainer_merge_rate: float = 0.5
    maintainer_avg_review_rounds: float = 1.5

    # Competition features
    n_open_prs: int = 0
    n_stale_prs: int = 0
    n_issue_prs_open: int = 0       # PR aperte che referenziano questa issue
    n_issue_prs_closed: int = 0     # PR chiuse/rifiutate su questa issue
    n_attempt_comments: int = 0     # commenti "/attempt" = bot che ci hanno provato
    has_merged_pr: bool = False     # qualcuno ha già risolto questa issue

    # Upwork-specific (vuoti per GitHub)
    upwork_job_type: str = ""           # "fixed" | "hourly"
    upwork_posted_hours_ago: float = 999.0
    upwork_experience_level: str = ""   # "entry" | "intermediate" | "expert"
    upwork_skills: list[str] = Field(default_factory=list)

    # Payout
    payout_usd: float = 0.0


class QualitativeScores(BaseModel):
    """LLM-generated qualitative assessment."""

    ai_score: float = Field(ge=0, le=100)
    business_score: float = Field(ge=0, le=100)
    ambiguity_risk: float = Field(ge=0, le=1)
    hidden_complexity_risk: float = Field(ge=0, le=1)
    perceived_complexity: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    reasoning: str = ""


class RankingResult(BaseModel):
    bounty_id: str
    ephh: float
    business_score: float
    human_hours_predicted: float
    p_correct: float
    p_maintainer_accepts: float
    expected_profit_usd: float
    confidence: float
    scorer_phase: int
    priority: int = 0


class Bounty(BaseModel):
    id: str
    platform: Platform
    title: str
    body: str
    url: str
    repo_url: str
    repo_name: str
    payout_usd: float
    currency: str = "USD"
    labels: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    author: str = ""
    features: Optional[BountyFeatures] = None
    qualitative: Optional[QualitativeScores] = None
    ranking: Optional[RankingResult] = None
    outcome: OutcomeStatus = OutcomeStatus.PENDING
    skip_reason: Optional[str] = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class TaskOutcome(BaseModel):
    """Logged after task completion for Learning Engine."""

    bounty_id: str
    status: OutcomeStatus
    payout_received: float = 0.0
    human_hours_actual: float = 0.0
    claude_hours_actual: float = 0.0
    n_prompts: int = 0
    n_human_interventions: int = 0
    n_test_failures_before_merge: int = 0
    n_review_rounds: int = 0
    api_cost_usd: float = 0.0
    ephh_actual: float = 0.0
    effective_first_prompt: str = ""
    bootstrap_commands: list[str] = Field(default_factory=list)
    failure_patterns: list[str] = Field(default_factory=list)
    maintainer_comments: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=datetime.utcnow)

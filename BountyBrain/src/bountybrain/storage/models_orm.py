"""SQLAlchemy ORM models corresponding to Pydantic domain models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class BountyRecord(Base):
    """Persists every bounty seen by BountyBrain."""

    __tablename__ = "bounties"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text)
    repo_url: Mapped[str] = mapped_column(Text, default="")
    repo_name: Mapped[str] = mapped_column(String(256), default="")
    payout_usd: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    labels: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    author: Mapped[str] = mapped_column(String(128), default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    qualitative: Mapped[dict] = mapped_column(JSON, default=dict)
    ranking: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(32), default="pending")
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskOutcomeRecord(Base):
    """Stores completed task outcomes for learning."""

    __tablename__ = "task_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bounty_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    payout_received: Mapped[float] = mapped_column(Float, default=0.0)
    human_hours_actual: Mapped[float] = mapped_column(Float, default=0.0)
    claude_hours_actual: Mapped[float] = mapped_column(Float, default=0.0)
    n_prompts: Mapped[int] = mapped_column(Integer, default=0)
    n_human_interventions: Mapped[int] = mapped_column(Integer, default=0)
    n_test_failures_before_merge: Mapped[int] = mapped_column(Integer, default=0)
    n_review_rounds: Mapped[int] = mapped_column(Integer, default=0)
    api_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ephh_actual: Mapped[float] = mapped_column(Float, default=0.0)
    effective_first_prompt: Mapped[str] = mapped_column(Text, default="")
    bootstrap_commands: Mapped[list] = mapped_column(JSON, default=list)
    failure_patterns: Mapped[list] = mapped_column(JSON, default=list)
    maintainer_comments: Mapped[list] = mapped_column(JSON, default=list)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PatternRecord(Base):
    """Reusable resolution patterns for the Similarity Engine."""

    __tablename__ = "patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern_type: Mapped[str] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(64), default="unknown")
    repo_language: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    bootstrap_commands: Mapped[list] = mapped_column(JSON, default=list)
    solution_approach: Mapped[str] = mapped_column(Text, default="")
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

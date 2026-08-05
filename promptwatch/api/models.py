import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, Boolean, DateTime, ForeignKey, JSON, Text, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
import enum

from database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Plan(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    GROWTH = "growth"
    SCALE = "scale"


PLAN_LIMITS = {
    Plan.FREE:    {"projects": 1, "tokens_per_month": 100_000},
    Plan.STARTER: {"projects": 1, "tokens_per_month": 1_000_000},
    Plan.GROWTH:  {"projects": 10, "tokens_per_month": 10_000_000},
    Plan.SCALE:   {"projects": -1, "tokens_per_month": -1},
}


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    plan: Mapped[Plan] = mapped_column(Enum(Plan), default=Plan.FREE)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="org")
    projects: Mapped[list["Project"]] = relationship(back_populates="org")
    alert_rules: Mapped[list["AlertRule"]] = relationship(back_populates="org")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="users")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="projects")
    events: Mapped[list["LLMEvent"]] = relationship(back_populates="project")


class LLMEvent(Base):
    """Single LLM call recorded by the SDK."""
    __tablename__ = "llm_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)

    # Provider info
    provider: Mapped[str] = mapped_column(String(50))   # openai, anthropic, google, etc.
    model: Mapped[str] = mapped_column(String(100))
    endpoint: Mapped[str] = mapped_column(String(50), default="chat")  # chat, completion, embedding

    # Tokens & cost
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Attribution
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    feature: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Performance
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)

    # Raw (optional, stored only on Growth+)
    metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    project: Mapped[Project] = relationship(back_populates="events")


class DailyAggregate(Base):
    """Pre-aggregated daily stats per project for fast dashboard queries."""
    __tablename__ = "daily_aggregates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    model: Mapped[str] = mapped_column(String(100))

    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    name: Mapped[str] = mapped_column(String(255))
    metric: Mapped[str] = mapped_column(String(50))   # cost_usd, tokens, error_rate
    operator: Mapped[str] = mapped_column(String(10))  # gt, lt, gte, lte
    threshold: Mapped[float] = mapped_column(Float)
    window: Mapped[str] = mapped_column(String(20), default="1h")  # 1h, 24h, 7d, 30d
    notify_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notify_webhook: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="alert_rules")

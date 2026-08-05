"""SQLAlchemy engine and session factory."""
from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config.settings import get_settings


class Base(DeclarativeBase):
    pass


def create_db_engine(url: str | None = None):
    settings = get_settings()
    db_url = url or settings.db_url
    kwargs: dict = {}
    if db_url.startswith("sqlite"):
        kwargs = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    engine = create_engine(db_url, **kwargs)
    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_wal(dbapi_conn, conn_record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
    return engine


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal()


def init_db() -> None:
    """Create all tables."""
    from .models_orm import BountyRecord, TaskOutcomeRecord, PatternRecord  # noqa: F401
    Base.metadata.create_all(get_engine())

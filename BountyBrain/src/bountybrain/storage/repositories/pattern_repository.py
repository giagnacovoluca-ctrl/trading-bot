"""Repository for PatternRecord ORM model."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models_orm import PatternRecord
from .base_repository import BaseRepository


class PatternRepository(BaseRepository[PatternRecord]):
    def __init__(self, session: Session | None = None):
        super().__init__(PatternRecord, session)

    def find_by_task_type(self, task_type: str) -> list[PatternRecord]:
        return (
            self._session.query(PatternRecord)
            .filter(PatternRecord.task_type == task_type)
            .order_by(PatternRecord.success_rate.desc())
            .all()
        )

    def find_by_language(self, language: str) -> list[PatternRecord]:
        return (
            self._session.query(PatternRecord)
            .filter(PatternRecord.repo_language == language)
            .all()
        )

    def top_patterns(self, n: int = 10) -> list[PatternRecord]:
        return (
            self._session.query(PatternRecord)
            .order_by(PatternRecord.success_rate.desc(), PatternRecord.usage_count.desc())
            .limit(n)
            .all()
        )

"""Repository for BountyRecord ORM model."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models_orm import BountyRecord
from .base_repository import BaseRepository


class BountyRepository(BaseRepository[BountyRecord]):
    def __init__(self, session: Session | None = None):
        super().__init__(BountyRecord, session)

    def find_by_platform(self, platform: str) -> list[BountyRecord]:
        return (
            self._session.query(BountyRecord)
            .filter(BountyRecord.platform == platform)
            .all()
        )

    def find_unprocessed(self) -> list[BountyRecord]:
        """Return bounties with outcome == 'pending'."""
        return (
            self._session.query(BountyRecord)
            .filter(BountyRecord.outcome == "pending")
            .all()
        )

    def upsert(self, record: BountyRecord) -> BountyRecord:
        existing = self.find_by_id(record.id)
        if existing:
            for col in BountyRecord.__table__.columns:
                if col.name != "id":
                    setattr(existing, col.name, getattr(record, col.name))
            self._session.commit()
            return existing
        return self.save(record)

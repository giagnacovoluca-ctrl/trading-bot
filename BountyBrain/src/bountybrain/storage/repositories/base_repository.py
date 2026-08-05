"""Base repository providing common CRUD operations."""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from ..database import get_session
from ...core.interfaces import RepositoryBase

T = TypeVar("T")


class BaseRepository(RepositoryBase, Generic[T]):
    """Generic SQLAlchemy repository."""

    def __init__(self, model_class: type[T], session: Session | None = None):
        self._model = model_class
        self._session: Session = session or get_session()

    def save(self, entity: T) -> T:
        self._session.add(entity)
        self._session.commit()
        self._session.refresh(entity)
        return entity

    def find_by_id(self, id: Any) -> T | None:
        return self._session.get(self._model, id)

    def find_all(self) -> list[T]:
        return list(self._session.query(self._model).all())

    def delete(self, entity: T) -> None:
        self._session.delete(entity)
        self._session.commit()

    def close(self) -> None:
        self._session.close()

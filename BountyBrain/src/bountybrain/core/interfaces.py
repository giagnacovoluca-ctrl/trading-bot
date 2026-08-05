"""Abstract base interfaces for BountyBrain components."""
from abc import ABC, abstractmethod

from .models import Bounty, BountyFeatures, RankingResult, TaskOutcome


class BountyAdapter(ABC):
    """Base interface for platform adapters (Scout)."""

    @abstractmethod
    async def fetch_bounties(self) -> list[Bounty]: ...

    @abstractmethod
    async def fetch_bounty_detail(self, bounty_id: str) -> Bounty: ...


class FeatureExtractorBase(ABC):
    @abstractmethod
    async def extract(self, bounty: Bounty) -> BountyFeatures: ...


class ScorerBase(ABC):
    """Interface for ranking scorers — same interface across Phase 0/1/2."""

    @abstractmethod
    def score(self, features: BountyFeatures) -> RankingResult: ...

    @abstractmethod
    def update(self, outcome: TaskOutcome) -> None: ...

    @property
    @abstractmethod
    def phase(self) -> int: ...


class RepositoryBase(ABC):
    @abstractmethod
    def save(self, entity): ...

    @abstractmethod
    def find_by_id(self, id: str): ...

    @abstractmethod
    def find_all(self) -> list: ...

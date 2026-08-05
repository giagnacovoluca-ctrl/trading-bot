"""Custom exceptions for BountyBrain."""


class BountyBrainError(Exception):
    """Base exception for all BountyBrain errors."""


class AdapterError(BountyBrainError):
    """Raised when a platform adapter fails to fetch data."""


class FeatureExtractionError(BountyBrainError):
    """Raised when feature extraction fails."""


class ScoringError(BountyBrainError):
    """Raised when the ranking scorer encounters an error."""


class EnvironmentBuildError(BountyBrainError):
    """Raised when workspace/environment setup fails."""


class KnowledgeBaseError(BountyBrainError):
    """Raised when knowledge base read/write fails."""


class ConfigError(BountyBrainError):
    """Raised when configuration is invalid or missing."""

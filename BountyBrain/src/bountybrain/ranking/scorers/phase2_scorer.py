"""Phase 2 scorer — Gradient Boosting. Requires ≥300 samples.

TODO: Implement GradientBoostingRegressor scorer.
Identical interface to Phase0/Phase1.
Activate when n_samples >= 300 (config: ranking.min_samples_phase2).
Use sklearn GradientBoostingRegressor or XGBoost.
Features: same as Phase1 + qualitative scores from QualitativeAnalyzer.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from loguru import logger

from ...core.models import BountyFeatures, RankingResult, TaskOutcome
from .phase1_scorer import TASK_TYPE_ENCODING, Phase1Scorer


class Phase2Scorer(Phase1Scorer):
    """Gradient Boosting scorer. Falls back to Phase1 when not fitted."""

    MODEL_PATH = Path("storage/models/phase2_model.pkl")  # type: ignore[assignment]

    @property
    def phase(self) -> int:
        return 2

    def score(self, features: BountyFeatures) -> RankingResult:
        if not self._is_fitted:
            # Fall back to Phase1 until we have enough data
            from .phase1_scorer import Phase1Scorer as P1
            result = P1().score(features)
            result.scorer_phase = self.phase
            return result

        # TODO: override with GBM model prediction
        # Placeholder: use Phase1 logic
        result = super().score(features)
        result.scorer_phase = self.phase
        return result

    def train_gbm(self, X: list[list[float]], y: list[float]) -> None:
        """Fit GradientBoostingRegressor (requires scikit-learn >= 1.4)."""
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.preprocessing import StandardScaler

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        gbm = GradientBoostingRegressor(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            random_state=42,
        )
        gbm.fit(X_scaled, y)
        self._model = gbm
        self._scaler = scaler
        self._is_fitted = True
        self._save()
        logger.info(f"Phase2Scorer (GBM) trained on {len(X)} samples")

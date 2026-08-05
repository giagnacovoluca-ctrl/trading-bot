"""Phase 1 scorer — Ridge regression. Requires ≥80 samples."""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from loguru import logger
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from ...core.models import BountyFeatures, RankingResult, TaskOutcome, TaskType
from .base_scorer import BaseScorerMixin

FEATURE_NAMES = [
    "issue_age_days",
    "issue_comment_count",
    "issue_has_repro",
    "issue_has_ac",
    "repo_stars_log",
    "repo_size_kb_log",
    "repo_has_tests",
    "repo_has_ci",
    "repo_has_docker",
    "n_open_prs",
    "maintainer_p50",
    "payout_usd_log",
    "task_type_encoded",
]

TASK_TYPE_ENCODING: dict[TaskType, int] = {
    TaskType.FIX_FAILING_TEST: 5,
    TaskType.FIX_BUG: 4,
    TaskType.ADD_FEATURE: 3,
    TaskType.PERFORMANCE: 3,
    TaskType.SECURITY: 2,
    TaskType.DOCUMENTATION: 2,
    TaskType.REFACTOR: 1,
    TaskType.UNKNOWN: 0,
}


class Phase1Scorer(BaseScorerMixin):
    """Linear regression scorer. Falls back to Phase0 when not fitted."""

    MODEL_PATH = Path("storage/models/phase1_model.pkl")

    def __init__(self):
        self._model = Ridge(alpha=1.0)
        self._scaler = StandardScaler()
        self._is_fitted = False
        self._training_X: list[list[float]] = []
        self._training_y: list[float] = []
        self._load_if_exists()

    @property
    def phase(self) -> int:
        return 1

    def score(self, features: BountyFeatures) -> RankingResult:
        if not self._is_fitted:
            # Fall back to Phase0 until we have training data
            from .phase0_scorer import Phase0Scorer
            result = Phase0Scorer().score(features)
            result.scorer_phase = self.phase  # mark as phase1 even if using p0 rules
            return result

        X = self._featurize(features)
        X_scaled = self._scaler.transform([X])
        predicted_ephh = float(self._model.predict(X_scaled)[0])

        # Keep rule-based p_correct/p_maintainer as structural priors
        from .phase0_scorer import Phase0Scorer
        p0 = Phase0Scorer()
        p_correct = p0._estimate_p_correct(features)
        p_maintainer = p0._estimate_p_maintainer(features)
        human_hours = max(0.1, p0._estimate_human_hours(features))

        return self._build_result(
            bounty_id="",
            p_correct=p_correct,
            p_maintainer_accepts=p_maintainer,
            payout=features.payout_usd,
            human_hours=human_hours,
            business_score=min(100.0, max(0.0, predicted_ephh * 5)),
            confidence=0.7,
        )

    def update(self, outcome: TaskOutcome) -> None:
        """Add outcome sample — retrain if we now have enough data."""
        # TODO: retrieve features for this bounty_id from KB and add to training set
        # For now, this is a placeholder that tracks the count
        logger.debug(f"Phase1Scorer.update called for {outcome.bounty_id} (TODO: retrieve features)")

    def train(self, X: list[list[float]], y: list[float]) -> None:
        """Fit Ridge regression model."""
        X_scaled = self._scaler.fit_transform(X)
        self._model.fit(X_scaled, y)
        self._is_fitted = True
        self._save()
        logger.info(f"Phase1Scorer trained on {len(X)} samples")

    def _featurize(self, f: BountyFeatures) -> list[float]:
        return [
            f.issue_age_days,
            float(f.issue_comment_count),
            float(f.issue_has_reproduction_steps),
            float(f.issue_has_acceptance_criteria),
            float(np.log1p(f.repo_stars)),
            float(np.log1p(f.repo_size_kb)),
            float(f.repo_has_tests),
            float(f.repo_has_ci),
            float(f.repo_has_docker),
            float(f.n_open_prs),
            f.maintainer_response_p50_days,
            float(np.log1p(f.payout_usd)),
            float(TASK_TYPE_ENCODING.get(f.task_type, 0)),
        ]

    def _load_if_exists(self) -> None:
        if self.MODEL_PATH.exists():
            try:
                with open(self.MODEL_PATH, "rb") as f:
                    data = pickle.load(f)
                self._model = data["model"]
                self._scaler = data["scaler"]
                self._is_fitted = True
                logger.info("Phase1Scorer: loaded pre-trained model")
            except Exception as e:
                logger.warning(f"Phase1Scorer: failed to load model: {e}")

    def _save(self) -> None:
        self.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(self.MODEL_PATH, "wb") as f:
            pickle.dump({"model": self._model, "scaler": self._scaler}, f)

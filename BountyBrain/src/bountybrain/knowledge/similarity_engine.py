"""TF-IDF cosine similarity engine for finding similar past tasks.

Phase 0: bag-of-words TF-IDF.
TODO Phase 2: replace with sentence-transformers for semantic similarity.
"""
from __future__ import annotations

import numpy as np
from loguru import logger
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..core.models import Bounty


class SimilarityEngine:
    def __init__(self, knowledge_base):
        self._kb = knowledge_base
        self._vectorizer = TfidfVectorizer(
            max_features=500,
            stop_words="english",
            ngram_range=(1, 2),
        )
        self._patterns: list[dict] = []
        self._matrix = None
        self._rebuild_index()

    def find_similar(self, bounty: Bounty, top_k: int = 5) -> list[dict]:
        """Return top_k most similar past tasks from knowledge base."""
        if not self._patterns or self._matrix is None:
            return []

        query = f"{bounty.title} {bounty.body[:500]}"
        try:
            q_vec = self._vectorizer.transform([query])
            scores = cosine_similarity(q_vec, self._matrix)[0]
            top_indices = np.argsort(scores)[::-1][:top_k]
            results = [
                {**self._patterns[i], "similarity_score": float(scores[i])}
                for i in top_indices
                if float(scores[i]) > 0.1
            ]
            logger.debug(f"SimilarityEngine: found {len(results)} similar tasks")
            return results
        except Exception as e:
            logger.warning(f"SimilarityEngine.find_similar failed: {e}")
            return []

    def rebuild(self) -> None:
        """Refresh index from knowledge base (call after recording new outcomes)."""
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        outcomes = self._kb.load_outcomes()
        if not outcomes:
            logger.debug("SimilarityEngine: no outcomes yet, index empty")
            return

        # Only learn from completed tasks (merged/submitted)
        self._patterns = [
            o for o in outcomes
            if o.get("status") in ("merged", "submitted")
        ]
        if not self._patterns:
            return

        texts = [
            f"{p.get('bounty_id', '')} {p.get('effective_first_prompt', '')} "
            f"{' '.join(p.get('bootstrap_commands', []))}"
            for p in self._patterns
        ]
        try:
            self._matrix = self._vectorizer.fit_transform(texts)
            logger.info(
                f"SimilarityEngine: index built with {len(self._patterns)} patterns"
            )
        except Exception as e:
            logger.warning(f"SimilarityEngine: failed to build index: {e}")
            self._matrix = None

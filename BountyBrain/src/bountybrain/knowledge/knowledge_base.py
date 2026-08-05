"""Knowledge base: JSONL-backed store for bounties, outcomes, and patterns."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from ..core.models import Bounty, TaskOutcome


class KnowledgeBase:
    """
    Stores every bounty interaction — accepted, skipped, completed.
    Phase 0: JSONL files.
    TODO Phase 1: migrate to SQLite via repository pattern.
    """

    def __init__(self, storage_dir: str = "storage/datasets"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bounties_file = self._dir / "bounties.jsonl"
        self._outcomes_file = self._dir / "outcomes.jsonl"
        self._patterns_file = self._dir / "patterns.jsonl"

    # ------------------------------------------------------------------ write

    def record_bounty(self, bounty: Bounty, skip_reason: str | None = None) -> None:
        """Persist a seen bounty (with features, ranking, skip reason)."""
        record = {
            "bounty_id": bounty.id,
            "platform": bounty.platform,
            "title": bounty.title,
            "url": bounty.url,
            "payout_usd": bounty.features.payout_usd if bounty.features else 0,
            "task_type": str(bounty.features.task_type) if bounty.features else "unknown",
            "features": bounty.features.model_dump() if bounty.features else {},
            "qualitative": bounty.qualitative.model_dump() if bounty.qualitative else {},
            "ranking": bounty.ranking.model_dump() if bounty.ranking else {},
            "skip_reason": skip_reason or bounty.skip_reason,
            "recorded_at": datetime.utcnow().isoformat(),
        }
        self._append(self._bounties_file, record)

    def record_outcome(self, outcome: TaskOutcome) -> None:
        """Persist a completed task outcome for learning."""
        self._append(self._outcomes_file, outcome.model_dump())
        logger.info(
            f"Outcome recorded: {outcome.bounty_id} → {outcome.status} | "
            f"EPHH=${outcome.ephh_actual:.1f}/h"
        )

    def record_pattern(self, pattern: dict) -> None:
        """Save a reusable resolution pattern for the Similarity Engine."""
        pattern = {**pattern, "saved_at": datetime.utcnow().isoformat()}
        self._append(self._patterns_file, pattern)
        logger.debug(f"Pattern saved: {pattern.get('pattern_type', '?')}")

    # ------------------------------------------------------------------ read

    def load_outcomes(self) -> list[dict]:
        return self._load_jsonl(self._outcomes_file)

    def load_bounties(self) -> list[dict]:
        return self._load_jsonl(self._bounties_file)

    def load_patterns(self) -> list[dict]:
        return self._load_jsonl(self._patterns_file)

    def get_outcome(self, bounty_id: str) -> dict | None:
        for o in self.load_outcomes():
            if o.get("bounty_id") == bounty_id:
                return o
        return None

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        outcomes = self.load_outcomes()
        merged = [o for o in outcomes if o.get("status") == "merged"]
        ephhs = [o["ephh_actual"] for o in merged if o.get("ephh_actual", 0) > 0]
        return {
            "total_bounties": len(self.load_bounties()),
            "total_outcomes": len(outcomes),
            "merged": len(merged),
            "merge_rate": len(merged) / len(outcomes) if outcomes else 0.0,
            "avg_ephh": sum(ephhs) / len(ephhs) if ephhs else 0.0,
        }

    # ------------------------------------------------------------------ internal

    def _append(self, path: Path, record: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _load_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"Skipping malformed JSONL line in {path.name}: {e}")
        return records

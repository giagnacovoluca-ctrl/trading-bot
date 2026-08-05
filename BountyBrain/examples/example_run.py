"""
Example: Run BountyBrain pipeline programmatically (without CLI).

This is useful for scripting, scheduling, or integrating into a larger workflow.

Usage:
    cd BountyBrain
    pip install -e .
    python examples/example_run.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# Add src to path when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main():
    from bountybrain.config.settings import get_settings
    from bountybrain.core.models import OutcomeStatus, TaskOutcome
    from bountybrain.extractor.feature_extractor import FeatureExtractor
    from bountybrain.knowledge.knowledge_base import KnowledgeBase
    from bountybrain.knowledge.similarity_engine import SimilarityEngine
    from bountybrain.learning.learning_engine import LearningEngine
    from bountybrain.ranking.ranking_engine import RankingEngine
    from bountybrain.ranking.scorers.phase0_scorer import Phase0Scorer
    from bountybrain.scout.collector import BountyCollector

    settings = get_settings()
    print(f"BountyBrain starting (env={settings.env}, phase=0)")

    # Initialize components
    kb = KnowledgeBase()
    scorer = Phase0Scorer()
    learning = LearningEngine(kb, scorer)
    collector = BountyCollector()
    extractor = FeatureExtractor()
    ranker = RankingEngine()
    similarity = SimilarityEngine(kb)

    # 1. Collect bounties
    print("\n[1/4] Collecting bounties...")
    bounties = await collector.collect()
    print(f"    Collected: {len(bounties)} bounties")

    # 2. Extract features
    print("[2/4] Extracting features...")
    for b in bounties:
        b.features = await extractor.extract(b)

    # 3. Rank
    print("[3/4] Ranking...")
    ranked = ranker.rank(bounties)
    print(f"    Ranked: {len(ranked)} candidates (rest filtered)")

    # 4. Display top 5
    print("\n[4/4] Top 5 bounties by EPHH:")
    print("-" * 80)
    for bounty, result in ranked[:5]:
        similar = similarity.find_similar(bounty, top_k=3)
        print(f"  #{result.priority}: {bounty.title[:50]}")
        print(f"     EPHH=${result.ephh:.1f}/h | Payout=${bounty.features.payout_usd:.0f} "
              f"| P(merge)={result.p_correct * result.p_maintainer_accepts:.0%} "
              f"| {result.human_hours_predicted:.1f}h predicted")
        print(f"     {bounty.url}")
        if similar:
            print(f"     Similar past tasks: {len(similar)}")
        print()

    # Learning stats
    stats = learning.get_stats()
    print(f"\nLearning stats: {stats}")
    print(f"\nNext phase upgrade at {stats.get('next_phase_at', '?')} outcomes "
          f"(current: {stats['total']})")

    # Example: log an outcome (normally done after work is done)
    # outcome = TaskOutcome(
    #     bounty_id="github_12345",
    #     status=OutcomeStatus.MERGED,
    #     payout_received=150.0,
    #     human_hours_actual=1.5,
    #     n_prompts=8,
    #     ephh_actual=95.0,
    #     effective_first_prompt="Fix the failing test by correcting the assertion in line 42",
    #     bootstrap_commands=["pip install -e .", "pytest tests/ -x"],
    # )
    # learning.record_outcome(outcome)
    # print("Outcome logged!")


if __name__ == "__main__":
    asyncio.run(main())

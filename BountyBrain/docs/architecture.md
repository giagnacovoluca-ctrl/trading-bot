# BountyBrain Architecture

## Data Flow

```
GitHub API           Algora GraphQL
     |                     |
     v                     v
 GitHubAdapter      AlgoraAdapter
          \          /
           BountyCollector
                |
                v
         FeatureExtractor   (GitHub API — no LLM)
                |
                v
         RankingEngine
          Phase0Scorer (rules)
          Phase1Scorer (Ridge, n>=80)
          Phase2Scorer (GBM,  n>=300)
                |
           (top 10 only)
                v
       QualitativeAnalyzer  (Claude Haiku ~$0.002/call)
                |
                v
         KnowledgeBase
          bounties.jsonl
          outcomes.jsonl
          patterns.jsonl
                |
          SimilarityEngine (TF-IDF → phase2: embeddings)
                |
          LearningEngine
                |
       ContextBuilder (Jinja2)
        CLAUDE.md
        TASK.md
        FIRST_STEPS.md
                |
    EnvironmentBuilder
     git clone + venv/npm/cargo/go
                |
    Claude Code  (human-supervised)
                |
          log_outcome() -> KnowledgeBase
                |
         LearningEngine.record_outcome()
                |
       phase upgrade? (n >= 80 / 300)
```

## Module Descriptions

### `scout/`
Platform adapters implementing `BountyAdapter(ABC)`.

- `GitHubAdapter`: searches GitHub Issues API for bounty labels
- `AlgoraAdapter`: queries Algora GraphQL endpoint
- `BountyCollector`: aggregates all adapters concurrently

**Adding a new platform**: create `opire_adapter.py` implementing `BountyAdapter`, register in `BountyCollector.__init__`.

### `extractor/`
`FeatureExtractor`: extracts deterministic features from bounty metadata and GitHub API. No LLM. Enriches `BountyFeatures`.

**Adding new features**: add field to `BountyFeatures` in `core/models.py`, then populate it in `_enrich_repo_features()`.

### `ranking/`
`RankingEngine` applies filters (min payout, max age, min EPHH) and delegates scoring to the active phase scorer.

**EPHH formula** (in `BaseScorerMixin._calc_ephh()`):
```
EPHH = (P(correct) × P(maintainer_accepts) × payout - api_cost - claude_pro_amortized) / human_hours
```

### `knowledge/`
- `KnowledgeBase`: JSONL append-only store (bounties.jsonl, outcomes.jsonl, patterns.jsonl)
- `SimilarityEngine`: TF-IDF cosine similarity over past successful tasks

### `learning/`
`LearningEngine` tracks outcomes and triggers phase upgrades when data thresholds are met.

### `environment/`
`EnvironmentBuilder`: clones repos and sets up dev environments (Python venv, npm, cargo, go mod).

### `context/`
`ContextBuilder`: renders Jinja2 templates into CLAUDE.md, TASK.md, FIRST_STEPS.md inside the cloned repo.

### `analyzer/`
`QualitativeAnalyzer`: optional LLM call (Claude Haiku) for qualitative scoring. Skipped if no API key.

### `dashboard/`
FastAPI web UI on port 8080. Endpoints: `/`, `/api/stats`, `/api/bounties`, `/api/outcomes`, `POST /api/outcome`.

## Phase Progression

| Phase | Scorer | When | Algorithm |
|-------|--------|------|-----------|
| 0 | `Phase0Scorer` | Always | Rule-based heuristics |
| 1 | `Phase1Scorer` | n >= 80 outcomes | Ridge regression |
| 2 | `Phase2Scorer` | n >= 300 outcomes | Gradient Boosting + qualitative features |

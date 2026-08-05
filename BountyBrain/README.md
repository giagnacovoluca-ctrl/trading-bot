# BountyBrain

Expert system that maximizes **Expected Profit per Human Hour (EPHH)** on bounty platforms (Algora, GitHub Issues) using Claude Code as the development engine.

## Core Formula

```
EPHH = (P(correct) × P(maintainer_accepts) × payout - api_cost - claude_pro_amortized)
       / human_hours_predicted
```

BountyBrain finds, scores, and ranks open bounties by this metric — so you always work on the task with the highest expected return per hour of your time.

---

## Architecture

```
GitHub Issues        Algora GraphQL
      |                    |
      v                    v
 GitHubAdapter      AlgoraAdapter
           \         /
          BountyCollector
                |
         FeatureExtractor   (GitHub API, no LLM)
                |
          RankingEngine
            Phase0Scorer  (rules, always active)
            Phase1Scorer  (Ridge regression, n>=80)
            Phase2Scorer  (Gradient Boosting, n>=300)
                |
         [top 10 only]
                |
       QualitativeAnalyzer  (Claude Haiku, ~$0.002/call)
                |
         KnowledgeBase (JSONL)
          bounties.jsonl
          outcomes.jsonl
          patterns.jsonl
                |
         SimilarityEngine  (TF-IDF → phase2: embeddings)
                |
         LearningEngine    (tracks outcomes, triggers upgrades)
                |
        ContextBuilder (Jinja2)
         CLAUDE.md / TASK.md / FIRST_STEPS.md
                |
       EnvironmentBuilder
        git clone + venv/npm/cargo/go setup
                |
        [You + Claude Code]
                |
        log_outcome() → LearningEngine
```

---

## Requirements

- Python 3.11+
- GitHub personal access token (for Issue search + repo features)
- Anthropic API key (optional — enables qualitative analysis via Claude Haiku)
- Algora API key (optional — enables Algora bounty scouting)

---

## Quick Start

```bash
git clone <repo>
cd BountyBrain
bash scripts/setup.sh       # creates .venv, installs deps, copies .env.example
```

Edit `.env`:
```env
GITHUB_TOKEN=ghp_your_token_here
ANTHROPIC_API_KEY=sk-ant-your_key_here   # optional
ALGORA_API_KEY=your_algora_key_here       # optional
```

Run:
```bash
source .venv/bin/activate

# Find and rank bounties
bountybrain run

# Start web dashboard on http://localhost:8080
bountybrain dashboard

# Log an outcome after work is done
bountybrain log-outcome github_123456 --status merged --payout 150 --hours 1.5 --prompts 8
```

---

## Three Phases of Learning

| Phase | Active When | Algorithm | Confidence |
|-------|-------------|-----------|-----------|
| **0** | Always | Rule-based heuristics | Low (fast, works day 1) |
| **1** | n >= 80 outcomes | Ridge Regression | Medium |
| **2** | n >= 300 outcomes | Gradient Boosting + qualitative features | High |

The system automatically collects training data from every task you run. Once enough outcomes are recorded, it upgrades to the next phase scorer automatically.

---

## Logging Outcomes (Critical for Learning)

After each task completes (merged, rejected, or abandoned), log the outcome. This is what trains the system to get better over time.

**Via CLI:**
```bash
bountybrain log-outcome github_456789 \
  --status merged \
  --payout 200 \
  --hours 2.0 \
  --prompts 12
```

**Via API:**
```bash
curl -X POST http://localhost:8080/api/outcome \
  -H "Content-Type: application/json" \
  -d '{
    "bounty_id": "github_456789",
    "status": "merged",
    "payout_received": 200.0,
    "human_hours_actual": 2.0,
    "n_prompts": 12,
    "api_cost_usd": 0.35,
    "ephh_actual": 97.5,
    "effective_first_prompt": "Fix the failing assertion by updating the expected value from X to Y",
    "bootstrap_commands": ["pip install -e .", "pytest tests/ -x"]
  }'
```

**Fields you must fill in:**
- `status`: `merged` | `rejected` | `abandoned` | `submitted`
- `payout_received`: actual payout (0 if rejected)
- `human_hours_actual`: total time you spent (including reading, reviewing, supervising)
- `n_prompts`: number of Claude Code prompts used

---

## Web Dashboard

```bash
bountybrain dashboard
# → http://localhost:8080
```

Dashboard shows:
- Total bounties seen / outcomes recorded / merge rate / avg EPHH
- Full list of past bounties and outcomes
- API endpoint for logging new outcomes

---

## Dataset Files

Located in `storage/datasets/`:

- `bounties.jsonl` — every bounty seen (features, ranking, skip reasons)
- `outcomes.jsonl` — completed task outcomes (training data)
- `patterns.jsonl` — reusable resolution patterns

See `docs/datasets.md` for full field documentation.

---

## Running Tests

```bash
# Unit + smoke (no API keys needed)
pytest tests/unit tests/smoke -v

# All tests (integration requires fastapi[testclient])
pytest tests/ -v

# With coverage
pytest tests/ --cov=src/bountybrain --cov-report=term-missing
```

---

## Docker

```bash
cp .env.example .env
# edit .env
docker-compose up --build
```

---

## Roadmap

See `docs/roadmap.md` for planned features.

Key upcoming: Phase1/2 auto-training, sentence-transformers similarity, PostgreSQL migration, auto-PR submission.


1. Scegli la bounty dal run
#3  github_2976543210  [BOUNTY $100] The Memanto Bug   $100  $84.3  56%  0.7h  Fix Failing Test

2. Setup
python -m bountybrain.main setup github_2976543210
→ Clona la repo in storage/workspaces/github_2976543210/repo/
→ Installa le dipendenze (venv Python / npm / ecc.)
→ Genera CLAUDE.md, TASK.md, FIRST_STEPS.md nella repo

3. Lavora con Claude Code
cd storage/workspaces/github_2976543210/repo
claude
Claude Code parte con il contesto già pronto: sa qual è il task, il payout, i test da far passare, e i pattern da task simili passati.

4. Apri la PR su GitHub (normale flusso git)

5. Logga l'esito (fondamentale per il Learning Engine)
# se il merge è andato a buon fine:
python -m bountybrain.main log-outcome github_2976543210 \
  --status merged --payout 100 --hours 1.2 --prompts 8

# se rifiutato:
python -m bountybrain.main log-outcome github_2976543210 \
  --status rejected --hours 0.5

Ogni log alimenta il ranking — dopo 80 task il sistema passa automaticamente alla regressione lineare e diventa più preciso.

# BountyBrain — Codebase Summary

> Consulta questo file PRIMA di aprire qualsiasi sorgente.
> Aggiornalo quando aggiungi moduli, funzioni o parametri rilevanti.

---

## Obiettivo

Massimizzare **Expected Profit per Human Hour (EPHH)**:
```
EPHH = (P(correct) × P(maintainer_accepts) × payout - api_cost - claude_pro_amortized) / human_hours
```

---

## Albero file

```
BountyBrain/
├── src/bountybrain/
│   ├── core/
│   │   ├── models.py          # Pydantic v2: tutti i datamodel
│   │   ├── interfaces.py      # ABC: BountyAdapter, FeatureExtractorBase, ScorerBase, RepositoryBase
│   │   └── exceptions.py      # BountyBrainError, FeatureExtractionError, ScorerError, ...
│   ├── config/
│   │   └── settings.py        # Settings(BaseSettings) + get_settings() singleton
│   ├── scout/
│   │   ├── github_adapter.py  # GitHubAdapter: GitHub Search API + payout regex
│   │   ├── algora_adapter.py  # AlgoraAdapter: GraphQL (free, no auth)
│   │   └── collector.py       # BountyCollector: aggrega adapter in async
│   ├── extractor/
│   │   └── feature_extractor.py  # FeatureExtractor: 20+ feature deterministiche, NO LLM
│   ├── analyzer/
│   │   └── qualitative_analyzer.py  # QualitativeAnalyzer: Groq (free) > Anthropic > None
│   ├── ranking/
│   │   ├── ranking_engine.py  # RankingEngine: filtra + ordina per EPHH
│   │   └── scorers/
│   │       ├── base_scorer.py    # BaseScorerMixin: _calc_ephh(), _build_result()
│   │       ├── phase0_scorer.py  # Phase0Scorer: regole deterministiche (attivo ora)
│   │       ├── phase1_scorer.py  # Phase1Scorer: Ridge regression (n≥80)
│   │       └── phase2_scorer.py  # Phase2Scorer: GradientBoosting (n≥300, TODO)
│   ├── knowledge/
│   │   ├── knowledge_base.py    # KnowledgeBase: JSONL append-only (bounties/outcomes/patterns)
│   │   └── similarity_engine.py # SimilarityEngine: TF-IDF cosine su outcomes merged
│   ├── learning/
│   │   └── learning_engine.py   # LearningEngine: tracking + trigger upgrade fase
│   ├── environment/
│   │   └── environment_builder.py  # EnvironmentBuilder: git clone + setup Python/Node/Rust/Go/Docker
│   ├── context/
│   │   ├── context_builder.py   # ContextBuilder: genera CLAUDE.md + TASK.md + FIRST_STEPS.md
│   │   └── templates/           # Jinja2: CLAUDE.md.j2, TASK.md.j2, FIRST_STEPS.md.j2
│   ├── dashboard/
│   │   └── app.py               # FastAPI: /, /api/stats, /api/bounties, /api/outcomes
│   └── main.py                  # CLI typer: run | dashboard | setup | log-outcome
├── config/default.yaml          # Tutti i parametri (mai hardcoded nel codice)
├── storage/
│   ├── datasets/bounties.jsonl  # Ogni bounty vista (anche scartate + skip_reason)
│   ├── datasets/outcomes.jsonl  # Outcome post-task (merged/rejected/abandoned)
│   ├── datasets/patterns.jsonl  # Pattern di risoluzione riusabili
│   ├── models/                  # phase1_model.pkl, phase2_model.pkl
│   ├── workspaces/              # Repo clonate per ogni bounty
│   └── db/bountybrain.db        # SQLite (per future query SQL)
└── tests/                       # 76 test: unit + integration + smoke
```

---

## Modelli core (`core/models.py`)

```python
class TaskType(Enum):      # fix_failing_test | fix_bug | add_feature | refactor | documentation | security | performance | unknown
class Platform(Enum):      # github | algora | opire
class OutcomeStatus(Enum): # pending | in_progress | submitted | merged | rejected | abandoned | skipped

class BountyFeatures(BaseModel):
    # Issue
    issue_age_days: float          # giorni dalla creazione
    issue_comment_count: int
    issue_has_reproduction_steps: bool
    issue_has_acceptance_criteria: bool
    issue_body_length: int
    task_type: TaskType
    # Repo
    repo_stars: int
    repo_forks: int
    repo_size_kb: int
    repo_language: str
    repo_has_tests: bool
    repo_has_ci: bool
    repo_has_docker: bool
    repo_has_contributing: bool
    repo_has_devcontainer: bool
    repo_dependency_count: int
    repo_contributor_count: int
    repo_commit_frequency_30d: float
    repo_open_issues_count: int
    # Maintainer
    maintainer_response_p50_days: float   # default 7.0 se non calcolato
    maintainer_merge_rate: float
    maintainer_avg_review_rounds: float
    # Competition (issue-level, non repo-level)
    n_open_prs: int             # PR aperte nel repo (generico)
    n_stale_prs: int            # PR stantie nel repo
    n_issue_prs_open: int       # PR aperte che referenziano QUESTA issue
    n_issue_prs_closed: int     # PR chiuse/rifiutate su QUESTA issue
    n_attempt_comments: int     # commenti "/attempt" = segnale bot farm
    has_merged_pr: bool         # True → bounty già pagata, skip immediato
    # Payout
    payout_usd: float

class QualitativeScores(BaseModel):   # output LLM (opzionale)
    ai_score: float          # 0-100: quanto è risolvibile da AI
    business_score: float    # 0-100: valore economico
    ambiguity_risk: float    # 0-1
    hidden_complexity_risk: float  # 0-1
    perceived_complexity: float    # 0-100
    confidence: float        # 0-1
    reasoning: str

class RankingResult(BaseModel):
    bounty_id: str
    ephh: float                    # metrica principale
    business_score: float
    human_hours_predicted: float
    p_correct: float
    p_maintainer_accepts: float
    expected_profit_usd: float
    confidence: float
    scorer_phase: int              # 0/1/2
    priority: int                  # rank nella lista ordinata

class Bounty(BaseModel):
    id: str                        # formato: "github_{issue_id}" | "algora_{id}"
    platform: Platform
    title: str
    body: str
    url: str
    repo_url: str
    repo_name: str                 # "owner/repo"
    payout_usd: float
    labels: list[str]
    created_at / updated_at: datetime
    features: BountyFeatures | None
    qualitative: QualitativeScores | None
    ranking: RankingResult | None
    outcome: OutcomeStatus
    skip_reason: str | None        # sempre popolato se scartata

class TaskOutcome(BaseModel):      # loggato dopo ogni task completato
    bounty_id: str
    status: OutcomeStatus
    payout_received: float
    human_hours_actual: float
    claude_hours_actual: float
    n_prompts: int
    n_human_interventions: int
    n_test_failures_before_merge: int
    n_review_rounds: int
    api_cost_usd: float
    ephh_actual: float             # metrica chiave per il Learning Engine
    effective_first_prompt: str
    bootstrap_commands: list[str]
    failure_patterns: list[str]
    maintainer_comments: list[str]
```

---

## Firme principali per modulo

### `scout/`
```python
# BountyCollector
async collect() -> list[Bounty]                  # aggrega tutti gli adapter

# GitHubAdapter
async fetch_bounties() -> list[Bounty]           # cerca via GitHub Search API
async fetch_bounty_detail(bounty_id) -> Bounty
_extract_payout(text: str) -> float             # regex $N / N USD / bounty: $N
_classify_task_type(text: str) -> TaskType      # pattern matching sul testo

# AlgoraAdapter
async fetch_bounties() -> list[Bounty]           # GraphQL console.algora.io
```

### `extractor/`
```python
# FeatureExtractor  (NO LLM — solo GitHub API)
async extract(bounty: Bounty) -> BountyFeatures
async _enrich_repo_features(features, repo_name)           # feature repo-level (core API)
async _enrich_competition_features(features, repo_name, issue_number, body)
#   → UNA sola Search API call (rate limit 30/min → max 1 per bounty)
#   → scarica 10 items, conta open/closed/merged lato client, scala proporzionalmente
async _count_attempt_comments(client, repo_name, issue_number) -> int  # issues API, no search quota
async _calc_response_p50(client, repo_name) -> float   # mediana da ultimi 20 issue chiuse
async _count_open_prs(client, repo_name) -> int
async _check_repo_files(client, repo_name) -> (has_ci, has_docker, has_devcontainer)
_extract_issue_number(url: str) -> int | None            # regex /issues/(\d+)
_has_repro(text) -> bool
_has_acceptance_criteria(text) -> bool
```

### `ranking/`
```python
# RankingEngine
rank(bounties: list[Bounty]) -> list[tuple[Bounty, RankingResult]]
# Hard filter PRIMA dello scoring (in ordine):
#   1. has_merged_pr → skip "bounty già pagata"
#   2. n_attempt_comments > max_attempt_comments(5) → skip "bot farm"
#   3. n_issue_prs_open > max_issue_prs_open(2) → skip "saturata"
#   4. n_issue_prs_closed > max_issue_prs_closed(4) → skip "bloccata"
#   5. payout_usd < min_payout_usd(50) → skip
#   6. issue_age_days > max_issue_age_days(180) → skip
#   7. ephh < min_ephh_threshold(10) → skip
# Ordina: EPHH decrescente

# Phase0Scorer (attivo — regole)
score(features) -> RankingResult
_estimate_p_correct(f)      # task_type (+0.25 fix_test), has_tests, has_repro, has_ac, has_ci
                             # penalità: n_attempt_comments (-0.05×min(n,4)), n_issue_prs_open (-0.08×n)
_estimate_p_maintainer(f)   # response_p50, has_contributing, n_stale_prs
_estimate_human_hours(f)    # base per task_type × moltiplicatori size/tests/docker/age
_calc_confidence(f)         # % feature non-default disponibili

# Phase1Scorer (n≥80 — Ridge regression)
score(features) -> RankingResult    # fallback su Phase0 se non trained
train(X, y)                         # X=_featurize(), y=ephh_actual
update(outcome: TaskOutcome)        # TODO: retrieve features da KB e aggiungi
_featurize(f) -> list[float]        # 13 feature numeriche (log1p su stars/size/payout)

# Phase2Scorer (n≥300 — GBM, TODO)
train_gbm(X, y)                     # TODO: GradientBoostingRegressor o XGBoost

# BaseScorerMixin
_calc_ephh(p_correct, p_maintainer, payout, human_hours, api_cost=0.5, claude_pro=0.625) -> float
_build_result(...) -> RankingResult
```

### `analyzer/`
```python
# QualitativeAnalyzer
# Provider: GROQ_API_KEY → Groq llama-3.3-70b (gratis) | ANTHROPIC_API_KEY → claude-haiku | None → skip
async analyze(bounty, features) -> QualitativeScores | None
_build_prompt(bounty, features) -> str    # invia solo feature strutturate + 1000 char body
```

### `knowledge/`
```python
# KnowledgeBase  (JSONL append-only)
record_bounty(bounty, skip_reason=None)   # salva TUTTE le bounty viste
record_outcome(outcome: TaskOutcome)       # salva dopo ogni task
record_pattern(pattern: dict)             # pattern riusabili (bootstrap, prompt, errori)
load_outcomes() -> list[dict]
load_bounties() -> list[dict]
load_patterns() -> list[dict]
get_outcome(bounty_id) -> dict | None
stats() -> dict                           # total/merged/merge_rate/avg_ephh

# SimilarityEngine  (TF-IDF cosine, max_features=500)
find_similar(bounty, top_k=5) -> list[dict]   # cerca in outcomes merged
rebuild()                                       # ricostruisce indice TF-IDF
```

### `learning/`
```python
# LearningEngine
record_outcome(outcome: TaskOutcome)    # → KB + scorer.update() + trigger upgrade
get_stats() -> dict                     # total/merged/merge_rate/avg_ephh/scorer_phase/next_phase_at
_maybe_upgrade_phase()                  # ogni retrain_every_n_tasks (default 10)
```

### `environment/`
```python
# EnvironmentBuilder
build(bounty: Bounty) -> Path           # clone + setup linguaggio + ritorna workspace/
destroy(bounty_id: str)                 # rm -rf workspace
_setup_python(repo_dir)                 # venv + pip install (requirements.txt / pyproject.toml)
_setup_node(repo_dir)                   # npm/yarn install
_setup_rust(repo_dir)                   # cargo build
_setup_go(repo_dir)                     # go mod download
_setup_docker(repo_dir)                 # log info (no build automatico)
```

### `context/`
```python
# ContextBuilder  (Jinja2)
build(bounty, ranking, workspace, similar_tasks=None) -> None
# Genera in workspace/repo/:
#   CLAUDE.md      → contesto completo + task simili passati
#   TASK.md        → descrizione + score + payout + issue body
#   FIRST_STEPS.md → comandi run test + repro steps + istruzioni
```

### `dashboard/`
```python
# FastAPI endpoints
GET  /                          # HTML stats overview
GET  /api/stats                 # JSON: total_bounties, merged, merge_rate, avg_ephh
GET  /api/bounties?limit=50     # ultimi N bounties.jsonl
GET  /api/outcomes?limit=50     # ultimi N outcomes.jsonl
GET  /api/outcomes/{bounty_id}  # singolo outcome
POST /api/outcome               # registra TaskOutcome (body JSON)
GET  /api/health                # {"status": "ok"}
```

### `main.py` — CLI
```bash
python -m bountybrain.main run [--limit N]          # pipeline completa
python -m bountybrain.main dashboard                # FastAPI su :8080
python -m bountybrain.main setup <bounty_id>        # build environment per bounty specifica
python -m bountybrain.main log-outcome <bounty_id> --status merged --payout 100 --human-hours 1.5
```

---

## Parametri chiave (`config/default.yaml`)

| Chiave | Default | Effetto |
|---|---|---|
| `ranking.phase` | `0` | Scorer attivo: 0=regole 1=Ridge 2=GBM |
| `ranking.min_ephh_threshold` | `10.0` | EPHH minimo per non scartare |
| `ranking.min_payout_usd` | `50` | Payout minimo USD |
| `ranking.max_issue_age_days` | `180` | Issue più vecchie → skip (alzato per bounty stantie) |
| `ranking.max_attempt_comments` | `5` | /attempt comments → skip (bot farm) |
| `ranking.max_issue_prs_open` | `2` | PR aperte sulla issue → skip (competitor attivi) |
| `ranking.max_issue_prs_closed` | `4` | PR chiuse sulla issue → skip (issue bloccata) |
| `ranking.claude_pro_monthly_cost` | `100.0` | Costo mensile Claude Pro ($) |
| `ranking.claude_pro_hours_per_month` | `160.0` | Ore/mese → amortizzato = 0.625$/h |
| `learning.min_samples_phase1` | `80` | Soglia upgrade Phase 0→1 |
| `learning.min_samples_phase2` | `300` | Soglia upgrade Phase 1→2 |
| `learning.retrain_every_n_tasks` | `10` | Frequenza check upgrade |
| `knowledge.similarity_top_k` | `5` | Task simili iniettati nel contesto |
| `scout.run_interval_minutes` | `30` | Frequenza scout (per cron) |

---

## Dataset (`storage/datasets/`)

**`bounties.jsonl`** — ogni bounty vista (anche scartate):
`bounty_id, platform, title, url, payout_usd, task_type, features{}, qualitative{}, ranking{}, skip_reason, recorded_at`

**`outcomes.jsonl`** — ogni task completato:
`bounty_id, status, payout_received, human_hours_actual, claude_hours_actual, n_prompts, n_human_interventions, n_test_failures_before_merge, n_review_rounds, api_cost_usd, ephh_actual, effective_first_prompt, bootstrap_commands[], failure_patterns[], maintainer_comments[], completed_at`

**`patterns.jsonl`** — pattern riusabili:
`task_type, repo_type, context_signals[], effective_first_prompt, failure_patterns[], bootstrap_commands[], human_interventions, intervention_cause, saved_at`

---

## Flow pipeline

```
BountyCollector.collect()
    → [GitHubAdapter] async  (Algora disabilitato — endpoint 404)
    → list[Bounty] deduplicate per (id, url)
    → query GitHub: bounty fresche + bounty stantie create:<30gg fa

FeatureExtractor.extract(bounty)          # per ogni bounty, sequenziale
    → BountyFeatures
        _enrich_repo_features()    → core GitHub API (repo, CI, Docker, contributors, PRs repo)
        _enrich_competition_features() → 1 Search API call (rate limit: 30/min)
                                         conta open/closed/merged PR sull'issue specifica

RankingEngine.rank(bounties)              # hard filter (7 regole) + EPHH
    → list[(Bounty, RankingResult)]

QualitativeAnalyzer.analyze(top_10)      # Groq/Anthropic/skip
    → QualitativeScores | None

SimilarityEngine.find_similar(bounty)    # TF-IDF su outcomes
    → list[dict] (task simili passati)

ContextBuilder.build(bounty, workspace)  # CLAUDE.md + TASK.md + FIRST_STEPS.md

[Claude Code lavora sul task]

LearningEngine.record_outcome(outcome)   # → KnowledgeBase + scorer.update()
```

---

## Aggiungere una nuova piattaforma

1. Crea `scout/mia_piattaforma_adapter.py` che estende `BountyAdapter`
2. Implementa `fetch_bounties()` e `fetch_bounty_detail()`
3. Aggiungi a `BountyCollector.__init__()` con flag in `config/default.yaml`

## Aggiungere nuove feature deterministiche

1. Aggiungi campo a `BountyFeatures` in `core/models.py`
2. Popola in `FeatureExtractor.extract()` o `_enrich_repo_features()`
3. Usa nel `Phase0Scorer` se rilevante per scoring

---

## Stato attuale (2026-06-26)

- Phase 0 attiva (regole deterministiche)
- Groq come provider LLM predefinito (free tier)
- 76/76 test verdi
- Filtri anti-saturazione attivi: has_merged_pr / n_attempt_comments / n_issue_prs_open / n_issue_prs_closed
- Search API ottimizzata: 1 call/bounty (era 3) per restare nel rate limit 30/min
- Query scout: fresche + stantie >30gg (bassa competizione)
- Phase 1 si attiva automaticamente dopo 80 outcome registrati

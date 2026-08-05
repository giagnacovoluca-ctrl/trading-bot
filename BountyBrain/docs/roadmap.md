# Roadmap

## Phase 0 (current) — Rules
- [x] Scout: GitHub Issues adapter
- [x] Scout: Algora GraphQL adapter
- [x] Feature Extractor (deterministic, GitHub API)
- [x] Phase 0 Scorer (rule-based EPHH)
- [x] Knowledge Base (JSONL append-only)
- [x] Similarity Engine (TF-IDF cosine)
- [x] Context Builder (Jinja2: CLAUDE.md, TASK.md, FIRST_STEPS.md)
- [x] Environment Builder (git clone + Python/Node/Rust/Go setup)
- [x] Dashboard (FastAPI: stats, outcomes, bounty list)
- [x] Qualitative Analyzer (Claude Haiku, optional)
- [x] Learning Engine (outcome tracking, phase upgrade detection)
- [x] CLI (typer: run, dashboard, log-outcome, setup)
- [x] Docker + docker-compose

## Phase 1 (80+ outcomes) — Linear Regression
- [ ] Phase1Scorer training loop (retrieve features by bounty_id)
- [ ] Feature importance visualization (coefficient plot)
- [ ] Qualitative scores integrated as Phase1 features
- [ ] Auto-retrain trigger on `--retrain` flag
- [ ] SQLite migration for bounties (via BountyRepository)

## Phase 2 (300+ outcomes) — Gradient Boosting
- [ ] Phase2Scorer (sklearn GradientBoostingRegressor)
- [ ] sentence-transformers in SimilarityEngine (replace TF-IDF)
- [ ] PostgreSQL migration (replace SQLite)
- [ ] Feature drift detection

## Future Ideas
- [ ] Opire adapter (opire.dev)
- [ ] GitLab Issues adapter
- [ ] Gitcoin adapter
- [ ] Auto-PR submission (post PR after Claude Code run)
- [ ] Maintainer behavior profiling (per-maintainer merge rate)
- [ ] Cooldown tracking (avoid same maintainer twice in a week)
- [ ] Multi-user SaaS mode (per-user KB)
- [ ] Slack/Telegram notifications for new high-EPHH bounties
- [ ] GitHub Actions integration (daily scout run)
- [ ] CLAUDE.md auto-update with best-prompt from similar tasks
- [ ] Cost tracking by session (via Anthropic API usage endpoint)

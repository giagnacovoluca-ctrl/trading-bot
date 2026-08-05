# Dataset Schema

All datasets are stored as JSONL (newline-delimited JSON) in `storage/datasets/`.

---

## `bounties.jsonl`

One record per bounty seen by BountyBrain (including skipped ones).

| Field | Type | Description |
|-------|------|-------------|
| `bounty_id` | str | Unique ID, e.g. `github_123456` |
| `platform` | str | `"github"` / `"algora"` / `"opire"` |
| `title` | str | Issue title |
| `url` | str | Direct link to the issue |
| `payout_usd` | float | Payout amount in USD (0 if unknown) |
| `task_type` | str | `fix_failing_test` / `fix_bug` / `add_feature` / etc. |
| `features` | dict | Full `BountyFeatures` model dump |
| `features.issue_age_days` | float | Days since issue was opened |
| `features.issue_has_reproduction_steps` | bool | Regex-detected repro steps |
| `features.issue_has_acceptance_criteria` | bool | Detected acceptance criteria |
| `features.repo_stars` | int | GitHub stars |
| `features.repo_has_tests` | bool | Test files detected in repo |
| `features.repo_has_ci` | bool | `.github/workflows/` exists |
| `features.repo_has_docker` | bool | Dockerfile or docker-compose.yml exists |
| `features.maintainer_response_p50_days` | float | Median response time (days) |
| `features.n_open_prs` | int | Open PRs at time of scoring |
| `qualitative` | dict | `QualitativeScores` model dump (nullable) |
| `qualitative.ai_score` | float | 0-100: AI solvability |
| `qualitative.ambiguity_risk` | float | 0-1: ambiguity level |
| `qualitative.hidden_complexity_risk` | float | 0-1: hidden complexity |
| `ranking` | dict | `RankingResult` model dump |
| `ranking.ephh` | float | Expected Profit per Human Hour |
| `ranking.p_correct` | float | P(Claude solves correctly) |
| `ranking.p_maintainer_accepts` | float | P(maintainer merges PR) |
| `ranking.human_hours_predicted` | float | Predicted human oversight hours |
| `ranking.scorer_phase` | int | 0 / 1 / 2 |
| `skip_reason` | str / null | Why bounty was filtered out (null if passed) |
| `recorded_at` | datetime | ISO 8601 UTC timestamp |

---

## `outcomes.jsonl`

One record per completed task. This is the training data for Phase 1/2 scorers.

| Field | Type | Description |
|-------|------|-------------|
| `bounty_id` | str | References `bounties.jsonl` |
| `status` | str | `merged` / `rejected` / `abandoned` / `submitted` |
| `payout_received` | float | Actual payout received (USD) |
| `human_hours_actual` | float | Real human hours spent |
| `claude_hours_actual` | float | Estimated Claude Code hours |
| `n_prompts` | int | Number of Claude prompts used |
| `n_human_interventions` | int | Times human had to intervene |
| `n_test_failures_before_merge` | int | Test failures before final pass |
| `n_review_rounds` | int | PR review rounds |
| `api_cost_usd` | float | Actual Anthropic API cost |
| `ephh_actual` | float | Realized EPHH (computed from actual data) |
| `effective_first_prompt` | str | The prompt that unlocked the solution |
| `bootstrap_commands` | list[str] | Commands that set up the environment |
| `failure_patterns` | list[str] | What went wrong (for learning) |
| `maintainer_comments` | list[str] | Key feedback from maintainer |
| `completed_at` | datetime | ISO 8601 UTC timestamp |

**EPHH actual formula:**
```
ephh_actual = (payout_received - api_cost_usd - 0.625) / human_hours_actual
```

---

## `patterns.jsonl`

Reusable resolution patterns extracted from successful tasks.

| Field | Type | Description |
|-------|------|-------------|
| `pattern_type` | str | e.g. `fix_test`, `add_endpoint`, `fix_import` |
| `task_type` | str | Associated task type |
| `repo_language` | str | e.g. `python`, `typescript` |
| `description` | str | Human-readable pattern description |
| `bootstrap_commands` | list[str] | Commands to run first |
| `solution_approach` | str | How to approach this class of problem |
| `success_rate` | float | Win rate of this pattern |
| `usage_count` | int | Times this pattern was applied |
| `saved_at` | datetime | ISO 8601 UTC timestamp |

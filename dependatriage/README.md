# DependaTriage

> GitHub App: AI-powered Dependabot PR triage. Batch 200 PRs into 3.

**The problem:** Dependabot generates 200+ PRs/week. Teams mute it and ignore real vulnerabilities.

**The solution:** AI analyzes reachability + severity → auto-merges safe patches → batches low-risk → flags critical.

## How it works
1. Install GitHub App on your org
2. DependaTriage intercepts all Dependabot PRs
3. LLM + AST analysis: is the vulnerable code path actually called?
4. Auto-merge: patch updates with no breaking changes
5. Batch: group by ecosystem (npm, pip, go) into weekly PRs
6. Alert: critical CVEs with actual reachability get Slack + PR comment

## Pricing
- Free: 1 repo
- Pro €29/mo: unlimited repos, AI triage
- Teams €79/mo: Slack + weekly digest + reachability analysis

## Stack
- Python FastAPI (webhook handler)
- GitHub App (Probot-style)
- PostgreSQL (PR history, org config)
- LLM (triage scoring)
- Redis (dedup, queue)

## Status
SKELETON — not yet developed (deprioritized vs PromptWatch)
See PromptWatch for active development.

# DocSync

> Auto-sync your API docs from code. Zero stale documentation.

**The problem:** 73% of API docs go stale within weeks. Devs change endpoints, forget to update OpenAPI specs.

**The solution:** GitHub webhook → detect API changes → LLM generates updated spec → opens PR automatically.

## How it works
1. Connect GitHub repo
2. DocSync monitors every push for API route changes (FastAPI, Flask, Express, Django, Rails)
3. When routes change: LLM generates/updates OpenAPI 3.1 spec
4. Opens PR: "Auto-update API docs for PR #123"
5. Teams review → merge → docs always match code

## Pricing
- Free: 1 public repo
- Pro €49/mo: 5 repos, AI descriptions
- Teams €149/mo: unlimited, custom templates, multiple formats

## Stack
- Python FastAPI
- GitHub App
- PostgreSQL
- Claude/GPT for description generation
- OpenAPI generator

## Status
SKELETON — secondary priority. Will develop after PromptWatch V1.

# PromptWatch

> LLM cost tracking, prompt versioning, and quality monitoring for small teams.

**2 lines of code. Full visibility on your LLM spend.**

```python
from promptwatch import PromptWatch
pw = PromptWatch(api_key="pw_...")
client = pw.wrap(openai.OpenAI())  # that's it
```

## Why PromptWatch?

- OpenAI dashboard shows total cost — not cost per feature/user/team
- Langfuse requires running your own infra
- Datadog LLM Observability costs 5x more and is buried in complexity

## Features

| Feature | Starter €49/mo | Growth €149/mo | Scale €499/mo |
|---------|----------------|----------------|----------------|
| Projects | 1 | 10 | Unlimited |
| Tokens tracked | 1M/mo | 10M/mo | Unlimited |
| Providers | All | All | All |
| Alert on spend | ✓ | ✓ | ✓ |
| Prompt versioning | ✗ | ✓ | ✓ |
| Team seats | 1 | 5 | Unlimited |
| A/B testing | ✗ | ✗ | ✓ |
| SSO | ✗ | ✗ | ✓ |

## Supported Providers

- OpenAI (GPT-4o, GPT-4, GPT-3.5)
- Anthropic (Claude 3.5, Claude 4)
- Google (Gemini 1.5, 2.0)
- Mistral, Groq, Together AI
- Any OpenAI-compatible endpoint

## Quick Start

```bash
pip install promptwatch
```

```python
import openai
from promptwatch import PromptWatch

pw = PromptWatch(api_key="pw_your_key_here")
client = pw.wrap(openai.OpenAI())

# All calls now tracked automatically
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}],
    extra_headers={"pw-project": "chatbot", "pw-user": "user_123"}
)
```

## Self-hosted

```bash
git clone https://github.com/promptwatch/promptwatch
cd promptwatch
cp .env.example .env
docker-compose up -d
```

## Architecture

```
SDK (Python/TS) → API (FastAPI) → PostgreSQL
                              → Redis (rate limiting)
                              → Dashboard (HTMX)
                              → Worker (alerts, aggregations)
```

## Roadmap

- [x] MVP: cost tracking per project
- [ ] V1: alert engine, prompt versioning
- [ ] V2: A/B testing, quality scoring
- [ ] Enterprise: SSO, on-premise, audit log

## License

MIT (SDK) + Commercial (hosted service)

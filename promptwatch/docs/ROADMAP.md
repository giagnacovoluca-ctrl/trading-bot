# PromptWatch Roadmap

## MVP — settimana 1-2 (ora)
- [x] Python SDK: wrap OpenAI + Anthropic, track tokens/cost
- [x] Ingest API: single + batch events
- [x] Auth: register, login, JWT
- [x] Projects: create, list, delete, rotate API key
- [x] Dashboard API: summary, cost-by-model, cost-by-feature, daily-cost, events
- [x] Alert rules: create, list, toggle, delete
- [x] Alert engine: evaluation worker, email + webhook notify
- [x] Billing: Stripe checkout, portal, webhook
- [x] Docker + docker-compose
- [ ] Dashboard HTML: HTMX frontend su FastAPI

## V1 — mese 2
- [ ] Prompt versioning: store named prompts, versioni, diff viewer
- [ ] Team management: invite members, role admin/viewer
- [ ] Slack/Discord webhook native (pre-built integration)
- [ ] Monthly cost report email automatica
- [ ] Rate limit alerts (avvisi prima di superare plan quota)
- [ ] SDK: Google Gemini wrapper
- [ ] SDK: Mistral/Groq wrapper (OpenAI-compatible = facile)

## V2 — mese 4
- [ ] A/B testing prompts: split traffic, compare cost+quality
- [ ] Quality scoring: response length, error patterns, latency percentili
- [ ] LLM cost forecasting: proiezione mensile basata su trend 7gg
- [ ] Cost allocation export: CSV per reparto/team
- [ ] Grafana dashboard JSON export
- [ ] API pubblica per query custom (Growth+)

## Enterprise — mese 8
- [ ] SSO (SAML/OIDC)
- [ ] On-premise Docker deploy con license key
- [ ] Audit log completo
- [ ] Custom data retention policy
- [ ] SLA 99.9% uptime + priority support
- [ ] Custom model pricing (per chi ha deal diretti con provider)

## Acquisizione Clienti
### Settimana 1-4
1. GitHub repo open source (SDK MIT) → organic discovery
2. Product Hunt launch (mirare a Top 5 of the day)
3. HN "Show HN: I built a 2-line LLM cost tracker" post
4. Dev.to articolo: "How we reduced our LLM costs by 60% after seeing the breakdown"

### Mese 2-3
5. SEO: "openai cost tracking", "claude api monitoring", "llm cost breakdown"
6. Cold outreach su HN comments dove parlano di LLM costs
7. Integration con LangChain, LlamaIndex (plugin/callback)
8. YouTube: "Stop getting surprised by your OpenAI bill"

## Pricing (EUR)
- **Free**: 1 progetto, 100K token/mo, no alerts → per tryout
- **Starter €49/mo**: 1 progetto, 1M tokens, alert email
- **Growth €149/mo**: 10 progetti, 10M tokens, alert + webhook + prompt versioning
- **Scale €499/mo**: illimitato + A/B + SSO + priority support

## Unit Economics Target (12 mesi)
- MRR Target: €10.000/mo
- At Growth (€149): 68 clienti
- CAC target: €0 (organic) → €100 (paid) max
- LTV/CAC target: >15x
- Churn target: <5%/mo (sticky quando in produzione)

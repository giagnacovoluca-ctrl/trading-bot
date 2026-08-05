# PROMPTWATCH — MAPPA DEL CODEBASE
Usa questa mappa per capire la struttura del progetto senza rileggere i file interi.
Aggiornato: 2026-06-25 (MVP BUILD — SDK + API + ALERT ENGINE + BILLING)

---

## Architettura in breve

```
SDK (pip install promptwatch)
  └── wrap(openai_client)  →  Transport (batch queue, background thread)
                                   └── POST /v1/events/batch  →  FastAPI API
                                                                    ├── PostgreSQL (events, projects, orgs, alerts)
                                                                    ├── Redis (rate limiting, futuro)
                                                                    └── Worker (alert evaluation, aggregations)
```

**Flusso dati:**
1. SDK intercetta ogni chiamata LLM (OpenAI/Anthropic) con `wrapt.decorator`
2. Estrae token usage e latenza dalla response
3. Accoda evento in `Queue` (non-blocking, drop se piena)
4. Background thread svuota la coda ogni 2s via `/v1/events/batch`
5. API calcola costo in USD (`pricing.py`), salva `LLMEvent` in PostgreSQL
6. Worker ogni 5min valuta `AlertRule` e spara email/webhook se soglia superata

**Deploy:** `docker-compose up -d` (API + Worker + PostgreSQL + Redis)

---

## 1. SDK — `sdk/promptwatch/`

### `__init__.py`
Esporta solo `PromptWatch`. Versione: `0.1.0`.

### `client.py` — classe `PromptWatch`
Entry point principale. Gestisce lifecycle del transport.

| Metodo | Firma | Descrizione |
|--------|-------|-------------|
| `__init__` | `(api_key, base_url, flush_interval, batch_size, debug)` | Init transport + avvia background thread |
| `wrap` | `(client: Any) → Any` | Detecta tipo client (openai/anthropic dal `__module__`), patcha in-place |
| `track` | `(provider, model, prompt_tokens, completion_tokens, feature, user_id, latency_ms, error, prompt_name, prompt_version, metadata)` | Track manuale per provider senza wrapper |
| `flush` | `()` | Svuota queue sync (utile prima di shutdown) |
| `shutdown` | `()` | Stop thread + flush finale |

**Rilevamento provider in `wrap()`:**
- `"openai"` in `type(client).__module__` → `wrap_openai()`
- `"anthropic"` in `type(client).__module__` → `wrap_anthropic()`
- `class_name in ("OpenAI", "AsyncOpenAI")` → `wrap_openai()`
- Altrimenti → `ValueError`

### `transport.py` — classe `Transport`
Queue + background thread. Non blocca mai il thread principale.

| Attributo/Metodo | Tipo/Firma | Note |
|-----------------|------------|------|
| `_queue` | `Queue(maxsize=10_000)` | Drop silenzioso se piena (log WARNING) |
| `flush_interval` | `float` default `2.0s` | Quanto spesso svuotare |
| `batch_size` | `int` default `20` | Max eventi per chiamata API |
| `enqueue(event)` | `dict → None` | Non-blocking, drop se piena |
| `flush()` | `→ None` | Drain sincrono, chiama `_send()` |
| `_send(events)` | `list[dict] → None` | POST a `/v1/events/batch` con `X-API-Key` |
| `shutdown()` | `→ None` | `_stop.set()` + join thread (timeout 5s) + flush finale |

**Gestione errori `_send()`:** cattura `Exception`, logga a DEBUG (non solleva). Mai interrompe il codice utente per errori di rete.

### `pricing.py` — dizionario + funzione
Prezzi in USD per 1M token (input, output). Identico al file in `api/`.

| Funzione | Firma | Note |
|----------|-------|------|
| `compute_cost` | `(provider, model, prompt_tokens, completion_tokens) → float` | Fallback: prefix match su `model`. Ritorna `0.0` se provider/model non trovato |

**Provider coperti:** `openai`, `anthropic`, `google`, `mistral`
**Modelli chiave:**
- `gpt-4o`: ($2.50, $10.00)/1M
- `claude-sonnet-4-6`: ($3.00, $15.00)/1M
- `gpt-4o-mini`: ($0.15, $0.60)/1M
- `gemini-2.0-flash`: ($0.075, $0.30)/1M

### `wrappers/openai.py` — `wrap_openai(client, transport)`
Patcha `client.chat.completions.create` e `client.embeddings.create` in-place con `wrapt.decorator`.

**Kwargs speciali estratti (e rimossi dalla request):**
- `pw_feature` → `feature`
- `pw_user` → `user_id`
- `pw_prompt` → `prompt_name`
- `pw_version` → `prompt_version`

Alternativamente leggibili da `extra_headers`: `pw-feature`, `pw-user`, `pw-prompt`, `pw-version`.

**Token source:** `response.usage.prompt_tokens` / `response.usage.completion_tokens`

### `wrappers/anthropic.py` — `wrap_anthropic(client, transport)`
Patcha `client.messages.create` in-place.

**Token source:** `response.usage.input_tokens` / `response.usage.output_tokens`

**Kwargs speciali:** `pw_feature`, `pw_user`, `pw_prompt`, `pw_version` (rimossi prima della chiamata originale).

---

## 2. API — `api/`

### `main.py` — FastAPI app
- Lifespan: `Base.metadata.create_all` su startup (crea tabelle se non esistono)
- CORS: `allow_origins=["*"]` (restringere in produzione)
- Route prefix: `/auth`, `/v1`, `/projects`, `/dashboard`, `/alerts`, `/billing`
- Health: `GET /health → {"status": "ok"}`

### `config.py` — classe `Settings` (pydantic-settings)
Legge da `.env`. Tutte le variabili con default sicuri per dev.

| Variabile | Default | Usata da |
|-----------|---------|----------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | `database.py` |
| `REDIS_URL` | `redis://localhost:6379/0` | futuro |
| `SECRET_KEY` | `"dev_secret_..."` | JWT signing |
| `JWT_EXPIRE_MINUTES` | `10080` (7 giorni) | `auth.py` |
| `STRIPE_SECRET_KEY` | `""` | `billing.py` |
| `STRIPE_WEBHOOK_SECRET` | `""` | `billing.py` |
| `SMTP_*` | vuoti | `alerts_engine.py` |

### `database.py`
- `engine`: `create_async_engine` con echo in dev
- `AsyncSessionLocal`: `async_sessionmaker`
- `get_db()`: dependency FastAPI, commit su successo, rollback su eccezione

### `models.py` — SQLAlchemy ORM

#### `Organization`
| Colonna | Tipo | Note |
|---------|------|------|
| `id` | UUID | PK |
| `slug` | String(100) | unique, index |
| `plan` | Enum(Plan) | default `FREE` |
| `stripe_customer_id` | String nullable | settato da webhook |
| `stripe_subscription_id` | String nullable | settato da webhook |

#### `User`
| Colonna | Tipo | Note |
|---------|------|------|
| `org_id` | FK → organizations | index |
| `hashed_password` | String | bcrypt |
| `is_admin` | Boolean | default True per il founder |

#### `Project`
| Colonna | Tipo | Note |
|---------|------|------|
| `api_key` | String(64) | `"pw_" + secrets.token_urlsafe(32)`, unique, index |

#### `LLMEvent` — tabella principale
| Colonna | Tipo | Note |
|---------|------|------|
| `project_id` | FK → projects | index |
| `org_id` | String(36) | denormalizzato per query veloci |
| `provider` | String(50) | `openai`, `anthropic`, `google`, ecc. |
| `model` | String(100) | nome completo |
| `prompt_tokens` | Integer | |
| `completion_tokens` | Integer | |
| `cost_usd` | Float | calcolato da `pricing.compute_cost()` |
| `user_id` | String nullable | attribution per utente finale |
| `feature` | String nullable | attribution per feature dell'app, index |
| `prompt_name` | String nullable | nome del prompt versionato |
| `latency_ms` | Integer nullable | |
| `error` | Text nullable | messaggio di eccezione se fallita |
| `cached` | Boolean | default False |
| `metadata` | JSON nullable | solo su plan Growth/Scale |
| `created_at` | DateTime(tz) | index |

#### `AlertRule`
| Colonna | Tipo | Note |
|---------|------|------|
| `metric` | String(50) | `cost_usd`, `tokens`, `error_rate`, `latency_ms` |
| `operator` | String(10) | `gt`, `gte`, `lt`, `lte` |
| `threshold` | Float | |
| `window` | String(20) | `1h`, `24h`, `7d`, `30d` |
| `notify_email` | String nullable | |
| `notify_webhook` | String nullable | |
| `last_triggered_at` | DateTime nullable | usato per cooldown (no re-fire nella stessa window) |

#### `Plan` enum + `PLAN_LIMITS`
```python
PLAN_LIMITS = {
    FREE:    {"projects": 1, "tokens_per_month": 100_000},
    STARTER: {"projects": 1, "tokens_per_month": 1_000_000},
    GROWTH:  {"projects": 10, "tokens_per_month": 10_000_000},
    SCALE:   {"projects": -1, "tokens_per_month": -1},   # -1 = unlimited
}
```

---

## 3. Routers — `api/routers/`

### `auth.py`
| Endpoint | Metodo | Body | Response |
|----------|--------|------|----------|
| `/auth/register` | POST | `{email, password, org_name}` | `{token, org_id, user_id}` |
| `/auth/login` | POST | `{email, password}` | `{token, org_id, user_id}` |

**`get_current_user(credentials)`** — dependency globale. Valida JWT, ritorna `User`.
**JWT payload:** `{sub: user_id, org: org_id, exp: utcnow+7d}`

### `ingest.py`
| Endpoint | Metodo | Auth | Body |
|----------|--------|------|------|
| `/v1/events` | POST | `X-API-Key` header | `EventPayload` |
| `/v1/events/batch` | POST | `X-API-Key` header | `BatchPayload` (max 100 eventi) |

**`_get_project(api_key, db)`** — lookup Project+Organization in join unico.
**`_build_event(payload, project, org)`** — calcola costo, strip metadata se piano < Growth.

### `projects.py`
| Endpoint | Metodo | Note |
|----------|--------|------|
| `GET /projects/` | lista tutti i progetti dell'org |
| `POST /projects/` | crea nuovo, verifica limite piano |
| `DELETE /projects/{id}` | elimina progetto |
| `POST /projects/{id}/rotate-key` | rigenera api_key (`"pw_" + secrets.token_urlsafe(32)`) |

### `dashboard.py`
Tutti gli endpoint richiedono JWT. Tutti accettano `?project_id=` e `?window=` (1h/24h/7d/30d).

| Endpoint | Metodo | Output |
|----------|--------|--------|
| `GET /dashboard/summary` | aggregato totale (requests, tokens, cost, latency, error_rate) |
| `GET /dashboard/cost-by-model` | lista `{provider, model, requests, cost_usd, tokens}` ordinata per costo desc |
| `GET /dashboard/cost-by-feature` | top 20 feature per costo |
| `GET /dashboard/daily-cost` | serie temporale `{date, cost_usd, requests}` per N giorni |
| `GET /dashboard/recent-events` | ultimi N eventi (default 50, max 200) |

**`_parse_window(window: str) → datetime`** — converte `"7d"` in `utcnow - 7 days`.

### `alerts.py`
| Endpoint | Metodo | Note |
|----------|--------|------|
| `GET /alerts/` | lista regole dell'org |
| `POST /alerts/` | crea regola, valida metric e operator |
| `PATCH /alerts/{id}/toggle` | attiva/disattiva |
| `DELETE /alerts/{id}` | elimina |

**Metriche valide:** `cost_usd`, `tokens`, `error_rate`, `latency_ms`
**Operatori validi:** `gt`, `gte`, `lt`, `lte`

### `billing.py`
| Endpoint | Metodo | Note |
|----------|--------|------|
| `POST /billing/checkout/{plan}` | crea Stripe Checkout Session, ritorna `{url}` |
| `POST /billing/portal` | crea Stripe Billing Portal Session, ritorna `{url}` |
| `POST /billing/webhook` | gestisce eventi Stripe (signature verification) |

**Webhook eventi gestiti:**
- `checkout.session.completed` → aggiorna `org.plan`, `stripe_customer_id`, `stripe_subscription_id`
- `customer.subscription.deleted` → downgrade a `FREE`

**`PRICE_TO_PLAN`**: dict che mappa price_id Stripe → `Plan` enum. Riempire in `.env`.

---

## 4. Worker e Alert Engine — `api/worker.py` + `api/alerts_engine.py`

### `worker.py`
Processo separato (`python -m worker`). Avviato come secondo container Docker.

- `alert_loop()`: ogni 300s chiama `check_alerts()` su tutte le regole `is_active=True`
- `check_alerts()`: carica regole, chiama `evaluate_and_fire(rule, db)` per ciascuna

### `alerts_engine.py`

**`_get_metric_value(rule, db) → float`**
Calcola valore metrica nell'intervallo `window` filtrato su `org_id` (e `project_id` se presente):
- `cost_usd`: `SUM(cost_usd)`
- `tokens`: `SUM(total_tokens)`
- `error_rate`: `SUM(errors) / COUNT(*) * 100`
- `latency_ms`: `AVG(latency_ms)`

**`evaluate_and_fire(rule, db)`**
1. Calcola metrica
2. Applica operatore (dict `OPERATORS`)
3. Controlla cooldown: `last_triggered_at + window_delta > utcnow` → skip
4. Aggiorna `last_triggered_at`
5. Spara email (se `notify_email` e SMTP configurato) + webhook (se `notify_webhook`)

**`_send_email(to, subject, body)`** — aiosmtplib, STARTTLS su port 587
**`_send_webhook(url, rule, value)`** — httpx POST JSON con `{alert_id, metric, value, threshold, ...}`

**`WINDOW_DELTA`** dict: `"1h" → timedelta(hours=1)`, ecc.

---

## 5. Infrastruttura

### `docker-compose.yml`
| Service | Immagine/Build | Port | Note |
|---------|----------------|------|------|
| `api` | `./api` | 8000 | `--reload` in dev |
| `worker` | `./api` | — | `python -m worker` |
| `db` | `postgres:16-alpine` | 5432 | healthcheck pg_isready |
| `redis` | `redis:7-alpine` | 6379 | healthcheck redis-cli ping |

### `Makefile`
```
make up       # docker-compose up -d
make dev      # docker-compose up --build (con log)
make migrate  # alembic upgrade head
make test     # pytest api/tests/
make test-sdk # pytest sdk/tests/
make psql     # shell PostgreSQL
```

---

## 6. Test

### `sdk/tests/test_sdk.py` — 8 test, tutti PASSING
| Test | Cosa verifica |
|------|--------------|
| `test_manual_track` | `.track()` accoda evento correttamente |
| `test_pricing_openai` | gpt-4o = $2.50/1M input |
| `test_pricing_anthropic` | claude-sonnet-4-6 = $3.00/1M input |
| `test_pricing_unknown_model` | ritorna 0.0 per modello sconosciuto |
| `test_pricing_prefix_match` | `gpt-4o-mini-2024-07-18` matcha `gpt-4o-mini` |
| `test_wrap_openai` | wrap intercetta, estrae tokens, accoda evento con feature |
| `test_wrap_anthropic` | wrap Anthropic intercetta input/output tokens |
| `test_error_tracked` | eccezione nel client → evento accodato con campo `error` |

**Nota:** i mock per `wrap()` devono avere `mock.__class__.__module__ = "openai"` (o "anthropic") per passare il type detection.

### `api/tests/test_ingest.py`
| Test | Cosa verifica |
|------|--------------|
| `test_health` | `GET /health` ritorna 200 |
| `test_ingest_invalid_key` | chiave API invalida → 401 |

---

## 7. Stato attuale MVP (25/06/2026)

**Completato e funzionante:**
- [x] SDK Python: wrap OpenAI + Anthropic, track manuale, pricing, transport con batch queue
- [x] Ingest API: `POST /v1/events` e `/v1/events/batch` con `X-API-Key`
- [x] Auth: register/login, JWT 7gg, dependency `get_current_user`
- [x] Projects: CRUD + rotate API key, check limite piano
- [x] Dashboard API: 5 endpoint aggregazione (summary, by-model, by-feature, daily, events)
- [x] Alert rules: CRUD + toggle
- [x] Alert engine: worker 5min, email (aiosmtplib) + webhook (httpx)
- [x] Billing: Stripe checkout/portal/webhook con downgrade automatico
- [x] Docker + docker-compose + Makefile
- [x] CI/CD GitHub Actions (test-sdk su Python 3.9-3.12, test-api con postgres, lint ruff)
- [x] 8/8 SDK tests passanti

**Da completare (V1):**
- [ ] Dashboard HTML — HTMX frontend su FastAPI (route `GET /`, chart.js per daily-cost)
- [ ] Alembic migrations (ora usa `create_all` in dev, non adatto a prod)
- [ ] Deploy Hetzner CX21: Nginx + Let's Encrypt + systemd per worker
- [ ] SDK: Google Gemini wrapper
- [ ] Prompt versioning (modello dati + API)
- [ ] Rate limiting su `/v1/events` per api_key (Redis token bucket)

---

## 8. Come avviare in sviluppo

```bash
cd /home/magic/Scrivania/code/GIT/promptwatch
cp .env.example .env        # editare DATABASE_URL se serve
make dev                     # avvia tutto con log

# In altro terminale — test SDK senza rete:
/tmp/pw_venv/bin/pytest sdk/tests/ -v
```

**URL locali:**
- API: `http://localhost:8000`
- Docs Swagger: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

---

## 9. Pricing (EUR) e Business Model

| Piano | Prezzo | Progetti | Tokens/mo | Feature |
|-------|--------|----------|-----------|---------|
| Free | €0 | 1 | 100K | Solo tryout |
| Starter | €49/mo | 1 | 1M | Alert email |
| Growth | €149/mo | 10 | 10M | Alert + webhook + prompt versioning |
| Scale | €499/mo | ∞ | ∞ | A/B testing + SSO + priority |

**Target 12 mesi:** €10.000/mo = 68 clienti Growth. LTV/CAC stimato >15x (CAC €0 organic).

**GTM priorità:**
1. GitHub repo open source (SDK MIT) + README killer
2. "Show HN" su HackerNews
3. Product Hunt launch
4. SEO: "openai cost tracking", "llm api monitoring", "anthropic claude cost breakdown"

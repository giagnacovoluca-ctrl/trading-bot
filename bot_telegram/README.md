# bot_telegram

Signal SaaS su Telegram. Consuma **read-only** i `*_signals.csv` e `live_trades.csv`
già prodotti dagli scanner. Zero modifiche al core di trading, processo isolato
dall'executor. Vedi `DESIGN.md` per l'architettura.

## Setup
```bash
cp bot_telegram/.env.example bot_telegram/.env
# compila TELEGRAM_BOT_TOKEN (@BotFather) e gli ID canale
# (gli RPC Helius/Alchemy sono riusati automaticamente da executor/.env)
pip install -r bot_telegram/requirements.txt   # solo requests + python-dotenv
```

Crea due canali Telegram (FREE e PREMIUM, opzionale VIP), aggiungi il bot come
**admin** in ognuno, e metti gli ID (`-100...`) nel `.env`.

## Avvio
```bash
# tutto insieme (publisher + bot comandi + gating + track record + pagamenti)
python bot_telegram/run_bot.py

# solo l'MVP del feed (build step 1)
python bot_telegram/publisher.py

# flag per disattivare componenti
python bot_telegram/run_bot.py --no-payments --no-track
```

## Componenti
| File | Ruolo |
|------|-------|
| `config.py` | env (riusa `executor/.env` per gli RPC) |
| `csv_tail.py` | tail incrementale dei CSV, offset byte persistente (anti-repost) |
| `formatter.py` | messaggi HTML: `format_full` (Premium) / `format_teaser` (Free, no prezzo) |
| `telegram_api.py` | Bot API via requests, retry 429 |
| `publisher.py` | feed: full su Premium/VIP, teaser ritardato su Free |
| `subscriptions.py` | store abbonati (tier/scadenza/referral) |
| `bot.py` | comandi `/start /plans /subscribe /status /referral` + admin `/grant /stats /broadcast` |
| `payments.py` | verifica USDC on-chain (Base+Solana) via invoice a importo univoco |
| `track_record.py` | P&L da `live_trades.csv` → recap Free + `state/stats.json` (landing) |
| `run_bot.py` | orchestratore thread daemon con auto-restart |

## Tier
- **Free**: teaser ritardato 15m, senza entry price → recap performance giornaliero.
- **Premium** ($49/mese): tutti i segnali real-time + entry/liq/BSR + canale privato.
- **VIP** ($149/mese): + `pre_grad` e `mirror` (alpha più early). Se `TELEGRAM_VIP_CHANNEL_ID`
  è vuoto, questi segnali vanno sul canale Premium.

## Pagamenti
Modello a invoice: `/subscribe` genera un importo USDC univoco (es. `49.74`); il watcher
rileva il Transfer in entrata al wallet e attiva l'accesso. Finché non è validato sul campo,
usa il path manuale affidabile: `/grant <chat_id> <premium|vip> [giorni]` (solo admin).

## Stato runtime (`state/`, gitignored)
`offsets.json` (tail), `free_queue.json` (teaser schedulati), `subscribers.json`,
`invoices.json`, `paywatch.json`, `update_offset.json`, `stats.json`.

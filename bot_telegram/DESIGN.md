# bot_telegram — Signal SaaS (design)

Trasforma i segnali già generati dagli scanner in un servizio Telegram monetizzabile.
**Principio chiave:** zero modifiche agli scanner. Il bot è un *consumatore* read-only dei CSV
esistenti (`defi/reports/*_signals.csv`) e di `live_trades.csv`. Disaccoppia i guadagni dal
capitale proprio: 100 utenti × $50/mese = ricavo ricorrente a rischio capitale zero.

---

## Architettura

```
defi/reports/*_signals.csv ──┐
defi/reports/live_trades.csv ─┤
                              ▼
                     [csv_tail.py]  offset persistente (no repost al restart)
                              ▼
   ┌─────────── publisher.py (daemon) ───────────┐
   │  formatter.py  → messaggio MarkdownV2        │
   │  routing per tier:                           │
   │    FREE   → canale gratuito, ritardo 10-15m  │
   │    PREMIUM→ canale pagante, real-time, full  │
   └──────────────────┬───────────────────────────┘
                       ▼  Telegram Bot API (sendMessage, retry 429)
        ┌── canale FREE ──┐     ┌── canale PREMIUM ──┐

   [bot.py] comandi utente: /start /plans /subscribe /status
        │
   [subscriptions.py] store chat_id→{tier,expires_at}
        │
   [payments.py] verifica on-chain (riusa RPC Helius/Alchemy già in .env)
        │
   [track_record.py] aggrega live_trades.csv → recap giornaliero + stats.json (landing)
```

### File del modulo
| File | Ruolo |
|------|-------|
| `config.py` | legge env (BOT_TOKEN, channel id, wallet ricezione, prezzi tier) |
| `csv_tail.py` | tail incrementale dei CSV con offset in `state/offsets.json` |
| `formatter.py` | dict segnale → messaggio; link DexScreener/explorer per chain |
| `publisher.py` | loop daemon: legge nuovi segnali, route FREE/PREMIUM, invia |
| `bot.py` | command handler (python-telegram-bot) + gating canale |
| `subscriptions.py` | store abbonati (JSON→SQLite), check tier/scadenza |
| `payments.py` | verifica pagamenti crypto in entrata, estende scadenza |
| `track_record.py` | P&L da `live_trades.csv` → recap + `state/stats.json` |
| `state/` | offsets, subscribers, processed ids — **gitignored** |

---

## Punto 2 — Publisher (feed segnali)

- **Sorgenti**: i 4+1 CSV condividono lo schema
  `signal_id,timestamp_entry,token_symbol,token_name,token_address,chain,pair_address,price_entry_usd,volume_1h_usd,liquidity_usd,buy_sell_ratio_1h,change_1h_pct,pump_probability,buy_tax,sell_tax,lp_locked,is_honeypot,top_features`
  → un solo parser per tutti.
- **No-repost**: offset byte per file in `state/offsets.json`; al boot riparte da fine file
  (non spamma lo storico). `signal_id` come dedup secondario.
- **Tier routing**:
  - PREMIUM: tutti i segnali, istantanei, con `price_entry`, liq, BSR, link diretto.
  - FREE: solo segnali con `pump_probability ≥ soglia`, **ritardati 10-15 min** e **senza prezzo
    di entry esatto** (teaser) → incentivo all'upgrade.
- **Formato messaggio** (MarkdownV2, escaping gestito in `formatter.py`):
  ```
  🟢 NUOVO SEGNALE · pump_grad · SOLANA
  $TICKER  (prob 0.78)
  Entry $0.00042 · Vol1h $58k · Liq $120k · BSR 0.62 · 1h +18%
  🔗 DexScreener  |  ⛓ Solscan
  ```
- **Robustezza**: retry su 429 con backoff (limite Telegram ~20 msg/min per canale);
  daemon con auto-restart come gli altri componenti.
- **Avvio**: processo standalone `python bot_telegram/publisher.py` (isolamento dal core di
  trading → un crash del bot NON tocca executor). Opzionale: hook in `defi/run.py`.

## Punto 3 — Monetizzazione

- **Tier**:
  | Tier | Prezzo | Contenuto |
  |------|--------|-----------|
  | Free | $0 | segnali ritardati 15m, no entry price, recap giornaliero |
  | Premium | ~$49/mese | tutti i segnali real-time + entry/liq/BSR + canale privato |
  | VIP | ~$149/mese | + pre_grad & wallet-mirror alpha (più early, più rischioso) |
- **Pagamenti crypto-first** (riusa wallet/RPC già configurati, frizione minima per questo
  pubblico): l'utente invia USDC/SOL/ETH a un indirizzo di ricezione con memo = chat_id;
  `payments.py` polla on-chain via Helius/Alchemy, accredita, estende `expires_at`, invia
  invite-link monouso al canale Premium. Alternativa: Telegram Stars / Stripe.
- **Gating**: `subscriptions.py` tiene `chat_id → {tier, expires_at, referred_by}`. Un loop
  giornaliero rimuove gli scaduti dal canale Premium (revoke invite / kick).
- **Track record = motore di acquisizione**: `track_record.py` aggrega i trade chiusi di
  `live_trades.csv` (`pnl_eur`, `exit_reason`) → win-rate, P&L cumulato, best trade →
  recap automatico sul canale FREE + `state/stats.json` per una **landing page statica**
  (GitHub Pages) con performance verificabile. È l'unico vero ostacolo alla vendita: ce l'hai
  già nei dati, va solo esposto.
- **Referral**: codice per abbonato; chi porta un pagante guadagna estensione/sconto
  (`referred_by` nello store).

---

## Sicurezza / costi
- Token bot e wallet di ricezione **solo in `bot_telegram/.env`** (già coperto da `.gitignore`).
- Mai esporre i segnali real-time sul canale FREE: il delay è il prodotto.
- Costo infra ~0 (riusa scanner, RPC, dati esistenti). Unico costo: hosting del processo bot.

## Dipendenze (`requirements.txt`)
`python-telegram-bot>=21`, `python-dotenv`, `requests` (+ `web3`/`solana` già nel venv per `payments.py`).

---

## Ordine di build consigliato
1. `csv_tail.py` + `formatter.py` + `publisher.py` → feed sul **solo canale PREMIUM** (MVP funzionante in giornata).
2. `bot.py` + `subscriptions.py` → comandi e gating (gestione manuale abbonati all'inizio).
3. `track_record.py` → recap automatico + `stats.json` per la landing (credibilità → acquisizione).
4. `payments.py` → automazione pagamenti crypto (ultimo: prima valida che la gente paghi davvero, anche manualmente).

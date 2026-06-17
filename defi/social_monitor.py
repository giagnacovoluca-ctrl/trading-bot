"""
social_monitor.py
=================
Daemon Telethon: monitora canali Telegram crypto e calcola "social velocity"
per ticker. Output aggiornato ogni 2 min:

  reports/social_velocity.json   → {TICKER: {"1h":N, "4h":N, "24h":N, "last_ts":float}}
  reports/social_events.jsonl    → log grezzo (ticker, channel, ts) per backtest futuro

Integrazione: midcap_scanner.analyze_coin() legge social_velocity.json e aggiunge
±5pt se il ticker ha menzioni significative nelle ultime 24h.

Setup una tantum:
  1. Crea app su https://my.telegram.org → TELEGRAM_API_ID + TELEGRAM_API_HASH
     → aggiungili in executor/.env (o esporta come variabile env)
  2. python defi/social_monitor.py --auth
     (interattivo: telefono + OTP → crea reports/social.session, poi non serve più)
  3. Da quel momento: avviato automaticamente da run.py (--no-social per skippare)

Dipendenze:
  pip install telethon
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("social")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

_HERE        = Path(__file__).parent
REPORTS_DIR  = _HERE / "reports"
SESSION_FILE = REPORTS_DIR / "social"          # Telethon aggiunge .session
EVENTS_JSONL = REPORTS_DIR / "social_events.jsonl"
VELOCITY_JSON= REPORTS_DIR / "social_velocity.json"

VELOCITY_UPDATE_SEC = 120    # ricalcola velocity ogni 2 min
EVENTS_MAX_AGE_H    = 48     # conserva eventi solo ultime 48h

# ── Alert settings ────────────────────────────────────────────────────────────
ALERT_SPIKE_MIN_1H  = 5      # menzioni minime nell'ultima ora per triggerare
ALERT_SPIKE_RATIO   = 2.5    # 1h deve essere ≥ 2.5× la media oraria delle 4h precedenti
ALERT_COOLDOWN_S    = 4 * 3600
ALERT_MAX_PER_HOUR  = 8      # cap anti-flood

# Token sempre menzionati — esclusi dagli alert salvo spike straordinari
_ALWAYS_ON = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "DOT",
    "AVAX", "MATIC", "LINK", "UNI", "ATOM", "LTC", "TRX",
    "ICP", "APT", "NEAR", "OP", "ARB", "SUI", "TON", "NOT",
    "INJ", "TIA", "JUP", "RAY", "BONK", "WIF", "TRUMP", "MELANIA",
}

_alert_cooldown: dict = {}    # ticker → last_alert_ts
_alert_hour_bucket = [0, 0.0] # [count_nell'ora, ts_inizio_ora]

def _env(k, d=""): return os.environ.get(k, d)

API_ID   = int(_env("TELEGRAM_API_ID",   "0") or 0)
API_HASH = _env("TELEGRAM_API_HASH", "")

# ─────────────────────────────────────────────────────────────────────────────
# CANALI DA MONITORARE
# Tutti pubblici (nessun invite richiesto). Telethon li joina automaticamente
# al primo avvio se l'account non ne è già membro.
# ─────────────────────────────────────────────────────────────────────────────

CHANNELS = [
    # ── On-chain / Whale data ─────────────────────────────────────────────────
    "whale_alert",           # Whale Alert: grandi trasferimenti wallet↔CEX (70k+ utenti)
    "lookonchain",           # LookOnChain: wallet smart money Solana/ETH — il più alpha
    "cryptoquant_official",  # CryptoQuant: inflow/outflow stablecoin, macro on-chain

    # ── News velocity ─────────────────────────────────────────────────────────
    "Unfolded",              # Unfolded: breaking news formato pillola (single-line, regex-friendly)
    "CoinTelegraph",         # CoinTelegraph: news istituzionale
    "rektHQ",                # Rekt.news: exploit/hack → contatore NEGATIVO per token colpito
    # "WuBlockchain" rimosso — username inesistente, sostituito da wublockchainenglish

    # ── Alpha calls / hype ────────────────────────────────────────────────────
    "EveningTrader",         # Evening Trader: setup swing + menzioni ticker

    # ── Solana-specific ───────────────────────────────────────────────────────
    "solanadailynews",       # Solana Daily: news ecosistema Solana

    # ── Aggiunti 16/06 ───────────────────────────────────────────────────────
    "wublockgroup",              # Wu Blockchain group (community, multi-utente)
    "wublockchainenglish",       # Wu Blockchain English (canale news Asia)
    "DeFimillionfreesignals",    # DeFi Million free signals
    "cryptoninjaclub",           # Crypto Ninja Club (alpha calls altcoin)
]

# ─────────────────────────────────────────────────────────────────────────────
# TICKER EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

# Parole comuni in maiuscolo da ignorare (falsi positivi)
_STOPWORDS = {
    "USD", "EUR", "GBP", "JPY", "CNY",
    "THE", "AND", "FOR", "ALL", "NEW", "BIG", "TOP", "NOW",
    "CEX", "DEX", "NFT", "DeFi", "DAO", "TVL", "ATH", "ATL",
    "API", "P2P", "KYC", "AML", "ETF", "ICO", "IEO", "IDO",
    "TPS", "EVM", "RPC", "AMM", "LTV", "APY", "APR",
    "UTC", "EST", "PST", "CET",
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP",
}

_CASHTAG_RE = re.compile(r"\$([A-Z]{2,7})")
_ALLCAPS_RE = re.compile(r"\b([A-Z]{3,6})\b")


def extract_tickers(text: str) -> list[str]:
    """Estrae ticker dal testo. Cashtag ($SOL) = peso doppio (aggiunto 2 volte)."""
    if not text:
        return []
    upper = text.upper()
    result = []
    # $TICKER = alta confidenza → aggiunto due volte (peso 2)
    for m in _CASHTAG_RE.finditer(upper):
        t = m.group(1)
        if t not in _STOPWORDS:
            result.extend([t, t])
    # ALLCAPS word = bassa confidenza → aggiunto una volta
    for m in _ALLCAPS_RE.finditer(upper):
        t = m.group(1)
        if t not in _STOPWORDS and t not in {tt for tt in result}:
            result.append(t)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# VELOCITY: calcolo rolling windows da JSONL
# ─────────────────────────────────────────────────────────────────────────────

def _append_events(events: list[dict]):
    """Scrive eventi grezzi nel JSONL (una riga = un evento ticker)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _recompute_velocity():
    """
    Rilegge social_events.jsonl (ultime 24h) e scrive social_velocity.json
    con conteggi rolling: 1h / 4h / 24h per ticker.
    Elimina anche le righe >48h per non far crescere il file indefinitamente.
    """
    if not EVENTS_JSONL.exists():
        return

    now_ts = time.time()
    cutoff_keep  = now_ts - EVENTS_MAX_AGE_H * 3600
    cutoff_1h    = now_ts - 3600
    cutoff_4h    = now_ts - 4 * 3600
    cutoff_24h   = now_ts - 24 * 3600

    counts_1h  = defaultdict(int)
    counts_4h  = defaultdict(int)
    counts_24h = defaultdict(int)
    last_ts    = {}
    kept_lines = []

    try:
        with open(EVENTS_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    ts = float(ev.get("ts", 0))
                    if ts < cutoff_keep:
                        continue          # troppo vecchio, scarta
                    kept_lines.append(line)
                    ticker = ev.get("ticker", "")
                    if not ticker:
                        continue
                    if ts >= cutoff_24h:
                        counts_24h[ticker] += ev.get("weight", 1)
                        if ts > last_ts.get(ticker, 0):
                            last_ts[ticker] = ts
                    if ts >= cutoff_4h:
                        counts_4h[ticker] += ev.get("weight", 1)
                    if ts >= cutoff_1h:
                        counts_1h[ticker] += ev.get("weight", 1)
                except Exception:
                    kept_lines.append(line)   # mantieni righe non parsabili

        # Riscrivi solo le righe recenti (pruning)
        with open(EVENTS_JSONL, "w", encoding="utf-8") as f:
            for line in kept_lines:
                f.write(line + "\n")

    except Exception as e:
        log.warning(f"[social] recompute error: {e}")
        return

    velocity = {}
    for ticker in counts_24h:
        velocity[ticker] = {
            "1h":      counts_1h.get(ticker, 0),
            "4h":      counts_4h.get(ticker, 0),
            "24h":     counts_24h[ticker],
            "last_ts": last_ts.get(ticker, 0),
        }

    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = VELOCITY_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(velocity, indent=2), encoding="utf-8")
        tmp.replace(VELOCITY_JSON)
        log.debug(f"[social] velocity aggiornata: {len(velocity)} ticker")
    except Exception as e:
        log.warning(f"[social] scrittura velocity fallita: {e}")


def get_social_score(symbol: str) -> int:
    """
    Helper per midcap_scanner.analyze_coin().
    Legge social_velocity.json e ritorna un delta score:
      +5  = menzioni moderate nelle 24h (3-20): early hype
      +3  = menzioni alte (>20): già virale, meno edge
      -3  = associato a exploit/hack (rektHQ mention) nelle 4h
       0  = nessun dato o sotto soglia

    Non solleva eccezioni — se il file manca ritorna 0 silenziosamente.
    """
    if not VELOCITY_JSON.exists():
        return 0
    try:
        data = json.loads(VELOCITY_JSON.read_text(encoding="utf-8"))
        v = data.get(symbol.upper())
        if not v:
            return 0
        m24 = v.get("24h", 0)
        m1h = v.get("1h", 0)
        # Segnale "rekt" nelle ultime 4h (canale rektHQ tracciato separatamente)
        rekt = data.get(f"REKT_{symbol.upper()}", {}).get("4h", 0)
        if rekt > 0:
            return -3
        if   3 <= m24 <= 20: return 5
        elif m24 > 20:       return 3
        return 0
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# ALERT: spike detection + notifica via Telethon → Saved Messages
# ─────────────────────────────────────────────────────────────────────────────

def _detect_spikes(velocity: dict) -> list[dict]:
    """
    Ritorna lista di {ticker, m1h, m4h, ratio} per ticker con spike anomalo.
    Spike = m1h ≥ ALERT_SPIKE_MIN_1H AND m1h ≥ ALERT_SPIKE_RATIO × (m4h/4).
    I token in _ALWAYS_ON richiedono ratio ≥ 5× (soglia più alta).
    """
    now = time.time()
    spikes = []

    # Reset bucket orario
    if now - _alert_hour_bucket[1] >= 3600:
        _alert_hour_bucket[0] = 0
        _alert_hour_bucket[1] = now

    if _alert_hour_bucket[0] >= ALERT_MAX_PER_HOUR:
        return []

    for ticker, v in velocity.items():
        if ticker.startswith("REKT_"):
            continue

        m1h = v.get("1h", 0)
        m4h = v.get("4h", 0)
        if m1h < ALERT_SPIKE_MIN_1H:
            continue

        hourly_avg = m4h / 4 if m4h > 0 else 0.1
        ratio = m1h / hourly_avg

        threshold = ALERT_SPIKE_RATIO * 2 if ticker in _ALWAYS_ON else ALERT_SPIKE_RATIO
        if ratio < threshold:
            continue

        if now - _alert_cooldown.get(ticker, 0) < ALERT_COOLDOWN_S:
            continue

        spikes.append({"ticker": ticker, "m1h": m1h, "m4h": m4h, "ratio": ratio})

    # Ordina per ratio decrescente, rispetta il cap orario
    spikes.sort(key=lambda x: x["ratio"], reverse=True)
    remaining = ALERT_MAX_PER_HOUR - _alert_hour_bucket[0]
    return spikes[:remaining]


async def _send_spike_alerts(client, spikes: list[dict], velocity: dict):
    """Manda notifiche ai Saved Messages (me) per ogni spike rilevato."""
    if not spikes:
        return
    now = time.time()
    for sp in spikes:
        ticker = sp["ticker"]
        m1h    = sp["m1h"]
        m4h    = sp["m4h"]
        ratio  = sp["ratio"]

        # Trova i canali sorgente dalla velocity (campo non ancora in JSON —
        # usiamo il JSONL per look back veloce sugli eventi recenti)
        sources = _get_recent_channels(ticker, window_s=3600)
        src_str = ", ".join(sources[:3]) if sources else "n/d"

        flag = "🚨" if ticker in _ALWAYS_ON else "📡"
        text = (
            f"{flag} <b>Social Spike: ${ticker}</b>\n"
            f"Menzioni 1h: <b>{m1h}</b>  |  4h: {m4h}  |  ratio: {ratio:.1f}×\n"
            f"Canali: {src_str}"
        )
        try:
            await client.send_message("me", text, parse_mode="html")
            _alert_cooldown[ticker] = now
            _alert_hour_bucket[0] += 1
            log.info(f"[social] 📡 Alert inviato: ${ticker} {m1h}× in 1h (ratio {ratio:.1f}×)")
        except Exception as e:
            log.warning(f"[social] alert send fallito per {ticker}: {e}")


def _get_recent_channels(ticker: str, window_s: int = 3600) -> list[str]:
    """Legge JSONL e ritorna i canali distinti che hanno menzionato ticker nell'ultima finestra."""
    if not EVENTS_JSONL.exists():
        return []
    cutoff = time.time() - window_s
    channels = []
    try:
        with open(EVENTS_JSONL, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line.strip())
                    if ev.get("ticker") == ticker and float(ev.get("ts", 0)) >= cutoff:
                        ch = ev.get("channel", "")
                        if ch and ch not in channels:
                            channels.append(ch)
                except Exception:
                    pass
    except Exception:
        pass
    return channels


# ─────────────────────────────────────────────────────────────────────────────
# DAEMON PRINCIPALE (Telethon async)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_async(stop_event: threading.Event):
    """Loop Telethon principale. Esce quando stop_event è settato."""
    try:
        from telethon import TelegramClient, events
    except ImportError:
        log.error("[social] telethon non installato — pip install telethon")
        return

    if not API_ID or not API_HASH:
        log.error("[social] TELEGRAM_API_ID / TELEGRAM_API_HASH mancanti in executor/.env")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSION_FILE.with_suffix(".session").exists():
        log.error(
            "[social] Session file mancante — esegui:\n"
            "  python defi/social_monitor.py --auth\n"
            "per autenticarti una volta con il tuo numero di telefono."
        )
        return

    client = TelegramClient(str(SESSION_FILE), API_ID, API_HASH)
    await client.start()

    # Sincronizza lo stato degli update (pts) — senza questo Telegram
    # non invia gli update dai canali a sessioni "stale"
    await client.get_dialogs()

    # Join canali pubblici non ancora seguiti e pre-risolvi a entity objects
    valid_entities: list = []
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch)
            valid_entities.append(entity)
        except Exception:
            try:
                from telethon.tl.functions.channels import JoinChannelRequest
                await client(JoinChannelRequest(ch))
                entity = await client.get_entity(ch)
                valid_entities.append(entity)
                log.info(f"[social] Joined {ch}")
            except Exception as e:
                log.warning(f"[social] {ch} non raggiungibile, saltato: {e}")

    log.info(f"[social] {len(valid_entities)}/{len(CHANNELS)} canali risolti")
    pending_events = []
    last_velocity_update = 0.0

    @client.on(events.NewMessage())
    async def _on_message(event):
        text = event.message.message or ""
        if not text:
            return
        channel = getattr(event.chat, "username", "") or getattr(event.chat, "title", "") or str(event.chat_id)
        ts_now  = time.time()
        is_rekt = "rekt" in channel.lower()

        tickers = extract_tickers(text)
        evts = []
        for t in tickers:
            key = f"REKT_{t}" if is_rekt else t
            evts.append({
                "ts":      ts_now,
                "ticker":  key,
                "channel": channel,
            })

        if evts:
            pending_events.extend(evts)
            log.debug(f"[social] {channel}: {len(evts)} ticker estratti")

    log.info(f"[social] Daemon attivo su {len(CHANNELS)} canali")

    async def _periodic_loop():
        nonlocal last_velocity_update
        while not stop_event.is_set():
            await asyncio.sleep(30)
            if pending_events:
                batch = pending_events[:]
                pending_events.clear()
                _append_events(batch)

            now = time.time()
            if now - last_velocity_update >= VELOCITY_UPDATE_SEC:
                _recompute_velocity()
                last_velocity_update = now
                if VELOCITY_JSON.exists():
                    try:
                        velocity = json.loads(VELOCITY_JSON.read_text(encoding="utf-8"))
                        spikes = _detect_spikes(velocity)
                        if spikes:
                            await _send_spike_alerts(client, spikes, velocity)
                    except Exception as e:
                        log.debug(f"[social] spike check error: {e}")

        await client.disconnect()
        log.info("[social] Client disconnesso")

    asyncio.ensure_future(_periodic_loop())
    await client.run_until_disconnected()


def main(stop_event: threading.Event = None):
    """Entry point per run.py."""
    _stop = stop_event or threading.Event()
    try:
        asyncio.run(_run_async(_stop))
    except Exception as e:
        log.error(f"[social] Errore: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP INTERATTIVO (--auth)
# ─────────────────────────────────────────────────────────────────────────────

def _run_auth():
    """Autenticazione interattiva — crea il session file."""
    try:
        from telethon.sync import TelegramClient as SyncClient
    except ImportError:
        print("Installa Telethon prima: pip install telethon")
        return

    if not API_ID or not API_HASH:
        print("Aggiungi in executor/.env:\n  TELEGRAM_API_ID=...\n  TELEGRAM_API_HASH=...")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Session file: {SESSION_FILE.with_suffix('.session')}")
    with SyncClient(str(SESSION_FILE), API_ID, API_HASH) as client:
        client.start()
        me = client.get_me()
        print(f"Autenticato come: {me.username or me.phone}")
        print("Session salvata. Ora puoi avviare run.py con il social monitor.")


if __name__ == "__main__":
    _root = Path(__file__).parent.parent
    _exec = _root / "executor"
    for _p in [str(Path(__file__).parent), str(_root), str(_exec)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=_exec / ".env", override=False)
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    # Ricarica API_ID/HASH dopo load_dotenv
    API_ID   = int(os.environ.get("TELEGRAM_API_ID",   "0") or 0)
    API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true", help="Setup interattivo session")
    a = parser.parse_args()

    if a.auth:
        _run_auth()
    else:
        print("Usa --auth per il setup iniziale, oppure avvia tramite run.py")

"""
trade_simulator.py
==================
Due modalità in uno stesso processo:

  1. BACKTEST  (run_backtest)
     Simula entrate/uscite su dati storici CSV.
     Produce report CSV + HTML.

  2. LIVE ENGINE  (LiveEngine)
     Processo persistente. Legge i nuovi segnali da CSV man mano
     che gemmeV2/V3/defi_optimized li generano, fetcha prezzi e
     volumi in real-time da DexScreener, e decide autonomamente
     di chiudere o mantenere ogni posizione in base a:
       • TP1 / TP2 (take profit a ordini limite)
       • Trailing stop (si arma dopo picco >= trail_activate_pct)
       • Stop loss adattivo (N snapshot consecutivi negativi sotto soglia)
       • Exit adattiva snap1 (solo DEFI: se snap1 < -20% esci subito)
       • Momentum filter: volume corrente vs volume di entry
       • BSR filter: se buy/sell ratio crolla sotto soglia, forza uscita

Configs ottimizzati dai risultati storici:
  DEFI    : TP1=+15%, TP2=+40%, trail>=+12%/-10%, hard SL -8%, SL adattivo -15%/3snap
  V2      : TP1=+15%, TP2=+40%, trail>=+12%/-10%, SL adattivo -15%/3snap
  V3      : TP1=+20%, TP2=+50%, trail>=+15%/-12%, SL adattivo -20%/4snap
  V3_LARGE: TP1=+20%, TP2=+60%, trail>=+25%/-18%, SL adattivo -28%/6snap (mid/large cap, fino a 7gg)

Capitale per trade: 100 EUR (simulazione, nessun costo reale).
"""

import csv
import html
import json
import logging
import re
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import requests

from data_quality import MIN_VOLUME_1H_USD_DEFI, is_valid_trade_event

try:
    import websocket   # websocket-client — usato da _RugWatcher (monitor real-time pump_grad)
    WS_OK = True
except ImportError:
    WS_OK = False

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trade_sim")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE          = os.path.dirname(os.path.abspath(__file__))
DEFI_SIGNALS      = os.path.join(BASE, "reports", "signals_log.csv")
PUMP_GRAD_SIGNALS = os.path.join(BASE, "reports", "pump_grad_signals.csv")
DEFI_FOLLOWUP = os.path.join(BASE, "reports",       "price_followup.csv")
_GEMME_DIR    = os.path.join(BASE, "..", "gemme", "reports")
V2_SIGNALS    = os.path.join(_GEMME_DIR, "gems_log.csv")
V2_FOLLOWUP   = os.path.join(_GEMME_DIR, "gems_followup.csv")
V3_SIGNALS    = os.path.join(_GEMME_DIR, "gems_log_v3.csv")
V3_FOLLOWUP   = os.path.join(_GEMME_DIR, "gems_followup_v3.csv")
BF_SIGNALS    = os.path.join(BASE, "reports", "binance_futures_signals.csv")
MIRROR_SIGNALS    = os.path.join(BASE, "reports", "mirror_signals.csv")
PRE_GRAD_SIGNALS  = os.path.join(BASE, "reports", "pre_grad_signals.csv")
REAL_EXEC_CSV     = os.path.join(BASE, "..", "executor", "real_executions.csv")
BASE_EXEC_CSV     = os.path.join(BASE, "..", "executor", "base_executions.csv")
BASE_PUMP_SIGNALS  = os.path.join(BASE, "reports", "base_pump_signals.csv")
MIDCAP_SIGNALS     = os.path.join(BASE, "reports", "midcap_signals.csv")
SHADOW_CSV         = os.path.join(BASE, "reports", "pump_grad_shadow.csv")
LIQ_SHADOW_QUEUE   = os.path.join(BASE, "reports", "liq_shadow_queue.csv")
V3_EXIT_SIGNALS   = os.path.join(BASE, "reports", "v3_exit_signals.csv")
WALLET_EVENTS_CSV = os.path.join(BASE, "reports", "wallet_events.csv")

LIVE_LOG_CSV  = os.path.join(BASE, "reports", "live_trades.csv")
DATA_FAULT_CSV = os.path.join(BASE, "reports", "data_fault_trades.csv")
LIVE_HTML     = os.path.join(BASE, "reports", "sim_report.html")
STATE_FILE    = os.path.join(BASE, "reports", "live_state.json")

CAPITAL_EUR   = 100.0

# Capitale per tier gemmeV3 (position sizing basato sulla confidenza del segnale)
CAPITAL_BY_TIER = {
    "DIAMOND": 150.0,
    "GOLD":    100.0,
    "SILVER":   60.0,
    "BRONZE":   30.0,
}

# Blacklist hard post-SL: token_address → expiry datetime (48h)
_hard_sl_blacklist: dict = {}

# Cooldown ore per tipo di exit — usato sia in _load_new_signals che in _process_position
_COOLDOWN_MAP: dict = {
    "entry":             8,
    "liq_collapse":      24,
    "hard_sl":           24,
    "sl_adaptive":       24,
    "exit_bsr_collapse": 4,
    "exit_vol_crash":    4,
    "exit_adaptive":     4,
    "exit_momentum":     4,
}

# Circuit breaker: blocca nuovi segnali se perdita 24h supera soglia.
# Default 0 = DISATTIVATO (11/06): in paper trading il blocco toglie solo dati
# — congelava tutti i sistemi quando pre_grad/base_pump perdevano (-122€ alle
# 13:27 → midcap incluso fermo). Riattivabile con MAX_DAILY_LOSS_EUR=-80 in env
# se/quando si passa a esecuzione reale.
MAX_DAILY_LOSS_EUR = float(os.getenv("MAX_DAILY_LOSS_EUR", "0"))
_daily_pnl_cache: dict = {"val": 0.0, "ts": 0.0}
_DAILY_PNL_CACHE_TTL = 120  # secondi



# ---------------------------------------------------------------------------
# Configs ottimizzati post-analisi risultati storici
# ---------------------------------------------------------------------------
CONFIGS = {
    "defi": dict(
        tp1_pct              = 15.0,
        tp2_pct              = 40.0,
        tp1_fraction         = 0.50,
        tp1_trail_only       = True,   # dopo TP1: trail invece di TP2 fisso
        tp1_trail_atr_mult   = 2.0,
        adaptive_snap1_exit  = -20.0,
        trail_activate_pct   = 12.0,   # era 6→10→12: trail non si arma mai sotto TP1 (15%), cattura solo move seri
        trail_drop_pct       = 8.0,    # era 7 → 8: trail adattivo gestisce i big movers, qui protegge i piccoli
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -15.0,
        hard_sl_pct          = -8.0,
        vol_drop_exit_ratio  = 0.20,
        bsr_exit_threshold   = 0.45,   # era 0.50 → 0.45: exit_quality mostra 67% BSR prematuri, richiede più pressione vendita
        bsr_confirm_count    = 7,      # era 5 → 7: exit_quality <10min = 71% prematuri, più conferme riducono i falsi segnali
    ),
    # ── pump.fun PRE-graduation — token ancora sulla bonding curve ───────────
    # Entrata a ~72 SOL (8 SOL prima della graduation a 80 SOL).
    # La graduation pump è tipicamente +30-80% dal prezzo bonding curve.
    # TP1 più alto (40%) per catturare tutto il pump post-graduation.
    # SL più stretto (-12%): se dumpa sulla bonding curve esci subito.
    # Time limit: 20 min (se non gradua in 20 min → exit bonding curve sell).
    "pre_grad": dict(
        tp1_pct              = 40.0,   # graduation pump tipico +30-80%, target conservativo 40%
        tp2_pct              = 100.0,
        tp1_fraction         = 1.00,   # vende tutto (come pump_grad)
        tp1_trail_only       = False,
        tp1_trail_atr_mult   = 2.0,
        adaptive_snap1_exit  = -8.0,   # esci subito se -8% (stesso di pump_grad)
        trail_activate_pct   = 15.0,
        trail_drop_pct       = 18.0,
        sl_consecutive_neg   = 2,      # meno paziente: 2 snap neg invece di 3
        sl_threshold_pct     = -12.0,
        hard_sl_pct          = -12.0,
        vol_drop_exit_ratio  = 0.15,
        bsr_exit_threshold   = 0.45,
    ),
    # ── pump.fun graduation — token appena migrati a Raydium ─────────────────
    # Pattern tipico: graduation pump +50-300% in 1-2h, poi retracement 20-30%
    # Rispetto a defi standard: TP1 più alto, trail più largo, SL leggermente più ampio
    "pump_grad": dict(
        # 25/06: backtest n=266 LIQ_ token_outcomes (post-18/06) — median peak=93.3%.
        # TP1=25%→50%: WR cala solo 65.8%→56.8% (-9pp) ma E[pnl] +6.76€/t vs -0.66€/t.
        # Il gap 25%-35% è solo 3.8% dei segnali (62.0% vs 65.8%) → quasi nessuna perdita.
        # shadow liq_queue (liq<25k) mostrava solo 17.9% a 35% — ma LIQ_ >25k pompano molto
        # di più (mediana 93%). Rivalutare dopo 3 settimane.
        tp1_pct              = 50.0,
        tp2_pct              = 80.0,
        tp1_fraction         = 1.00,   # vende 100% al tp1: pool liquida al picco, evita trail su pool morte
        tp1_trail_only       = False,  # no trail: uscita unica e pulita
        tp1_trail_atr_mult   = 2.0,
        adaptive_snap1_exit  = -8.0,   # abbassato -15→-8%: cattura rug precoci (GRUMBO -10.3% non veniva preso)
        trail_activate_pct   = 15.0,   # trail si arma a +15% (non 6%: pump token rimbalzano)
        trail_drop_pct       = 18.0,   # consente 18% di retracement dal picco (volatile)
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -20.0,
        hard_sl_pct          = -12.0,  # SL leggermente più largo (volatilità graduation)
        vol_drop_exit_ratio  = 0.15,   # vol_crash stretto: se il volume crolla → exit
        bsr_exit_threshold   = 0.45,   # solo selling pressure forte giustifica exit
    ),
    "mirror": dict(
        # Wallet alpha copiati: stessa volatilità pump_grad ma edge superiore (smart money)
        # → vale tenere 50% per catturare outlier (SPX +893%, SPCXx +871% il 17/06)
        tp1_pct              = 25.0,
        tp2_pct              = 300.0,  # outlier mirror spesso vanno 5-10x
        tp1_fraction         = 1.00,   # 100% al TP1: outlier mirror non raggiungibili in Jupiter (WR 86% LIQ)
        tp1_trail_only       = False,
        tp1_trail_atr_mult   = 2.0,
        adaptive_snap1_exit  = -8.0,
        trail_activate_pct   = 15.0,
        trail_drop_pct       = 18.0,
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -20.0,
        hard_sl_pct          = -12.0,
        vol_drop_exit_ratio  = 0.15,
        bsr_exit_threshold   = 0.45,
    ),
    "v2": dict(
        tp1_pct              = 15.0,   # abbassato da 25 (mediano TP1 era T+240min!)
        tp2_pct              = 40.0,   # abbassato da 60
        tp1_fraction         = 0.50,
        adaptive_snap1_exit  = -20.0,  # aggiunto: era assente, ora protegge anche V2
        trail_activate_pct   = 12.0,   # abbassato da 20
        trail_drop_pct       = 10.0,   # abbassato da 15
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -15.0,
        hard_sl_pct          = -8.0,   # hard SL anche su V2
        vol_drop_exit_ratio  = 0.20,   # allineato a defi
        bsr_exit_threshold   = 0.50,   # abbassato da 0.60 (allineato a defi)
    ),
    "v3": dict(
        tp1_pct              = 20.0,   # V3 ha win rate 59%, TP più alto sostenibile
        tp2_pct              = 50.0,
        tp1_fraction         = 0.50,
        adaptive_snap1_exit  = -20.0,
        trail_activate_pct   = 15.0,
        trail_drop_pct       = 12.0,
        sl_consecutive_neg   = 4,      # V3 è più paziente (token più solidi)
        sl_threshold_pct     = -20.0,
        hard_sl_pct          = -15.0,  # cap perdita: V3 può includere token volatili (pumpswap)
        vol_drop_exit_ratio  = 0.25,   # meno aggressivo: V3 token hanno volume più stabile
        vol_crash_grace_min  = 25.0,   # exit_quality: 100% vol_crash prematuri < 20min → grace a 25min
        bsr_exit_threshold   = 0.55,
    ),
    "bnf": dict(
        # Binance Futures: large-cap, già quotati su CEX, movimenti più contenuti
        tp1_pct              = 8.0,    # TP1 conservativo per large-cap
        tp2_pct              = 20.0,
        tp1_fraction         = 0.50,
        adaptive_snap1_exit  = -5.0,   # esci subito se snap1 < -5%
        trail_activate_pct   = 6.0,    # trailing si arma a +6%
        trail_drop_pct       = 3.0,    # trailing drop -3% (tight per large-cap)
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -7.0,   # SL più stretto (large-cap meno volatili)
        vol_drop_exit_ratio  = 0.20,   # permissivo (vol Binance molto stabile)
        bsr_exit_threshold   = 0.40,   # non usato (BSR fisso a 1.0 su Binance)
    ),
    "v3_large": dict(
        # Mid/large cap da CoinGecko (mcap > $10M): timeframe settimanale/mensile
        # Più paziente su SL/trailing, TP2 più alto, finestra di hold 7 giorni
        tp1_pct              = 20.0,   # stesso TP1 di v3
        tp2_pct              = 60.0,   # TP2 più alto: mossa grande richiede spazio
        tp1_fraction         = 0.40,   # chiudi solo 40% a TP1, lascia 60% per TP2
        adaptive_snap1_exit  = -15.0,  # meno aggressivo di v3 (-20): dà più respiro
        trail_activate_pct   = 20.0,   # abbassato 25→20: si arma al TP1 → protegge il 60% rimanente subito
        trail_drop_pct       = 14.0,   # tolera retracement fino a -14% dal picco (era 18%)
        sl_consecutive_neg   = 6,      # 6 snap negativi consecutivi (vs 4 di v3)
        sl_threshold_pct     = -28.0,  # SL più largo: large cap hanno drawdown profondi
        vol_drop_exit_ratio  = 0.15,   # molto permissivo su volume (large cap stabile)
        vol_crash_grace_min  = 25.0,
        bsr_exit_threshold   = 0.45,   # permissivo su BSR
    ),
    "v3_midcap": dict(
        # Mid-cap da CoinGecko ($5M–$300M, source=coingecko_midcap/trending)
        # CEX-listati, no rug risk → no hard_sl, TP conservativi, trailing aggressivo
        tp1_pct              = 12.0,   # TP1 basso: mid-cap muovono piano
        tp2_pct              = 30.0,
        tp1_fraction         = 0.50,
        adaptive_snap1_exit  = -15.0,
        trail_activate_pct   = 8.0,    # trail si arma presto (token stabili)
        trail_drop_pct       = 5.0,    # trailing stretto: protegge profitti rapidamente
        sl_consecutive_neg   = 5,
        sl_threshold_pct     = -12.0,
        hard_sl_pct          = None,   # no hard SL: token $40M+ non ruggano in 5 min
        vol_drop_exit_ratio  = 0.20,
        vol_crash_grace_min  = 25.0,
        bsr_exit_threshold   = 0.45,
    ),
    "midcap": dict(
        # midcap_scanner: BB Squeeze su candele DAILY, CEX spot (binance/mexc/gateio)
        # mid/large cap già quotati → no rug risk, book reali, mosse lente su giorni.
        # Stessa filosofia di v3_midcap (no hard_sl, trailing stretto) ma orizzonte
        # di hold più lungo (segnale daily, non intraday): vedi MAX_SIGNAL_AGE_H.
        tp1_pct              = 12.0,   # mid-cap muovono piano: TP1 conservativo
        tp2_pct              = 30.0,
        tp1_fraction         = 0.50,
        adaptive_snap1_exit  = -15.0,
        # 15/06: era 8.0/5.0 — analisi book aperto (34 pos) mostrava round-trip
        # sistematico: picchi +4/+8% mai armavano il trailing e ritornavano
        # a -3/-7% (Q, UB, GEOD, 币安人生, M...). Abbassato per proteggere
        # i picchi minori più comuni. Live-monitoring: validare dopo 2-3 settimane.
        trail_activate_pct   = 5.0,
        trail_drop_pct       = 3.0,
        # 25/06: backtest n=7 sl_adaptive → tutti usciti tra -12% e -17% (median -12.5%).
        # CEX daily-candle volatilità ATR >> 12% → sl_adaptive scattava su noise normale.
        # WR algo (senza manual_pause) era 74.1% ma PnL negativo per sole 7 loss sl_adaptive.
        # Raised threshold -12%→-20%, window 5→8 (4min): chiede conferma più lunga
        # su calo significativo prima di uscire. Rivalutare dopo 3-4 settimane.
        sl_consecutive_neg   = 8,
        sl_threshold_pct     = -20.0,
        hard_sl_pct          = None,   # no hard SL: CEX spot mid/large-cap non rugga
        vol_drop_exit_ratio  = 0.20,
        vol_crash_grace_min  = 25.0,
        bsr_exit_threshold   = 0.45,   # non usato (bsr fisso a 1.0 su CEX spot)
    ),
    # ── Base chain new pool listing — Uniswap V3 + Aerodrome ─────────────────
    # Stesso pattern di pump_grad: token freschi, liquidità appena creata,
    # potenziale pump breve. Stessi parametri di pump_grad come baseline.
    "base_pump": dict(
        tp1_pct              = 25.0,
        tp2_pct              = 80.0,
        tp1_fraction         = 1.00,   # vende 100% al tp1: pool fresca, rischio rug
        tp1_trail_only       = False,
        tp1_trail_atr_mult   = 2.0,
        adaptive_snap1_exit  = -8.0,   # exit rapida se dump subito
        trail_activate_pct   = 15.0,
        trail_drop_pct       = 18.0,
        sl_consecutive_neg   = 3,
        sl_threshold_pct     = -20.0,
        hard_sl_pct          = -12.0,
        vol_drop_exit_ratio  = 0.15,
        bsr_exit_threshold   = 0.45,
    ),
}

def _compute_daily_pnl() -> float:
    """P&L reale delle ultime 24h da live_trades.csv (cache 2 min).
    pnl_eur è CUMULATIVO per segnale: il 24h corretto è il delta tra l'ultima
    riga del segnale e l'ultima riga PRIMA della finestra. Sommare le righe
    double-conta le uscite parziali (+156€ mostrati vs +64€ reali al 12/06 —
    stesso bug class del track record gonfiato 4x fixato il 10/06)."""
    now = time.time()
    if now - _daily_pnl_cache["ts"] < _DAILY_PNL_CACHE_TTL:
        return _daily_pnl_cache["val"]
    last: dict[str, float] = {}    # sid → pnl cumulativo ultima riga in finestra
    base: dict[str, float] = {}    # sid → pnl cumulativo ultima riga pre-finestra
    try:
        with open(LIVE_LOG_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    pnl = float((row.get("pnl_eur") or "0").replace("+", ""))
                    ts  = datetime.fromisoformat(row["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                if row.get("system") == "mirror":
                    continue
                sid = row.get("signal_id") or row.get("token_symbol") or "?"
                if now - ts > 86400:
                    base[sid] = pnl
                else:
                    last[sid] = pnl
    except OSError:
        pass
    total = sum(p - base.get(sid, 0.0) for sid, p in last.items())
    _daily_pnl_cache.update({"val": total, "ts": now})
    return total


# ---------------------------------------------------------------------------
# Struttura risultato trade (backtest + live)
# ---------------------------------------------------------------------------
@dataclass
class TradeResult:
    system:            str
    signal_id:         str
    token_symbol:      str
    capital:           float = CAPITAL_EUR
    exit_reason:       str   = "open"
    n_snapshots:       int   = 0
    tp1_hit:           bool  = False
    tp1_pct:           float = 0.0
    tp1_minutes:       int   = 0
    tp2_hit:           bool  = False
    tp2_pct:           float = 0.0
    tp2_minutes:       int   = 0
    trailing_hit:      bool  = False
    trailing_exit_pct: float = 0.0
    trailing_minutes:  int   = 0
    sl_hit:            bool  = False
    sl_exit_pct:       float = 0.0
    sl_minutes:        int   = 0
    last_snap_pct:     float = 0.0
    last_minutes:      int   = 0
    pnl_eur:           float = 0.0
    pnl_pct:           float = 0.0


# ===========================================================================
# BACKTEST ENGINE
# ===========================================================================

def simulate_trade(signal_id: str, token_symbol: str, system: str,
                   snapshots: pd.DataFrame) -> TradeResult:
    """Simula un singolo trade sui dati storici (snapshots già raccolti)."""
    cfg    = CONFIGS[system]
    result = TradeResult(system=system, signal_id=signal_id, token_symbol=token_symbol)

    if snapshots.empty:
        result.exit_reason = "no_data"
        return result

    snaps = snapshots.sort_values("minutes_since_entry").dropna(subset=["change_pct"])
    result.n_snapshots = len(snaps)

    if snaps.empty:
        result.exit_reason = "no_data"
        return result

    tp1_pct      = cfg["tp1_pct"]
    tp2_pct      = cfg["tp2_pct"]
    tp1_fraction = cfg["tp1_fraction"]
    tp2_fraction = 1.0 - tp1_fraction
    snap1_exit   = cfg["adaptive_snap1_exit"]
    trail_act    = cfg["trail_activate_pct"]
    trail_drop   = cfg["trail_drop_pct"]
    sl_n         = cfg["sl_consecutive_neg"]
    sl_thresh    = cfg["sl_threshold_pct"]
    MAX_CAP      = 2000.0

    last_row = snaps.iloc[-1]
    result.last_snap_pct = float(last_row["change_pct"])
    result.last_minutes  = int(last_row["minutes_since_entry"])

    # Exit adattiva snap1
    first_row = snaps.iloc[0]
    if snap1_exit is not None and float(first_row["change_pct"]) < snap1_exit:
        ep = float(first_row["change_pct"])
        result.exit_reason   = "adaptive"
        result.last_snap_pct = ep
        result.last_minutes  = int(first_row["minutes_since_entry"])
        result.pnl_eur       = CAPITAL_EUR * ep / 100.0
        result.pnl_pct       = ep
        return result

    remaining     = 1.0
    pnl_parts: List[float] = []
    peak_pct      = float("-inf")
    neg_streak     = 0

    for _, row in snaps.iterrows():
        chg  = float(row["change_pct"])
        mins = int(row["minutes_since_entry"])

        if chg > peak_pct:
            peak_pct = chg

        # Streak negativi per SL adattivo
        if chg < 0:
            neg_streak += 1
        else:
            neg_streak = 0

        # SL adattivo: N snap consecutivi negativi E P&L sotto soglia
        if (remaining > 1e-6 and not result.sl_hit
                and neg_streak >= sl_n and chg <= sl_thresh):
            result.sl_hit      = True
            result.sl_exit_pct = chg
            result.sl_minutes  = mins
            pnl_parts.append(CAPITAL_EUR * remaining * max(chg, -100.0) / 100.0)
            remaining = 0.0
            break

        # TP1
        if not result.tp1_hit and chg >= tp1_pct:
            result.tp1_hit     = True
            result.tp1_pct     = chg
            result.tp1_minutes = mins
            pnl_parts.append(CAPITAL_EUR * tp1_fraction * tp1_pct / 100.0)
            remaining -= tp1_fraction

        # TP2
        if result.tp1_hit and not result.tp2_hit and chg >= tp2_pct:
            result.tp2_hit     = True
            result.tp2_pct     = chg
            result.tp2_minutes = mins
            pnl_parts.append(CAPITAL_EUR * tp2_fraction * tp2_pct / 100.0)
            remaining -= tp2_fraction
            break

        # Trailing stop
        if remaining > 1e-6 and not result.trailing_hit:
            if peak_pct >= trail_act and (peak_pct - chg) >= trail_drop:
                result.trailing_hit      = True
                result.trailing_exit_pct = chg
                result.trailing_minutes  = mins
                pnl_parts.append(CAPITAL_EUR * remaining * min(chg, MAX_CAP) / 100.0)
                remaining = 0.0
                break

    if remaining > 1e-6:
        pnl_parts.append(CAPITAL_EUR * remaining * min(result.last_snap_pct, MAX_CAP) / 100.0)

    result.pnl_eur = sum(pnl_parts)
    result.pnl_pct = result.pnl_eur / CAPITAL_EUR * 100.0

    if result.tp2_hit:
        result.exit_reason = "tp1_tp2"
    elif result.tp1_hit and result.trailing_hit:
        result.exit_reason = "tp1_trail"
    elif result.tp1_hit and result.sl_hit:
        result.exit_reason = "tp1_sl"
    elif result.tp1_hit:
        result.exit_reason = "tp1_only"
    elif result.sl_hit:
        result.exit_reason = "sl_adaptive"
    elif result.trailing_hit:
        result.exit_reason = "trailing_sl"
    elif result.exit_reason != "adaptive":
        result.exit_reason = "last_snap"

    return result


def run_simulation(system: str, signals_path: str,
                   followup_path: str) -> pd.DataFrame:
    log.info(f"[backtest/{system}] {signals_path}")
    signals  = pd.read_csv(signals_path,  on_bad_lines="skip")
    followup = pd.read_csv(followup_path, on_bad_lines="skip")
    if "gem_id" in followup.columns:
        followup = followup.rename(columns={"gem_id": "signal_id"})
    if "gem_id" in signals.columns:
        signals  = signals.rename(columns={"gem_id": "signal_id"})
    log.info(f"  {len(signals)} segnali | {len(followup)} snapshot")
    results = []
    for _, sig in signals.iterrows():
        sid   = str(sig.get("signal_id", ""))
        sym   = str(sig.get("token_symbol", ""))
        snaps = followup[followup["signal_id"] == sid].copy()
        results.append(simulate_trade(sid, sym, system, snaps))
    return pd.DataFrame([vars(r) for r in results])


def print_summary(df: pd.DataFrame, system: str):
    valid = df[df.exit_reason != "no_data"]
    n     = len(valid)
    if n == 0:
        log.info(f"[{system.upper()}] Nessun dato valido.")
        return
    cfg     = CONFIGS[system]
    wr      = (valid.pnl_eur > 0).mean() * 100
    print(f"\n{'='*55}")
    print(f"  {system.upper()}  (TP1={cfg['tp1_pct']}% TP2={cfg['tp2_pct']}%"
          f" trail>={cfg['trail_activate_pct']}%/{cfg['trail_drop_pct']}%"
          f" SL={cfg['sl_consecutive_neg']}snap/{cfg['sl_threshold_pct']}%)")
    print(f"{'='*55}")
    print(f"  Trade validi : {n}")
    print(f"  Win rate     : {wr:.1f}%")
    print(f"  P&L totale   : {valid.pnl_eur.sum():+.1f} EUR")
    print(f"  P&L medio    : {valid.pnl_eur.mean():+.2f} EUR/trade")
    print(f"  P&L mediano  : {valid.pnl_eur.median():+.2f} EUR/trade")
    for reason in ["tp1_tp2","tp1_trail","tp1_sl","tp1_only","sl_adaptive",
                   "trailing_sl","adaptive","last_snap"]:
        cnt = (valid.exit_reason == reason).sum()
        if cnt:
            print(f"    {reason:<16}: {cnt:>4} ({cnt/n*100:.1f}%)")



def run_backtest():
    print("="*55)
    print("  BACKTEST — gemmeV2 / gemmeV3 / defi")
    print(f"  Capitale: {CAPITAL_EUR} EUR/trade")
    print("="*55)
    for label, path in [
        ("DEFI signals", DEFI_SIGNALS), ("DEFI followup", DEFI_FOLLOWUP),
        ("V2 signals",   V2_SIGNALS),   ("V2 followup",   V2_FOLLOWUP),
        ("V3 signals",   V3_SIGNALS),   ("V3 followup",   V3_FOLLOWUP),
    ]:
        if not os.path.exists(path):
            log.warning(f"  Mancante: {label} ({path})")
    results = {}
    for sys_name, sig_path, fu_path in [
        ("defi", DEFI_SIGNALS, DEFI_FOLLOWUP),
        ("v3",   V3_SIGNALS,   V3_FOLLOWUP),
    ]:
        try:
            df = run_simulation(sys_name, sig_path, fu_path)
            results[sys_name] = df
            print_summary(df, sys_name)
        except Exception as e:
            log.warning(f"[backtest/{sys_name}] {e}")
    if results:
        all_df = pd.concat(list(results.values()), ignore_index=True)
        detail = os.path.join(BASE, "reports", "sim_results_detail.csv")
        all_df.to_csv(detail, index=False)
        log.info(f"[backtest] Salvato: {detail}")
    return results


# ===========================================================================
# LIVE ENGINE
# ===========================================================================

# Finestra massima per sistema (da analisi P&L per bucket entry-lag, 2026-05-29):
#   DEFI: solo il bucket 0-1h è positivo (+196€); 4h+ è catastrofico (-635€ su 64 trade).
#         Ridotto da 12h → 3h: cattura freschi + marginali 1-3h, blocca stantii 3h+.
#   V2:   TP1 mediano 17.5h, p90 60h         → 48h
#   V3:   TP1 mediano 12.5h, SL lento 10h    → 48h
MAX_SIGNAL_AGE_H: dict = {"defi": 3, "v2": 48, "v3": 48, "bnf": 6, "v3_large": 168, "v3_midcap": 24, "pump_grad": 1, "mirror": 1, "pre_grad": 0.33, "midcap": 48}
# midcap: segnale daily (scanner ogni 4h, breakout su candela giornaliera) → validità
# entry più larga (48h, ~12 cicli scanner) e max hold = 48h*3 = 144h (6gg, vedi riga ~1718)
MAX_SIGNAL_AGE_H_DEFAULT = 24   # fallback
REFRESH_SEC      = 10    # abbassato 30→10s per ridurre latenza entry LIQ Base (<5min pool)

# Catene abilitate — BSC/ETH disabilitati; BASE abilitato al loro posto.
# Per riabilitare: ALLOWED_CHAINS = {"solana", "bsc", "ethereum", "base"}
ALLOWED_CHAINS: set = {"solana", "base", "cex_spot"}
MAX_CLOSED_SHOW  = 100   # trade chiusi mostrati nel report

LIVE_COLUMNS = [
    "ts", "signal_id", "system", "token_symbol", "chain", "pair_address",
    "action", "price", "change_pct", "vol_h1", "bsr",
    "pump_prob", "prepump_score",
    "remaining", "pnl_eur", "exit_reason", "note",
]


def _fetch_price_binance(symbol: str, timeout: int = 8):
    """Fetch prezzo corrente da Binance Futures. Ritorna (price, vol_1h_usd, bsr, liq) o None.
    bsr è sempre 1.0 (non disponibile via REST), liq è inf (non applicabile su CEX)."""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            timeout=timeout,
        )
        r.raise_for_status()
        t     = r.json()
        price = float(t.get("lastPrice", 0) or 0)
        vol24 = float(t.get("quoteVolume", 0) or 0)
        vol1h = vol24 / 24.0   # stima 1h da vol24
        return price, vol1h, 1.0, float("inf")
    except Exception:
        return None


def _fetch_price_cex_spot(symbol: str, timeout: int = 8):
    """Fetch prezzo spot CEX con cascata binance → mexc → gateio (stesso ordine
    e simboli di midcap_scanner: i token possono migrare tra i tre exchange nel
    tempo, da qui il retry in cascata anche in fase di tracking).
    `symbol` nel formato ccxt "SOON/USDT". Ritorna (price, vol_1h_usd, bsr, liq)
    o None. bsr=1.0 e liq=inf: non disponibili/non applicabili su CEX spot
    (book reali con profondità propria, non pool DEX da monitorare per rug)."""
    base_quote = symbol.replace("/", "")
    pair_gate  = symbol.replace("/", "_")
    attempts = [
        ("binance", "https://api.binance.com/api/v3/ticker/24hr",
         {"symbol": base_quote}, "lastPrice", "quoteVolume"),
        ("mexc", "https://api.mexc.com/api/v3/ticker/24hr",
         {"symbol": base_quote}, "lastPrice", "quoteVolume"),
        ("gateio", "https://api.gateio.ws/api/v4/spot/tickers",
         {"currency_pair": pair_gate}, "last", "quote_volume"),
    ]
    for ex_id, url, params, price_key, vol_key in attempts:
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            t = data[0] if isinstance(data, list) else data
            if not t:
                continue
            price = float(t.get(price_key, 0) or 0)
            vol24 = float(t.get(vol_key, 0) or 0)
            if price > 0:
                return price, vol24 / 24.0, 1.0, float("inf")
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Rug watcher — monitor real-time via Helius WS dei pool pump_grad appena aperti.
#
# Perché: brainfry/Taylor/SHIBURAI (07/06, -89%/-73%/-70%) sono "rug" che drenano
# liquidità e prezzo nella stessa transazione atomica — tra un poll e l'altro
# (REFRESH_SEC=30s) il prezzo "salta" da vicino-entry a -70/-90%, scavalcando
# hard_sl (-12%) che non fa in tempo a vedere i livelli intermedi.
# Soluzione: sottoscriviamo le transazioni sui pool pump_grad appena aperti
# (transactionSubscribe, stesso pattern di wallet_mirror_bot) e ad ogni tx che
# tocca il pool scateniamo un _process_position immediato invece di aspettare
# il prossimo poll — la latenza di rilevazione scende da ~30s a ~1-5s.
#
# Costo/rischio chiamate DexScreener: limitato a)nei primi FAST_CHECK_WINDOW_MIN
# (dove avvengono i rug osservati: 17-18 min), b) con debounce per pool, c) solo
# sui pool effettivamente toccati dalla tx (parsing accountKeys, non "tutti i
# pool aperti" — evita di moltiplicare i fetch per N posizioni simultanee).
# ---------------------------------------------------------------------------

FAST_CHECK_WINDOW_MIN   = 15.0   # entro quanti minuti dall'apertura attivare il WS fast-check
FAST_CHECK_DEBOUNCE_SEC = 5.0    # min secondi tra due fetch innescati da WS sullo stesso pool


class _RugWatcher:
    """Sottoscrizione dinamica via `logsSubscribe` (RPC Solana standard, non
    l'Atlas Enhanced di Helius — quello richiede piano "developer o superiore",
    indisponibile sul piano attuale: vedi errore -32403 osservato in produzione)
    ai pool pump_grad appena aperti. Una sub per pool con filtro `mentions`:
    ogni notifica corrisponde univocamente a quel pool, niente parsing di
    accountKeys/transazioni. Notifica via callback quale pool è stato toccato,
    cosicché LiveEngine rifaccia subito il fetch del prezzo invece di aspettare
    il prossimo poll a 30s. Nessun impatto se HELIUS_API_KEY/websocket-client
    mancano o se l'RPC nega le subscription: resta inattivo, polling normale."""

    _SILENCE_MAX     = 90    # secondi senza messaggi con pool attivi → forza reconnect
    _RECONNECT_S     = 10   # backoff base
    _RECONNECT_429_S = 300  # backoff su 429 (Helius rate limit): 5 minuti
    _MAX_SUB_ERRORS  = 3     # subscription rifiutate consecutive → disabilita (piano non supportato)

    def __init__(self, on_pool_activity):
        self._on_activity  = on_pool_activity   # callback(pair_address: str)
        self._watched: set = set()
        self._sub_by_pool: dict = {}   # pair_address → subscription_id
        self._pool_by_sub: dict = {}   # subscription_id → pair_address
        self._pending: dict = {}       # request_id → pair_address (in attesa di conferma)
        self._lock         = threading.Lock()
        self._ws           = None
        self._last_msg_ts  = time.time()
        self._stop         = threading.Event()
        self._sub_errors   = 0
        self._enabled      = bool(HELIUS_API_KEY and WS_OK)
        self._got_429      = False     # flag: ultimo close era 429 → backoff lungo

    def start(self):
        if not self._enabled:
            log.debug("[rug_watch] disattivo (manca HELIUS_API_KEY o websocket-client)")
            return
        threading.Thread(target=self._watchdog_loop, daemon=True).start()
        threading.Thread(target=self._run_forever, daemon=True).start()
        log.info("[rug_watch] avviato — fast-check WS attivo per pump_grad nei primi "
                 f"{FAST_CHECK_WINDOW_MIN:.0f} min dall'apertura")

    def stop(self):
        self._stop.set()
        if self._ws:
            try: self._ws.close()
            except Exception: pass

    def sync(self, pools: set):
        """Aggiorna l'insieme dei pool da monitorare (chiamato ad ogni ciclo da
        LiveEngine): sottoscrive i nuovi, disiscrive quelli non più aperti."""
        if not self._enabled:
            return
        with self._lock:
            if pools == self._watched:
                return
            added   = pools - self._watched
            removed = self._watched - pools
            self._watched = set(pools)
        for pa in added:
            self._subscribe_pool(pa)
        for pa in removed:
            self._unsubscribe_pool(pa)

    # -- WS lifecycle ------------------------------------------------------
    def _run_forever(self):
        url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        while not self._stop.is_set():
            self._got_429 = False
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close,
                )
                self._ws = ws
                # reconnect= rimosso: gestione backoff manuale per distinguere 429 da altri errori
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.debug(f"[rug_watch] WS crash: {e}")
            if not self._stop.is_set() and self._enabled:
                wait = self._RECONNECT_429_S if self._got_429 else self._RECONNECT_S
                if self._got_429:
                    log.info(f"[rug_watch] 429 Helius — backoff {wait}s prima di riprovare")
                time.sleep(wait)

    def _watchdog_loop(self):
        """Pattern ripreso da pre_grad_monitor: forza reconnect se il WS resta
        silenzioso troppo a lungo mentre ci sono pool attivi da monitorare."""
        while not self._stop.is_set():
            time.sleep(20)
            with self._lock:
                _has_watched = bool(self._watched)
            if (self._enabled and self._ws and _has_watched
                    and (time.time() - self._last_msg_ts) > self._SILENCE_MAX):
                log.warning(f"[rug_watch] WS silenzioso >{self._SILENCE_MAX}s con pool attivi → reconnect")
                try: self._ws.close()
                except Exception: pass

    def _disable(self, reason: str):
        if not self._enabled:
            return
        self._enabled = False
        log.warning(f"[rug_watch] disattivato definitivamente: {reason} — "
                    "si torna al solo polling a 30s, nessun impatto sul resto del sistema")
        self.stop()

    # -- subscription management -------------------------------------------
    def _subscribe_pool(self, pair_address: str):
        if not self._ws:
            return
        req_id = int(time.time() * 1000) % 1_000_000
        with self._lock:
            self._pending[req_id] = pair_address
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": req_id, "method": "logsSubscribe",
                "params": [{"mentions": [pair_address]}, {"commitment": "confirmed"}],
            }))
        except Exception as e:
            log.debug(f"[rug_watch] subscribe {pair_address[:8]}…: {e}")

    def _unsubscribe_pool(self, pair_address: str):
        if not self._ws:
            return
        with self._lock:
            sub_id = self._sub_by_pool.pop(pair_address, None)
            if sub_id is not None:
                self._pool_by_sub.pop(sub_id, None)
        if sub_id is None:
            return
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) % 1_000_000,
                "method": "logsUnsubscribe", "params": [sub_id],
            }))
        except Exception as e:
            log.debug(f"[rug_watch] unsubscribe {pair_address[:8]}…: {e}")

    def _resubscribe_all(self):
        """Dopo una riconnessione le subscription precedenti sono perse: ripristina."""
        with self._lock:
            self._sub_by_pool.clear()
            self._pool_by_sub.clear()
            self._pending.clear()
            pools = list(self._watched)
        for pa in pools:
            self._subscribe_pool(pa)

    # -- callbacks ----------------------------------------------------------
    def _on_open(self, ws):
        self._ws = ws
        self._last_msg_ts = time.time()
        self._resubscribe_all()

    def _on_message(self, ws, raw):
        self._last_msg_ts = time.time()
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Conferma/errore di subscribe (risposta con stesso "id" della richiesta)
        if "id" in msg:
            req_id = msg.get("id")
            with self._lock:
                pa = self._pending.pop(req_id, None)
            if pa is None:
                return
            err = msg.get("error")
            if err is not None:
                with self._lock:
                    self._sub_errors += 1
                    n_err = self._sub_errors
                code = err.get("code") if isinstance(err, dict) else err
                log.debug(f"[rug_watch] subscribe rifiutata per {pa[:8]}… ({code})")
                if code == -32403 or n_err >= self._MAX_SUB_ERRORS:
                    self._disable("logsSubscribe non disponibile su questo piano Helius (-32403)")
                return
            with self._lock:
                self._sub_errors = 0
                sub_id = msg.get("result")
                if isinstance(sub_id, int):
                    self._sub_by_pool[pa] = sub_id
                    self._pool_by_sub[sub_id] = pa
            return

        # Notifica logsNotification: {"params": {"subscription": <id>, "result": {...}}}
        try:
            params = msg.get("params") or {}
            sub_id = params.get("subscription")
            with self._lock:
                pa = self._pool_by_sub.get(sub_id)
            if pa:
                self._on_activity(pa)
        except Exception:
            pass

    def _on_error(self, ws, err):
        err_str = str(err)
        if "429" in err_str or "max usage" in err_str.lower():
            self._got_429 = True
        log.debug(f"[rug_watch] WS errore: {err}")

    def _on_close(self, ws, code, msg):
        if code == 429 or "429" in str(msg or ""):
            self._got_429 = True
        log.debug(f"[rug_watch] WS chiuso (code={code})")


# ── Smart money overlap (wallet_events.csv del wallet_mirror_bot) ────────────
# SOLO annotazione/visibilità nel note dell'entry: nessun filtro o boost finché
# un backtest non ne valida l'edge (regola: win bloccati vs loss evitate).
_SM_WINDOW_H   = 6.0
_sm_cache      = {"ts": 0.0, "buys": {}}   # mint → set(wallet)


def _smart_money_count(token_address: str) -> int:
    """Quanti wallet alpha distinti hanno comprato questo mint nelle ultime 6h.
    Legge la coda di wallet_events.csv (cache 60s); 0 se file assente."""
    if not token_address:
        return 0
    now_t = time.time()
    if now_t - _sm_cache["ts"] > 60:
        buys: dict = {}
        try:
            if os.path.exists(WALLET_EVENTS_CSV):
                size = os.path.getsize(WALLET_EVENTS_CSV)
                with open(WALLET_EVENTS_CSV, "r", encoding="utf-8", errors="replace") as f:
                    if size > 200_000:
                        f.seek(size - 200_000)
                        f.readline()  # scarta riga troncata
                    cutoff = now_t - _SM_WINDOW_H * 3600
                    for line in f:
                        # colonne: ts,wallet,side,mint,usd,confluence,wake_days,note
                        parts = line.rstrip("\n").split(",")
                        if len(parts) < 4 or parts[2] != "buy":
                            continue
                        try:
                            ev_ts = datetime.fromisoformat(parts[0]).timestamp()
                        except Exception:
                            continue
                        if ev_ts >= cutoff:
                            buys.setdefault(parts[3], set()).add(parts[1])
        except Exception as e:
            log.debug(f"[smart_money] lettura wallet_events: {e}")
        _sm_cache["buys"] = buys
        _sm_cache["ts"]   = now_t
    return len(_sm_cache["buys"].get(token_address, ()))


def _parse_dex_pair(pair: dict):
    """Estrae (price, vol_h1, bsr, liq, token_addr) da un oggetto pair DexScreener."""
    price      = float(pair.get("priceUsd") or 0)
    vol        = float((pair.get("volume") or {}).get("h1") or 0)
    txns       = pair.get("txns", {}).get("h1", {})
    buys       = int(txns.get("buys", 0))
    sells      = int(txns.get("sells", 0))
    bsr        = buys / (buys + sells) if (buys + sells) > 0 else 1.0
    liq        = float((pair.get("liquidity") or {}).get("usd") or 0)
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    return price, vol, bsr, liq, token_addr


def _fetch_price(pair_address: str, chain: str, timeout: int = 8):
    """Fetch prezzo corrente da DexScreener.
    Prova prima l'endpoint diretto, poi la search API come fallback.
    Ritorna (price, vol_h1, bsr, liq, token_addr) o None."""
    c = chain.lower()
    # 1. Endpoint diretto per chain
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/{c}/{pair_address}"
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        data  = r.json()
        pairs = data.get("pairs") or ([data.get("pair")] if data.get("pair") else [])
        pair  = pairs[0] if pairs else None
        if pair and float(pair.get("priceUsd") or 0) > 0:
            return _parse_dex_pair(pair)
    except Exception:
        pass
    # 2. Fallback: search API (funziona per qualsiasi chain/formato)
    try:
        url2 = f"https://api.dexscreener.com/latest/dex/search?q={pair_address}"
        r2 = requests.get(url2, timeout=timeout)
        r2.raise_for_status()
        data2 = r2.json()
        pairs2 = data2.get("pairs") or []
        pair2  = next(
            (p for p in pairs2
             if (p.get("pairAddress") or "").lower() == pair_address.lower()),
            pairs2[0] if pairs2 else None
        )
        if pair2 and float(pair2.get("priceUsd") or 0) > 0:
            return _parse_dex_pair(pair2)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Jupiter price feed — fonte primaria per posizioni Solana.
# DexScreener resta usato per vol/bsr/liq; Jupiter per il prezzo eseguibile.
# ---------------------------------------------------------------------------

_JUP_QUOTE_URL   = "https://api.jup.ag/swap/v1/quote"
_JUP_TOKEN_URL   = "https://tokens.jup.ag/token/"
_USDC_MINT_SOL   = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_jup_no_route:   set          = set()   # token_address senza route Jupiter (skip per sessione)
_jup_last_t:     float        = 0.0     # throttle: min 1.5s tra chiamate quote endpoint
_token_dec_cache: dict[str, int] = {}   # token_address → decimali SPL (cache per sessione)


def _get_solana_token_decimals(token_address: str) -> int | None:
    """Fetcha i decimali SPL del token dal catalogo Jupiter (cached per sessione).
    Ritorna None se il token non è nel catalogo; si cade su trial-and-error."""
    if token_address in _token_dec_cache:
        return _token_dec_cache[token_address]
    try:
        r = requests.get(_JUP_TOKEN_URL + token_address, timeout=5)
        if r.status_code == 200:
            dec = r.json().get("decimals")
            if dec is not None:
                _token_dec_cache[token_address] = int(dec)
                return int(dec)
    except Exception:
        pass
    return None


def _real_buy_price(sid: str) -> float | None:
    """
    Prezzo di esecuzione reale (price_actual) registrato dall'executor in
    real_executions.csv per il BUY di `sid`, se presente.

    pre_grad: il prezzo del segnale è stimato dalla bonding curve e può
    differire enormemente (anche 2x) dal prezzo Jupiter al momento
    dell'esecuzione, pochi secondi dopo la graduation. Usare price_actual
    come entry_price evita drop fantasma misurati da un prezzo mai pagato.
    """
    try:
        with open(REAL_EXEC_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("signal_id") == sid and r.get("action") == "buy":
                    p = float(r.get("price_actual", 0) or 0)
                    return p if p > 0 else None
    except Exception:
        pass
    return None


_RAYDIUM_QUOTE_URL = "https://transaction-v1.raydium.io/compute/swap-base-in"

def _fetch_price_jupiter(token_address: str, entry_price_usd: float,
                          timeout: int = 8,
                          pair_address: str = "") -> float | None:
    """
    Prezzo Solana token → USDC con cascata: Raydium → Jupiter → DexScreener.
    Raydium: nessun throttle, nessuna API key, risponde in <1s.
    Jupiter: throttle 1.5s/call, fallback se Raydium non ha route.
    DexScreener: spot price da pair_address, ultimo fallback se entrambi down.
    Mai restituisce None se almeno una fonte risponde con un prezzo plausibile.
    """
    import math
    global _jup_last_t

    if not token_address or entry_price_usd <= 0:
        return None

    QUOTE_USD = 10.0
    known_dec = _get_solana_token_decimals(token_address)
    decimals_to_try = (known_dec,) if known_dec is not None else (6, 9)

    def _price_from_output(usdc_out_lam: int, lamports: int, decimals: int) -> float | None:
        usdc_out  = usdc_out_lam / 1_000_000
        tokens_in = lamports / (10 ** decimals)
        if usdc_out <= 0 or tokens_in <= 0:
            return None
        price = usdc_out / tokens_in
        ratio = price / entry_price_usd
        if not (0.001 <= ratio <= 1000):
            return None
        return price

    # ── 1. Raydium (nessun throttle, no API key) ──────────────────────────────
    if token_address not in _jup_no_route:
        candidates = []
        for decimals in decimals_to_try:
            lamports = int((QUOTE_USD / entry_price_usd) * (10 ** decimals))
            if lamports <= 0:
                continue
            try:
                r = requests.get(_RAYDIUM_QUOTE_URL, params={
                    "inputMint":   token_address,
                    "outputMint":  _USDC_MINT_SOL,
                    "amount":      str(lamports),
                    "slippageBps": "50",
                    "txVersion":   "V0",
                }, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("success"):
                        out_lam = int(data.get("data", {}).get("outputAmount", 0))
                        price = _price_from_output(out_lam, lamports, decimals)
                        if price:
                            log_dist = abs(math.log10(price / entry_price_usd))
                            candidates.append((log_dist, price, decimals))
            except Exception as e:
                log.debug(f"[Raydium] {token_address[:16]}… {e}")
        if candidates:
            candidates.sort()
            best_price, best_dec = candidates[0][1], candidates[0][2]
            if known_dec is None and token_address not in _token_dec_cache:
                _token_dec_cache[token_address] = best_dec
            return best_price

    # ── 2. Jupiter (throttle 1.5s) ────────────────────────────────────────────
    elapsed = time.time() - _jup_last_t
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _jup_last_t = time.time()

    if token_address not in _jup_no_route:
        candidates = []
        for decimals in decimals_to_try:
            lamports = int((QUOTE_USD / entry_price_usd) * (10 ** decimals))
            if lamports <= 0:
                continue
            try:
                r = requests.get(_JUP_QUOTE_URL, params={
                    "inputMint":        token_address,
                    "outputMint":       _USDC_MINT_SOL,
                    "amount":           str(lamports),
                    "slippageBps":      "50",
                    "onlyDirectRoutes": "false",
                    "maxAccounts":      "64",
                }, timeout=timeout)
                if r.status_code == 400:
                    _jup_no_route.add(token_address)
                    break
                r.raise_for_status()
                data    = r.json()
                out_lam = int(data.get("outAmount", 0))
                price   = _price_from_output(out_lam, lamports, decimals)
                if price:
                    log_dist = abs(math.log10(price / entry_price_usd))
                    candidates.append((log_dist, price, decimals))
            except Exception as e:
                log.debug(f"[Jupiter] {token_address[:16]}… {e}")
                break
        if candidates:
            candidates.sort()
            best_price, best_dec = candidates[0][1], candidates[0][2]
            if known_dec is None and token_address not in _token_dec_cache:
                _token_dec_cache[token_address] = best_dec
            return best_price

    # ── 3. DexScreener (spot price da pair_address) ───────────────────────────
    pa = pair_address.strip() if pair_address else ""
    if pa and pa.lower() != "nan":
        result = _fetch_price(pa, "solana")
        if result:
            price = result[0]  # tupla (price, vol_h1, bsr, liq, token_addr)
            if price and price > 0:
                ratio = price / entry_price_usd
                if 0.001 <= ratio <= 1000:
                    log.debug(f"[price] {token_address[:12]}… DexScreener fallback: {price:.4g}")
                    return price

    return None


_CG_API_KEY = os.environ.get("COINGECKO_API_KEY_SIM") or os.environ.get("COINGECKO_API_KEY", "")
_CG_PLATFORM = {"ethereum": "ethereum", "bsc": "binance-smart-chain", "solana": "solana", "base": "base"}

def _fetch_price_coingecko(token_address: str, chain: str, timeout: int = 10):
    """Fallback CoinGecko via contract address. Ritorna (price, 0, 1.0, 0, '') o None."""
    platform = _CG_PLATFORM.get(chain.lower())
    if not platform or not token_address:
        return None
    url = (f"https://api.coingecko.com/api/v3/simple/token_price/{platform}"
           f"?contract_addresses={token_address}&vs_currencies=usd")
    try:
        r = requests.get(url, headers={"x-cg-demo-api-key": _CG_API_KEY}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        entry = data.get(token_address.lower()) or data.get(token_address)
        if not entry:
            return None
        price = float(entry.get("usd") or 0)
        if price <= 0:
            return None
        return price, 0, 1.0, 0, token_address
    except Exception:
        return None


_pf_sol_cache: dict = {"price": 0.0, "ts": 0.0}


_SOLANA_RPC      = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
_PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _derive_bonding_curve(mint: str) -> str:
    """PDA della bonding curve pump.fun per un mint."""
    try:
        from solders.pubkey import Pubkey
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM_ID),
        )
        return str(pda)
    except Exception:
        return ""


def _decode_bonding_curve(data_b64: str):
    """(vSol, vTokens, complete) o None. Layout anchor: 8B discriminator +
    5×u64 (virtual_token, virtual_sol, real_token, real_sol, total_supply) + bool complete."""
    import base64
    import struct
    try:
        raw = base64.b64decode(data_b64)
        if len(raw) < 49:
            return None
        vtok_raw, vsol_lamports = struct.unpack_from("<QQ", raw, 8)
        complete = bool(raw[48])
        return vsol_lamports / 1_000_000_000, vtok_raw / 1_000_000, complete
    except Exception:
        return None


def _fetch_price_pumpfun(mint: str) -> Optional[tuple]:
    """
    Prezzo dalla bonding curve pump.fun on-chain (frontend-api.pump.fun è morta, 530).
    Ritorna (price_usd, 0, 1.0, liq_usd, mint) oppure None.
    Se il token è già graduato (complete=True) ritorna una 6-tupla speciale
    (0.0, 0, 1.0, 0, mint, "") → pos["pair_address"] viene impostato a "" e il
    chiamante deve risolvere la pool via DexScreener al prossimo giro.
    """
    curve = _derive_bonding_curve(mint)
    if not curve:
        return None
    try:
        r = requests.post(_SOLANA_RPC, timeout=8, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [curve, {"encoding": "base64"}],
        })
        acc = (r.json().get("result") or {}).get("value")
        if not acc:
            return None
        decoded = _decode_bonding_curve((acc.get("data") or [""])[0])
        if not decoded:
            return None
        v_sol, v_tok, complete = decoded

        if complete:
            # graduato: niente raydium_pool noto on-chain, ma _fetch_price(mint,...)
            # risolve la pool via search API DexScreener usando il mint come query
            return (0.0, 0, 1.0, 0, mint, mint)  # 6-tupla speciale: graduato

        if v_tok <= 0:
            return None

        # SOL price con cache 5 min
        now_t = time.time()
        if now_t - _pf_sol_cache["ts"] > 300 or _pf_sol_cache["price"] <= 0:
            try:
                rs = requests.get(
                    "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112",
                    timeout=5,
                )
                if rs.status_code == 200:
                    pairs = rs.json() if isinstance(rs.json(), list) else []
                    for p in pairs:
                        if "usdc" in (p.get("quoteToken", {}).get("symbol") or "").lower():
                            _pf_sol_cache["price"] = float(p.get("priceUsd") or 0)
                            _pf_sol_cache["ts"] = now_t
                            break
            except Exception:
                pass
        sol_price = _pf_sol_cache["price"] or 180.0

        price_usd = (v_sol / v_tok) * sol_price
        liq_usd   = v_sol * sol_price * 2   # approssimazione: liq = 2 * valore SOL
        return (price_usd, 0, 1.0, liq_usd, mint)
    except Exception as e:
        log.debug(f"[pre_grad] RPC bonding curve {mint[:8]}: {e}")
        return None


def _log_trade(row: dict):
    """Appende una riga a live_trades.csv."""
    exists = os.path.exists(LIVE_LOG_CSV)
    with open(LIVE_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LIVE_COLUMNS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        # Riempie le colonne nuove con stringa vuota se assenti (retrocompatibilità)
        for col in ("pump_prob", "prepump_score"):
            row.setdefault(col, "")
        w.writerow(row)


_SIGNALS_LOG_COLUMNS = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name", "token_address",
    "chain", "pair_address", "price_entry_usd", "volume_1h_usd", "liquidity_usd",
    "buy_sell_ratio_1h", "change_1h_pct", "pump_probability",
    "buy_tax", "sell_tax", "lp_locked", "is_honeypot", "top_features",
]
# signal_id già scritti in questa sessione: evita duplicati su restart ravvicinati
_signals_log_written: set = set()


def _log_to_signals_csv(row: dict, sid: str, chain: str, now: datetime):
    """Scrive in signals_log.csv i segnali v3 routati a defi (via_gemmeV3).
    Normalmente signals_log è scritto da defi_optimized; questa funzione colma
    il gap che rendeva i 26 vol_crash defi privi di dati signal per le analisi."""
    if sid in _signals_log_written:
        return
    _signals_log_written.add(sid)
    exists = os.path.exists(DEFI_SIGNALS)
    top_feats = (
        f"tier={row.get('tier','')} | score={row.get('score','')} | "
        f"gem_class={row.get('gem_class','')} | via_gemmeV3=true"
    )
    sig_row = {
        "signal_id":        sid,
        "timestamp_entry":  str(row.get("timestamp", now.isoformat())),
        "token_symbol":     str(row.get("token_symbol", "")),
        "token_name":       str(row.get("token_name", row.get("token_symbol", ""))),
        "token_address":    str(row.get("token_address", "")),
        "chain":            chain,
        "pair_address":     str(row.get("pair_address", "")),
        "price_entry_usd":  str(row.get("price_usd", "")),
        "volume_1h_usd":    str(row.get("volume_1h_usd", "")),
        "liquidity_usd":    str(row.get("liquidity_usd", "")),
        "buy_sell_ratio_1h": str(row.get("buy_sell_ratio_1h", "")),
        "change_1h_pct":    str(row.get("change_1h_pct", "")),
        "pump_probability": "",   # non prodotto da gemmeV3
        "buy_tax":          str(row.get("buy_tax", "")),
        "sell_tax":         str(row.get("sell_tax", "")),
        "lp_locked":        str(row.get("lp_locked", "")),
        "is_honeypot":      str(row.get("is_honeypot", "")),
        "top_features":     top_feats,
    }
    with open(DEFI_SIGNALS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_SIGNALS_LOG_COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(sig_row)


FOLLOWUP_COLUMNS = [
    "signal_id", "token_symbol", "chain", "pair_address",
    "price_entry_usd", "snapshot_num", "timestamp_snapshot",
    "minutes_since_entry", "price_snapshot_usd", "change_pct", "status",
]


def _log_followup_snapshot(pos: dict, sid: str, cur_price: float, chg: float, now: datetime):
    """Appende uno snapshot a price_followup.csv (15/06: copre anche midcap MC_*
    per costruire un dataset storico utilizzabile da /validate-filter)."""
    pos["_followup_snap_num"] = pos.get("_followup_snap_num", 0) + 1
    try:
        entry_ts = datetime.fromisoformat(pos["entry_ts"])
        minutes  = (now - entry_ts.replace(tzinfo=None)).total_seconds() / 60.0
    except Exception:
        minutes = ""
    row = {
        "signal_id":           sid,
        "token_symbol":        pos.get("token_symbol", ""),
        "chain":               pos.get("chain", ""),
        "pair_address":        pos.get("pair_address", ""),
        "price_entry_usd":     pos.get("entry_price", ""),
        "snapshot_num":        pos["_followup_snap_num"],
        "timestamp_snapshot":  now.isoformat(),
        "minutes_since_entry": round(minutes, 1) if minutes != "" else "",
        "price_snapshot_usd":  cur_price,
        "change_pct":          round(chg, 4),
        "status":              "ok",
    }
    exists = os.path.exists(DEFI_FOLLOWUP)
    with open(DEFI_FOLLOWUP, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FOLLOWUP_COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(row)


_DATA_FAULT_COLUMNS = ["signal_id", "system", "token_symbol", "ts", "action",
                       "change_pct", "pnl_eur", "fault_reason", "note"]


def _log_data_fault_trades(all_states: dict, has_entry: set):
    """
    Scrive (dedup per signal_id) i trade chiusi classificati come data_fault
    in data_fault_trades.csv — esclusi da PF/WR/EV ma tracciati per audit.
    """
    already = set()
    if os.path.exists(DATA_FAULT_CSV):
        try:
            for r in csv.DictReader(open(DATA_FAULT_CSV, encoding="utf-8")):
                already.add(r.get("signal_id", ""))
        except Exception:
            pass

    new_rows = []
    for sid, s in all_states.items():
        if float(s.get("remaining", 0) or 0) > 0:
            continue
        if sid in already:
            continue
        ok, reason = is_valid_trade_event(s, sid in has_entry)
        if ok:
            continue
        new_rows.append({
            "signal_id": sid, "system": s.get("system", "?"),
            "token_symbol": s.get("token_symbol", "?"), "ts": s.get("ts", ""),
            "action": s.get("action", ""), "change_pct": s.get("change_pct", ""),
            "pnl_eur": s.get("pnl_eur", ""), "fault_reason": reason,
            "note": s.get("note", ""),
        })

    if not new_rows:
        return
    exists = os.path.exists(DATA_FAULT_CSV)
    with open(DATA_FAULT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_DATA_FAULT_COLUMNS)
        if not exists:
            w.writeheader()
        for row in new_rows:
            w.writerow(row)


class LiveEngine:
    """
    Motore live: legge segnali dai CSV di defi/V2/V3, fetcha prezzi
    da DexScreener e applica la logica di exit in autonomia.
    """

    def __init__(self):
        self.positions: Dict[str, dict] = {}
        self._lock  = threading.Lock()
        self._stop  = threading.Event()
        self._v3_flagged: Dict[str, str] = {}   # signal_id → severity ("warn"|"exit")
        # signal_id mai loggati su live_trades.csv ma scartati (es. cooldown):
        # senza questa cache _load_new_signals li rivaluta e ri-logga ad ogni ciclo per sempre
        self._signal_skip_cache: set = set()
        self.recent_tokens: dict  = {}   # sym_upper → (ts, action) — cooldown cross-sistema
        self.recent_taddrs: dict  = {}   # token_address_lower → (ts, action)
        self._shadows: dict       = {}   # sid → shadow pos (segnali pump_grad scartati dai filtri)
        self._shadow_queue_seen: set = set()  # sid già consumati da liq_shadow_queue.csv
        # ── Rug watcher (fast-check WS pump_grad, vedi _RugWatcher) ────────
        self._fast_lock          = threading.Lock()
        self._fast_check_pending: set  = set()   # pair_address con attività rilevata
        self._fast_check_last:    dict = {}      # pair_address → ts ultimo fetch innescato
        self._rug_watcher = _RugWatcher(on_pool_activity=self._on_pool_activity)
        if not self._load_state():    # prova prima il JSON di stato
            self._load_existing()     # fallback: ricostruzione da CSV
        self._purge_stale()           # rimuovi fantasmi senza pair_address
        self._load_new_signals()
        self._rug_watcher.start()
        threading.Thread(target=self._fast_check_loop, daemon=True).start()

    # ── Persistenza stato JSON ─────────────────────────────────────────────

    @staticmethod
    def _build_pair_lookup() -> dict:
        """Costruisce {signal_id: pair_address} da tutti i CSV sorgente."""
        lookup = {}
        sources = [
            (DEFI_SIGNALS,  "signal_id"),
            (DEFI_FOLLOWUP, "signal_id"),
            (V2_SIGNALS,    "gem_id"),
            (V2_FOLLOWUP,   "gem_id"),
            (V3_SIGNALS,    "gem_id"),
            (BF_SIGNALS,    "signal_id"),
        ]
        for path, id_col in sources:
            if not os.path.exists(path):
                continue
            try:
                for r in csv.DictReader(open(path, encoding="utf-8")):
                    sid = r.get(id_col, "").strip()
                    pa  = r.get("pair_address", "").strip()
                    if sid and pa and sid not in lookup:
                        lookup[sid] = pa
            except Exception:
                pass
        return lookup

    def _load_state(self) -> bool:
        """Carica posizioni da JSON. Ritorna True se riuscito."""
        if not os.path.exists(STATE_FILE):
            return False
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            positions = data.get("positions", {})
            if not positions:
                return False
            now   = datetime.now()
            valid = {}
            for sid, pos in positions.items():
                if pos.get("remaining", 0) <= 0:
                    continue
                try:
                    age_h   = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600
                    sys_key = pos.get("system", "")
                    sys_max = MAX_SIGNAL_AGE_H.get(sys_key, MAX_SIGNAL_AGE_H_DEFAULT)
                    if age_h > sys_max * 2:   # margine generoso al reload (2x il max per sistema)
                        # Scrive exit su live_trades.csv così la posizione non rimane "aperta" nel dashboard
                        _log_trade({
                            "ts": now.isoformat(), "signal_id": sid,
                            "system": pos.get("system", "?"),
                            "token_symbol": pos.get("token_symbol", "?"),
                            "chain": pos.get("chain", ""),
                            "pair_address": pos.get("pair_address", ""),
                            "action": "purged_stale",
                            "price": str(pos.get("entry_price", 0)),
                            "change_pct": f"{pos.get('current_pct', 0):+.2f}",
                            "vol_h1": "0", "bsr": "0", "remaining": "0.00",
                            "pnl_eur": f"{pos.get('pnl_eur', 0.0):+.2f}",
                            "exit_reason": "purged_stale",
                            "note": f"esclusa al reload: età {age_h:.1f}h > {sys_max*2:.0f}h",
                        })
                        continue
                except Exception:
                    pass
                # Assicura float su campi critici
                for k in ("peak_pct", "remaining", "pnl_eur", "entry_price",
                          "entry_vol", "current_pct", "current_vol", "current_bsr",
                          "neg_streak"):
                    try: pos[k] = float(pos[k])
                    except Exception: pass
                pos.setdefault("peak_pct", float("-inf"))
                pos.setdefault("tp1_hit",  False)
                pos.setdefault("tp2_hit",  False)
                pos.setdefault("last_update", None)
                valid[sid] = pos
            self.positions = valid
            log.info(f"[live] Stato caricato da JSON: {len(self.positions)} posizioni.")
            return bool(valid)
        except Exception as e:
            log.warning(f"[live] Errore caricamento stato JSON: {e}")
            return False

    def _save_state(self):
        """Salva stato posizioni su JSON (scrittura atomica tmp→rename)."""
        try:
            with self._lock:
                snap = {}
                for sid, pos in self.positions.items():
                    if pos.get("remaining", 0) <= 0:
                        continue
                    snap[sid] = {k: v for k, v in pos.items()
                                 if k not in ("price_is_live",) and not k.startswith("_")}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved_at": datetime.now().isoformat(),
                           "n_positions": len(snap),
                           "positions": snap}, f, indent=2, default=str)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.warning(f"[live] Errore salvataggio stato: {e}")

    def _purge_stale(self):
        """
        Per ogni posizione senza pair_address:
          - tenta il recupero dai CSV sorgente
          - se non recuperabile E più vecchia di MAX_SIGNAL_AGE_H, la chiude
            con exit_reason=purged_stale (non distorce più le statistiche)
        """
        pair_lookup = self._build_pair_lookup()
        now         = datetime.now()
        recovered   = 0
        to_purge    = []

        for sid, pos in self.positions.items():
            if pos.get("remaining", 0) <= 0:
                continue
            if pos.get("pair_address", "").strip():
                continue                          # ha già pair_address
            if pos.get("system") == "pre_grad":
                continue                          # pre_grad: bonding curve, pair_address vuoto è normale
            if sid in pair_lookup:
                pos["pair_address"] = pair_lookup[sid]
                recovered += 1
                continue
            # Nessun pair_address → valuta età
            try:
                age_h = (now - datetime.fromisoformat(pos.get("entry_ts", ""))).total_seconds() / 3600
            except Exception:
                age_h = 9999
            max_age = MAX_SIGNAL_AGE_H.get(pos.get("system",""), MAX_SIGNAL_AGE_H_DEFAULT)
            if age_h > max_age:
                to_purge.append(sid)

        for sid in to_purge:
            pos = self.positions.pop(sid)
            _log_trade({
                "ts": now.isoformat(), "signal_id": sid,
                "system": pos.get("system", "?"),
                "token_symbol": pos.get("token_symbol", "?"),
                "chain": pos.get("chain", ""),
                "action": "purged", "price": "0",
                "change_pct": "+0.00", "vol_h1": "0", "bsr": "0",
                "remaining": "0.00",
                "pnl_eur": f"{pos.get('pnl_eur', 0.0):+.2f}",
                "exit_reason": "purged_stale",
                "note": f"no_pair_address,age>{age_h:.0f}h",
            })

        if recovered or to_purge:
            log.info(f"[live] Pulizia: {recovered} pair_address recuperati, "
                     f"{len(to_purge)} posizioni stale purgiate.")

    # ── Shadow tracking: segnali pump_grad/mirror scartati dai filtri ─────────

    _SHADOW_COLS = [
        "ts_entry", "ts_exit", "signal_id", "token_symbol", "chain",
        "pair_address", "token_address", "entry_price",
        "skip_reason", "skip_value",
        "peak_pct", "exit_pct", "duration_min", "exit_reason",
    ]
    _SHADOW_TP1     = 25.0
    _SHADOW_HARD_SL = -12.0
    _SHADOW_LIMIT_M = 45.0

    def _shadow_register(self, sid: str, row: dict, reason: str, skip_val: float,
                         tok_addr: str, now: datetime):
        """Registra un segnale scartato per tracking contrfattuale."""
        if sid in self._shadows:
            return
        entry_price = float(row.get("price_entry_usd", row.get("price_usd", 0)) or 0)
        pair_addr   = str(row.get("pair_address", "") or "")
        if pair_addr.lower() == "nan":
            pair_addr = ""
        if not pair_addr or entry_price <= 0:
            return
        self._shadows[sid] = {
            "ts_entry":    now,
            "token_symbol": str(row.get("token_symbol", "") or ""),
            "chain":        str(row.get("chain", "") or ""),
            "pair_address": pair_addr,
            "token_address": tok_addr,
            "entry_price":  entry_price,
            "skip_reason":  reason,
            "skip_value":   skip_val,
            "peak_pct":     float("-inf"),
        }

    def _shadow_close(self, sid: str, sh: dict, exit_pct: float, exit_reason: str, now: datetime):
        """Scrive la riga finale su pump_grad_shadow.csv."""
        dur = (now - sh["ts_entry"]).total_seconds() / 60
        new_file = not os.path.exists(SHADOW_CSV)
        try:
            with open(SHADOW_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self._SHADOW_COLS)
                if new_file:
                    w.writeheader()
                w.writerow({
                    "ts_entry":    sh["ts_entry"].isoformat(),
                    "ts_exit":     now.isoformat(),
                    "signal_id":   sid,
                    "token_symbol": sh["token_symbol"],
                    "chain":       sh["chain"],
                    "pair_address": sh["pair_address"],
                    "token_address": sh["token_address"],
                    "entry_price": f"{sh['entry_price']:.8g}",
                    "skip_reason": sh["skip_reason"],
                    "skip_value":  f"{sh['skip_value']:.4g}",
                    "peak_pct":    f"{sh['peak_pct']:+.2f}" if sh["peak_pct"] != float("-inf") else "?",
                    "exit_pct":    f"{exit_pct:+.2f}",
                    "duration_min": f"{dur:.1f}",
                    "exit_reason": exit_reason,
                })
        except Exception as e:
            log.debug(f"[shadow] write error {sid}: {e}")

    def _process_shadows(self):
        """Aggiorna i shadow trades attivi: fetch prezzo, chiude per TP1/SL/time_limit."""
        if not self._shadows:
            return
        now  = datetime.now()
        done = []
        for sid, sh in list(self._shadows.items()):
            age_min = (now - sh["ts_entry"]).total_seconds() / 60
            # Time limit superato: chiudi al prezzo corrente (o sconosciuto)
            def _shadow_price(pair_addr, chain):
                """_fetch_price ritorna tupla (price,...) — estrae solo il prezzo."""
                r = _fetch_price(pair_addr, chain)
                return r[0] if r else None

            if age_min >= self._SHADOW_LIMIT_M:
                fetch = _shadow_price(sh["pair_address"], sh["chain"])
                ep    = sh["entry_price"]
                pct   = ((fetch - ep) / ep * 100) if (fetch and ep > 0) else 0.0
                self._shadow_close(sid, sh, pct, "time_limit", now)
                done.append(sid)
                continue
            fetch = _shadow_price(sh["pair_address"], sh["chain"])
            if not fetch or fetch <= 0:
                continue
            ep  = sh["entry_price"]
            if ep <= 0:
                continue
            pct = (fetch - ep) / ep * 100
            sh["peak_pct"] = max(sh.get("peak_pct", float("-inf")), pct)
            if pct >= self._SHADOW_TP1:
                self._shadow_close(sid, sh, pct, "tp1_would_hit", now)
                done.append(sid)
            elif pct <= self._SHADOW_HARD_SL:
                self._shadow_close(sid, sh, pct, "hard_sl_would_hit", now)
                done.append(sid)
        for sid in done:
            self._shadows.pop(sid, None)
        if done:
            log.info(f"[shadow] chiusi {len(done)} shadow trades → {SHADOW_CSV}")

    def _consume_shadow_queue(self):
        """Legge liq_shadow_queue.csv, registra nuovi shadow in memoria, poi tronca il file."""
        if not os.path.exists(LIQ_SHADOW_QUEUE):
            return
        try:
            with open(LIQ_SHADOW_QUEUE, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            log.debug(f"[shadow_queue] read: {e}")
            return
        if not content.strip():
            return
        try:
            rows = list(csv.DictReader(content.splitlines()))
        except Exception as e:
            log.debug(f"[shadow_queue] parse: {e}")
            rows = []
        now = datetime.now()
        new = 0
        for row in rows:
            sid = str(row.get("signal_id", ""))
            if not sid or sid in self._shadow_queue_seen:
                continue
            self._shadow_queue_seen.add(sid)
            try:
                tok_addr = str(row.get("token_address", "") or "")
                liq_val  = float(row.get("liquidity_usd", 0) or 0)
                self._shadow_register(sid, row, f"liq_queue(${liq_val:,.0f})", liq_val, tok_addr, now)
                new += 1
            except Exception as e:
                log.debug(f"[shadow_queue] register {sid}: {e}")
        if new:
            log.info(f"[shadow_queue] +{new} shadow registrati da liq_shadow_queue.csv")
        # Tronca sempre: entry in memoria, header riscritto per il prossimo append
        try:
            cols = ",".join(next(csv.reader(content.splitlines()), []))
            with open(LIQ_SHADOW_QUEUE, "w", newline="") as f:
                f.write(cols + "\n" if cols else "")
        except Exception as e:
            log.debug(f"[shadow_queue] truncate: {e}")

    # ── Ricostruzione stato da CSV ──────────────────────────────────────────

    def _load_existing(self):
        if not os.path.exists(LIVE_LOG_CSV):
            return
        try:
            rows = list(csv.DictReader(open(LIVE_LOG_CSV, encoding="utf-8")))
        except Exception as e:
            log.warning(f"[live] Errore lettura CSV: {e}"); return

        states: Dict[str, dict] = {}
        for r in rows:
            sid = r["signal_id"]
            try:    chg = float(r["change_pct"].replace("+", "") or 0)
            except: chg = 0.0
            if sid not in states:
                try:    ep = float(r["price"] or 0)
                except: ep = 0.0
                try:    ev = float(r["vol_h1"] or 0)
                except: ev = 0.0
                states[sid] = {
                    "signal_id":    sid,  "system":      r["system"],
                    "token_symbol": r["token_symbol"], "chain": r["chain"],
                    "pair_address": r.get("pair_address", ""),
                    "entry_price":  ep,
                    "entry_vol":    ev,
                    "entry_ts":     r["ts"],
                    "peak_pct":     float("-inf"), "neg_streak": 0,
                    "remaining":    1.0,  "pnl_eur": 0.0,
                    "tp1_hit":      False, "tp2_hit": False,
                    "exit_reason":  "open",
                    "current_pct":  0.0,  "current_vol": 0.0, "current_bsr": 1.0,
                    "last_update":  None,
                }
            s = states[sid]
            try:    s["remaining"] = float(r["remaining"] or 0)
            except: s["remaining"] = 0.0
            try:    s["pnl_eur"] = float(r["pnl_eur"].replace("+", "") or 0)
            except: s["pnl_eur"] = 0.0
            s["exit_reason"] = r.get("exit_reason", "open")
            s["current_pct"] = chg
            if chg > s["peak_pct"]: s["peak_pct"] = chg
            if r["action"] == "tp1": s["tp1_hit"] = True
            if r["action"] == "tp2": s["tp2_hit"] = True
            note = r.get("note", "")
            if "vol_entry=" in note:
                try: s["entry_vol"] = float(note.split("vol_entry=")[1].split()[0])
                except: pass

        self.positions = {sid: s for sid, s in states.items() if s["remaining"] > 0}
        log.info(f"[live] {len(self.positions)} posizioni aperte ricaricate.")

    def _load_new_signals(self):
        known = set()
        if os.path.exists(LIVE_LOG_CSV):
            try:
                with open(LIVE_LOG_CSV, encoding="utf-8") as f:
                    known = {r["signal_id"] for r in csv.DictReader(f)}
            except: pass

        # Permanent blacklist: pair_address mai più entrabili (es. 光源/WBNB — 3 perdite)
        _perm_bl: set = set()
        try:
            _st = json.loads(open(STATE_FILE).read())
            _perm_bl = set(_st.get("permanent_blacklist", []))
        except Exception:
            pass

        now = datetime.now()
        # Deduplicazione: pair_address o token_address già in portafoglio (posizioni APERTE)
        active_pairs:   set = set()
        active_taddrs:  set = set()   # dedup cross-sistema per token_address
        with self._lock:
            for pos in self.positions.values():
                if pos.get("remaining", 0) <= 0:
                    continue
                pa = pos.get("pair_address","").strip()
                if pa and pa != "0" * len(pa):
                    active_pairs.add(pa)
                ta = pos.get("token_address","").strip().lower()
                if ta:
                    active_taddrs.add(ta)

        self.recent_tokens = {}
        self.recent_taddrs = {}
        recent_entry_prices: dict = {}  # token_address_lower → last entry price_usd
        if os.path.exists(LIVE_LOG_CSV):
            try:
                with open(LIVE_LOG_CSV, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        action = r.get("action","")
                        if action not in _COOLDOWN_MAP:
                            continue
                        sym = str(r.get("token_symbol","")).upper()
                        ta  = str(r.get("token_address","") or "").strip().lower()
                        try:
                            ts = datetime.fromisoformat(r["ts"])
                            if sym and (sym not in self.recent_tokens or ts > self.recent_tokens[sym][0]):
                                self.recent_tokens[sym] = (ts, action)
                            if ta and (ta not in self.recent_taddrs or ts > self.recent_taddrs[ta][0]):
                                self.recent_taddrs[ta] = (ts, action)
                            # Traccia ultimo prezzo di entry per filtro re-entry
                            if action == "entry" and ta:
                                try:
                                    ep = float(r.get("price","0") or 0)
                                    if ep > 0:
                                        recent_entry_prices[ta] = ep
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

        # Blacklist coppie zombie: pair_address con 2+ exit bsr/liq nelle ultime 12h
        ZOMBIE_EXIT_TYPES = {"exit_bsr_collapse", "exit_low_liq"}
        ZOMBIE_WINDOW_H   = 12
        ZOMBIE_MIN_EXITS  = 2
        zombie_pair_counts: dict = {}   # pair_address → count uscite recenti
        if os.path.exists(LIVE_LOG_CSV):
            try:
                with open(LIVE_LOG_CSV, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        if r.get("exit_reason","") not in ZOMBIE_EXIT_TYPES:
                            continue
                        pa = str(r.get("pair_address","") or "").strip()
                        if not pa:
                            continue
                        try:
                            ts_exit = datetime.fromisoformat(r["ts"])
                            if (now - ts_exit).total_seconds() / 3600 < ZOMBIE_WINDOW_H:
                                zombie_pair_counts[pa] = zombie_pair_counts.get(pa, 0) + 1
                        except Exception:
                            pass
            except Exception:
                pass
        zombie_pairs = {pa for pa, cnt in zombie_pair_counts.items() if cnt >= ZOMBIE_MIN_EXITS}
        if zombie_pairs:
            log.debug(f"[live] {len(zombie_pairs)} coppie zombie bloccate: {zombie_pairs}")

        # Fingerprint Dune per segnali v3 già processati:
        # token_address → set di (inflow_last_2h, buyers_last_2h) già visti.
        # Se un nuovo segnale ha gli stessi valori → dato Dune stale (identico a sessione precedente).
        _prev_dune_prints: dict = {}
        if os.path.exists(V3_SIGNALS):
            try:
                _v3df = pd.read_csv(V3_SIGNALS, on_bad_lines="skip")
                if "gem_id" in _v3df.columns:
                    _v3df = _v3df.rename(columns={"gem_id": "signal_id"})
                for _, _vr in _v3df.iterrows():
                    if str(_vr.get("signal_id","")) not in known:
                        continue   # segnale nuovo, non ancora processato — non conta
                    _ta   = str(_vr.get("token_address","") or "").strip().lower()
                    _i2h  = str(_vr.get("inflow_last_2h","") or "")
                    _b2h  = str(_vr.get("buyers_last_2h","") or "")
                    if _ta and _i2h and _b2h:
                        _prev_dune_prints.setdefault(_ta, set()).add((_i2h, _b2h))
            except Exception:
                pass

        # ── Circuit breaker: ferma nuovi ingressi se P&L 24h sotto soglia ──────
        if MAX_DAILY_LOSS_EUR:
            _dpnl = _compute_daily_pnl()
            if _dpnl < MAX_DAILY_LOSS_EUR:
                log.warning(
                    f"[circuit] P&L 24h={_dpnl:+.2f}€ < soglia {MAX_DAILY_LOSS_EUR:+.2f}€ "
                    "→ nuovi segnali bloccati fino a recupero"
                )
                return

        new_count = 0
        for system, path in [("defi", DEFI_SIGNALS), ("v3", V3_SIGNALS), ("pump_grad", PUMP_GRAD_SIGNALS), ("mirror", MIRROR_SIGNALS), ("pre_grad", PRE_GRAD_SIGNALS), ("base_pump", BASE_PUMP_SIGNALS), ("midcap", MIDCAP_SIGNALS)]:
        # BNF (Binance Futures) disabilitato: logica futures incompatibile con bot spot
            if not os.path.exists(path):
                continue
            try:
                df = pd.read_csv(path, on_bad_lines="skip")
                if "gem_id" in df.columns:
                    df = df.rename(columns={"gem_id": "signal_id"})
                for _, row in df.iterrows():
                    sid = str(row.get("signal_id", ""))
                    if not sid or sid in known or sid in self._signal_skip_cache:
                        continue
                    # Skip se pair_address in permanent blacklist
                    pair_addr_check = str(row.get("pair_address","") or "").strip()
                    if pair_addr_check.lower() == "nan":
                        # pre_grad: pair_address vuoto (bonding curve, niente DEX pair)
                        # → pandas legge NaN → str(nan)="nan" → "nan" finiva in
                        # active_pairs alla 1a entry e bloccava in silenzio TUTTI
                        # i segnali pre_grad successivi (stesso pattern del caso
                        # recent_tokens[""], vedi 07/06)
                        pair_addr_check = ""
                    if pair_addr_check and pair_addr_check in _perm_bl:
                        known.add(sid)
                        continue
                    # Skip se stessa pair_address già in portafoglio
                    if pair_addr_check and pair_addr_check in active_pairs:
                        known.add(sid)
                        continue
                    # Skip se stesso token_address già aperto (cross-sistema: es. EUP/v3 = ReelRush/defi)
                    tok_addr_check = str(row.get("token_address","") or "").strip().lower()
                    if tok_addr_check == "nan":
                        # pandas legge la cella vuota (token_address="" su CEX spot,
                        # es. midcap_scanner) come NaN float → str(nan or "")="nan":
                        # ogni segnale CEX-spot collassa sulla stessa fake-key "nan"
                        # in active_taddrs/recent_taddrs → blocco cross-sistema silenzioso
                        # (vedi caso "solo CC ha aperto fra ~12 segnali midcap" 07/06)
                        tok_addr_check = ""
                    if tok_addr_check and tok_addr_check in active_taddrs:
                        log.debug(f"[live/{system}] {sid}: token_address {tok_addr_check[:12]}… già in portafoglio (altro sistema) → skip")
                        known.add(sid)
                        continue
                    # Routing sistema
                    chain            = str(row.get("chain", "solana") or "solana")
                    effective_system = system
                    if system == "mirror":
                        effective_system = "mirror"  # profilo dedicato: tp1_fraction=0.50, tp2=300%
                    elif system == "pre_grad":
                        pass  # mantiene "pre_grad" — ha config e logica dedicata
                    elif system == "v3":
                        sig_src  = str(row.get("source", "") or "")
                        sig_mcap = float(row.get("market_cap_usd", 0) or 0)
                        sig_dex  = str(row.get("dex_id", "") or "")

                        def _skip_routing(reason: str):
                            # Traccia su log+CSV gli scarti del router (prima erano
                            # known.add silenziosi: impossibili da diagnosticare a posteriori)
                            log.info(f"[routing] {sid}: scartato → {reason}")
                            _log_trade({
                                "ts": now.isoformat(), "signal_id": sid, "system": "v3",
                                "token_symbol": str(row.get("token_symbol", "")),
                                "chain": str(row.get("chain", "")),
                                "pair_address": pair_addr_check,
                                "action": "skip_routing",
                                "price": str(row.get("price_usd", row.get("price", "0"))),
                                "change_pct": "0", "vol_h1": "0", "bsr": "0",
                                "remaining": "0.00", "pnl_eur": "+0.00",
                                "exit_reason": "skip_routing", "note": reason,
                            })
                        if sig_src in ("coingecko_midcap", "coingecko_trending") and sig_mcap >= 5_000_000:
                            # Promozione a v3_large: token CoinGecko mcap>$10M con BSR forte su DEX
                            # Sostituisce il proxy "inflow_wallet_count=10" con un gate reale on-chain
                            _cg_bsr  = float(row.get("cg_dex_bsr", 0) or 0)
                            _cg_liq  = float(row.get("cg_dex_liq", 0) or 0)
                            _cg_chg  = float(row.get("cg_price_chg24", 0) or 0)
                            # 26/06: rimossa restrizione chain=="solana".
                            # Il path CoinGecko generava 0 segnali: i token midcap su CG
                            # con DEX primario su Solana sono rarissimi (quasi tutti su ETH/BSC).
                            # Il gate rimane robusto: mcap>$10M + bsr>=0.60 + liq>=$30k + chg24>=+5%.
                            if (sig_mcap > 10_000_000
                                    and _cg_bsr >= 0.60
                                    and _cg_liq >= 30_000
                                    and _cg_chg >= 5.0):
                                effective_system = "v3_large"
                                log.info(f"[routing] {sid}: CoinGecko mid-cap promosso a v3_large "
                                         f"(mcap=${sig_mcap/1e6:.1f}M bsr={_cg_bsr:.2f} liq=${_cg_liq:,.0f} chg={_cg_chg:+.1f}% chain={chain})")
                            else:
                                # v3_midcap disabilitato: sostituito da midcap_scanner (BB Squeeze)
                                _skip_routing(
                                    f"CoinGecko {sig_src} mcap=${sig_mcap:,.0f} non promosso a v3_large "
                                    f"(bsr={_cg_bsr:.2f} liq=${_cg_liq:,.0f} chg24={_cg_chg:+.1f}% chain={chain}) "
                                    "e v3_midcap disabilitato"
                                )
                                known.add(sid)
                                continue
                        elif sig_mcap > 10_000_000:
                            effective_system = "v3_large"
                            # Filtro qualità v3_large: solo DIAMOND/GOLD con smart money forte
                            # pnl soglia 10% (non 20%): 17% è ragionevole, evita falsi skip
                            _tier_vl   = str(row.get("tier","") or "").upper()
                            _score_vl  = float(row.get("score", 0) or 0)
                            _bsr_vl    = float(row.get("buy_sell_ratio_1h", 1.0) or 1.0)
                            _inflow_vl = int(float(row.get("inflow_wallet_count", 0) or 0))
                            _pnl_vl    = float(row.get("avg_wallet_pnl_pct", 0) or 0)
                            # inflow=10 è il placeholder hardcoded per binance_scan (no on-chain data).
                            # Non bloccare su inflow quando è il default: bsr+tier+score coprono già.
                            # score 65→60: cattura GOLD borderline (storico: 2 token bloccati a 61-62).
                            _inflow_ok = _inflow_vl >= 15 or _inflow_vl == 10
                            if (_tier_vl not in ("DIAMOND", "GOLD")
                                    or _score_vl < 60
                                    or _bsr_vl < 0.6
                                    or not _inflow_ok
                                    or _pnl_vl < 10):
                                _skip_routing(
                                    f"gate v3_large: tier={_tier_vl} score={_score_vl:.0f} "
                                    f"bsr={_bsr_vl:.2f} inflow={_inflow_vl} pnl={_pnl_vl:.0f}% "
                                    "(serve DIAMOND/GOLD + score>=60 + bsr>=0.6 + pnl>=10%)"
                                )
                                known.add(sid)
                                continue
                        elif sig_dex in ("pumpswap", "pump.fun") or sig_mcap < 1_000_000:
                            # Token pumpswap o micro-cap: comportamento da memecoin → usa config defi.
                            # Gate qualità: questa via ("via_gemmeV3") bypassava i filtri hard/pre-pump
                            # di defi_optimized (c2-c11, comp>=0.55) → 49.5% hard_sl vs 22.6% nativo
                            # (-68€/107 trade). Filtro minimo: scarta BRONZE/score<50.
                            # Backtest 17/06 n=195: soglia 50 taglia 26 trade WR=22.7% PnL=-84€;
                            # hs_rate invariato (51%→49%) — score non predice hard_sl, solo win rate.
                            _tier_vg  = str(row.get("tier","") or "").upper()
                            _score_vg = float(row.get("score", 0) or 0)
                            if _tier_vg == "BRONZE" or _score_vg < 50:
                                _skip_routing(
                                    f"gate via_gemmeV3: tier={_tier_vg} score={_score_vg:.0f} "
                                    "(serve score>=50, no BRONZE) — bypassa filtri defi"
                                )
                                known.add(sid)
                                continue
                            # 25/06 backtest n=137 v3 all-time: bsr 0.5-0.8 → n=12, -143€ (-11.94€/t).
                            # bsr >= 0.8 passa; bsr < 0.8 è segnale di sellers dominanti → rug imminente.
                            _bsr_vg = float(row.get("buy_sell_ratio_1h", 0) or 0)
                            if 0 < _bsr_vg < 0.8:
                                _skip_routing(
                                    f"gate via_gemmeV3: bsr={_bsr_vg:.2f} < 0.8 "
                                    "(sellers dominanti — backtest: -143€ su n=12)"
                                )
                                known.add(sid)
                                continue
                            effective_system = "defi"
                            log.debug(f"[routing] {sid}: pumpswap/microcap → defi (mcap=${sig_mcap:,.0f}, dex={sig_dex})")
                            # Registra il segnale v3 in signals_log.csv così i trade via_gemmeV3
                            # avranno dati signal disponibili per le analisi future
                            _log_to_signals_csv(row, sid, chain, now)

                    # Filtro dato Dune stale per segnali v3:
                    # se inflow_last_2h e buyers_last_2h sono identici a un segnale già processato
                    # per lo stesso token, il dato Dune non si è aggiornato → accumulo vecchio → skip
                    if system == "v3" and tok_addr_check:
                        _i2h_new = str(row.get("inflow_last_2h","") or "")
                        _b2h_new = str(row.get("buyers_last_2h","") or "")
                        if _i2h_new and _b2h_new:
                            _seen_fp = _prev_dune_prints.get(tok_addr_check, set())
                            if (_i2h_new, _b2h_new) in _seen_fp:
                                sym_log = str(row.get("token_symbol","?"))
                                log.info(
                                    f"[live/v3] {sym_log}: dato Dune stale "
                                    f"(inflow_2h={float(_i2h_new):,.0f} buyers_2h={_b2h_new} "
                                    f"identico a segnale precedente) → skip definitivo"
                                )
                                _log_trade({
                                    "ts": now.isoformat(),
                                    "signal_id": sid, "system": "v3",
                                    "token_symbol": sym_log, "chain": str(row.get("chain", "")),
                                    "pair_address": pair_addr_check, "action": "skip_stale",
                                    "price": str(row.get("price_usd", row.get("price", "0"))),
                                    "change_pct": "0", "vol_h1": "0", "bsr": "0",
                                    "remaining": "0.00", "pnl_eur": "+0.00",
                                    "exit_reason": "skip_stale",
                                    "note": (
                                        f"dato Dune invariato (inflow_2h={_i2h_new} buyers_2h={_b2h_new}) "
                                        "rispetto a un segnale già processato per lo stesso token"
                                    ),
                                })
                                known.add(sid)
                                continue

                    # Cooldown cross-sistema per symbol e token_address.
                    # Differenziato per tipo di uscita: hard_sl=12h, loss=4h, entry=8h, liq=24h
                    sym_check = str(row.get("token_symbol", "") or "").upper()
                    _cd_sym = self.recent_tokens.get(sym_check)
                    if _cd_sym:
                        _cd_ts, _cd_action = _cd_sym
                        _cd_limit = _COOLDOWN_MAP.get(_cd_action, 8)
                        if (now - _cd_ts).total_seconds() / 3600 < _cd_limit:
                            self._signal_skip_cache.add(sid)
                            log.debug(f"[live/{effective_system}] {sym_check} cooldown {_cd_action} "
                                     f"({_cd_ts.strftime('%H:%M')}, <{_cd_limit}h) → skip")
                            continue
                    _cd_ta = self.recent_taddrs.get(tok_addr_check) if tok_addr_check else None
                    if _cd_ta:
                        _cd_ts, _cd_action = _cd_ta
                        _cd_limit = _COOLDOWN_MAP.get(_cd_action, 8)
                        if (now - _cd_ts).total_seconds() / 3600 < _cd_limit:
                            self._signal_skip_cache.add(sid)
                            log.debug(f"[live/{effective_system}] {tok_addr_check[:12]}… taddr cooldown "
                                     f"{_cd_action} ({_cd_ts.strftime('%H:%M')}, <{_cd_limit}h) → skip")
                            continue

                    # Filtri qualità per pump_grad/mirror: liq minima, chg1h cap, vol_h1 minimo
                    # Backtest 16/06: liq<25k → -197€ su 45 trade (WR invariato);
                    # chg1h>20% → token già pompato pre-graduation (es. SOLANA -53€)
                    # Backtest 17/06: vol_h1 1-5k → n=26 WR=19% PnL=-206€ (−7.9€/trade)
                    if effective_system in ("pump_grad", "mirror"):
                        _sig_chain = row.get("chain", "solana").lower()
                        _sig_liq = float(row.get("liquidity_usd", 0) or 0)
                        if 0 < _sig_liq < 25000:
                            self._signal_skip_cache.add(sid)
                            log.debug(f"[live/pump_grad] {sym_check} liq=${_sig_liq:,.0f} < $25k → skip")
                            self._shadow_register(sid, row, "liq<25k", _sig_liq, tok_addr_check, now)
                            continue
                        _sig_chg = float(row.get("change_1h_pct", 0) or 0)
                        if _sig_chg > 80:
                            self._signal_skip_cache.add(sid)
                            log.debug(f"[live/pump_grad] {sym_check} chg1h={_sig_chg:+.0f}% > 80% → parossismo, skip")
                            self._shadow_register(sid, row, "chg1h>80%", _sig_chg, tok_addr_check, now)
                            continue
                        # vol_h1<15k: skip solo su Solana (pool già attive post-graduation)
                        # Base: pool nuovissime (<2min), vol_h1 è sempre 0 per definizione
                        # 25/06 backtest n=1585 LIQ_ post-18/06:
                        #   vol_h1 0-5k:   n=14,  WR=50%,  PF=34.0, +137€   ← vol_h1=0 = pool nuovissima
                        #   vol_h1 5k-15k: n=555, WR=61%, PF=0.595, -5806€  ← killer (-10.46€/t)
                        #   vol_h1 15k+:   n=374, WR=82%, PF=2.07,  +3572€  ← edge reale
                        # vol_h1=0 è legittimo (nuova pool senza dati 1h ancora), va tenuto.
                        _sig_vol_pg = float(row.get("volume_1h_usd", 0) or 0)
                        if _sig_chain != "base" and 0 < _sig_vol_pg < 15000:
                            self._signal_skip_cache.add(sid)
                            log.debug(f"[live/pump_grad] {sym_check} vol_h1=${_sig_vol_pg:,.0f} < $15k → skip")
                            self._shadow_register(sid, row, "vol_h1<15k", _sig_vol_pg, tok_addr_check, now)
                            continue

                    # Filtro re-entry su token in downtrend:
                    # se il nuovo segnale arriva a un prezzo < 75% dell'ultimo entry → token in calo, skip
                    # Cattura: FOUR re-entrato a -59% (→ -42€), Workbench a -51% (→ -4.29€)
                    REENTRY_MIN_RATIO = 0.75
                    if tok_addr_check and tok_addr_check in recent_entry_prices:
                        last_ep = recent_entry_prices[tok_addr_check]
                        new_ep  = float(row.get("price_entry_usd", row.get("price_usd", 0)) or 0)
                        if new_ep > 0 and last_ep > 0 and new_ep < last_ep * REENTRY_MIN_RATIO:
                            known.add(sid)
                            drop_pct = (new_ep - last_ep) / last_ep * 100
                            log.info(f"[live/{effective_system}] {sym_check} re-entry bloccata: "
                                     f"prezzo {new_ep:.3e} < {last_ep:.3e}×0.75 ({drop_pct:+.1f}% dal prev entry)")
                            continue

                    ts_str = str(row.get("timestamp_entry", row.get("timestamp", "")))
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is not None:
                            # midcap_scanner scrive timestamp UTC tz-aware (es. "+00:00"),
                            # gli altri sistemi naive locale: senza normalizzazione "now - ts"
                            # solleva TypeError, intercettato dal bare except → retry infinito silenzioso
                            ts = ts.astimezone().replace(tzinfo=None)
                        age_h = (now - ts).total_seconds() / 3600
                        max_age = MAX_SIGNAL_AGE_H.get(effective_system, MAX_SIGNAL_AGE_H_DEFAULT)
                        if age_h > max_age:
                            continue
                    except:
                        continue
                    entry_price = float(row.get("price_entry_usd", row.get("price_usd", 0)) or 0)
                    if entry_price <= 0:
                        # Prezzo non valido già al momento del segnale (es. API origine giù) —
                        # non cambierà ai cicli successivi: marca come noto per evitare retry infiniti.
                        log.info(f"[live/{system}] {sid}: price_entry_usd<=0 nel segnale → skip definitivo (non tracciabile)")
                        known.add(sid)
                        continue
                    pair_addr = str(row.get("pair_address", "") or "")
                    if pair_addr.lower() == "nan":
                        pair_addr = ""
                    # Blocca segnali v2/defi senza pair_address: non possiamo tracciarne il prezzo
                    if effective_system in ("v2", "defi") and not pair_addr.strip():
                        log.debug(f"[live/{system}] {sid}: nessun pair_address → skip (non tracciabile)")
                        known.add(sid)
                        continue
                    # ── Blocca indirizzi mock BSC (generati da USE_MOCK_FALLBACK) ──────────
                    if pair_addr.startswith("0x000000000000000000000000"):
                        log.warning(
                            f"[live/{system}] {sid}: indirizzo mock rilevato "
                            f"({pair_addr[:20]}…) → skip permanente"
                        )
                        # Scrivi skip in live_trades.csv così il signal_id entra in
                        # `known` ai cicli successivi e il warning non si ripete.
                        _log_trade({
                            "ts": now.isoformat(), "signal_id": sid,
                            "system": system, "token_symbol": str(row.get("token_symbol","")),
                            "chain": str(row.get("chain","")),
                            "pair_address": pair_addr,
                            "action": "skip", "price": "0", "change_pct": "0",
                            "vol_h1": "0", "bsr": "0", "remaining": "0.00",
                            "pnl_eur": "0.00", "exit_reason": "skip",
                            "note": "mock address (USE_MOCK_FALLBACK)",
                        })
                        known.add(sid)
                        continue
                    # Blocca coppie zombie: stessa pair_address ha avuto 2+ exit bsr/liq nelle ultime 12h
                    if pair_addr.strip() and pair_addr.strip() in zombie_pairs:
                        log.debug(f"[live/{system}] {sid}: pair {pair_addr[:12]}… zombie (2+ exit recenti) → skip")
                        known.add(sid)
                        continue
                    chain     = str(row.get("chain", "solana") or "solana")

                    # ── Filtro catena ──────────────────────────────────────────────────────
                    if chain not in ALLOWED_CHAINS:
                        known.add(sid)
                        continue

                    # ── Filtri qualità DEFI ────────────────────────────────────────────────
                    if effective_system == "defi":
                        # Guardia volume: token senza liquidità attiva o troppo liquido
                        # Backtest n=605: vol 50k-150k unico bucket positivo (+238€, +1.42€/t)
                        # vol >150k: n=66, WR=39%, avg=-2.49€/t, tot=-164€ (token troppo maturi)
                        _sig_vol = float(row.get("volume_1h_usd", 0) or 0)
                        if _sig_vol < MIN_VOLUME_1H_USD_DEFI:
                            log.debug(f"[live/defi] {sid}: vol_h1=${_sig_vol:.0f}<{MIN_VOLUME_1H_USD_DEFI:.0f} → skip")
                            known.add(sid)
                            continue
                        if _sig_vol > 150_000:
                            log.debug(f"[live/defi] {sid}: vol_h1=${_sig_vol:.0f}>150k → skip")
                            known.add(sid)
                            continue

                        # Filtro anti-dump: non entrare su token che sta scendendo
                        # change_1h < -2% E BSR < 0.5 → distribuzione attiva, non accumulo
                        _sig_chg1h = float(row.get("change_1h_pct", 0) or 0)
                        _sig_bsr   = float(row.get("buy_sell_ratio_1h", 1) or 1)
                        if _sig_chg1h < -2.0 and _sig_bsr < 0.50:
                            log.info(
                                f"[live/defi] {sid}: anti-dump "
                                f"chg1h={_sig_chg1h:+.1f}% bsr={_sig_bsr:.2f} → skip"
                            )
                            known.add(sid)
                            continue
                        # Guardia prepump_composite_score: analisi 7g mostra score<0.55
                        # produce WR=12% e P&L=-30€ su 31 trade → taglio netto
                        import re as _re
                        _feats = str(row.get("top_features", "") or "")
                        _m = _re.search(r'prepump_composite_score=([0-9.]+)', _feats)
                        if _m:
                            _score = float(_m.group(1))
                            if _score < 0.55:
                                log.debug(f"[live/defi] {sid}: prepump_score={_score:.2f}<0.55 → skip")
                                known.add(sid)
                                continue

                    token_address = str(row.get("token_address", "") or "")
                    if token_address.lower() == "nan":
                        token_address = ""  # vedi normalizzazione tok_addr_check sopra

                    # ── Blacklist hard post-SL ─────────────────────────────────────────────
                    # Salta se il token_address è stato blacklistato dopo un hard_sl > -25%
                    if token_address and _hard_sl_blacklist.get(token_address):
                        if now < _hard_sl_blacklist[token_address]:
                            log.info(f"[live/{system}] {sid}: {token_address[:12]}… in blacklist post-hard_sl → skip")
                            known.add(sid)
                            continue
                        else:
                            del _hard_sl_blacklist[token_address]

                    # ── Pre-entry price check via Jupiter (solo Solana) ────────────────────
                    # Soglia allineata all'executor: pump_grad usa slippage(8%)+margin(12%)=20%
                    # NOTA 11/06: il check anti-stantio per i segnali gemmeV3 (anche Base)
                    # sta ALLA RADICE in gemmeV3.stampa_gemma — il segnale soppresso non
                    # arriva proprio qui (regola: filtri alla fonte, non all'entry).
                    _ENTRY_DROP_THRESH = {"pump_grad": 0.20, "mirror": 0.20, "pre_grad": 0.50, "defi": 0.08}
                    _entry_drop_max = _ENTRY_DROP_THRESH.get(effective_system, 0.08)
                    if chain == "solana" and token_address and entry_price > 0:
                        _pa_fb = str(row.get("pair_address", "") or "")
                        live_price = _fetch_price_jupiter(token_address, entry_price,
                                                          pair_address=_pa_fb)
                        if live_price and live_price > 0:
                            drift = (live_price - entry_price) / entry_price
                            if drift < -_entry_drop_max:
                                log.info(
                                    f"[live/{effective_system}] {sid}: prezzo calato "
                                    f"{drift*100:.1f}% dal segnale (>{_entry_drop_max*100:.0f}%) → skip definitivo (stantio)"
                                )
                                # persiste lo skip su CSV: senza questo, _load_new_signals lo
                                # ritenta ad ogni ciclo finché un singolo tick anomalo (spike
                                # transitorio su pool a bassa liquidità) lo fa rientrare nella
                                # soglia → entry "a coltello che cade" (vedi caso FPU 07/06).
                                _log_trade({
                                    "ts": now.isoformat(),
                                    "signal_id": sid, "system": effective_system,
                                    "token_symbol": str(row.get("token_symbol", "?")),
                                    "chain": chain, "pair_address": str(row.get("pair_address", "")),
                                    "action": "skip_stale", "price": f"{entry_price}",
                                    "change_pct": f"{drift*100:+.2f}", "vol_h1": "0", "bsr": "0",
                                    "remaining": "0.00", "pnl_eur": "+0.00",
                                    "exit_reason": "skip_stale",
                                    "note": f"prezzo già calato {drift*100:.1f}% prima dell'entry, tesi pre-pump invalidata",
                                })
                                known.add(sid)
                                continue
                            entry_price = live_price  # usa prezzo live come entry

                    entry_vol = float(row.get("volume_1h_usd", 0) or 0)
                    entry_liq = float(row.get("liquidity_usd", 0) or 0)

                    # ── Segnali v3 stantii: entry_vol storico inaffidabile ─────────────────
                    # Se il segnale è stato generato >2h fa, il volume registrato al momento
                    # del segnale non riflette più lo stato attuale della pool. Azzerando
                    # entry_vol, il primo fetch live lo aggiorna al valore reale ed evita
                    # exit_vol_crash istantaneo su token con volume naturalmente decaduto.
                    if effective_system in ("v3", "v3_midcap", "v3_large") and age_h > 2.0:
                        entry_vol = 0.0

                    # ── Position sizing per tier ───────────────────────────────────────────
                    sig_tier    = str(row.get("tier", "") or "").upper()
                    sig_capital = CAPITAL_BY_TIER.get(sig_tier, CAPITAL_EUR)
                    # Solo per segnali v3/v3_midcap (gemmeV3 ha tier affidabile)
                    if effective_system not in ("v3", "v3_midcap", "v3_large"):
                        sig_capital = CAPITAL_EUR

                    # Overlap smart money: wallet alpha hanno comprato lo stesso mint
                    # nelle ultime 6h? Solo annotazione (per futura validazione)
                    entry_note = "vol_na"
                    # Attribuzione: gemme v3 mascherate da "defi" dal routing mcap<$1M
                    # restano riconoscibili nelle statistiche per sistema
                    if system == "v3" and effective_system == "defi":
                        entry_note += " | via_gemmeV3"

                    # pre_grad shadow (12/06): segnali passati con rugcheck rilassato
                    # (top_holder 25-55%) ma sotto soglia originale → size=0, il pnl
                    # viene comunque tracciato per stimare il costo/beneficio futuro
                    # di una soglia rugcheck più permissiva.
                    is_shadow = "shadow=true" in str(row.get("top_features", ""))
                    if is_shadow:
                        entry_note += " | shadow=true"
                        sig_capital = 0.0
                    if effective_system != "mirror" and chain == "solana":
                        sm_n = _smart_money_count(token_address)
                        if sm_n > 0:
                            entry_note += f" | smart_money={sm_n}"
                            log.info(f"[live] 🐋 {row.get('token_symbol','?')}: {sm_n} wallet alpha "
                                     f"hanno comprato questo token nelle ultime {_SM_WINDOW_H:.0f}h")

                    # Estrai feature signal per l'entry log (analisi future)
                    _entry_bsr = float(row.get("buy_sell_ratio_1h") or 0) or ""
                    _entry_pp  = str(row.get("pump_probability") or "")
                    _entry_pre = ""
                    _tf_entry  = str(row.get("top_features", "") or "")
                    _m_pre     = re.search(r'prepump_composite_score=([0-9.]+)', _tf_entry)
                    if _m_pre:
                        _entry_pre = _m_pre.group(1)
                    elif row.get("score"):   # v3: usa score come proxy
                        _entry_pre = f"v3score={float(row.get('score', 0) or 0):.1f}"

                    _log_trade({
                        "ts": now.isoformat(), "signal_id": sid, "system": effective_system,
                        "token_symbol": str(row.get("token_symbol", "")), "chain": chain,
                        "pair_address": pair_addr,
                        "action": "entry", "price": f"{entry_price:.8g}",
                        "change_pct": "+0.00", "vol_h1": f"{entry_vol:.0f}",
                        "bsr": f"{_entry_bsr:.3f}" if _entry_bsr else "1.000",
                        "pump_prob": _entry_pp, "prepump_score": _entry_pre,
                        "remaining": "1.00",
                        "pnl_eur": "+0.00", "exit_reason": "open", "note": entry_note,
                    })
                    known.add(sid)
                    if pair_addr and pair_addr not in ("", "0"*len(pair_addr) if pair_addr else ""):
                        active_pairs.add(pair_addr)
                    new_count += 1
                    if sym_check:
                        # guardia "": senza, un token_symbol mancante (es. CSV scritto
                        # prima che lo scanner aggiungesse il campo) avvelena la chiave
                        # "" in recent_tokens — ogni segnale successivo con token_symbol
                        # vuoto, di QUALSIASI sistema, collassa sullo stesso cooldown
                        # 8h "entry" e viene scartato in silenzio (log.debug invisibile)
                        self.recent_tokens[sym_check] = (now, "entry")
                    if tok_addr_check:
                        self.recent_taddrs[tok_addr_check] = (now, "entry")
                    with self._lock:
                        self.positions[sid] = {
                            "signal_id": sid, "system": effective_system,
                            "token_symbol": str(row.get("token_symbol", "")),
                            "chain": chain, "pair_address": pair_addr,
                            "token_address": token_address,
                            "entry_price": entry_price, "entry_vol": entry_vol,
                            "entry_liq": entry_liq,
                            "entry_ts": ts_str,
                            "position_open_ts": now.isoformat(),  # tempo apertura engine (per grace)
                            "peak_pct": float("-inf"),
                            "neg_streak": 0, "remaining": 1.0, "pnl_eur": 0.0,
                            "tp1_hit": False, "tp2_hit": False, "exit_reason": "open",
                            "current_pct": 0.0, "current_vol": entry_vol,
                            "current_bsr": 1.0, "last_update": None,
                            "capital": sig_capital,
                            "prev_liq": entry_liq,
                            "shadow": is_shadow,
                            "shadow_pnl_eur": 0.0,
                        }
            except Exception as e:
                log.warning(f"[live/{system}] {e}")
        if new_count:
            log.info(f"[live] {new_count} nuovi segnali caricati.")
        self._load_v3_flags()

    def _load_v3_flags(self):
        """Legge v3_exit_signals.csv scritto da gemmeV3 ogni ~3min."""
        if not os.path.exists(V3_EXIT_SIGNALS):
            return
        try:
            new_flags: Dict[str, str] = {}
            with open(V3_EXIT_SIGNALS, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid = row.get("signal_id", "").strip()
                    sev = row.get("severity", "warn").strip()
                    if sid:
                        new_flags[sid] = sev
            # Log variazioni
            for sid, sev in new_flags.items():
                if sid not in self._v3_flagged:
                    sym = self.positions.get(sid, {}).get("token_symbol", sid[:12])
                    log.info(f"[v3_monitor] 🚩 {sym}: momentum flag [{sev.upper()}] — "
                             f"BSR_CONFIRM ridotto")
            for sid in list(self._v3_flagged):
                if sid not in new_flags:
                    sym = self.positions.get(sid, {}).get("token_symbol", sid[:12])
                    log.info(f"[v3_monitor] ✅ {sym}: momentum recuperato — flag rimosso")
            self._v3_flagged = new_flags
        except Exception as e:
            log.debug(f"[v3_flags] {e}")

    # ── Rug watcher: fast-check pump_grad via WS (vedi _RugWatcher) ─────────

    def _on_pool_activity(self, pair_address: str):
        """Callback dal _RugWatcher: una tx ha toccato questo pool pump_grad
        sotto osservazione → marca per un refresh immediato (con debounce)."""
        with self._fast_lock:
            self._fast_check_pending.add(pair_address)

    def _sync_rug_watcher(self):
        """Allinea l'insieme dei pool monitorati via WS: pump_grad/pre_grad aperti
        e ancora entro FAST_CHECK_WINDOW_MIN dall'apertura — è lì che si sono
        consumati tutti i rug osservati (brainfry 18min, SHIBURAI 17min).
        15/06: estesa a pre_grad — overshoot hard_sl medio -48% (target -12%) con
        poll 30s, indipendente dalla liquidità di entry (backtest n=44).
        Fuori da quella finestra si torna al solo poll a 30s, zero overhead extra."""
        now = datetime.now()
        pools = set()
        with self._lock:
            for pos in self.positions.values():
                if (pos.get("system") in ("pump_grad", "mirror", "pre_grad") and pos.get("remaining", 0) > 0
                        and pos.get("pair_address")):
                    try:
                        ts = datetime.fromisoformat(pos.get("position_open_ts") or pos["entry_ts"])
                        age_min = (now - ts).total_seconds() / 60
                    except Exception:
                        age_min = 0.0
                    if age_min <= FAST_CHECK_WINDOW_MIN:
                        pools.add(pos["pair_address"])
        self._rug_watcher.sync(pools)

    def _fast_check_loop(self):
        """Consuma le notifiche del _RugWatcher: rifà subito _process_position
        sui pool toccati da una tx (con debounce per evitare di moltiplicare
        i fetch DexScreener su pool molto attivi)."""
        while not self._stop.is_set():
            time.sleep(1.0)
            with self._fast_lock:
                if not self._fast_check_pending:
                    continue
                pending = self._fast_check_pending
                self._fast_check_pending = set()
            now_t = time.time()
            due = [pa for pa in pending
                   if now_t - self._fast_check_last.get(pa, 0.0) >= FAST_CHECK_DEBOUNCE_SEC]
            if not due:
                continue
            for pa in due:
                self._fast_check_last[pa] = now_t
            with self._lock:
                targets = [(sid, pos) for sid, pos in self.positions.items()
                           if pos.get("system") in ("pump_grad", "mirror", "pre_grad") and pos.get("remaining", 0) > 0
                           and pos.get("pair_address") in due]
            for sid, pos in targets:
                try:
                    self._process_position(sid, pos)
                except Exception as e:
                    log.debug(f"[rug_watch] fast-check {sid}: {e}")

    # ── Logica di exit ──────────────────────────────────────────────────────

    def _process_position(self, sid: str, pos: dict):
        if pos["remaining"] <= 0:
            return
        # pre_grad: l'entry_price del segnale è stimato dalla bonding curve e
        # può differire enormemente (anche 2x) dal prezzo Jupiter pagato
        # dall'executor pochi secondi dopo, alla graduation. Appena
        # real_executions.csv riporta il fill reale, ri-ancora entry_price
        # per evitare drop fantasma misurati da un prezzo mai pagato.
        # GUARD: se old_ep viene da Jupiter (token già graduato su DEX) e real_price
        # viene dalla bonding curve, le due scale possono differire di 10-100x.
        # In quel caso il ri-ancoraggio produrrebbe gain fantasmi (caso CUTIEPATOOTIE
        # 16/06: bonding 1.75e-6 vs DEX 4.50e-4 → +4374% fake). Si salta e si
        # mantiene l'entry_price Jupiter che è sulla stessa scala di DexScreener.
        if pos.get("system") == "pre_grad" and not pos.get("_entry_price_checked"):
            real_price = _real_buy_price(sid)
            if real_price:
                old_ep = pos.get("entry_price", 0)
                if old_ep > 0:
                    ratio = real_price / old_ep
                    if ratio < 0.1 or ratio > 10.0:
                        log.warning(
                            f"[live/pre_grad] {sid}: _real_buy_price {real_price:.3e} vs "
                            f"entry {old_ep:.3e} (ratio={ratio:.1f}x) — scale bonding/DEX "
                            f"incompatibili, ri-ancoraggio saltato"
                        )
                        pos["_entry_price_checked"] = True
                    else:
                        if abs(ratio - 1.0) > 0.05:
                            log.info(
                                f"[live/pre_grad] {sid}: entry_price ri-ancorato "
                                f"{old_ep:.8g} → {real_price:.8g} (fill reale executor)"
                            )
                        pos["entry_price"] = real_price
                        pos["_entry_price_checked"] = True
                else:
                    pos["entry_price"] = real_price
                    pos["_entry_price_checked"] = True
        # pre_grad: pair_address vuoto = ancora sulla bonding curve (normale,
        # non un errore) → gestito al ramo dedicato sotto via _fetch_price_pumpfun
        if not pos.get("pair_address") and pos.get("system") != "pre_grad":
            return

        # ── Blocca posizioni con indirizzo mock BSC (USE_MOCK_FALLBACK) ────────
        # Queste posizioni non avranno mai un prezzo reale e resterebbero aperte
        # per sempre. Le chiudiamo a +0% appena rilevate.
        if pos.get("pair_address", "").startswith("0x000000000000000000000000"):
            sym = pos.get("token_symbol", "?")
            log.warning(
                f"[live] {sid} ({sym}): indirizzo mock rilevato nel loop continuo "
                f"→ chiusura immediata a +0.00%"
            )
            pos["remaining"] = 0.0
            _log_trade({
                "ts": datetime.now().isoformat(),
                "signal_id": sid,
                "system": pos.get("system", ""),
                "token_symbol": sym,
                "chain": pos.get("chain", ""),
                "pair_address": pos.get("pair_address", ""),
                "action": "manual_close",
                "price": str(pos.get("entry_price", 0)),
                "change_pct": "+0.00",
                "vol_h1": "0",
                "bsr": "0",
                "remaining": "0.00",
                "pnl_eur": "+0.00",
                "exit_reason": "manual_close",
                "note": "annullata: token mock (USE_MOCK_FALLBACK)",
            })
            return

        if pos.get("chain") == "binance_futures":
            fetch = _fetch_price_binance(pos["pair_address"])
        elif pos.get("system") == "midcap":
            fetch = _fetch_price_cex_spot(pos["pair_address"])
        elif pos.get("system") == "pre_grad" and not pos.get("pair_address"):
            # ── Pre-grad: token ancora sulla bonding curve ────────────────────
            # Usa pump.fun API; quando gradua → aggiorna pair_address e passa a DexScreener
            mint = pos.get("token_address", "")
            pf   = _fetch_price_pumpfun(mint) if mint else None
            if pf and len(pf) == 6 and pf[5]:
                # Token graduato: aggiorna pair_address e rifai fetch DexScreener
                pos["pair_address"] = pf[5]
                log.info(f"[live] {sid}: 🎓 graduato! pair={pf[5][:12]}… → switch a DexScreener")
                fetch = _fetch_price(pf[5], pos["chain"])
            else:
                fetch = pf  # (price, 0, 1.0, liq, mint) dalla bonding curve
        else:
            fetch = _fetch_price(pos["pair_address"], pos["chain"])
            if fetch:
                # Salva il token contract address per eventuale fallback CoinGecko
                if fetch[4] and not pos.get("token_address"):
                    pos["token_address"] = fetch[4]
            else:
                # Fallback CoinGecko se DexScreener non risponde
                fetch = _fetch_price_coingecko(pos.get("token_address", ""), pos["chain"])
                if fetch:
                    log.debug(f"[live] {sid}: prezzo da CoinGecko fallback ({fetch[0]:.6g})")
        if not fetch:
            # `now` qui non è ancora definito (viene assegnato dopo il fetch):
            # senza questa riga il blocco sotto moriva di NameError silenzioso
            # (except: pass) e i pre_grad senza prezzo restavano aperti per sempre.
            now = datetime.now()
            # Contatore fetch falliti: rende visibile in run.log il motivo per cui
            # una posizione non si aggiorna (prima era log.debug invisibile).
            pos["_nofetch_count"] = pos.get("_nofetch_count", 0) + 1
            if pos["_nofetch_count"] == 1 or pos["_nofetch_count"] % 20 == 0:
                log.warning(f"[live] {sid} ({pos.get('token_symbol','?')}): nessun prezzo "
                            f"({pos['_nofetch_count']} fetch falliti consecutivi)")
            # pre_grad / pump_grad senza prezzo: controlla il time_limit
            # (pool morta o bonding curve non risponde → exit_time_limit non scatterebbe mai)
            _time_limits = {"pump_grad": 45.0, "mirror": 45.0, "pre_grad": 20.0}
            _sys_limit   = _time_limits.get(pos.get("system", ""))
            if _sys_limit and not pos.get("tp1_hit"):
                try:
                    _age_min = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 60
                    if _age_min > _sys_limit:
                        ep = pos.get("entry_price", 0)
                        pos["remaining"] = 0.0
                        pos["exit_reason"] = "exit_time_limit"
                        _log_trade({
                            "ts": now.isoformat(), "signal_id": sid,
                            "system": pos["system"], "token_symbol": pos["token_symbol"],
                            "chain": pos["chain"], "pair_address": pos.get("pair_address", ""),
                            "action": "exit_time_limit", "price": f"{ep:.8g}",
                            "change_pct": "-100.00", "vol_h1": "0", "bsr": "0",
                            "remaining": "0.00", "pnl_eur": f"{pos.get('pnl_eur', 0.0):+.2f}",
                            "exit_reason": "exit_time_limit",
                            "note": f"no_price + {_age_min:.0f}min>{_sys_limit:.0f}min",
                        })
                        pos["last_update"] = now
                        log.info(f"[live] {sid}: exit_time_limit (no price, {_age_min:.0f}min)")
                except Exception as e:
                    log.warning(f"[live] {sid}: errore exit_time_limit: {e}")
            # Max hold zombie per gli altri sistemi: posizione che non ha MAI
            # avuto un prezzo oltre 3x il max hold → chiusura. (Il check originale
            # stava dopo il fetch riuscito, dove last_fetch è sempre valorizzato:
            # codice morto, spostato qui dove serve.)
            elif pos.get("last_fetch") is None:
                try:
                    _pos_age_h = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600
                    _max_hold_h = MAX_SIGNAL_AGE_H.get(pos.get("system", ""), MAX_SIGNAL_AGE_H_DEFAULT) * 3
                    if _pos_age_h > _max_hold_h:
                        pos["remaining"] = 0.0
                        pos["exit_reason"] = "exit_max_age"
                        _log_trade({
                            "ts": now.isoformat(), "signal_id": sid,
                            "system": pos["system"], "token_symbol": pos["token_symbol"],
                            "chain": pos["chain"], "pair_address": pos.get("pair_address", ""),
                            "action": "exit_max_age", "price": f"{pos.get('entry_price', 0):.8g}",
                            "change_pct": "+0.00", "vol_h1": "0", "bsr": "0",
                            "remaining": "0.00", "pnl_eur": f"{pos.get('pnl_eur', 0.0):+.2f}",
                            "exit_reason": "exit_max_age",
                            "note": f"nessun prezzo da {_pos_age_h:.1f}h > {_max_hold_h:.0f}h max",
                        })
                        pos["last_update"] = now
                        log.info(f"[live] {sid}: exit_max_age (mai avuto prezzo, {_pos_age_h:.1f}h)")
                except Exception as e:
                    log.warning(f"[live] {sid}: errore exit_max_age: {e}")
            return
        cur_price, cur_vol, cur_bsr, cur_liq = fetch[0], fetch[1], fetch[2], fetch[3]
        if cur_price <= 0:
            return

        # ── Sanity check: crollo di prezzo >99.9999% con liquidità ancora sana ──
        # è internamente incoerente (liq USD = reserve×price: se il prezzo
        # crollasse davvero così tanto, anche la liq crollerebbe). Indica un
        # glitch di parsing dell'API (es. PEAK 11/06: 0.0007106 → 2.9e-27,
        # liq invariata) → scarta il dato, ritenta al ciclo successivo.
        ep = pos.get("entry_price", 0)
        if ep > 0 and cur_price < ep * 1e-6 and cur_liq > 500.0:
            log.warning(
                f"[live] {sid}: prezzo {cur_price:.3e} incoerente con entry {ep:.3e} "
                f"(liq=${cur_liq:.0f} ancora sana) → scarto come glitch API"
            )
            return

        # Aggiorna entry_liq dal primo fetch live se non disponibile dal segnale
        if pos.get("entry_liq", 0) == 0 and cur_liq > 0:
            pos["entry_liq"] = cur_liq

        # ── Rilevamento pair secca (pool migrata o sbagliata) ─────────────────
        # Se Dexscreener restituisce liq < soglia minima la pair è probabilmente
        # inattiva (es. pool Uniswap/Aerodrome su Base o Raydium su Solana
        # abbandonata mentre il token ha migrato su una nuova pool).
        # Su Solana Jupiter è la fonte autorevole; su Base non esiste un
        # aggregatore equivalente quindi skippiamo il fetch (evita -99.9% falsi).
        MIN_TRACK_LIQ_USD = 500.0
        _dex_pair_dead = (
            cur_liq < MIN_TRACK_LIQ_USD
            and pos.get("chain") in ("solana", "base")
            and pos.get("system") in ("defi", "v2")
        )
        if _dex_pair_dead:
            log.warning(
                f"[live] {sid}: pair Dex liq=${cur_liq:.0f} < ${MIN_TRACK_LIQ_USD:.0f}"
                f" [{pos.get('chain')}] — pool secca/migrata"
                + (" → affido a Jupiter" if pos.get("chain") == "solana" else " → prezzo ignorato")
            )

        # ── Prezzo Solana: Jupiter primario, DexScreener fallback ────────────
        # Jupiter è la fonte autorevole per Solana: quota il prezzo eseguibile
        # reale (con slippage e liquidità vere, stessa logica dell'executor).
        # DexScreener rimane usato per vol/bsr/liq, non per il prezzo.
        #
        # Priorità:
        #   1. Jupiter OK → usa Jupiter (sempre)
        #   2. Jupiter KO + pair Dex viva → usa DexScreener come fallback (log warn)
        #   3. Jupiter KO + pair Dex secca → skip (prezzo inaffidabile)
        if pos.get("chain") == "solana" and pos.get("token_address"):
            jup_price = _fetch_price_jupiter(pos["token_address"], pos["entry_price"],
                                             pair_address=pos.get("pair_address", ""))
            if jup_price and jup_price > 0:
                log.debug(
                    f"[live] {sid}: prezzo Jupiter {jup_price:.8g} USD "
                    f"(Dex: {cur_price:.8g}, diff={((jup_price/cur_price)-1)*100:+.1f}%)"
                )
                cur_price = jup_price
            elif _dex_pair_dead:
                log.warning(
                    f"[live] {sid}: pair secca + nessuna route Jupiter"
                    f" → prezzo ignorato (evita -99.9% falso)"
                )
                return
            else:
                # Jupiter KO, pool DexScreener ancora viva → fallback con warning
                if not pos.get("_jup_fallback_logged"):
                    log.warning(
                        f"[live] {sid} ({pos.get('token_symbol','?')}): "
                        f"Jupiter non disponibile → prezzo DexScreener usato come fallback."
                    )
                    pos["_jup_fallback_logged"] = True
                # cur_price rimane quello di DexScreener

        # ── Prezzo Base: oracle on-chain diretto (no DexScreener lag) ───────────
        # base_executor legge slot0()/getReserves() + Chainlink ETH/USD dal pool contract.
        # Sostituisce il prezzo DexScreener (stantio fino a 30s) con quello real-time.
        # vol/bsr/liq rimangono da DexScreener (non disponibili on-chain facilmente).
        if pos.get("chain") == "base" and pos.get("token_address") and pos.get("pair_address"):
            try:
                import sys as _sys_b, os as _os_b
                _exec_dir = _os_b.path.join(
                    _os_b.path.dirname(_os_b.path.abspath(__file__)), "..", "executor")
                if _exec_dir not in _sys_b.path:
                    _sys_b.path.insert(0, _exec_dir)
                from base_executor import quote_onchain as _base_quote
                _oc_price = _base_quote(pos["token_address"])
                if _oc_price and _oc_price > 0:
                    # Sanity check: se l'oracle diverge >50% da DexScreener, è
                    # quasi certamente un pool sbagliato/vuoto (es. V3 garbage
                    # vs V2 reale) → caso PEAK/ECLYPSE 12/06, -100% fantasma.
                    if cur_price > 0 and abs(_oc_price / cur_price - 1) > 0.5:
                        log.warning(f"[live] {sid}: prezzo on-chain {_oc_price:.8g} "
                                    f"diverge troppo da Dex {cur_price:.8g} "
                                    f"→ scartato, uso DexScreener")
                    else:
                        if cur_price > 0:
                            log.debug(f"[live] {sid}: prezzo on-chain {_oc_price:.8g} (Dex: {cur_price:.8g}, diff={(_oc_price/cur_price-1)*100:+.1f}%)")
                        cur_price = _oc_price
            except Exception as _e_oc:
                log.warning(f"[live] {sid}: oracle Base errore ({_e_oc})")

        # ── Exit liquidity BASE: pool secca ───────────────────────────────────
        # Se la liquidità è sotto soglia skippiamo (on-chain oracle potrebbe ancora
        # restituire prezzi teorici su pool con liq residua minima).
        if pos.get("chain") == "base" and _dex_pair_dead:
            return

        cfg         = CONFIGS[pos["system"]]
        _capital    = pos.get("capital", CAPITAL_EUR)   # position sizing per tier
        ep          = pos["entry_price"]
        chg         = (cur_price - ep) / ep * 100.0
        # Sanity check: ignora spike oracle assurdi (>5000% o <-99.9%)
        # Caso reale: VIMAX peak=3.4 trilioni% → bug prezzo oracle
        if chg > 5000.0 or chg < -99.9:
            if chg < -99.9:
                # -99.9% persistente non è un glitch oracle: è un rug. Senza
                # questo contatore la posizione resta zombie per sempre (caso
                # PEAK 10/06: rug a 43s dall'entry, 990 cicli skippati in 11h,
                # perdita mai realizzata nelle statistiche).
                pos["anomaly_streak"] = pos.get("anomaly_streak", 0) + 1
                if pos["anomaly_streak"] >= 10:
                    chg = -100.0   # prosegue → hard_sl chiude e blacklista
                else:
                    log.warning(f"[live] {sid}: prezzo anomalo ignorato "
                                f"cur={cur_price:.8g} ep={ep:.8g} chg={chg:+.1f}% — skip ciclo "
                                f"({pos['anomaly_streak']}/10 prima del force-close)")
                    return
            else:
                log.warning(f"[live] {sid}: prezzo anomalo ignorato "
                            f"cur={cur_price:.8g} ep={ep:.8g} chg={chg:+.1f}% — skip ciclo")
                return
        else:
            pos.pop("anomaly_streak", None)
        pos["current_pct"]   = chg
        pos["current_vol"]   = cur_vol
        pos["current_bsr"]   = cur_bsr
        pos["price_is_live"] = True
        pos["last_fetch"]    = datetime.now().isoformat()
        # Primo fetch su segnale stantio (entry_vol=0): calibra entry_vol sul volume reale attuale
        if pos.get("entry_vol", 0) == 0 and cur_vol > 0:
            pos["entry_vol"] = cur_vol
        if chg > pos["peak_pct"]: pos["peak_pct"] = chg
        # 15/06: snapshot storico per midcap (MC_*) su price_followup.csv,
        # finora coperto solo da defi/v2/v3 — serve per backtest dopo ~2 settimane
        if pos.get("system") == "midcap":
            try:
                _log_followup_snapshot(pos, sid, cur_price, chg, datetime.now())
            except Exception as _e_fu:
                log.debug(f"[live] {sid}: errore log followup snapshot ({_e_fu})")
        # neg_streak: conta fetch consecutivi sotto soglia SL (NON semplicemente < 0)
        # Così una posizione a -26% non si "salva" con un micro-rimbalzo a +0.1%
        sl_thresh_live = cfg.get("sl_threshold_pct", -15.0)
        if chg <= sl_thresh_live:
            pos["neg_streak"] = pos.get("neg_streak", 0) + 1
        else:
            pos["neg_streak"] = 0

        now       = datetime.now()
        remaining = pos["remaining"]
        pnl       = pos["pnl_eur"]
        MAX_CAP   = 2000.0

        def _exit(action, note, chg_val=None, exit_r="open"):
            nonlocal remaining, pnl
            _chg = chg if chg_val is None else chg_val
            _factor = max(min(_chg, MAX_CAP), -100) / 100
            pnl_new = _capital * remaining * _factor
            pnl    += pnl_new
            if pos.get("shadow"):
                shadow_pnl = pos.get("shadow_pnl_eur", 0.0) + CAPITAL_EUR * remaining * _factor
                pos["shadow_pnl_eur"] = shadow_pnl
                note = f"{note} | shadow_pnl={shadow_pnl:+.2f}€ (size reale=0)"
            remaining = 0.0
            pos["remaining"] = 0.0; pos["pnl_eur"] = pnl; pos["exit_reason"] = exit_r
            _log_trade({
                "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                "action": action, "price": f"{cur_price:.8g}",
                "change_pct": f"{_chg:+.2f}",
                "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                "remaining": "0.00", "pnl_eur": f"{pnl:+.2f}",
                "exit_reason": exit_r, "note": note,
            })
            # Aggiorna cooldown runtime per loss exit: senza questo, il cooldown resta
            # sull'ultima "entry" (8h) anziché sull'hard_sl (24h), permettendo re-entry
            # entro poche ore dallo stesso token (visto su SAOS: 3 entry in 24h).
            if action in _COOLDOWN_MAP:
                _sym_exit = str(pos.get("token_symbol", "") or "").upper()
                _ta_exit  = str(pos.get("token_address", "") or "").strip().lower()
                if _sym_exit:
                    self.recent_tokens[_sym_exit] = (now, action)
                if _ta_exit:
                    self.recent_taddrs[_ta_exit] = (now, action)

        snap1_exit = cfg["adaptive_snap1_exit"]
        # 0a. (rimosso) Il check exit_max_age "mai avuto prezzo" stava qui, ma a
        #     questo punto last_fetch è appena stato valorizzato → non scattava mai.
        #     Spostato nel ramo `if not fetch:` sopra.
        # 0b. Timeout prezzo: defi/v2 senza fetch riusciti per >15 min → exit preventivo
        #    Protegge da rug silenziosi dove DexScreener smette di rispondere.
        #    Nota: usa last_fetch (aggiornato ad ogni fetch riuscito), NON last_update
        #    (che viene settato solo sulle exit). Fix: pos["system"] invece di system.
        PRICE_TIMEOUT_SYSTEMS = ("defi", "v2")
        PRICE_TIMEOUT_MIN     = 15
        if pos["system"] in PRICE_TIMEOUT_SYSTEMS and pos.get("last_fetch") is None:
            try:
                age_min = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 60
                if age_min > PRICE_TIMEOUT_MIN:
                    _exit("exit_price_timeout",
                          f"nessun prezzo per {age_min:.0f}min (>{PRICE_TIMEOUT_MIN}min)",
                          exit_r="exit_price_timeout")
                    pos["last_update"] = now
                    return
            except Exception:
                pass

        # 1. Exit adattiva snap1 (solo al PRIMO fetch riuscito)
        if snap1_exit is not None and pos.get("last_fetch") is None and chg < snap1_exit:
            _exit("exit_adaptive", f"snap1={chg:.1f}%<{snap1_exit}%", exit_r="adaptive"); pos["last_update"] = now; return

        # Grace period: nei primi 13 min dall'apertura della posizione non uscire per BSR/vol/liq.
        # Usa position_open_ts (quando l'engine ha aperto la posizione) invece di entry_ts
        # (timestamp del segnale) per evitare che la grace si consumi durante il lag di pickup.
        # Fix bug: se il parse fallisce _in_grace=True (sicuro) invece di False.
        ENTRY_GRACE_MIN = 13.0
        _entry_age_min = 0.0
        try:
            _grace_ts_str = pos.get("position_open_ts") or pos["entry_ts"]
            _entry_age_min = (now - datetime.fromisoformat(_grace_ts_str)).total_seconds() / 60
        except Exception:
            pass   # _entry_age_min resta 0.0 → _in_grace=True (default sicuro)
        _in_grace = _entry_age_min < ENTRY_GRACE_MIN
        _v3_flag  = getattr(self, "_v3_flagged", {}).get(sid, "")   # momentum flag da gemmeV3

        # 2. Liquidità collassata (rug detection)
        # Double-check: liq bassa deve essere confermata per 2 cicli consecutivi
        # prima di uscire, per evitare falsi positivi da DexScreener (dati stantii/glitch).
        # Eccezione: liq=0 esatto esce immediatamente (pool matematicamente vuota).
        _liq_threshold = cfg.get("liq_collapse_threshold", 5_000)

        # 2-pre. Liq drain velocity: exit immediato senza 2-cicli se liq crolla >35% in 30s.
        # Applicato a TUTTI i sistemi (era solo pump_grad/pre_grad — bug: SUMA v3_large uscita troppo tardi).
        # Su constant-product AMM: liq ∝ sqrt(price), quindi -35% liq ≈ -58% price già avvenuto.
        _prev_liq = pos.get("prev_liq", 0)
        if (_prev_liq > 5_000 and cur_liq > 0
                and cur_liq < _prev_liq * 0.65):
            _exit("liq_collapse",
                  f"liq_velocity: ${cur_liq:.0f}←${_prev_liq:.0f} ({cur_liq/_prev_liq*100:.0f}% in {REFRESH_SEC}s)",
                  exit_r="liq_collapse")
            pos["prev_liq"] = cur_liq; pos["last_update"] = now; return
        pos["prev_liq"] = cur_liq

        # 2a. Soglia relativa pump_grad/pre_grad: exit se liq < X% dell'entry_liq
        # pre_grad usa 40% (più stretto di pump_grad 30%) perché entra a liq più bassa
        # e i rug post-graduation su questi token sono più veloci.
        _liq_rel_systems = {"pump_grad": 0.30, "pre_grad": 0.40}
        _liq_rel_thresh  = _liq_rel_systems.get(pos.get("system", ""))
        if _liq_rel_thresh:
            _entry_liq = pos.get("entry_liq", 0)
            if _entry_liq > 8_000 and 0 <= cur_liq < _entry_liq * _liq_rel_thresh:
                if pos.get("liq_collapse_pending"):
                    _exit("liq_collapse",
                          f"liq_drain: ${cur_liq:.0f}/${_entry_liq:.0f} ({cur_liq/_entry_liq*100:.0f}% entry_liq, thresh={_liq_rel_thresh*100:.0f}%)",
                          exit_r="liq_collapse"); pos["last_update"] = now; return
                pos["liq_collapse_pending"] = True
            else:
                pos.pop("liq_collapse_pending", None)

        # 2b. Soglia assoluta (tutti i sistemi)
        # Sempre 2 cicli: liq=0 su Meteora DLMM può essere fuori range (non rug).
        if not _in_grace and cur_liq < _liq_threshold:
            if pos.get("liq_collapse_pending"):
                _exit("liq_collapse", f"liq=${cur_liq:.0f}<${_liq_threshold:.0f} (pool svuotato)",
                      exit_r="liq_collapse"); pos["last_update"] = now; return
            pos["liq_collapse_pending"] = True
        else:
            if cur_liq >= _liq_threshold:
                pos.pop("liq_collapse_pending", None)

        # 3. Crollo volume
        # Guard BSR: se i compratori sono ancora dominanti (bsr>0.65) il vol drop è
        # temporaneo (token in fase di accumulo). exit_quality mostra exit come
        # Print (bsr=0.67→+17.9%) e NIL (bsr=0.88) che erano premature per questo motivo.
        # Guard tempo: per token v3/v3_large/v3_midcap il vol fluttua naturalmente nei
        # primi 25 min — exit_quality mostra 100% prematuri per vol_crash < 20min su v3.
        ev = pos["entry_vol"]
        VOL_CRASH_BSR_MAX = 0.65
        _vol_crash_grace = cfg.get("vol_crash_grace_min", ENTRY_GRACE_MIN)
        _in_vol_crash_grace = _entry_age_min < _vol_crash_grace
        # Predittivo: slope vol accelerata negativamente per 2 cicli consecutivi (≥15%/ciclo).
        # Esce PRIMA della soglia assoluta → cattura prezzo più alto pre-dump.
        # Buffer _vol_hist: ultimi 4 campioni vol (ogni ciclo = 30s).
        if not _in_vol_crash_grace and ev > 0 and cur_vol > 0 and cur_bsr <= VOL_CRASH_BSR_MAX:
            _vh = pos.setdefault("_vol_hist", [])
            _vh.append(cur_vol)
            if len(_vh) > 4: _vh.pop(0)
            if len(_vh) >= 3:
                _d1 = (_vh[-2] - _vh[-3]) / max(_vh[-3], 1)
                _d2 = (_vh[-1] - _vh[-2]) / max(_vh[-2], 1)
                if _d1 < -0.15 and _d2 < -0.15 and _d2 < _d1:
                    _exit("exit_vol_crash", f"vol_slope {_vh[-3]:.0f}→{_vh[-2]:.0f}→{_vh[-1]:.0f} bsr={cur_bsr:.2f}", exit_r="exit_vol_crash")
                    pos["last_update"] = now
                    return
        if not _in_vol_crash_grace and ev > 0 and cur_vol > 0 and cur_vol < ev * cfg.get("vol_drop_exit_ratio", 0.30):
            if cur_bsr <= VOL_CRASH_BSR_MAX:
                _exit("exit_vol_crash", f"vol={cur_vol:.0f}/{ev:.0f} bsr={cur_bsr:.2f}", exit_r="exit_vol_crash"); pos["last_update"] = now; return

        # 4. Hard stop-loss assoluto — PRIMA del BSR collapse
        # Spostato qui: caso 光源/WBNB — prezzo stabile 6h poi crash -48% in un ciclo.
        # BSR (step precedente) sparava prima di hard_sl per ordine del codice,
        # registrando -48€ invece di cappare a -8€.
        # Scatta anche durante grace: i rug avvengono nei primi minuti.
        _hard_sl = cfg.get("hard_sl_pct")
        if _hard_sl is not None and pos.get("last_fetch") is not None and chg <= _hard_sl:
            _tok = pos.get("token_address", "")
            if _tok:
                # Rug profondo (>-25%): blacklist 48h
                # Hard_sl ordinario (<-8%): blacklist 12h per bloccare re-entry ravvicinati
                # (es. 光源/WBNB entrato due volte a 7h22m di distanza → doppia loss)
                bl_hours = 48 if chg <= -25.0 else 12
                _hard_sl_blacklist[_tok] = now + timedelta(hours=bl_hours)
                log.info(f"[live] {sid}: {_tok[:12]}… blacklistato {bl_hours}h (hard_sl {chg:.1f}%)")
            _exit("hard_sl", f"Δ={chg:.1f}%<={_hard_sl}% (hard stop-loss)", exit_r="hard_sl"); pos["last_update"] = now; return

        # 4.4 v3 momentum exit: gemmeV3 ha segnalato deterioramento su posizione in profitto
        # "exit" = 2+ cicli consecutivi o BSR < 0.20 → exit immediato a prezzo corrente
        if _v3_flag == "exit" and pos.get("system") in ("v3", "v3_large", "v3_midcap"):
            _exit("exit_momentum",
                  f"v3_monitor: {getattr(self,'_v3_flagged',{}).get(sid,'')} "
                  f"bsr={cur_bsr:.2f} chg={chg:+.1f}%",
                  exit_r="exit_momentum")
            pos["last_update"] = now; return

        # 4.5 Time-based exit per pump_grad: se dopo 45 min non ha mai toccato tp1 → exit
        # Pattern graduation: pump nelle prime 30-60 min o non parte; tenere oltre è bleeding lento
        if pos.get("system") == "pump_grad" and not pos["tp1_hit"]:
            _PUMP_MAX_NO_TP1_MIN = 45.0
            try:
                _pump_age_min = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 60
                if _pump_age_min > _PUMP_MAX_NO_TP1_MIN:
                    _exit("exit_time_limit",
                          f"no tp1 dopo {_pump_age_min:.0f}min>{_PUMP_MAX_NO_TP1_MIN:.0f}min",
                          exit_r="exit_time_limit")
                    pos["last_update"] = now; return
            except Exception:
                pass

        # 4.6 Time-based exit per midcap stagnant: dopo 144h (6gg, design horizon)
        # senza mai aver toccato TP1 e senza che il picco sia mai arrivato a
        # trail_activate_pct (mai avuto una vera occasione) → chiusura forzata.
        if pos.get("system") == "midcap" and not pos["tp1_hit"]:
            _MIDCAP_MAX_STAGNANT_H = 144.0
            try:
                _mc_age_h = (now - datetime.fromisoformat(pos["entry_ts"]).replace(tzinfo=None)).total_seconds() / 3600
                if _mc_age_h > _MIDCAP_MAX_STAGNANT_H and pos.get("peak_pct", float("-inf")) < cfg["trail_activate_pct"]:
                    _exit("exit_stagnant",
                          f"peak={pos['peak_pct']:+.1f}%<{cfg['trail_activate_pct']:.0f}% dopo {_mc_age_h:.0f}h>{_MIDCAP_MAX_STAGNANT_H:.0f}h",
                          exit_r="exit_stagnant")
                    pos["last_update"] = now; return
            except Exception:
                pass

        # 4.5 BSR collapse (dopo hard_sl: se entrambi scattano nello stesso ciclo vince hard_sl)
        # pump_grad: 2 conferme | flagged v3 (momentum deteriorato): 2 exit / 3 warn | defi: 5 | altri: 4
        BSR_CONFIRM_COUNT = (
            2 if pos.get("system") == "pump_grad"
            else cfg.get("bsr_confirm_count", 7) if pos.get("system") == "defi"
            else 2 if _v3_flag == "exit"
            else 3 if _v3_flag == "warn"
            else 4
        )
        BSR_MIN_VOL_USD   = 3_000
        if not _in_grace and cur_vol >= BSR_MIN_VOL_USD and cur_bsr < cfg.get("bsr_exit_threshold", 0.50):
            pos["bsr_consec"] = pos.get("bsr_consec", 0) + 1
            pos["bsr_warn_peak"] = max(pos.get("bsr_warn_peak", chg), chg)
            if pos["bsr_consec"] < BSR_CONFIRM_COUNT:
                log.debug(f"[live] {sid}: BSR basso ({cur_bsr:.2f}) [{pos['bsr_consec']}/{BSR_CONFIRM_COUNT}] — attesa conferma")
            elif chg > -3.0 and cur_bsr >= 0.15:
                # Guard: BSR basso ma prezzo ancora su → attendi conferma price drop.
                # Eccezione: se BSR < 0.15 (95%+ sells) il token è praticamente morto
                # → exit anche con prezzo stantio (DexScreener Base ha lag significativo).
                pos["bsr_warning"] = True
                log.debug(f"[live] {sid}: BSR collapse x{pos['bsr_consec']} ma chg={chg:+.1f}%>-3% → attesa")
            else:
                note = f"bsr={cur_bsr:.2f} x{pos['bsr_consec']}consec"
                if pos.get("bsr_warn_peak", 0) > 0:
                    note += f" (peak={pos['bsr_warn_peak']:.1f}%→{chg:.1f}%)"
                _exit("exit_bsr_collapse", note, exit_r="exit_bsr_collapse"); pos["last_update"] = now; return
        else:
            if pos.get("bsr_consec", 0) > 0:
                log.debug(f"[live] {sid}: BSR recuperato (bsr={cur_bsr:.2f}) → reset contatore")
            pos["bsr_consec"]   = 0
            pos["bsr_warning"]  = False
            pos.pop("bsr_warn_peak", None)

        # 5. SL adattivo
        if pos["neg_streak"] >= cfg["sl_consecutive_neg"] and chg <= cfg["sl_threshold_pct"]:
            _exit("sl_adaptive", f"{pos['neg_streak']}neg Δ={chg:.1f}%", exit_r="sl_adaptive"); pos["last_update"] = now; return

        # 6. TP1
        if not pos["tp1_hit"] and chg >= cfg["tp1_pct"]:
            tp1_frac = cfg["tp1_fraction"]
            tp1_gain = _capital * tp1_frac * cfg["tp1_pct"] / 100
            pnl += tp1_gain; remaining -= tp1_frac
            pos["tp1_hit"] = True; pos["remaining"] = remaining; pos["pnl_eur"] = pnl
            # Salva BSR e volume al momento del tp1 per analisi futura adaptive TP
            pos["bsr_at_tp1"]    = round(cur_bsr, 3)
            pos["vol_at_tp1"]    = round(cur_vol, 0)
            pos["chg_at_tp1"]    = round(chg, 2)
            tp1_note = f"Δ={chg:.1f}% >= TP1={cfg['tp1_pct']}% | bsr_tp1={cur_bsr:.3f} vol_tp1={cur_vol:.0f}"
            if pos.get("shadow"):
                shadow_pnl = pos.get("shadow_pnl_eur", 0.0) + CAPITAL_EUR * tp1_frac * cfg["tp1_pct"] / 100
                pos["shadow_pnl_eur"] = shadow_pnl
                tp1_note += f" | shadow_pnl={shadow_pnl:+.2f}€ (size reale=0)"
            _log_trade({
                "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                "action": "tp1", "price": f"{cur_price:.8g}",
                "change_pct": f"{cfg['tp1_pct']:+.2f}",
                "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                "remaining": f"{remaining:.2f}", "pnl_eur": f"{pnl:+.2f}",
                "exit_reason": "tp1" if remaining <= 0 else "open",
                "note": tp1_note,
            })

        # 7. TP2 o attivazione trail dopo TP1
        if pos["tp1_hit"] and not pos["tp2_hit"]:
            if cfg.get("tp1_trail_only", False):
                # Modalità trail-only: dopo TP1 non c'è TP2 fisso.
                # Il trailing stop gestisce il 50% rimanente da breakeven in poi.
                # Attiva subito al raggiungimento del TP1 (non aspettare cfg["trail_activate_pct"])
                if not pos.get("trailing_active", False):
                    pos["trailing_active"] = True
                    pos["peak_pct"]  = max(pos.get("peak_pct", chg), chg)
                    # Trail con moltiplicatore più largo per memecoins (tp1_trail_atr_mult)
                    trail_mult = cfg.get("tp1_trail_atr_mult", 2.0)
                    trail_sl = (round(ep + (chg - trail_mult * cfg["trail_drop_pct"]) * ep / 100, 2)
                                if False else  # semplificazione: usa peak-drop%
                                round(ep * (1 + max(0, chg - cfg["trail_drop_pct"]) / 100), 2))
                    pos["trailing_sl"] = trail_sl
                    log.debug(f"[live] {sid}: trail attivato post-TP1 @ {chg:+.1f}%")
            elif chg >= cfg["tp2_pct"]:
                tp2_frac = 1.0 - cfg["tp1_fraction"]
                tp2_gain = _capital * tp2_frac * cfg["tp2_pct"] / 100
                pnl += tp2_gain; remaining = 0.0
                pos["tp2_hit"] = True; pos["remaining"] = 0.0; pos["pnl_eur"] = pnl; pos["exit_reason"] = "tp1_tp2"
                tp2_note = f"Δ={chg:.1f}% >= TP2={cfg['tp2_pct']}%"
                if pos.get("shadow"):
                    shadow_pnl = pos.get("shadow_pnl_eur", 0.0) + CAPITAL_EUR * tp2_frac * cfg["tp2_pct"] / 100
                    pos["shadow_pnl_eur"] = shadow_pnl
                    tp2_note += f" | shadow_pnl={shadow_pnl:+.2f}€ (size reale=0)"
                _log_trade({
                    "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                    "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                    "action": "tp2", "price": f"{cur_price:.8g}",
                    "change_pct": f"{cfg['tp2_pct']:+.2f}",
                    "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                    "remaining": "0.00", "pnl_eur": f"{pnl:+.2f}",
                    "exit_reason": "tp1_tp2", "note": tp2_note,
                })
                pos["last_update"] = now; return

        # 8. Trailing stop (sia standard che post-TP1)
        # Trail drop adattivo: più largo quando il peak è alto → non taglia i big movers troppo presto
        # Dati reali Base: FOUR peak=30% usciva a +14% con drop fisso 7%; AgentFloat peak=52% usciva a -1.2%
        if remaining > 1e-6 and pos["peak_pct"] >= cfg["trail_activate_pct"]:
            _peak = pos["peak_pct"]
            _base_drop = cfg["trail_drop_pct"]
            if _peak >= 40.0:
                _eff_drop = max(_base_drop, 15.0)   # peak >40%: drop 15% (lascia correre)
            elif _peak >= 20.0:
                _eff_drop = max(_base_drop, 11.0)   # peak >20%: drop 11%
            else:
                _eff_drop = _base_drop               # peak <20%: drop standard
            dd = _peak - chg
            if dd >= _eff_drop:
                er = "tp1_trail" if pos["tp1_hit"] else "trailing_sl"
                _exit("trail_exit", f"peak={_peak:.1f}% dd={dd:.1f}% drop_used={_eff_drop:.0f}%", exit_r=er)
                pos["last_update"] = now; return

        pos["last_update"] = now

    # ── Loop principale ─────────────────────────────────────────────────────

    def run(self):
        log.info(f"[live] Avviato. {len(self.positions)} posizioni. Refresh {REFRESH_SEC}s.")
        while not self._stop.is_set():
            try:
                with self._lock:
                    pos_list = list(self.positions.items())
                for sid, pos in pos_list:
                    if pos["remaining"] > 0 and (pos.get("pair_address") or pos.get("system") == "pre_grad"):
                        try: self._process_position(sid, pos)
                        # warning, non debug: un'eccezione qui significa posizione
                        # mai aggiornata/chiusa, deve essere visibile in run.log
                        except Exception as e: log.warning(f"[live] {sid}: _process_position fallita: {e}")
                try: self._generate_html()
                except Exception as e: log.warning(f"[live] HTML: {e}")
                try: self._load_new_signals()
                except Exception as e: log.debug(f"[live] nuovi segnali: {e}")
                try: self._consume_shadow_queue()
                except Exception as e: log.debug(f"[live] shadow queue: {e}")
                try: self._process_shadows()
                except Exception as e: log.warning(f"[live] shadow tracking: {e}")
                try: self._sync_rug_watcher()
                except Exception as e: log.debug(f"[live] rug_watch sync: {e}")
                try: self._save_state()
                except Exception as e: log.warning(f"[live] salvataggio stato: {e}")
            except Exception as e:
                log.error(f"[live] loop error: {e}")
            self._stop.wait(REFRESH_SEC)

    def stop(self):
        self._save_state()
        self._stop.set()
        self._rug_watcher.stop()

    def pause_all_positions(self):
        """
        Chiude tutte le posizioni aperte al prezzo attuale con reason=manual_pause.
        Usato da Ctrl+C o auto-pausa per assenza connessione.
        """
        now = datetime.now()
        with self._lock:
            open_pos = [(sid, dict(pos)) for sid, pos in self.positions.items()
                        if pos.get("remaining", 0) > 0]

        if not open_pos:
            log.info("[pause] Nessuna posizione aperta da chiudere.")
            return

        log.info(f"[pause] Chiusura pulita di {len(open_pos)} posizione/i...")
        for sid, pos in open_pos:
            # Fetch prezzo attuale
            try:
                if pos.get("chain") == "binance_futures":
                    fetch = _fetch_price_binance(pos["pair_address"])
                else:
                    fetch = _fetch_price(pos["pair_address"], pos["chain"]) if pos.get("pair_address") else None
            except Exception:
                fetch = None

            ep  = pos["entry_price"]
            if fetch and fetch[0] > 0:
                cur_price = fetch[0]
                chg = (cur_price - ep) / ep * 100.0
            else:
                # Nessuna connessione: usa l'ultimo Δ noto
                chg = pos.get("current_pct", 0.0)
                cur_price = ep * (1 + chg / 100.0)

            remaining = pos["remaining"]
            pnl_exit  = pos.get("capital", CAPITAL_EUR) * remaining * max(min(chg, 2000.0), -100.0) / 100.0
            pnl_total = pos.get("pnl_eur", 0.0) + pnl_exit

            with self._lock:
                if sid in self.positions:
                    self.positions[sid]["remaining"]   = 0.0
                    self.positions[sid]["pnl_eur"]     = pnl_total
                    self.positions[sid]["exit_reason"] = "manual_pause"

            _log_trade({
                "ts": now.isoformat(), "signal_id": sid,
                "system": pos["system"], "token_symbol": pos["token_symbol"],
                "chain": pos["chain"], "pair_address": pos.get("pair_address",""),
                "action": "manual_pause", "price": f"{cur_price:.8g}",
                "change_pct": f"{chg:+.2f}",
                "vol_h1": "0", "bsr": "0",
                "remaining": "0.00", "pnl_eur": f"{pnl_total:+.2f}",
                "exit_reason": "manual_pause",
                "note": "pausa manuale" if fetch else "pausa — prezzo stimato (no conn)",
            })
            log.info(f"[pause]  {pos['token_symbol']:10s}  Δ={chg:+.1f}%  P&L={pnl_total:+.2f}€"
                     + ("" if fetch else "  ⚠ prezzo stimato"))

        self._save_state()
        log.info("[pause] ✅ Tutte le posizioni chiuse. Stato salvato.")

    # ── Generazione HTML ────────────────────────────────────────────────────

    def _generate_html(self):
        rows = []
        try:
            rows = list(csv.DictReader(open(LIVE_LOG_CSV, encoding="utf-8")))
        except: pass

        # Leggi anche storico
        storico_rows = []
        storico_path = os.path.join(BASE, "reports", "live_trades_storico.csv")
        try:
            storico_rows = list(csv.DictReader(open(storico_path, encoding="utf-8")))
        except: pass

        # Ultima riga per ogni signal (stato corrente) + peak
        latest: Dict[str, dict] = {}
        peaks:  Dict[str, float] = {}
        for r in rows:
            sid = r["signal_id"]
            try: chg = float(r["change_pct"].replace("+", "") or 0)
            except: chg = 0.0
            if sid not in peaks or chg > peaks[sid]: peaks[sid] = chg
            latest[sid] = r

        # Storico: solo chiuse, senza sovrascrivere live
        storico_states: Dict[str, dict] = {}
        storico_peaks:  Dict[str, float] = {}
        for r in storico_rows:
            sid = r["signal_id"]
            try: chg = float(r["change_pct"].replace("+", "") or 0)
            except: chg = 0.0
            if sid not in storico_peaks or chg > storico_peaks[sid]: storico_peaks[sid] = chg
            storico_states[sid] = r
        # Esclude dallo storico i signal_id già presenti nel live
        storico_states = {sid: s for sid, s in storico_states.items()
                         if sid not in latest
                         and float(s.get("remaining", 0) or 0) <= 0}

        # Merge prezzi live dalla memoria
        with self._lock:
            for sid, pos in self.positions.items():
                if sid in latest:
                    latest[sid] = dict(latest[sid])
                    latest[sid]["_live_pct"]  = pos.get("current_pct", 0)
                    latest[sid]["_live_vol"]  = pos.get("current_vol", 0)
                    latest[sid]["_live_bsr"]  = pos.get("current_bsr", 1.0)
                    latest[sid]["_peak"]      = max(pos.get("peak_pct", float("-inf")), peaks.get(sid, float("-inf")))
                    latest[sid]["_pair"]          = pos.get("pair_address", "")
                    latest[sid]["_has_live_fetch"] = pos.get("price_is_live", False)

        HIDDEN_SYSTEMS = {"v2"}   # sistemi disabilitati — non mostrare nel dashboard
        open_list   = sorted(
            [(sid, s) for sid, s in latest.items()
             if float(s.get("remaining", 0) or 0) > 0
             and s.get("system") not in HIDDEN_SYSTEMS],
            key=lambda x: x[1].get("_live_pct", 0), reverse=True)
        closed_live = [(sid, s) for sid, s in latest.items()
                       if float(s.get("remaining", 0) or 0) <= 0
                       and s.get("system") not in HIDDEN_SYSTEMS]
        closed_storico = [(sid, s) for sid, s in storico_states.items()
                          if s.get("system") not in HIDDEN_SYSTEMS]
        # Unisci e ordina per data (più recenti prima), senza limite fisso
        closed_list = sorted(
            closed_live + closed_storico,
            key=lambda x: x[1].get("ts", ""), reverse=True)

        all_states = {sid: s for sid, s in list(latest.items()) + list(storico_states.items())
                      if s.get("system") not in HIDDEN_SYSTEMS}

        # Costruisce dict {signal_id: entry_price} leggendo la riga con action="entry"
        entry_prices: Dict[str, str] = {}
        for r in rows + storico_rows:
            if r.get("action") == "entry" and r.get("price",""):
                entry_prices[r["signal_id"]] = r["price"]

        # Attribuzione v3→defi (anche retroattiva): marker "via_gemmeV3" sulle
        # entry + match diretto signal_id↔gem_id in gems_log_v3.csv per i trade
        # precedenti all'introduzione del marker (10/06)
        v3_routed = {r["signal_id"] for r in rows + storico_rows
                     if r.get("action") == "entry"
                     and "via_gemmeV3" in (r.get("note") or "")}
        try:
            with open(V3_SIGNALS, encoding="utf-8") as _fh:
                v3_routed |= {r2.get("gem_id", "") for r2 in csv.DictReader(_fh)}
        except Exception:
            pass

        html = _build_live_html(open_list, closed_list, all_states, entry_prices, v3_routed)
        Path(LIVE_HTML).write_text(html, encoding="utf-8")
        log.info(f"[live] HTML aggiornato: {len(open_list)} aperte, {len(closed_list)} chiuse mostrate.")

        # Ogni 10 cicli rigenera anche il report qualità uscite
        if not hasattr(self, "_eq_cycle"): self._eq_cycle = 0
        self._eq_cycle += 1
        if self._eq_cycle % 10 == 1:
            try:
                eq_html = _build_exit_quality_html()
                Path(os.path.join(BASE, "reports", "exit_quality.html")).write_text(eq_html, encoding="utf-8")
            except Exception as e:
                log.debug(f"[live] exit_quality report: {e}")


# ===========================================================================
# EXIT QUALITY REPORT
# ===========================================================================

def _build_exit_quality_html() -> str:
    """
    Per ogni uscita precoce (exit_vol_crash / exit_bsr_collapse) entro MAX_EXIT_MIN minuti,
    controlla i snapshot successivi nel followup per vedere se il prezzo ha recuperato.

    Verdetto per ogni trade:
      ✅ Corretto  — il prezzo non ha superato il punto di uscita (o è sceso ulteriormente)
      ⚠️ Dubbio    — il prezzo ha recuperato fino a +5% oltre il punto di uscita
      ❌ Prematuro — il prezzo ha superato di >5% il punto di uscita entro 8h
    """
    # Costanti di configurazione (rispecchiano quelle in _process_position)
    ENTRY_GRACE_MIN = 13.0
    EARLY_EXIT_REASONS = {"exit_vol_crash", "exit_bsr_collapse"}
    MAX_EXIT_MIN       = 180    # considera "precoce" se uscita entro 3h dall'entrata
    PREMATURE_GAP      = 5.0    # soglia: prezzo post-exit supera exit_change di almeno 5%
    LOOK_AHEAD_H       = 8      # ore di followup da considerare dopo l'uscita

    # ── Leggi live_trades.csv ─────────────────────────────────────────────────
    exits: dict = {}   # signal_id → {exit_ts, exit_change, exit_reason, sym, system}
    if os.path.exists(LIVE_LOG_CSV):
        try:
            with open(LIVE_LOG_CSV, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("exit_reason","") not in EARLY_EXIT_REASONS:
                        continue
                    if float(r.get("remaining","1") or 1) > 0:
                        continue   # non è ancora chiusa
                    sid = r["signal_id"]
                    # Calcola minuti dall'entrata all'uscita
                    try:
                        _parts = sid.rsplit("_", 2)
                        entry_ts = datetime.strptime(_parts[-2] + _parts[-1], "%Y%m%d%H%M%S")
                        exit_ts  = datetime.fromisoformat(r["ts"])
                        exit_min = (exit_ts - entry_ts).total_seconds() / 60
                    except Exception:
                        continue
                    if exit_min > MAX_EXIT_MIN:
                        continue   # non era precoce
                    try:
                        exit_chg = float(r["change_pct"].replace("+","") or 0)
                    except Exception:
                        exit_chg = 0.0
                    exits[sid] = {
                        "exit_ts":     exit_ts,
                        "exit_min":    exit_min,
                        "exit_chg":    exit_chg,
                        "exit_reason": r["exit_reason"],
                        "sym":         r.get("token_symbol","?"),
                        "system":      r.get("system","?"),
                        "chain":       r.get("chain",""),
                        "pair":        r.get("pair_address",""),
                    }
        except Exception as e:
            pass

    # ── Pump_grad: tutte le exit (sezione separata, nessun followup CSV) ─────
    PUMP_EXIT_ACTIONS = {"tp1_trail", "trail_exit", "hard_sl", "exit_time_limit",
                         "liq_collapse", "exit_vol_crash", "exit_bsr_collapse", "sl_adaptive",
                         "exit_momentum"}
    pump_rows = []
    if os.path.exists(LIVE_LOG_CSV):
        try:
            _entry_ts_map: dict = {}   # signal_id → entry_ts (primo "entry" letto)
            with open(LIVE_LOG_CSV, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sid = r.get("signal_id", "")
                    if r.get("system", "") != "pump_grad":
                        continue
                    if r.get("action", "") == "entry":
                        if sid not in _entry_ts_map:
                            try: _entry_ts_map[sid] = datetime.fromisoformat(r["ts"])
                            except Exception: pass
                        continue
                    action = r.get("action", "")
                    if action not in PUMP_EXIT_ACTIONS:
                        continue
                    try: remaining = float(r.get("remaining", "1") or 1)
                    except: remaining = 1.0
                    if remaining > 0:
                        continue   # non ancora chiusa
                    try: exit_ts = datetime.fromisoformat(r["ts"])
                    except: continue
                    entry_ts = _entry_ts_map.get(sid)
                    exit_min = (exit_ts - entry_ts).total_seconds() / 60 if entry_ts else None
                    try: exit_chg = float(r.get("change_pct", "0").replace("+", "") or 0)
                    except: exit_chg = 0.0
                    try: pnl = float(r.get("pnl_eur", "0").replace("+", "") or 0)
                    except: pnl = 0.0
                    sym = r.get("token_symbol", "?")
                    pair = r.get("pair_address", "")
                    note = r.get("note", "")
                    zeroed = (pnl == 0.0 and abs(exit_chg) > 1.0)
                    dex_link = f"https://dexscreener.com/solana/{pair}" if pair else "#"
                    # Verdetto basato sul tipo di exit
                    if action in ("tp1_trail", "trail_exit"):
                        verdict, vcolor = "✅ Take Profit", "#3fb950"
                    elif action == "hard_sl":
                        verdict, vcolor = "🛑 Hard SL", "#f85149"
                    elif action == "exit_time_limit":
                        verdict, vcolor = "⏱ Time Limit", "#e3b341"
                    elif action == "liq_collapse":
                        verdict, vcolor = "💀 Rug", "#f85149"
                    elif action == "sl_adaptive":
                        verdict, vcolor = "🔻 SL Adattivo", "#f85149"
                    else:
                        verdict, vcolor = "📉 Exit Early", "#e3b341"
                    pump_rows.append({
                        "sid": sid, "sym": sym, "action": action,
                        "exit_ts": exit_ts, "exit_min": exit_min,
                        "exit_chg": exit_chg, "pnl": pnl,
                        "verdict": verdict, "vcolor": vcolor, "dex_link": dex_link,
                        "zeroed": zeroed, "note": note,
                    })
        except Exception:
            pass
    pump_rows.sort(key=lambda r: r.get("exit_ts", datetime.min), reverse=True)

    if not exits and not pump_rows:
        return ("<html><body style='background:#0d1117;color:#e6edf3;font-family:sans-serif;"
                "padding:20px'><h2>Nessuna uscita trovata</h2></body></html>")

    # ── Leggi followup files ──────────────────────────────────────────────────
    # signal_id → lista di {minutes_since_entry, change_pct}
    followup: dict = {}
    for fpath in [V3_FOLLOWUP, V2_FOLLOWUP, DEFI_FOLLOWUP]:
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sid = r.get("gem_id") or r.get("signal_id","")
                    if sid not in exits:
                        continue
                    try:
                        mins = float(r.get("minutes_since_entry",0) or 0)
                        chg  = float(r.get("change_pct",0) or 0)
                    except Exception:
                        continue
                    followup.setdefault(sid, []).append((mins, chg))
        except Exception:
            pass

    # ── Calcola verdetti ──────────────────────────────────────────────────────
    rows = []
    for sid, ex in exits.items():
        snaps = followup.get(sid, [])
        look_mins = LOOK_AHEAD_H * 60
        post = [(m, c) for m, c in snaps if m > ex["exit_min"] and m <= ex["exit_min"] + look_mins]

        if not post:
            verdict = "⬜ Nessun dato"
            vcolor  = "#8b949e"
            max_post = None
        else:
            max_post = max(c for _, c in post)
            gap = max_post - ex["exit_chg"]
            if gap > PREMATURE_GAP:
                verdict = "❌ Prematuro"
                vcolor  = "#f85149"
            elif gap > 0:
                verdict = "⚠️ Dubbio"
                vcolor  = "#e3b341"
            else:
                verdict = "✅ Corretto"
                vcolor  = "#3fb950"

        dex_link = (f"https://dexscreener.com/{ex['chain']}/{ex['pair']}"
                    if ex['pair'] else "#")
        rows.append({
            "sid": sid, "sym": ex["sym"], "system": ex["system"],
            "exit_min": ex["exit_min"], "exit_chg": ex["exit_chg"],
            "exit_reason": ex["exit_reason"],
            "max_post": max_post, "verdict": verdict, "vcolor": vcolor,
            "dex_link": dex_link,
            "exit_ts": ex["exit_ts"],
        })

    # Ordina: prima i prematuri (per missed gain desc), poi dubbi, poi corretti
    order = {"❌ Prematuro": 0, "⚠️ Dubbio": 1, "✅ Corretto": 2, "⬜ Nessun dato": 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["max_post"] or 0)))

    n_prem   = sum(1 for r in rows if "Prematuro"  in r["verdict"])
    n_dubb   = sum(1 for r in rows if "Dubbio"     in r["verdict"])
    n_corr   = sum(1 for r in rows if "Corretto"   in r["verdict"])
    n_nodata = sum(1 for r in rows if "Nessun dato" in r["verdict"])

    # ── Statistiche aggiuntive ────────────────────────────────────────────────
    prem_rows    = [r for r in rows if "Prematuro" in r["verdict"] and r["max_post"] is not None]
    missed_gains = sorted([r["max_post"] - r["exit_chg"] for r in prem_rows])
    median_missed = missed_gains[len(missed_gains) // 2] if missed_gains else 0
    max_missed    = missed_gains[-1] if missed_gains else 0

    bsr_prem = sum(1 for r in rows if "Prematuro" in r["verdict"] and "bsr" in r["exit_reason"])
    vol_prem = sum(1 for r in rows if "Prematuro" in r["verdict"] and "vol" in r["exit_reason"])
    bsr_corr = sum(1 for r in rows if "Corretto"  in r["verdict"] and "bsr" in r["exit_reason"])
    vol_corr = sum(1 for r in rows if "Corretto"  in r["verdict"] and "vol" in r["exit_reason"])

    def _bucket(m):
        if m < 10:  return "&lt;10 min"
        if m < 30:  return "10–30 min"
        return "&gt;30 min"
    bucket_prem: dict = {}
    bucket_corr: dict = {}
    for r in rows:
        if r["max_post"] is None: continue
        b = _bucket(r["exit_min"])
        if "Prematuro" in r["verdict"]: bucket_prem[b] = bucket_prem.get(b, 0) + 1
        if "Corretto"  in r["verdict"]: bucket_corr[b] = bucket_corr.get(b, 0) + 1

    bucket_html = ""
    for b in ("&lt;10 min", "10–30 min", "&gt;30 min"):
        p = bucket_prem.get(b, 0)
        c = bucket_corr.get(b, 0)
        tot = p + c
        pct = f"{p/tot*100:.0f}% prem" if tot else "—"
        bucket_html += (
            f'<span style="margin-right:18px;color:#8b949e">'
            f'<span style="color:#e6edf3;font-weight:600">{b}</span>: '
            f'❌{p} ✅{c} <span style="color:#f85149">({pct})</span></span>'
        )

    sys_colors = {"defi":"#1f6feb","v3":"#e3b341","v2":"#8b949e","v3_large":"#a371f7","v3_midcap":"#8b949e","midcap":"#3fb950","pump_grad":"#f0883e","pre_grad":"#58a6ff","mirror":"#bc8cff"}

    table_rows = ""
    for r in rows:
        sc = sys_colors.get(r["system"], "#8b949e")
        post_str     = f"{r['max_post']:+.1f}%" if r["max_post"] is not None else "—"
        post_color   = "#3fb950" if (r["max_post"] or 0) > r["exit_chg"] else "#f85149"
        reason_short = r["exit_reason"].replace("exit_","").replace("_collapse","_coll")
        reason_color = "#e3b341" if "bsr" in r["exit_reason"] else "#58a6ff"
        time_color   = "#f85149" if r["exit_min"] < 10 else "#8b949e"
        try:
            date_str = r["exit_ts"].strftime("%m-%d %H:%M")
        except Exception:
            date_str = "?"
        table_rows += (
            f'<tr>'
            f'<td><a href="{r["dex_link"]}" target="_blank" style="color:#58a6ff;font-weight:600">{r["sym"]}</a>'
            f' <span style="font-size:.65rem;background:{sc}22;color:{sc};padding:1px 4px;border-radius:3px">{r["system"].upper()}</span></td>'
            f'<td style="color:#484f58;font-size:.8rem">{date_str}</td>'
            f'<td style="color:{time_color}">{r["exit_min"]:.0f} min</td>'
            f'<td style="color:{reason_color};font-size:.8rem">{reason_short}</td>'
            f'<td style="color:{"#f85149" if r["exit_chg"]<0 else "#8b949e"}">{r["exit_chg"]:+.1f}%</td>'
            f'<td style="color:{post_color};font-weight:600">{post_str}</td>'
            f'<td style="color:{r["vcolor"]};font-weight:600">{r["verdict"]}</td>'
            f'</tr>\n'
        )

    # ── Tabella pump_grad ─────────────────────────────────────────────────────
    pump_table_rows = ""
    for r in pump_rows:
        dur_str = f"{r['exit_min']:.0f} min" if r["exit_min"] is not None else "?"
        dur_color = "#f85149" if (r["exit_min"] or 99) < 10 else "#8b949e"
        chg_color = "#3fb950" if r["exit_chg"] >= 0 else "#f85149"
        pnl_color = "#3fb950" if r["pnl"] >= 0 else "#f85149"
        try: date_str = r["exit_ts"].strftime("%m-%d %H:%M")
        except: date_str = "?"
        action_short = r["action"].replace("exit_", "").replace("_collapse", "_coll")
        zero_mark = (f' <span title="P&amp;L azzerato retroattivamente — {html.escape(r["note"])}" '
                     f'style="cursor:help;color:#8b949e;font-size:.7rem">ⓘ</span>') if r["zeroed"] else ""
        pump_table_rows += (
            f'<tr>'
            f'<td><a href="{r["dex_link"]}" target="_blank" style="color:#58a6ff;font-weight:600">{r["sym"]}</a></td>'
            f'<td style="color:#484f58;font-size:.8rem">{date_str}</td>'
            f'<td style="color:{dur_color}">{dur_str}</td>'
            f'<td style="color:#f0883e;font-size:.8rem">{action_short}</td>'
            f'<td style="color:{chg_color}">{r["exit_chg"]:+.1f}%</td>'
            f'<td style="color:{pnl_color};font-weight:600">{r["pnl"]:+.2f}€{zero_mark}</td>'
            f'<td style="color:{r["vcolor"]};font-weight:600">{r["verdict"]}</td>'
            f'</tr>\n'
        )
    if pump_table_rows:
        pump_tp  = sum(1 for r in pump_rows if "Take Profit" in r["verdict"])
        pump_sl  = sum(1 for r in pump_rows if r["verdict"] in ("🛑 Hard SL", "🔻 SL Adattivo", "💀 Rug"))
        pump_tl  = sum(1 for r in pump_rows if "Time Limit" in r["verdict"])
        pump_pnl = sum(r["pnl"] for r in pump_rows)
        pump_section = f"""
<div style="margin-top:28px;border-top:1px solid #21262d;padding-top:18px">
  <div style="font-size:1rem;font-weight:600;margin-bottom:12px">
    🎓 Pump Grad Exits
    <span style="font-size:.75rem;font-weight:400;color:#8b949e;margin-left:10px">
      ✅{pump_tp} TP · 🛑{pump_sl} SL/Rug · ⏱{pump_tl} TimeLimit ·
      P&L totale <span style="color:{'#3fb950' if pump_pnl>=0 else '#f85149'};font-weight:600">{pump_pnl:+.2f}€</span>
    </span>
  </div>
  <table>
  <thead><tr>
    <th>Token</th><th>Data exit</th><th>Durata</th><th>Motivo</th><th>Δ exit</th><th>P&L sim</th><th>Tipo</th>
  </tr></thead>
  <tbody>{pump_table_rows}</tbody>
  </table>
</div>"""
    else:
        pump_section = ""

    now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _bsr_parts = " · ".join(
        f"{s}={CONFIGS[s].get('bsr_exit_threshold', 0.50):.2f}"
        for s in ("defi", "v3", "v3_midcap", "v3_large", "midcap", "pump_grad", "pre_grad")
        if s in CONFIGS
    )
    bsr_thresh_note = _bsr_parts or "0.50"
    _defi_confirm   = CONFIGS.get("defi", {}).get("bsr_confirm_count", 7)
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="600">
<title>Exit Quality Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;padding:20px 24px;font-size:13px}}
a{{color:#58a6ff;text-decoration:none}}
table{{border-collapse:collapse;width:100%}}
th{{font-size:.72rem;color:#8b949e;font-weight:500;text-transform:uppercase;letter-spacing:.4px;
    padding:8px 12px;border-bottom:1px solid #21262d;text-align:left;background:#161b22}}
td{{padding:8px 12px;border-bottom:1px solid #161b22;font-size:.85rem}}
tr:hover td{{background:#161b2280}}
.stat{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;min-width:110px;display:inline-block;margin-right:10px;margin-bottom:10px}}
.stat .lbl{{font-size:.68rem;color:#8b949e;text-transform:uppercase}}
.stat .val{{font-size:1.3rem;font-weight:500;margin-top:2px}}
.note{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 14px;
       margin:16px 0;font-size:.8rem;color:#8b949e;line-height:1.6}}
</style></head><body>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;border-bottom:1px solid #21262d;padding-bottom:14px">
  <span style="font-size:1.2rem;font-weight:600">⚡ Exit Quality Report</span>
  <span style="color:#484f58;font-size:.8rem">{now_str} · auto-refresh 10min</span>
</div>

<div style="margin-bottom:12px">
  <div class="stat"><div class="lbl">Prematuri</div><div class="val" style="color:#f85149">{n_prem}</div></div>
  <div class="stat"><div class="lbl">Dubbi</div><div class="val" style="color:#e3b341">{n_dubb}</div></div>
  <div class="stat"><div class="lbl">Corretti</div><div class="val" style="color:#3fb950">{n_corr}</div></div>
  <div class="stat"><div class="lbl">Nessun dato</div><div class="val" style="color:#8b949e">{n_nodata}</div></div>
  <div class="stat"><div class="lbl">Guadagno perso (mediano)</div><div class="val" style="color:#f85149">{median_missed:+.1f}%</div></div>
  <div class="stat"><div class="lbl">Guadagno perso (max)</div><div class="val" style="color:#f85149">{max_missed:+.1f}%</div></div>
</div>

<div style="margin-bottom:10px;font-size:.8rem">
  <span style="color:#8b949e;margin-right:8px">Per motivo:</span>
  <span style="margin-right:18px"><span style="color:#e3b341">bsr_collapse</span> → ❌{bsr_prem} prematuri / ✅{bsr_corr} corretti</span>
  <span><span style="color:#58a6ff">vol_crash</span> → ❌{vol_prem} prematuri / ✅{vol_corr} corretti</span>
</div>

<div style="margin-bottom:16px;font-size:.8rem">
  <span style="color:#8b949e;margin-right:8px">Per tempo:</span>{bucket_html}
</div>

<div class="note">
  <strong>Come leggere:</strong>
  uscite <em>exit_vol_crash</em> e <em>exit_bsr_collapse</em> entro {MAX_EXIT_MIN//60}h dall'entrata.
  <strong>Δ uscita</strong> = variazione % al momento dell'exit.
  <strong>Max post-exit</strong> = massimo nelle {LOOK_AHEAD_H}h successive (da followup prezzi).
  ❌ <strong>Prematuro</strong>: prezzo &gt;+{PREMATURE_GAP:.0f}% oltre Δ exit entro {LOOK_AHEAD_H}h.
  ⚠️ <strong>Dubbio</strong>: recupero 0–{PREMATURE_GAP:.0f}%.
  ✅ <strong>Corretto</strong>: nessun recupero.
  — Impostazioni correnti: grace <strong>{ENTRY_GRACE_MIN:.0f} min</strong> · BSR conferme <strong>{_defi_confirm:.0f} (DEFI) / 2 (pump_grad) / 2-3 (v3 flag) / 4 (altri)</strong> · vol_crash guard BSR≤0.65<br>
  — BSR soglie per sistema: <strong>{bsr_thresh_note}</strong>
  — <span style="color:#f85149">Tempo in rosso</span> = uscita entro 10 min.
</div>

<table>
<thead><tr>
  <th>Token</th><th>Data exit</th><th>Uscita dopo</th><th>Motivo</th><th>Δ uscita</th><th>Max post-exit ({LOOK_AHEAD_H}h)</th><th>Verdetto</th>
</tr></thead>
<tbody>{table_rows}</tbody>
</table>

{pump_section}
</body></html>"""


# ===========================================================================
# HTML REPORT
# ===========================================================================

def _fmt_price(p_str: str) -> str:
    """Formatta un prezzo stringa per la visualizzazione (notazione compatta)."""
    try:
        v = float(p_str or 0)
        if v <= 0:        return "—"
        if v < 0.000001:  return f"{v:.2e}"   # < $0.000001 → notazione scientifica
        if v < 0.0001:    return f"{v:.8f}"   # $0.00005995 invece di 5.995e-05
        if v < 0.01:      return f"{v:.6f}"
        if v < 1:         return f"{v:.4f}"
        if v < 10000:     return f"{v:,.2f}"
        return f"{v:,.0f}"
    except: return "—"


_INJ_DB = os.path.join(os.path.dirname(BASE), "injective_autopilot", "injective_autopilot.db")

def _build_inj_section() -> str:
    """Sezione read-only con i trade di injective_autopilot (DB SQLite).
    Statistiche separate: NON tocca sys_stats né i filtri della dashboard."""
    import sqlite3 as _sq
    try:
        conn = _sq.connect(f"file:{_INJ_DB}?mode=ro", uri=True, timeout=2)
        conn.row_factory = _sq.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ticker, direction, status, entry_price, exit_price, "
            "pnl_usdt, pnl_pct, exit_reason, confidence, entry_ts, exit_ts "
            "FROM trades ORDER BY entry_ts DESC LIMIT 60")]
        conn.close()
    except Exception as e:
        return (f'<div class="section-title">🤖 Injective Autopilot</div>'
                f'<div style="color:#8b949e;font-size:.8rem;padding:8px">DB non disponibile: {e}</div>')
    if not rows:
        return ""

    def _fp(v):
        try: v = float(v)
        except (TypeError, ValueError): return "—"
        if v == 0: return "—"
        return f"{v:.4f}" if v >= 1 else f"{v:.12f}".rstrip("0").rstrip(".")

    closed = [r for r in rows if r["status"] == "CLOSED"]
    opened = [r for r in rows if r["status"] == "OPEN"]
    win_l  = [r["pnl_usdt"] or 0 for r in closed if (r["pnl_usdt"] or 0) > 0]
    loss_l = [r["pnl_usdt"] or 0 for r in closed if (r["pnl_usdt"] or 0) < 0]
    wins, losses = len(win_l), len(loss_l)
    tot_win, tot_loss = sum(win_l), sum(loss_l)
    pnl    = tot_win + tot_loss
    wr     = wins / len(closed) * 100 if closed else 0
    pnl_c  = "#3fb950" if pnl > 0 else ("#f85149" if pnl < 0 else "#8b949e")
    wr_c   = "#3fb950" if wr >= 40 else "#e3b341" if wr >= 25 else "#f85149"

    trs = ""
    for r in opened + closed:
        is_open = r["status"] == "OPEN"
        p   = r["pnl_usdt"] or 0
        pct = r["pnl_pct"] or 0
        pc  = "#3fb950" if p > 0 else ("#f85149" if p < 0 else "#8b949e")
        try:
            et  = datetime.fromtimestamp(r["entry_ts"]).strftime("%d/%m %H:%M")
            dur = ((r["exit_ts"] or datetime.now().timestamp()) - r["entry_ts"]) / 60
            dur_s = f"{int(dur//60)}h{int(dur%60):02d}m"
        except Exception:
            et, dur_s = "?", "?"
        stato_raw = "OPEN" if is_open else (r["exit_reason"] or "—")
        stato   = '<span style="color:#58a6ff">● OPEN</span>' if is_open else (r["exit_reason"] or "—")
        dir_c   = "#3fb950" if r["direction"] == "LONG" else "#f85149"
        exit_s  = "—" if is_open else _fp(r["exit_price"])
        pnl_s   = "" if is_open else f"{p:+.2f}$"
        pct_s   = "" if is_open else f"{pct:+.2f}%"
        conf    = (r["confidence"] or 0) * 100
        trs += (
            f'<tr class="injrow" style="opacity:{1 if is_open else .8}" '
            f'data-id="{r["id"]}" data-ticker="{r["ticker"].lower()}" data-dir="{r["direction"]}" '
            f'data-entry="{r["entry_price"] or 0}" data-exit="{r["exit_price"] or 0}" '
            f'data-pnl="{p:.4f}" data-pct="{pct:.4f}" data-stato="{stato_raw}" '
            f'data-conf="{conf:.0f}" data-ts="{r["entry_ts"] or 0}">'
            f'<td style="font-size:.72rem;color:#484f58;font-family:monospace">{r["id"]}</td>'
            f'<td style="font-weight:600">{r["ticker"]}</td>'
            f'<td style="color:{dir_c}">{r["direction"]}</td>'
            f'<td style="font-family:monospace;font-size:.72rem;color:#8b949e">{_fp(r["entry_price"])}</td>'
            f'<td style="font-family:monospace;font-size:.72rem;color:#8b949e">{exit_s}</td>'
            f'<td style="color:{pc};font-weight:600">{pnl_s}</td>'
            f'<td style="color:{pc};font-size:.8rem">{pct_s}</td>'
            f'<td style="font-size:.8rem;color:#8b949e">{stato}</td>'
            f'<td style="font-size:.8rem;color:#8b949e">{conf:.0f}%</td>'
            f'<td style="font-size:.75rem;color:#484f58">{et} ({dur_s})</td>'
            f'</tr>'
        )

    # Esiti distinti per il filtro (TP, SL, MANUAL, …)
    esiti = sorted({(r["exit_reason"] or "—") for r in closed})
    esiti_opts = "".join(f'<option value="{e}">{e}</option>' for e in esiti)

    # Header ordinabili: colonna → (label, tipo num/str)
    _cols = [("id", "ID", 0), ("ticker", "Ticker", 0), ("dir", "Dir", 0),
             ("entry", "Entry", 1), ("exit", "Exit", 1), ("pnl", "P&L", 1),
             ("pct", "%", 1), ("stato", "Stato/Exit", 0), ("conf", "Conf", 1),
             ("ts", "Apertura (durata)", 1)]
    ths = "".join(
        f'<th style="cursor:pointer" onclick="injSort(\'{c}\',{n})">{lbl} '
        f'<span class="inj-ind" id="inj-ind-{c}">&#8645;</span></th>'
        for c, lbl, n in _cols)

    _card = ('background:#161b22;border:1px solid #30363d;border-radius:8px;'
             'padding:10px 16px;min-width:130px')
    return (
        f'<details style="margin:4px 0 14px">\n'
        f'<summary style="cursor:pointer;list-style-position:inside;background:#161b22;'
        f'border:1px solid #30363d;border-radius:8px;padding:10px 14px;font-weight:600">'
        f'🤖 Injective Autopilot (PAPER) '
        f'<span style="font-size:.75rem;font-weight:400;color:#8b949e;margin-left:10px">'
        f'aperte {len(opened)} · chiuse {len(closed)} · '
        f'WR <span style="color:{wr_c}">{wr:.0f}%</span> · '
        f'P&L <span style="color:{pnl_c}">{pnl:+.2f}$</span> · '
        f'statistiche separate — clicca per i trade</span></summary>\n'
        # ── Card vincite / perdite / netto ──
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0">\n'
        f'  <div style="{_card}"><div style="font-size:.7rem;color:#8b949e;text-transform:uppercase">Vincite</div>'
        f'<div style="font-size:1.15rem;font-weight:700;color:#3fb950">{tot_win:+.2f}$</div>'
        f'<div style="font-size:.72rem;color:#484f58">{wins} trade</div></div>\n'
        f'  <div style="{_card}"><div style="font-size:.7rem;color:#8b949e;text-transform:uppercase">Perdite</div>'
        f'<div style="font-size:1.15rem;font-weight:700;color:#f85149">{tot_loss:+.2f}$</div>'
        f'<div style="font-size:.72rem;color:#484f58">{losses} trade</div></div>\n'
        f'  <div style="{_card}"><div style="font-size:.7rem;color:#8b949e;text-transform:uppercase">Netto</div>'
        f'<div style="font-size:1.15rem;font-weight:700;color:{pnl_c}">{pnl:+.2f}$</div>'
        f'<div style="font-size:.72rem;color:#484f58">WR {wr:.0f}% · payoff '
        f'{abs(tot_win/wins/(tot_loss/losses)) if wins and losses else 0:.2f}</div></div>\n'
        f'</div>\n'
        # ── Filtri rapidi ──
        f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:.78rem;color:#8b949e">\n'
        f'  Dir: <select id="inj_fdir" onchange="injFilter()" style="background:#21262d;border:1px solid #30363d;'
        f'color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:.75rem">'
        f'<option value="">Tutte</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select>\n'
        f'  Esito: <select id="inj_fstato" onchange="injFilter()" style="background:#21262d;border:1px solid #30363d;'
        f'color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:.75rem">'
        f'<option value="">Tutti</option><option value="OPEN">OPEN</option>{esiti_opts}</select>\n'
        f'  🔍 <input type="text" id="inj_ftok" onkeyup="injFilter()" placeholder="ticker..." '
        f'style="background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;'
        f'padding:3px 6px;font-size:.75rem;width:110px">\n'
        f'  <span id="inj_vis" style="color:#484f58"></span>\n'
        f'</div>\n'
        f'<div class="wrap"><table>\n'
        f'  <thead><tr>{ths}</tr></thead>\n'
        f'  <tbody id="inj_tbody">{trs}</tbody>\n'
        f'</table></div>\n'
        f'<script>\n'
        f'var _injCol="",_injDir=-1;\n'
        f'function injSort(col,isNum){{\n'
        f'  if(_injCol===col){{_injDir=-_injDir;}}else{{_injCol=col;_injDir=-1;}}\n'
        f'  var tb=document.getElementById("inj_tbody");\n'
        f'  var rs=Array.from(tb.querySelectorAll("tr.injrow"));\n'
        f'  rs.sort(function(a,b){{\n'
        f'    var av=a.dataset[col]||"",bv=b.dataset[col]||"";\n'
        f'    if(isNum){{return(parseFloat(av)-parseFloat(bv))*_injDir;}}\n'
        f'    return(av<bv?-1:av>bv?1:0)*_injDir;\n'
        f'  }});\n'
        f'  rs.forEach(r=>tb.appendChild(r));\n'
        f'  document.querySelectorAll(".inj-ind").forEach(function(s){{s.textContent="\\u21C5";}});\n'
        f'  var ind=document.getElementById("inj-ind-"+col);\n'
        f'  if(ind) ind.textContent=_injDir>0?"\\u25B2":"\\u25BC";\n'
        f'}}\n'
        f'function injFilter(){{\n'
        f'  var fd=document.getElementById("inj_fdir").value;\n'
        f'  var fs=document.getElementById("inj_fstato").value;\n'
        f'  var ft=document.getElementById("inj_ftok").value.toLowerCase().trim();\n'
        f'  var n=0;\n'
        f'  document.querySelectorAll("#inj_tbody tr.injrow").forEach(function(r){{\n'
        f'    var ok=(!fd||r.dataset.dir===fd)&&(!fs||r.dataset.stato===fs)\n'
        f'        &&(!ft||r.dataset.ticker.indexOf(ft)>=0);\n'
        f'    r.style.display=ok?"":"none"; if(ok)n++;\n'
        f'  }});\n'
        f'  document.getElementById("inj_vis").textContent=n+" visibili";\n'
        f'}}\n'
        f'</script>\n'
        f'</details>\n'
    )


def _build_kpi_section(hours: float = 24) -> str:
    """Sezione KPI per sim_report.html: ultime N ore da live_trades.csv."""
    try:
        rows = list(csv.DictReader(open(LIVE_LOG_CSV, newline="")))
    except Exception:
        return ""

    cutoff = datetime.now() - timedelta(hours=hours)
    SKIP = {"", "open", "skip_stale", "skip", "skip_routing",
            "purged_stale", "duplicate_pair", "no_pair_address",
            "expired_max_age", "-98.28"}

    last_by_sid  = {}
    entry_by_sid = {}
    for r in rows:
        last_by_sid[r["signal_id"]] = r
        if r["action"] == "entry" and r["signal_id"] not in entry_by_sid:
            entry_by_sid[r["signal_id"]] = r

    closed = []
    for r in last_by_sid.values():
        if r["exit_reason"] in SKIP or r["action"] in ("entry", "open"):
            continue
        if r.get("system") == "mirror":
            continue
        try:
            if datetime.fromisoformat(r["ts"]) >= cutoff:
                closed.append(r)
        except ValueError:
            pass

    if not closed:
        return ""

    def _pnl(r):   return float(r.get("pnl_eur") or 0)
    def _ev(sid, f):
        try: return float(entry_by_sid.get(sid, {}).get(f) or 0)
        except ValueError: return 0.0
    def _pf(lst):
        w = sum(_pnl(r) for r in lst if _pnl(r) > 0)
        l = sum(abs(_pnl(r)) for r in lst if _pnl(r) < 0)
        return w / l if l > 0 else 0.0
    def _wr(lst):
        return sum(1 for r in lst if _pnl(r) > 0) / len(lst) * 100 if lst else 0.0
    def _c(v):   # colore HTML
        return "#3fb950" if v > 0 else ("#f85149" if v < 0 else "#8b949e")

    n   = len(closed)
    tot = sum(_pnl(r) for r in closed)
    wr  = _wr(closed)
    pf  = _pf(closed)
    hs  = sum(1 for r in closed if r["exit_reason"] == "hard_sl")

    # ── righe per sistema ──────────────────────────────────────────────────
    from collections import defaultdict
    by_sys   = defaultdict(list)
    by_exit  = defaultdict(list)
    for r in closed:
        by_sys[r["system"]].append(r)
        by_exit[r["exit_reason"]].append(r)

    def sys_rows():
        lines = []
        for s, lst in sorted(by_sys.items(), key=lambda x: sum(_pnl(r) for r in x[1])):
            t = sum(_pnl(r) for r in lst)
            lines.append(
                f'<tr><td>{s}</td><td>{len(lst)}</td>'
                f'<td>{_wr(lst):.0f}%</td><td>{_pf(lst):.2f}</td>'
                f'<td style="color:{_c(t)}">{t:+.1f}€</td>'
                f'<td>{sum(1 for r in lst if r["exit_reason"]=="hard_sl")/len(lst)*100:.0f}%</td></tr>'
            )
        return "\n".join(lines)

    def exit_rows():
        lines = []
        for er, lst in sorted(by_exit.items(), key=lambda x: sum(_pnl(r) for r in x[1])):
            t = sum(_pnl(r) for r in lst)
            lines.append(
                f'<tr><td>{er}</td><td>{len(lst)}</td>'
                f'<td style="color:{_c(t)}">{t:+.1f}€</td>'
                f'<td>{t/len(lst):+.1f}€</td></tr>'
            )
        return "\n".join(lines)

    def bucket_rows(buckets, field):
        lines = []
        for lo, hi, label in buckets:
            sub = [r for r in closed if lo <= _ev(r["signal_id"], field) < hi]
            if not sub: continue
            t = sum(_pnl(r) for r in sub)
            lines.append(
                f'<tr><td>{label}</td><td>{len(sub)}</td>'
                f'<td>{_wr(sub):.0f}%</td>'
                f'<td style="color:{_c(t)}">{t:+.1f}€</td>'
                f'<td>{t/len(sub):+.1f}€</td></tr>'
            )
        return "\n".join(lines)

    VOL_B = [(0,1,"= 0"),(1,5000,"1-5k"),(5000,15000,"5-15k"),
             (15000,30000,"15-30k"),(30000,50000,"30-50k"),(50000,9e9,"50k+")]
    BSR_B = [(0,.45,"0.0-0.45"),(.45,.55,"0.45-0.55"),(.55,.65,"0.55-0.65"),
             (.65,.75,"0.65-0.75"),(.75,1.01,"0.75+")]

    th = "style='background:#161b22;color:#8b949e;font-size:.72rem;padding:4px 8px;text-align:left'"
    td_css = "style='padding:3px 8px;font-size:.78rem;border-bottom:1px solid #21262d'"
    tbl_css = "style='border-collapse:collapse;width:100%'"

    def mini_table(headers, body_rows):
        ths = "".join(f"<th {th}>{h}</th>" for h in headers)
        return (f'<table {tbl_css}><thead><tr>{ths}</tr></thead>'
                f'<tbody>{body_rows}</tbody></table>')

    section_css = ("background:#0d1117;border:1px solid #21262d;border-radius:8px;"
                   "padding:14px 18px;margin-bottom:18px")
    h3_css = "style='margin:0 0 10px;font-size:.85rem;color:#8b949e;letter-spacing:.5px'"
    grid = "display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px"

    tot_color = _c(tot)
    return (
        f'<div style="{section_css}">'
        f'<div style="display:flex;align-items:baseline;gap:16px;margin-bottom:12px">'
        f'<span style="font-size:.9rem;font-weight:600;color:#c9d1d9">&#128202; KPI Ultime {hours:.0f}h</span>'
        f'<span style="font-size:.75rem;color:#8b949e">n={n} &middot; '
        f'WR={wr:.0f}% &middot; PF={pf:.2f} &middot; '
        f'<span style="color:{tot_color}">{tot:+.1f}€</span> &middot; '
        f'hs%={hs/n*100:.0f}%</span>'
        f'</div>'
        f'<div style="{grid}">'

        f'<div>'
        f'<p {h3_css}>PER SISTEMA</p>'
        + mini_table(["Sistema","n","WR","PF","PnL","hs%"], sys_rows()) +
        f'</div>'

        f'<div>'
        f'<p {h3_css}>PER EXIT REASON</p>'
        + mini_table(["Exit","n","PnL","avg"], exit_rows()) +
        f'</div>'

        f'<div>'
        f'<p {h3_css}>vol_h1 ALL\'ENTRY</p>'
        + mini_table(["Fascia","n","WR","PnL","avg"], bucket_rows(VOL_B, "vol_h1")) +
        f'<p {h3_css} style="margin-top:12px">BSR ALL\'ENTRY</p>'
        + mini_table(["BSR","n","WR","PnL","avg"], bucket_rows(BSR_B, "bsr")) +
        f'</div>'

        f'</div>'  # grid
        f'</div>'  # section
    )


def _load_executor_map() -> dict:
    """
    Legge real_executions.csv (Solana) e base_executions.csv (Base).
    Ritorna {signal_id: {"chain", "status", "action", "note", "pnl_exec"}}
    con lo stato finale per ogni segnale (ultima riga rilevante).
    """
    _WHY = {
        "no_route_raydium_jupiter": "Nessuna route Jupiter/Raydium",
        "rugcheck_failed":          "Rugcheck fallito",
        "processed_no_buy":         "Processato ma buy non eseguito",
        "no_route":                 "Nessuna route V3/Aerodrome/V2",
        "balance=0_post_buy":       "TX ok ma saldo=0 on-chain",
        "balance=0_post_swap":      "TX ok ma saldo=0 (honeypot tax)",
        "honeypot_sell_lock":       "Honeypot: sell simulato bloccato",
        "reserves_check_failed":    "Reserves non leggibili → skip",
        "price_impact":             "Price impact troppo alto",
        "via_pumpswap":             "Eseguito via PumpSwap SDK",
    }
    result: dict = {}

    def _parse_csv(path, chain):
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return
        by_sid: dict = {}
        for r in rows:
            sid = r.get("signal_id", "").strip()
            if not sid:
                continue
            by_sid.setdefault(sid, []).append(r)

        for sid, rs in by_sid.items():
            # Determina stato finale: preferisci buy/sell su dry_run e skipped
            priority = {"sent": 5, "confirmed": 5, "failed_onchain": 4,
                        "error": 3, "stuck": 3, "skipped": 2, "dry_run": 1}
            best = max(rs, key=lambda r: priority.get(r.get("status",""), 0))
            action = best.get("action", "")
            status = best.get("status", "")
            note   = best.get("note", "") or ""

            # PnL da nota "pnl=+X$"
            pnl_exec = None
            for r in rs:
                n = r.get("note","") or ""
                if "pnl=" in n:
                    try:
                        pnl_exec = float(n.split("pnl=")[1].split("USDC")[0].split("$")[0])
                    except Exception:
                        pass

            # Motivo leggibile
            why = ""
            for key, label in _WHY.items():
                if key in note:
                    why = label
                    break
            if not why and "entry_drop=" in note:
                try:
                    drop = note.split("entry_drop=")[1].split(">")[0]
                    why = f"Prezzo calato {drop} prima dell'entry"
                except Exception:
                    why = "Prezzo calato prima dell'entry"
            if not why and "live_liq=" in note and "<10k" in note:
                try:
                    liq_val = note.split("live_liq=")[1].split("<")[0]
                    why = f"Liq live {liq_val} < $10k al momento del buy"
                except Exception:
                    why = "Liquidità insufficiente al momento del buy"
            if not why and "entry_drop=" in note:
                try:
                    drop = note.split("entry_drop=")[1].split(">")[0]
                    why = f"Prezzo calato {drop} prima dell'entry"
                except Exception:
                    why = "Prezzo calato prima dell'entry"
            if not why and action in ("buy_skipped", "buy_failed"):
                why = note[:70] if note else "Motivo sconosciuto"
            if not why and status == "dry_run":
                why = "Dry run"
            if not why and status in ("stuck",):
                why = "Sell bloccato (3 tentativi falliti)"

            # Tag esito
            if status in ("sent", "confirmed"):
                outcome = "exec"
            elif action in ("buy_failed", "buy_skipped") or status == "error":
                outcome = "fail"
            elif status == "stuck":
                outcome = "stuck"
            elif status == "dry_run":
                outcome = "dryrun"
            else:
                outcome = "skip"

            # Token address dalla riga buy (per bottone copia in dashboard)
            tok_addr = ""
            for r in rs:
                candidate = r.get("token_address", "") or ""
                if candidate and len(candidate) > 10:
                    tok_addr = candidate
                    break

            result[sid] = {
                "chain": chain, "status": status, "action": action,
                "note": note, "why": why, "outcome": outcome, "pnl_exec": pnl_exec,
                "token_address": tok_addr,
            }

    _parse_csv(REAL_EXEC_CSV, "solana")
    _parse_csv(BASE_EXEC_CSV, "base")
    return result


def _build_executor_section(all_states: dict, executor_map: dict) -> str:
    """Sezione HTML: confronto Simulator vs Executor per tutti i sistemi."""
    OUTCOME_LABEL = {
        "exec":   ("✅", "#3fb950", "Eseguito"),
        "dryrun": ("🔵", "#58a6ff", "Dry run"),
        "fail":   ("❌", "#f85149", "Skip/Fail"),
        "stuck":  ("🔒", "#e3b341", "Sell bloccato"),
        "skip":   ("⚠️",  "#e3b341", "Skip"),
        "unseen": ("❓", "#8b949e", "Non raggiunto"),
    }
    SYS_COLORS = {
        "pump_grad": "#f0883e", "mirror": "#bc8cff", "defi": "#1f6feb",
        "v3": "#e3b341", "v3_large": "#a371f7", "midcap": "#3fb950",
        "pre_grad": "#58a6ff", "defi_v3": "#39c5cf",
    }

    # Tutti i sistemi, ordina per ts decrescente
    candidates = sorted(
        all_states.items(),
        key=lambda x: x[1].get("ts", ""), reverse=True
    )[:300]  # ultimi 300

    rows_html = []
    rug_count = 0
    for sid, s in candidates:
        sym      = s.get("token_symbol", "?")
        sys_name = s.get("system", "?")
        chain    = (s.get("chain") or "solana").lower()
        chain_c  = "🟣" if chain == "solana" else "🔵"
        remaining = float(s.get("remaining", "0") or 0)
        pnl_s    = float(s.get("pnl_eur", "0").replace("+","") or 0)
        exit_r   = s.get("exit_reason", s.get("action","?"))
        is_open  = remaining > 0
        sc       = SYS_COLORS.get(sys_name, "#8b949e")

        pnl_color = "#3fb950" if pnl_s > 0 else ("#f85149" if pnl_s < 0 else "#8b949e")
        sim_str   = ("OPEN" if is_open else f"{pnl_s:+.0f}€")
        sim_color = "#8b949e" if is_open else pnl_color

        ex = executor_map.get(sid)
        if ex is None:
            oc = "unseen"
            why = "Non raggiunto dall'executor"
            pnl_ex_str = "—"
        else:
            oc  = ex["outcome"]
            why = ex["why"] or ex["note"][:70]
            pe  = ex.get("pnl_exec")
            pnl_ex_str = f"{pe:+.1f}$" if pe is not None else ("OPEN" if oc in ("exec","dryrun") else "—")

        icon, oc_color, oc_label = OUTCOME_LABEL.get(oc, ("❓","#8b949e","?"))

        # Rugpull detection:
        # 1. Honeypot tax 100%: balance=0_post_swap / honeypot_sell_lock
        # 2. Sell stuck dopo buy (LP rimossa dopo acquisto)
        # 3. Sim mostra liq_collapse con pnl vicino a 0 (pool drenata subito)
        # 4. Hard_sl estremo (>-90%): probabile rug istantaneo
        _is_rug = False
        _rug_type = ""
        ex_note = (ex or {}).get("note", "") or ""
        if "honeypot_sell_lock" in ex_note or "honeypot_sell_lock" in why:
            _is_rug = True; _rug_type = "honeypot (sell bloccato)"
        elif "balance=0_post_swap" in ex_note or "balance=0_post_swap" in why:
            _is_rug = True; _rug_type = "honeypot (tax 100%)"
        elif oc == "stuck":
            _is_rug = True; _rug_type = "sell stuck (LP rimossa?)"
        elif exit_r in ("liq_collapse",) and not is_open and pnl_s < 5:
            _is_rug = True; _rug_type = "liq_collapse senza profitto"
        elif exit_r in ("hard_sl",) and pnl_s < -80:
            _is_rug = True; _rug_type = "hard_sl estremo (rug istantaneo)"

        if _is_rug:
            rug_count += 1
        rug_flag = "1" if _is_rug else "0"
        rug_cell = (
            f'<span style="color:#f85149;font-weight:600;font-size:.8rem">🔴 {_rug_type}</span>'
            if _is_rug else
            '<span style="color:#3fb950;font-size:.8rem">✅ no</span>'
        )

        try:
            parts = sid.rsplit("_", 2)
            d, t = parts[-2], parts[-1]
            sig_ts = f"{d[6:8]}/{d[4:6]} {t[:2]}:{t[2:4]}"
        except Exception:
            sig_ts = ""

        # Token address: da executor_map o da all_states (pair_address come fallback)
        tok_addr = (ex or {}).get("token_address", "") or s.get("token_address", "") or ""
        copy_btn = (
            f'<button onclick="navigator.clipboard.writeText(\'{tok_addr}\');this.textContent=\'✓\';'
            f'setTimeout(()=>this.textContent=\'⧉\',1200)" '
            f'style="background:none;border:1px solid #30363d;border-radius:3px;color:#8b949e;'
            f'cursor:pointer;font-size:.65rem;padding:0 3px;margin-left:4px" title="{tok_addr}">⧉</button>'
            if tok_addr else ""
        )
        rows_html.append(
            f'<tr class="exrow" data-chain="{chain}" data-oc="{oc}" data-sys="{sys_name}" data-rug="{rug_flag}">'
            f'<td style="font-weight:600">{sym}{copy_btn} '
            f'<span style="font-size:.7rem;color:#484f58">{sig_ts}</span><br>'
            f'<span class="tag" style="background:{sc}22;color:{sc};font-size:.65rem">{sys_name.upper()}</span></td>'
            f'<td style="color:#8b949e;font-size:.8rem">{chain_c} {chain}</td>'
            f'<td style="color:{sim_color};font-weight:600">{sim_str}</td>'
            f'<td style="color:#8b949e;font-size:.78rem">{exit_r[:18]}</td>'
            f'<td style="color:{oc_color};font-weight:600">{icon} {oc_label}</td>'
            f'<td style="color:#8b949e;font-size:.78rem">{pnl_ex_str}</td>'
            f'<td>{rug_cell}</td>'
            f'<td style="color:#484f58;font-size:.75rem;max-width:200px">{why}</td>'
            f'</tr>'
        )

    rows_str = "\n".join(rows_html)
    total    = len(candidates)
    exec_n   = sum(1 for sid, _ in candidates if executor_map.get(sid, {}).get("outcome") == "exec")
    dry_n    = sum(1 for sid, _ in candidates if executor_map.get(sid, {}).get("outcome") == "dryrun")
    fail_n   = sum(1 for sid, _ in candidates if executor_map.get(sid, {}).get("outcome") in ("fail","skip"))
    stuck_n  = sum(1 for sid, _ in candidates if executor_map.get(sid, {}).get("outcome") == "stuck")
    unseen_n = total - exec_n - dry_n - fail_n - stuck_n

    sys_present = sorted({s.get("system","?") for _, s in candidates if s.get("system")})
    sys_btns = ''.join(
        f'<button class="filter-btn" onclick="exFilterSys(this,\'{sy}\')" '
        f'style="color:{SYS_COLORS.get(sy,"#8b949e")}">{sy.upper()}</button>'
        for sy in sys_present
    )

    return (
        f'<div class="section-title">⚡ Executor vs Simulator — tutti i sistemi</div>\n'
        f'<div style="margin:6px 0 4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">\n'
        f'  <span style="color:#8b949e;font-size:.75rem">Sistema:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="filter-btn active" onclick="exFilterSys(this,\'\')">Tutti</button>\n'
        f'    {sys_btns}\n'
        f'  </div>\n'
        f'</div>\n'
        f'<div style="margin:0 0 4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">\n'
        f'  <span style="color:#8b949e;font-size:.75rem">Chain:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="filter-btn active" onclick="exFilterChain(this,\'\')">Tutti</button>\n'
        f'    <button class="filter-btn" onclick="exFilterChain(this,\'solana\')">🟣 Solana</button>\n'
        f'    <button class="filter-btn" onclick="exFilterChain(this,\'base\')">🔵 Base</button>\n'
        f'  </div>\n'
        f'  <span style="color:#8b949e;font-size:.75rem;margin-left:8px">Esito:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="filter-btn active" onclick="exFilterOc(this,\'\')">Tutti ({total})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterOc(this,\'exec\')" style="color:#3fb950">✅ Eseguito ({exec_n})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterOc(this,\'dryrun\')" style="color:#58a6ff">🔵 Dry run ({dry_n})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterOc(this,\'fail skip\')" style="color:#f85149">❌ Skip/Fail ({fail_n})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterOc(this,\'stuck\')" style="color:#e3b341">🔒 Stuck ({stuck_n})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterOc(this,\'unseen\')" style="color:#8b949e">❓ Non raggiunto ({unseen_n})</button>\n'
        f'  </div>\n'
        f'  <span style="color:#8b949e;font-size:.75rem;margin-left:8px">Rugpull:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="filter-btn active" onclick="exFilterRug(this,\'\')">Tutti</button>\n'
        f'    <button class="filter-btn" onclick="exFilterRug(this,\'1\')" style="color:#f85149">🔴 Rug ({rug_count})</button>\n'
        f'    <button class="filter-btn" onclick="exFilterRug(this,\'0\')" style="color:#3fb950">✅ Legit</button>\n'
        f'  </div>\n'
        f'</div>\n'
        f'<div class="wrap"><table id="extable">\n'
        f'  <thead><tr>\n'
        f'    <th>Token / Sistema</th><th>Chain</th>'
        f'    <th>Sim PnL</th><th>Sim exit</th>'
        f'    <th>Executor</th><th>Exec PnL</th>'
        f'    <th>Rugpull?</th><th>Perché</th>\n'
        f'  </tr></thead>\n'
        f'  <tbody id="extbody">{rows_str}</tbody>\n'
        f'</table></div>\n'
        f'<script>\n'
        f'var _exChain="", _exOc="", _exSys="", _exRug="";\n'
        f'function exFilterSys(btn,v){{_exSys=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active"); _exApply();}}\n'
        f'function exFilterRug(btn,v){{_exRug=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active"); _exApply();}}\n'
        f'function exFilterChain(btn,v){{_exChain=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active"); _exApply();}}\n'
        f'function exFilterOc(btn,v){{_exOc=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active"); _exApply();}}\n'
        f'function _exApply(){{\n'
        f'  document.querySelectorAll("#extbody .exrow").forEach(r=>{{\n'
        f'    var c=r.dataset.chain, o=r.dataset.oc, sy=r.dataset.sys, rg=r.dataset.rug;\n'
        f'    var okC=!_exChain||c===_exChain;\n'
        f'    var okO=!_exOc||_exOc.split(" ").some(v=>o===v);\n'
        f'    var okS=!_exSys||sy===_exSys;\n'
        f'    var okR=!_exRug||rg===_exRug;\n'
        f'    r.style.display=(okC&&okO&&okS&&okR)?"":"none";\n'
        f'  }});\n'
        f'}}\n'
        f'</script>\n'
    )


def _build_live_html(open_list, closed_list, all_states, entry_prices: dict = None,
                     v3_routed: set = None):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    v3_routed = v3_routed or set()

    # Sistema "visualizzato": le gemme v3 instradate su config defi (mcap<$1M)
    # appaiono come defi_v3 — stessa gestione exit, ma fonte segnale diversa:
    # senza distinzione le statistiche dello scanner pre-pump sono inquinate
    def _disp_sys(sid, s):
        sys_name = s.get("system", "?")
        if sys_name == "defi" and sid in v3_routed:
            return "defi_v3"
        return sys_name

    _SYS_LABELS = {"defi_v3": "DEFI·V3"}

    # pre_grad shadow (12/06): segnali a size=0 (rugcheck rilassato), pnl reale
    # sempre 0€ — esclusi da WR/contatori per non sporcare le statistiche, ma
    # restano visibili nelle tabelle con shadow_pnl=...€ nella nota.
    def _is_shadow(s):
        return "shadow=true" in s.get("note", "")

    # data_fault (15/06): trade la cui riga finale non rappresenta un esito
    # di strategia valido (es. nessuna entry registrata + drawdown immediato
    # estremo, vedi WINNING/GOBLIEN/SERENA/PXC 23-24/05). Esclusi da
    # PF/WR/EV e loggati separatamente in data_fault_trades.csv.
    _has_entry = set(entry_prices.keys())

    def _is_data_fault(sid, s):
        ok, _ = is_valid_trade_event(s, sid in _has_entry)
        return not ok

    _log_data_fault_trades(all_states, _has_entry)

    # ── Stats per sistema ───────────────────────────────────────────────────
    sys_stats = {}
    for sys_name in ("defi", "defi_v3", "v3", "v3_large", "midcap", "pump_grad", "pre_grad", "mirror"):
        open_n   = sum(1 for sid, s in open_list if _disp_sys(sid, s) == sys_name)
        closed_s = [(sid, s) for sid, s in all_states.items()
                    if _disp_sys(sid, s) == sys_name and float(s.get("remaining", 0) or 0) <= 0
                    and not _is_shadow(s) and not _is_data_fault(sid, s)]
        wins     = sum(1 for _, s in closed_s if float(s.get("pnl_eur","0").replace("+","") or 0) > 0)
        wr       = wins / len(closed_s) * 100 if closed_s else 0
        pnl_tot  = sum(float(s.get("pnl_eur","0").replace("+","") or 0) for _, s in closed_s)
        # Solo trade con dati reali (non vol_na al momento dell'uscita)
        real_closed = [(sid, s) for sid, s in closed_s if s.get("note", "") != "vol_na"]
        real_wins   = sum(1 for _, s in real_closed if float(s.get("pnl_eur","0").replace("+","") or 0) > 0)
        real_wr     = real_wins / len(real_closed) * 100 if real_closed else 0
        sys_stats[sys_name] = {
            "open": open_n, "closed": len(closed_s),
            "wr": wr, "pnl": pnl_tot,
            "real_wr": real_wr, "real_closed": len(real_closed),
        }

    total_open   = len(open_list)
    total_closed = sum(s["closed"] for k, s in sys_stats.items() if k != "mirror")
    total_pnl    = sum(float(s.get("pnl_eur","0").replace("+","") or 0)
                       for sid, s in all_states.items()
                       if float(s.get("remaining", 0) or 0) <= 0 and not _is_shadow(s)
                       and not _is_data_fault(sid, s)
                       and s.get("system") != "mirror")
    # WR reale (esclude vol_na, shadow, data_fault e mirror)
    real_closed_all = [(sid, s) for sid, s in all_states.items()
                       if float(s.get("remaining", 0) or 0) <= 0 and s.get("note","") != "vol_na"
                       and not _is_shadow(s) and not _is_data_fault(sid, s)
                       and s.get("system") != "mirror"]
    real_wins_all   = sum(1 for _, s in real_closed_all
                          if float(s.get("pnl_eur","0").replace("+","") or 0) > 0)
    real_wr_all     = real_wins_all / len(real_closed_all) * 100 if real_closed_all else 0

    # posizioni aperte con dati reali (esclude prezzi TP congelati da CSV)
    def _pos_has_real_price(s):
        if not s.get("_has_live_fetch", False):
            if s.get("action", "entry") in ("tp1", "tp2"):
                return False
        return s.get("note", "") != "vol_na" or s.get("_live_pct", 0) != 0
    has_data = sum(1 for _, s in open_list if _pos_has_real_price(s))

    def pnl_color(v):
        return "#3fb950" if v > 0 else ("#f85149" if v < 0 else "#8b949e")

    def chg_color(v):
        if v >= 15:  return "#3fb950"
        if v >= 5:   return "#58a6ff"
        if v >= -5:  return "#8b949e"
        if v >= -15: return "#e3b341"
        return "#f85149"

    def decisione_badge(chg, bsr, has_price):
        if not has_price:
            return '<span class="badge b-na">N/D</span>'
        if chg >= 20:   return '<span class="badge b-pump">⬆ Pump</span>'
        if chg >= 8:    return '<span class="badge b-up">⬆ Salita</span>'
        if chg >= -5:   return '<span class="badge b-neu">⚪ Neutro</span>'
        if chg >= -20:  return '<span class="badge b-down">⬇ Calo</span>'
        return '<span class="badge b-dump">⬇ Dump</span>'

    # ── Stats cards ─────────────────────────────────────────────────────────
    def sys_card(name, color):
        s    = sys_stats[name]
        wr_str = f"{s['real_wr']:.0f}%" if s['real_closed'] else "—"
        pnl_c  = pnl_color(s['pnl'])
        wr_c   = "#3fb950" if s["real_wr"] >= 40 else "#e3b341" if s["real_wr"] >= 25 else "#f85149"
        return (
            f'<div class="sys-card" style="border-top:3px solid {color}">'
            f'<div class="sys-lbl">{_SYS_LABELS.get(name, name.upper())}</div>'
            f'<div class="sys-row"><span class="lbl2">Aperte</span>'
            f'<strong id="sc-{name}-open">{s["open"]}</strong></div>'
            f'<div class="sys-row"><span class="lbl2">Chiuse</span>'
            f'<strong id="sc-{name}-closed">{s["closed"]}</strong></div>'
            f'<div class="sys-row"><span class="lbl2">WR reale</span>'
            f'<strong id="sc-{name}-wr" style="color:{wr_c}">'
            f'{wr_str}</strong></div>'
            f'<div class="sys-row"><span class="lbl2">P&L</span>'
            f'<strong id="sc-{name}-pnl" style="color:{pnl_c}">{s["pnl"]:+.0f}€</strong></div>'
            f'</div>'
        )

    def _copy_btn(sid):
        """Bottone ⧉ che copia il token_address in clipboard."""
        addr = (_ex_map.get(sid) or {}).get("token_address", "")
        if not addr:
            return ""
        return (
            f'<button onclick="navigator.clipboard.writeText(\'{addr}\');'
            f'this.textContent=\'✓\';setTimeout(()=>this.textContent=\'⧉\',1200)" '
            f'style="background:none;border:1px solid #30363d;border-radius:3px;'
            f'color:#8b949e;cursor:pointer;font-size:.65rem;padding:0 3px;margin-left:3px" '
            f'title="{addr}">⧉</button>'
        )

    # ── Righe tabella aperte ─────────────────────────────────────────────────
    def open_row(sid, s):
        sys_name       = _disp_sys(sid, s)
        sym            = s.get("token_symbol", "?")
        chain          = s.get("chain", "").lower()
        pair           = s.get("_pair", s.get("pair_address",""))
        has_live_fetch = s.get("_has_live_fetch", False)
        last_action    = s.get("action", "entry")
        live_pct  = s.get("_live_pct", float(s.get("change_pct","0").replace("+","") or 0))
        live_vol  = s.get("_live_vol", float(s.get("vol_h1","0") or 0))
        live_bsr  = s.get("_live_bsr", 1.0)
        peak      = s.get("_peak", live_pct)
        pnl       = float(s.get("pnl_eur","0").replace("+","") or 0)
        note      = s.get("note","")
        remaining = float(s.get("remaining","1") or 1)
        # Senza fetch live reale, il change_pct dopo tp1/tp2 è il prezzo di
        # esecuzione del TP (es +15%), NON il prezzo corrente di mercato.
        tp_price_frozen = not has_live_fetch and last_action in ("tp1", "tp2")
        has_price = (note != "vol_na" or live_pct != 0) and not tp_price_frozen
        ev        = float(s.get("vol_h1","0") or 0)
        vol_ratio = f"{live_vol/ev*100:.0f}%" if ev > 0 and live_vol > 0 else "—"

        # Calcola età dal timestamp della segnalazione originale (da signal_id: SYM_YYYYMMDD_HHMMSS)
        # Fallback su ts (quando il bot ha loggato l'entry)
        try:
            from datetime import datetime as dt
            _parts = sid.rsplit("_", 2)
            sig_ts = dt.strptime(f"{_parts[-2]}_{_parts[-1]}", "%Y%m%d_%H%M%S")
        except Exception:
            sig_ts = None
        ts_str = s.get("ts","")
        try:
            from datetime import datetime as dt
            ref_ts = sig_ts if sig_ts else dt.fromisoformat(ts_str)
            age_s = (dt.now() - ref_ts).total_seconds()
            h, m  = int(age_s//3600), int((age_s%3600)//60)
            age   = f"{h}h{m:02d}m"
        except: age = "?"

        # TP status
        tp1_hit = s.get("tp1_hit", False) or any(
            r.get("action") == "tp1" for r in [s])
        # (semplificato: leggiamo dal remaining)
        if remaining <= 0.5 + 1e-6 and remaining > 1e-6:
            tp_status = '<span style="color:#3fb950">●TP1</span> <span style="color:#484f58">○TP2</span>'
        elif remaining > 0.5 + 1e-6:
            tp_status = '<span style="color:#484f58">○TP1</span>'
        else:
            tp_status = '<span style="color:#3fb950">●TP1 ●TP2</span>'

        dex_link = f"https://dexscreener.com/{chain}/{pair}" if pair and chain != "cex_spot" else "#"
        sys_colors = {"defi": "#1f6feb", "defi_v3": "#39c5cf", "v3": "#e3b341", "v3_large": "#a371f7", "midcap": "#3fb950", "pump_grad": "#f0883e", "pre_grad": "#58a6ff", "mirror": "#bc8cff"}
        sc = sys_colors.get(sys_name, "#8b949e")
        chg_c = chg_color(live_pct)
        pnl_c = pnl_color(pnl)
        # Colonna Δ attuale: mostra badge TP se il prezzo è congelato
        if tp_price_frozen:
            tp_lbl     = "TP2&#10003;" if last_action == "tp2" else "TP1&#10003;"
            delta_cell = f'<td style="color:#3fb950;font-weight:600;font-size:.8rem">{tp_lbl} <span style="color:#484f58;font-size:.7rem">attendi</span></td>'
        else:
            delta_cell = f'<td style="color:{chg_c};font-weight:600">{live_pct:+.1f}%</td>'

        opacity = "" if has_price else ' style="opacity:.5"'
        # Estrai data e ora di apertura da signal_id (SYM_YYYYMMDD_HHMMSS)
        sig_date = ""; entry_time = ""
        try:
            _parts = sid.rsplit("_", 2)
            _d = _parts[-2]; _t = _parts[-1]
            sig_date   = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]}"
            entry_time = f"{_d[6:8]}/{_d[4:6]} ore {_t[:2]}:{_t[2:4]}"
        except: pass
        sym_lower  = sym.lower()
        data_attrs = (f'data-sys="{sys_name}" data-chain="{chain}" '
                      f'data-hasdata="{1 if has_price else 0}" '
                      f'data-pct="{live_pct:.2f}" data-pnl="{pnl:.2f}" '
                      f'data-sigdate="{sig_date}" data-token="{sym_lower}"')

        peak_str   = "—" if peak == float("-inf") else f"{peak:+.1f}%"
        entry_tag  = (f'<span style="font-size:.68rem;color:#484f58;display:block;margin-top:1px">'
                      f'apertura {entry_time}</span>') if entry_time else ""
        ep_str     = _fmt_price((entry_prices or {}).get(sid, s.get("price","")))
        shadow_tag = (' <span class="tag" style="background:#8b949e22;color:#8b949e" '
                      'title="rugcheck rilassato, size=0: pnl tracciato ma escluso dal totale">SHADOW</span>') \
            if "shadow=true" in note else ""
        return (
            f'<tr class="orow"{opacity} {data_attrs}>'
            f'<td><a href="{dex_link}" target="_blank" style="color:#58a6ff;font-weight:600">{sym}</a>'
            f'{_copy_btn(sid)}{shadow_tag}'
            f'<span class="dup-badge" data-sym="{sym_lower}"></span>'
            f'<br><span class="tag" style="background:{sc}22;color:{sc}">{_SYS_LABELS.get(sys_name, sys_name.upper())}</span>'
            f'{entry_tag}</td>'
            f'<td style="color:#8b949e;font-size:.8rem">{age}</td>' +
            delta_cell +
            f'<td style="font-size:.72rem;color:#8b949e;font-family:monospace">{ep_str}</td>'
            f'<td style="color:#8b949e;font-size:.75rem">{peak_str}</td>'
            f'<td style="font-size:.8rem;color:#8b949e">{vol_ratio}</td>'
            f'<td style="font-size:.78rem;color:#8b949e">{live_bsr:.2f}</td>'
            f'<td style="font-size:.78rem">{tp_status}</td>'
            f'<td style="color:{pnl_c};font-weight:600">{pnl:+.2f}€</td>'
            f'<td>{decisione_badge(live_pct, live_bsr, has_price)}</td>'
            f'</tr>'
        )

    # ── Righe tabella chiuse ─────────────────────────────────────────────────
    def closed_row(sid, s):
        sym      = s.get("token_symbol", "?")
        sys_name = _disp_sys(sid, s)
        chg      = float(s.get("change_pct","0").replace("+","") or 0)
        pnl      = float(s.get("pnl_eur","0").replace("+","") or 0)
        reason   = s.get("exit_reason", s.get("action","?"))
        note     = s.get("note","")
        ts_str   = s.get("ts","")
        try:
            from datetime import datetime as dt
            exit_ts_dt = dt.fromisoformat(ts_str)
            # Durata hold: exit_ts - entry_ts (estratto dal signal_id SYM_YYYYMMDD_HHMMSS)
            _parts2 = sid.rsplit("_", 2)
            _entry_dt = dt.strptime(_parts2[-2] + _parts2[-1], "%Y%m%d%H%M%S")
            hold_s = (exit_ts_dt - _entry_dt).total_seconds()
            h, m  = int(hold_s//3600), int((hold_s%3600)//60)
            age   = f"{h}h{m:02d}m"
        except: age = "?"
        chg_c = chg_color(chg); pnl_c = pnl_color(pnl)
        sys_colors = {"defi": "#1f6feb", "defi_v3": "#39c5cf", "v3": "#e3b341", "v3_large": "#a371f7", "midcap": "#3fb950", "pump_grad": "#f0883e", "pre_grad": "#58a6ff", "mirror": "#bc8cff"}
        sc = sys_colors.get(sys_name, "#8b949e")
        # Estrai data e ora di apertura da signal_id (SYM_YYYYMMDD_HHMMSS)
        sig_date = ""; entry_time = ""
        try:
            _parts = sid.rsplit("_", 2)
            _d = _parts[-2]; _t = _parts[-1]
            sig_date   = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]}"
            entry_time = f"{_d[6:8]}/{_d[4:6]} ore {_t[:2]}:{_t[2:4]}"
        except: pass
        # Escludi righe con exit_reason da non mostrare
        EXCL = {"purged_stale","archiviato_pre_2026-05-20","duplicate_pair",
                "no_pair_address","expired_max_age","skip_stale"}
        if reason in EXCL:
            return ""
        sym_lower = sym.lower()
        win_flag  = 1 if pnl > 0 else 0
        entry_tag = (f'<span style="font-size:.68rem;color:#484f58;display:block;margin-top:1px">'
                     f'apertura {entry_time}</span>') if entry_time else ""
        chain_c = s.get("chain","").lower()
        exit_r    = reason.replace("exit_","").replace("_collapse","_coll")
        ep_str    = _fmt_price((entry_prices or {}).get(sid, ""))
        ex_str    = _fmt_price(s.get("price",""))
        shadow_flag = 1 if "shadow=true" in s.get("note", "") else 0
        shadow_tag = (' <span class="tag" style="background:#8b949e22;color:#8b949e" '
                      'title="rugcheck rilassato, size=0: pnl tracciato ma escluso dal totale">SHADOW</span>') \
            if shadow_flag else ""
        # Data/epoch di USCITA (da ts, ISO) — usati per il filtro 24h/range: sig_date
        # è l'apertura, ma "Ultime 24h" deve riflettere il pnl REALIZZATO in 24h
        # (sigdate vecchio causava +148 vs +64 reali del recap, stesso bug class)
        exit_date = ts_str[:10] if ts_str else sig_date
        try:
            exit_ts_epoch = exit_ts_dt.timestamp()
        except NameError:
            exit_ts_epoch = 0
        return (
            f'<tr class="crow" style="opacity:.8" data-sigdate="{sig_date}" data-exitdate="{exit_date}" '
            f'data-exitts="{exit_ts_epoch:.0f}" data-sys="{sys_name}" '
            f'data-chain="{chain_c}" data-exitr="{exit_r}" data-shadow="{shadow_flag}" '
            f'data-pnl="{pnl:.2f}" data-pct="{chg:.2f}" data-win="{win_flag}" data-token="{sym_lower}">'
            f'<td style="font-weight:600">{sym}{_copy_btn(sid)}{shadow_tag} '
            f'<span class="dup-badge" data-sym="{sym_lower}"></span>'
            f'<span class="tag" style="background:{sc}22;color:{sc}">{_SYS_LABELS.get(sys_name, sys_name.upper())}</span>'
            f'{entry_tag}</td>'
            f'<td style="color:#8b949e;font-size:.8rem">{age}</td>'
            f'<td style="color:{pnl_c};font-weight:600">{pnl:+.2f}€</td>'
            f'<td style="color:#8b949e;font-size:.8rem">{reason}</td>'
            f'<td style="color:{chg_c};font-size:.8rem">{chg:+.1f}%</td>'
            f'<td style="font-size:.72rem;color:#8b949e;font-family:monospace">{ep_str}</td>'
            f'<td style="font-size:.72rem;color:#8b949e;font-family:monospace">{ex_str}</td>'
            f'<td style="font-size:.75rem;color:#484f58">{note[:70] if shadow_flag else note[:40]}</td>'
            f'</tr>'
        )

    # Carica executor map una volta — usato da open_row, closed_row e executor_section
    _ex_map = _load_executor_map()

    open_rows   = "".join(open_row(sid, s) for sid, s in open_list)
    closed_rows = "".join(closed_row(sid, s) for sid, s in closed_list)

    pnl_c = pnl_color(total_pnl)
    wr_c  = "#3fb950" if real_wr_all >= 40 else "#e3b341" if real_wr_all >= 25 else "#f85149"

    html = (
        '<!DOCTYPE html>\n<html lang="it">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta http-equiv="refresh" content="60">\n'
        '<title>Trade Simulator Live</title>\n'
        '<style>\n'
        '*{box-sizing:border-box;margin:0;padding:0}\n'
        'body{font-family:\'Segoe UI\',system-ui,sans-serif;background:#0d1117;color:#e6edf3;padding:20px 24px;font-size:13px}\n'
        'a{color:#58a6ff;text-decoration:none}\n'
        'table{border-collapse:collapse;width:100%}\n'
        'th{font-size:.72rem;color:#8b949e;font-weight:500;text-transform:uppercase;'
        'letter-spacing:.4px;padding:8px 12px;border-bottom:1px solid #21262d;'
        'text-align:left;background:#161b22}\n'
        'th.sortable{cursor:pointer;user-select:none}\n'
        'th.sortable:hover{color:#e6edf3}\n'
        'th.sortable.sorted{color:#58a6ff}\n'
        '.sort-ind{font-size:.7rem;margin-left:3px;opacity:.5}\n'
        'th.sortable.sorted .sort-ind{opacity:1;color:#58a6ff}\n'
        'td{padding:8px 12px;border-bottom:1px solid #161b22;font-size:.85rem}\n'
        'tr.orow:hover td,tr.crow:hover td{background:#161b2244;cursor:pointer}\n'
        '.hdr{display:flex;align-items:center;gap:12px;margin-bottom:20px;'
        'border-bottom:1px solid #21262d;padding-bottom:14px;flex-wrap:wrap}\n'
        '.stat{background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'padding:10px 14px;min-width:110px}\n'
        '.stat .lbl{font-size:.68rem;color:#8b949e;text-transform:uppercase}\n'
        '.stat .val{font-size:1.3rem;font-weight:500;margin-top:2px}\n'
        '.sys-cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}\n'
        '.sys-card{background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'padding:10px 14px;min-width:130px}\n'
        '.sys-lbl{font-size:.9rem;font-weight:600;margin-bottom:6px}\n'
        '.sys-row{display:flex;justify-content:space-between;gap:12px;'
        'font-size:.78rem;margin-top:2px}\n'
        '.lbl2{color:#8b949e;font-size:.72rem}\n'
        '.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}\n'
        '.btn-grp{display:flex}\n'
        '.btn-grp button{background:#21262d;border:1px solid #30363d;color:#8b949e;'
        'font-size:.75rem;padding:5px 12px;cursor:pointer;transition:.15s}\n'
        '.btn-grp button:first-child{border-radius:5px 0 0 5px}\n'
        '.btn-grp button:last-child{border-radius:0 5px 5px 0;margin-left:-1px}\n'
        '.btn-grp button.active{background:#1f6feb;border-color:#1f6feb;color:white}\n'
        '.chain-btn.active,.sol-btn.active{background:#9945ff;border-color:#9945ff;color:white}\n'
        '.bsc-btn.active{background:#f3ba2f;border-color:#f3ba2f;color:#000}\n'
        '.pump-btn.active{background:#f0883e;border-color:#f0883e;color:#000}\n'
        '.eth-btn.active{background:#627eea;border-color:#627eea;color:white}\n'
        '.base-btn.active{background:#0052ff;border-color:#0052ff;color:white}\n'
        '.pre-btn.active{background:#58a6ff;border-color:#58a6ff;color:#000}\n'
        '.section-title{font-size:.78rem;font-weight:500;color:#8b949e;text-transform:uppercase;'
        'letter-spacing:.4px;margin:20px 0 8px;padding-bottom:6px;border-bottom:1px solid #21262d}\n'
        '.badge{padding:2px 8px;border-radius:10px;font-size:.72rem;border:1px solid}\n'
        '.b-pump{background:#3fb95022;color:#3fb950;border-color:#3fb95044}\n'
        '.b-up{background:#58a6ff22;color:#58a6ff;border-color:#58a6ff44}\n'
        '.b-neu{background:#8b949e22;color:#8b949e;border-color:#8b949e44}\n'
        '.b-down{background:#e3b34122;color:#e3b341;border-color:#e3b34144}\n'
        '.b-dump{background:#f8514922;color:#f85149;border-color:#f8514944}\n'
        '.b-na{background:#21262d;color:#484f58;border-color:#30363d}\n'
        '.tag{font-size:.65rem;padding:1px 5px;border-radius:4px;font-weight:500}\n'
        '.dup-badge{font-size:.65rem;padding:1px 5px;border-radius:4px;font-weight:600;'
        'background:#e3b34122;color:#e3b341;border:1px solid #e3b34144;'
        'margin-left:4px;display:none}\n'
        '.wrap{overflow-x:auto}\n'
        '.disclaimer{background:#161005;border:1px solid #6e5908;border-radius:6px;'
        'padding:10px 14px;margin-top:24px;font-size:.72rem;color:#b39a2e}\n'
        '</style>\n'
        '</head>\n<body>\n'
        '<div style="max-width:1600px;margin:0 auto">\n'
        f'<div class="hdr">\n'
        f'  <span style="font-size:1.2rem;font-weight:600">&#9889; Trade Simulator Live</span>\n'
        f'  <span style="font-size:.75rem;color:#484f58">{now_str} &middot; auto-refresh 60s</span>\n'
        f'  <div style="margin-left:auto;display:flex;gap:10px;flex-wrap:wrap">\n'
        f'    <div class="stat"><div class="lbl">Aperte</div>'
        f'<div class="val" id="stat_open" style="color:#58a6ff">{total_open}</div></div>\n'
        f'    <div class="stat"><div class="lbl">Chiuse</div>'
        f'<div class="val" id="stat_chiuse">{total_closed}</div></div>\n'
        f'    <div class="stat"><div class="lbl">WR reale</div>'
        f'<div class="val" id="stat_wr" style="color:{wr_c}">{real_wr_all:.0f}%</div></div>\n'
        f'    <div class="stat"><div class="lbl">P&L tot</div>'
        f'<div class="val" id="stat_pnl" style="color:{pnl_c}">{total_pnl:+.0f}€</div></div>\n'
        f'    <div class="stat"><div class="lbl">Con prezzi</div>'
        f'<div class="val" style="color:#e3b341">{has_data}</div></div>\n'
        f'  </div>\n'
        f'</div>\n'
        f'<div class="sys-cards">\n'
        f'{sys_card("defi","#1f6feb")}'
        f'{sys_card("defi_v3","#39c5cf")}'
        f'{sys_card("v3","#e3b341")}'
        f'{sys_card("v3_large","#a371f7")}'
        f'{sys_card("midcap","#3fb950")}'
        f'{sys_card("pump_grad","#f0883e")}'
        f'{sys_card("pre_grad","#58a6ff")}'
        f'{sys_card("mirror","#bc8cff")}'
        f'</div>\n'
        f'{_build_inj_section()}'
        f'{_build_kpi_section(hours=24)}'
        f'<div class="filters">\n'
        f'  <span class="lbl2">Dal:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button id="btn24h" class="preset-btn" data-v="24h" onclick="setPreset24h(this)">Ultime 24h</button>\n'
        f'    <button class="preset-btn" data-v="7d" onclick="setPresetDays(this,7)">7 giorni</button>\n'
        f'    <button class="preset-btn" data-v="30d" onclick="setPresetDays(this,30)">Ultimo mese</button>\n'
        f'    <button class="preset-btn" data-v="all" onclick="setPresetDays(this,0)">Tutto</button>\n'
        f'  </div>\n'
        f'  <input type="date" id="df" onchange="clearPreset();applyFilter()" '
        f'style="background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.75rem">\n'
        f'  <span class="lbl2">a</span>\n'
        f'  <input type="date" id="dt" onchange="clearPreset();applyFilter()" '
        f'style="background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.75rem">\n'
        f'  <span class="lbl2" style="margin-left:8px">Sistema:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="active sys-btn" onclick="setSys(this,\'\')">Tutti</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'defi\')">DEFI</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'defi_v3\')">DEFI·V3</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'v3\')">V3</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'v3_large\')">V3L</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'v3_midcap\')">V3M</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'midcap\')">MIDCAP</button>\n'
        f'    <button class="sys-btn pump-btn" onclick="setSys(this,\'pump_grad\')">🚀 PUMP</button>\n'
        f'    <button class="sys-btn pre-btn" onclick="setSys(this,\'pre_grad\')">⚡ PRE</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'mirror\')">MIRROR</button>\n'
        f'  </div>\n'
        f'  <span class="lbl2" style="margin-left:8px">Chain:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="active chain-btn" onclick="setChain(this,\'\')">Tutte</button>\n'
        f'    <button class="chain-btn sol-btn" onclick="setChain(this,\'solana\')">◎ SOL</button>\n'
        f'    <button class="chain-btn base-btn" onclick="setChain(this,\'base\')">🔵 BASE</button>\n'
        f'    <button class="chain-btn bsc-btn" onclick="setChain(this,\'bsc\')">◈ BSC</button>\n'
        f'  </div>\n'
        f'  <span style="font-size:.75rem;color:#8b949e;margin-left:8px">'
        f'Visibili: <span id="vis">—</span> aperte &nbsp;|&nbsp; <span id="vis_c_hdr">—</span> chiuse</span>\n'
        f'  <button onclick="exportCSV()" style="margin-left:12px;background:#21262d;border:1px solid #30363d;'
        f'color:#8b949e;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:.75rem" '
        f'title="Esporta i trade chiusi visibili in CSV">&#11015; Esporta CSV</button>\n'
        f'</div>\n'
        f'<div class="filters" style="margin-top:6px">\n'
        f'  <span class="lbl2">Esito:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button class="active out-btn" onclick="setOutcome(this,\'\')">Tutti</button>\n'
        f'    <button class="out-btn" onclick="setOutcome(this,\'win\')" style="color:#3fb950">&#10003; Win</button>\n'
        f'    <button class="out-btn" onclick="setOutcome(this,\'loss\')" style="color:#f85149">&#10007; Loss</button>\n'
        f'  </div>\n'
        f'  <span class="lbl2" style="margin-left:8px">Motivo exit:</span>\n'
        f'  <select id="exitr_sel" onchange="setExitr(this.value)" '
        f'style="background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.75rem">\n'
        f'    <option value="">Tutti</option>\n'
        f'    <option value="hard_sl">Hard SL</option>\n'
        f'    <option value="bsr_coll">BSR Collapse</option>\n'
        f'    <option value="vol_crash">Vol Crash</option>\n'
        f'    <option value="liq_coll">Liq Collapse</option>\n'
        f'    <option value="tp1_tp2">TP1+TP2</option>\n'
        f'    <option value="trail_exit">Trail Exit</option>\n'
        f'    <option value="sl_adaptive">SL Adattivo</option>\n'
        f'    <option value="manual_pause">Pausa</option>\n'
        f'  </select>\n'
        f'  <span class="lbl2" style="margin-left:8px">&#128269;</span>\n'
        f'  <input type="text" id="tok_search" placeholder="Cerca token..." onkeyup="setTokSearch(this.value)" '
        f'style="background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.75rem;width:150px">\n'
        f'</div>\n'
        f'<div class="section-title">Posizioni aperte (<span id="vis">—</span> su {total_open})</div>\n'
        f'<div class="wrap"><table id="tbl">\n'
        f'  <thead><tr>\n'
        f'    <th class="sortable" data-sortcol="token" onclick="sortTable(\'token\',\'tbody\')">'
        f'Token <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="sigdate" onclick="sortTable(\'sigdate\',\'tbody\')">'
        f'Et&#224; <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="pct" onclick="sortTable(\'pct\',\'tbody\')">'
        f'&#916; attuale <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th>Entrata</th><th>Picco</th><th>Vol%entry</th><th>BSR</th><th>TP / Trail</th>\n'
        f'    <th class="sortable" data-sortcol="pnl" onclick="sortTable(\'pnl\',\'tbody\')">'
        f'P&amp;L <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th>Decisione</th>\n'
        f'  </tr></thead>\n'
        f'  <tbody id="tbody">{open_rows}</tbody>\n'
        f'</table></div>\n'
        + _build_executor_section(all_states, _ex_map)
        + f'<div class="section-title">Trade chiusi (<span id="vis_c">—</span> visibili su {len(closed_list)})</div>\n'
        f'<div class="wrap"><table>\n'
        f'  <thead><tr>\n'
        f'    <th class="sortable" data-sortcol="token" onclick="sortTable(\'token\',\'ctbody\')">'
        f'Token <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="sigdate" onclick="sortTable(\'sigdate\',\'ctbody\')">'
        f'Et&#224; <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="pnl" onclick="sortTable(\'pnl\',\'ctbody\')">'
        f'P&amp;L <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="exitr" onclick="sortTable(\'exitr\',\'ctbody\')">'
        f'Motivo <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th class="sortable" data-sortcol="pct" onclick="sortTable(\'pct\',\'ctbody\')">'
        f'&#916; finale <span class="sort-ind">&#8645;</span></th>\n'
        f'    <th>Entrata</th><th>Uscita</th>\n'
        f'    <th>Note</th>\n'
        f'  </tr></thead>\n'
        f'  <tbody id="ctbody">{closed_rows}</tbody>\n'
        f'</table></div>\n'
        f'<div class="disclaimer">&#9888; Solo a scopo educativo. '
        f'Non costituisce consulenza finanziaria.</div>\n'
        f'<script>\n'
        f'var _sys="", _chain="", _outcome="", _exitr="", _tokSearch="", _sortCol="", _sortDir=-1, _cutoff_ts=0;\n'
        f'function dateNDaysAgo(n){{\n'
        f'  var d=new Date(); d.setDate(d.getDate()-n);\n'
        f'  return d.toISOString().slice(0,10);\n'
        f'}}\n'
        f'function dateToday(){{\n'
        f'  return new Date().toISOString().slice(0,10);\n'
        f'}}\n'
        f'function setPresetDays(btn,n){{\n'
        f'  _cutoff_ts=n>0?(Date.now()/1000-n*86400):0;\n'
        f'  document.getElementById("df").value="";\n'
        f'  document.getElementById("dt").value="";\n'
        f'  document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active");\n'
        f'  applyFilter();\n'
        f'}}\n'
        f'function setPreset24h(btn){{\n'
        f'  _cutoff_ts=Date.now()/1000-86400;\n'
        f'  document.getElementById("df").value="";\n'
        f'  document.getElementById("dt").value="";\n'
        f'  document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active");\n'
        f'  applyFilter();\n'
        f'}}\n'
        f'function clearPreset(){{\n'
        f'  document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));\n'
        f'}}\n'
        f'function setSys(btn,v){{_sys=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active");applyFilter();}}\n'
        f'function setChain(btn,v){{_chain=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active");applyFilter();}}\n'
        f'function setOutcome(btn,v){{_outcome=v;\n'
        f'  btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));\n'
        f'  btn.classList.add("active");applyFilter();}}\n'
        f'function setExitr(v){{_exitr=v;applyFilter();}}\n'
        f'function setTokSearch(v){{_tokSearch=v.toLowerCase().trim();applyFilter();}}\n'
        f'function sortTable(col,tbodyId){{\n'
        f'  if(_sortCol===col){{_sortDir=-_sortDir;}}else{{_sortCol=col;_sortDir=-1;}}\n'
        f'  var tbody=document.getElementById(tbodyId);\n'
        f'  var rows=Array.from(tbody.querySelectorAll("tr"));\n'
        f'  var numCols=["pnl","pct","win"];\n'
        f'  rows.sort(function(a,b){{\n'
        f'    var av=a.dataset[col]||"", bv=b.dataset[col]||"";\n'
        f'    if(numCols.indexOf(col)>=0){{return(parseFloat(av)-parseFloat(bv))*_sortDir;}}\n'
        f'    return(av<bv?-1:av>bv?1:0)*_sortDir;\n'
        f'  }});\n'
        f'  rows.forEach(r=>tbody.appendChild(r));\n'
        f'  document.querySelectorAll("th.sortable").forEach(function(th){{\n'
        f'    var isSorted=th.dataset.sortcol===col;\n'
        f'    th.classList.toggle("sorted",isSorted);\n'
        f'    var ind=th.querySelector(".sort-ind");\n'
        f'    if(ind) ind.textContent=isSorted?(_sortDir>0?"&#9650;":"&#9660;"):"&#8645;";\n'
        f'  }});\n'
        f'  applyFilter();\n'
        f'}}\n'
        f'function applyFilter(){{\n'
        f'  var df=document.getElementById("df").value;\n'
        f'  var dt=document.getElementById("dt").value;\n'
        f'  var vo=0, vc=0, tot_pnl=0, wins=0, losses=0;\n'
        f'  var sysStat={{defi:{{o:0,c:0,wins:0,pnl:0}},defi_v3:{{o:0,c:0,wins:0,pnl:0}},v3:{{o:0,c:0,wins:0,pnl:0}},v3_large:{{o:0,c:0,wins:0,pnl:0}},midcap:{{o:0,c:0,wins:0,pnl:0}},mirror:{{o:0,c:0,wins:0,pnl:0}},bnf:{{o:0,c:0,wins:0,pnl:0}},pump_grad:{{o:0,c:0,wins:0,pnl:0}},pre_grad:{{o:0,c:0,wins:0,pnl:0}}}};\n'
        f'  document.querySelectorAll("#tbody tr.orow").forEach(function(r){{\n'
        f'    var sd=r.dataset.sigdate||"";\n'
        f'    var tok=(r.dataset.token||"");\n'
        f'    var ok_df  = !df         || sd>=df;\n'
        f'    var ok_dt  = !dt         || sd<=dt;\n'
        f'    var ok_sys = !_sys       || r.dataset.sys===_sys;\n'
        f'    var ok_ch  = !_chain     || r.dataset.chain===_chain;\n'
        f'    var ok_tok = !_tokSearch || tok.indexOf(_tokSearch)>=0;\n'
        f'    var show   = ok_df && ok_dt && ok_sys && ok_ch && ok_tok;\n'
        f'    r.style.display=show?"":"none";\n'
        f'    if(show){{ vo++; var sys=r.dataset.sys||""; if(sysStat[sys]) sysStat[sys].o++; }}\n'
        f'  }});\n'
        f'  document.querySelectorAll("#ctbody tr.crow").forEach(function(r){{\n'
        f'    var win=parseInt(r.dataset.win||0);\n'
        f'    var tok=(r.dataset.token||"");\n'
        f'    var ok_df, ok_dt;\n'
        f'    if(_cutoff_ts>0){{ var et=parseFloat(r.dataset.exitts||0); ok_df=ok_dt=(et>=_cutoff_ts); }}\n'
        f'    else {{ var sd=r.dataset.exitdate||r.dataset.sigdate||""; ok_df = !df || sd>=df; ok_dt = !dt || sd<=dt; }}\n'
        f'    var ok_sys = !_sys       || r.dataset.sys===_sys;\n'
        f'    var ok_ch  = !_chain     || r.dataset.chain===_chain;\n'
        f'    var ok_out = !_outcome   || (_outcome==="win"&&win>0) || (_outcome==="loss"&&win===0);\n'
        f'    var ok_er  = !_exitr     || r.dataset.exitr===_exitr;\n'
        f'    var ok_tok = !_tokSearch || tok.indexOf(_tokSearch)>=0;\n'
        f'    var show   = ok_df && ok_dt && ok_sys && ok_ch && ok_out && ok_er && ok_tok;\n'
        f'    r.style.display=show?"":"none";\n'
        f'    if(show){{\n'
        f'      vc++;\n'
        f'      if(r.dataset.shadow!=="1" && r.dataset.sys!=="mirror"){{\n'
        f'      var p=parseFloat(r.dataset.pnl||0); tot_pnl+=p;\n'
        f'      if(win>0) wins++; else losses++;\n'
        f'      var sys=r.dataset.sys||""; if(sysStat[sys]){{\n'
        f'        sysStat[sys].c++; sysStat[sys].pnl+=p;\n'
        f'        if(win>0) sysStat[sys].wins++;\n'
        f'      }}\n'
        f'      }}\n'
        f'    }}\n'
        f'  }});\n'
        f'  ["vis","vis_c","vis_c_hdr"].forEach(function(id){{\n'
        f'    var el=document.getElementById(id); if(el) el.textContent=(id==="vis"?vo:vc);\n'
        f'  }});\n'
        f'  var sc=document.getElementById("stat_chiuse"); if(sc) sc.textContent=vc;\n'
        f'  var so=document.getElementById("stat_open"); if(so) so.textContent=vo;\n'
        f'  var tot=wins+losses;\n'
        f'  var wr=tot>0?Math.round(wins/tot*100):0;\n'
        f'  var swr=document.getElementById("stat_wr");\n'
        f'  if(swr){{swr.textContent=wr+"%";swr.style.color=wr>=40?"#3fb950":wr>=25?"#e3b341":"#f85149";}}\n'
        f'  var sp=document.getElementById("stat_pnl");\n'
        f'  if(sp){{\n'
        f'    sp.textContent=(tot_pnl>=0?"+":"")+Math.round(tot_pnl)+"\u20ac";\n'
        f'    sp.style.color=tot_pnl>=0?"#3fb950":"#f85149";\n'
        f'  }}\n'
        f'  ["defi","defi_v3","v3","v3_large","midcap","bnf","pump_grad","pre_grad","mirror"].forEach(function(sys){{\n'
        f'    var st=sysStat[sys];\n'
        f'    var eo=document.getElementById("sc-"+sys+"-open"); if(eo) eo.textContent=st.o;\n'
        f'    var ec=document.getElementById("sc-"+sys+"-closed"); if(ec) ec.textContent=st.c;\n'
        f'    var ewr=document.getElementById("sc-"+sys+"-wr");\n'
        f'    if(ewr){{\n'
        f'      if(st.c>0){{var w=Math.round(st.wins/st.c*100);ewr.textContent=w+"%";ewr.style.color=w>=40?"#3fb950":w>=25?"#e3b341":"#f85149";}}\n'
        f'      else{{ewr.textContent="\u2014";ewr.style.color="#8b949e";}}\n'
        f'    }}\n'
        f'    var ep=document.getElementById("sc-"+sys+"-pnl");\n'
        f'    if(ep){{\n'
        f'      ep.textContent=(st.pnl>=0?"+":"")+Math.round(st.pnl)+"\u20ac";\n'
        f'      ep.style.color=st.pnl>=0?"#3fb950":"#f85149";\n'
        f'    }}\n'
        f'  }});\n'
        f'  // Badge \u00d7N: conta solo posizioni APERTE simultanee per stesso token\n'
        f'  var tokCnt={{}};\n'
        f'  document.querySelectorAll("#tbody tr.orow").forEach(function(r){{\n'
        f'    if(r.style.display==="none") return;\n'
        f'    var t=r.dataset.token||""; if(t) tokCnt[t]=(tokCnt[t]||0)+1;\n'
        f'  }});\n'
        f'  document.querySelectorAll(".dup-badge").forEach(function(b){{\n'
        f'    var sym=b.dataset.sym||""; var n=tokCnt[sym]||0;\n'
        f'    if(n>1){{b.textContent="\u00d7"+n;b.style.display="inline";}}\n'
        f'    else{{b.style.display="none";}}\n'
        f'  }});\n'
        f'}}\n'
        f'function exportCSV(){{\n'
        f'  var df=document.getElementById("df").value||"tutto";\n'
        f'  var dt=document.getElementById("dt").value||"oggi";\n'
        f'  var sys=_sys||"tutti";\n'
        f'  var header="token,sistema,eta,pnl_eur,motivo,delta_finale,note,data_segnale\\n";\n'
        f'  var lines=[];\n'
        f'  document.querySelectorAll("#ctbody tr.crow").forEach(function(r){{\n'
        f'    if(r.style.display==="none") return;\n'
        f'    var cells=r.querySelectorAll("td");\n'
        f'    if(cells.length<6) return;\n'
        f'    var token=cells[0].querySelector("a")?cells[0].querySelector("a").textContent.trim():cells[0].textContent.trim();\n'
        f'    var pnl=r.dataset.pnl||"0";\n'
        f'    var motivo=cells[3].textContent.trim();\n'
        f'    var delta=cells[4].textContent.trim();\n'
        f'    var nota=cells[5].textContent.trim();\n'
        f'    var sigdate=r.dataset.sigdate||"";\n'
        f'    lines.push([token,sigdate,pnl,motivo,delta,nota].join(","));\n'
        f'  }});\n'
        f'  if(!lines.length){{alert("Nessun trade visibile.");return;}}\n'
        f'  var csv=header+lines.join("\\n");\n'
        f'  var blob=new Blob([csv],{{type:"text/csv"}});\n'
        f'  var a=document.createElement("a");\n'
        f'  a.href=URL.createObjectURL(blob);\n'
        f'  a.download="trades.csv"; a.click();\n'
        f'}}\n'
        f'document.addEventListener("DOMContentLoaded",function(){{\n'
        f'  // Default: ultime 24h\n'
        f'  var b=document.getElementById("btn24h");\n'
        f'  if(b) setPreset24h(b);\n'
        f'  else applyFilter();\n'
        f'}});\n'
        '</script>\n'
        '</div>\n</body></html>'
    )
    return html

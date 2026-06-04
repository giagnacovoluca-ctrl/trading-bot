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
import json
import logging
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
BASE_PUMP_SIGNALS = os.path.join(BASE, "reports", "base_pump_signals.csv")
V3_EXIT_SIGNALS   = os.path.join(BASE, "reports", "v3_exit_signals.csv")

LIVE_LOG_CSV  = os.path.join(BASE, "reports", "live_trades.csv")
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
        tp1_pct              = 25.0,   # abbassato 30→25: pre_grad entra a graduation price, +25% più facile da raggiungere
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
MAX_SIGNAL_AGE_H: dict = {"defi": 3, "v2": 48, "v3": 48, "bnf": 6, "v3_large": 168, "v3_midcap": 24, "pump_grad": 1, "mirror": 1, "pre_grad": 0.33}
MAX_SIGNAL_AGE_H_DEFAULT = 24   # fallback
REFRESH_SEC      = 30    # frequenza aggiornamento prezzi (era 60s → dimezza latenza entry)

# Catene abilitate — BSC/ETH disabilitati; BASE abilitato al loro posto.
# Per riabilitare: ALLOWED_CHAINS = {"solana", "bsc", "ethereum", "base"}
ALLOWED_CHAINS: set = {"solana", "base"}
MAX_CLOSED_SHOW  = 100   # trade chiusi mostrati nel report

LIVE_COLUMNS = [
    "ts", "signal_id", "system", "token_symbol", "chain", "pair_address",
    "action", "price", "change_pct", "vol_h1", "bsr",
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


def _fetch_price_jupiter(token_address: str, entry_price_usd: float,
                          timeout: int = 8) -> float | None:
    """
    Quota token → USDC su Jupiter e ritorna il prezzo corrente in USD/token.

    Usa i decimali SPL reali (da _get_solana_token_decimals); se non disponibili
    li determina per trial su (6, 9) scegliendo il risultato con ratio più vicino
    a 1.0 rispetto all'entry price — questo evita il bug in cui formula a 6 dec
    su un token a 9 dec restituisce un prezzo 1000x troppo basso (ratio≈0.001)
    e triggera un hard_sl falso.

    Ritorna None se: nessuna route (400), errore di rete, o nessun decimale
    produce un prezzo entro 0.001x–1000x dell'entry.
    """
    import math
    global _jup_last_t
    if not token_address or entry_price_usd <= 0:
        return None
    if token_address in _jup_no_route:
        return None

    # Throttle: min 1.5s tra chiamate per rispettare free tier Jupiter (30 req/min)
    elapsed = time.time() - _jup_last_t
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _jup_last_t = time.time()

    QUOTE_USD = 10.0

    # Decimali corretti dal catalogo; se assenti proviamo entrambi
    known_dec = _get_solana_token_decimals(token_address)
    decimals_to_try = (known_dec,) if known_dec is not None else (6, 9)

    candidates: list[tuple[float, float, int]] = []  # (log_dist, price, decimals)

    for decimals in decimals_to_try:
        tokens_for_10_usd = QUOTE_USD / entry_price_usd
        lamports = int(tokens_for_10_usd * (10 ** decimals))
        if lamports <= 0:
            continue
        params = {
            "inputMint":        token_address,
            "outputMint":       _USDC_MINT_SOL,
            "amount":           str(lamports),
            "slippageBps":      "50",
            "onlyDirectRoutes": "false",
            "maxAccounts":      "64",
        }
        try:
            r = requests.get(_JUP_QUOTE_URL, params=params, timeout=timeout)
            if r.status_code == 400:
                _jup_no_route.add(token_address)
                log.debug(f"[Jupiter] {token_address[:16]}… nessuna route (400) → skip sessione.")
                return None
            r.raise_for_status()
            data     = r.json()
            usdc_out  = int(data.get("outAmount", 0)) / 1_000_000
            tokens_in = lamports / (10 ** decimals)
            if usdc_out <= 0 or tokens_in <= 0:
                continue
            jup_price = usdc_out / tokens_in

            ratio = jup_price / entry_price_usd
            if not (0.001 <= ratio <= 1000):
                log.debug(
                    f"[Jupiter] {token_address[:12]}… {jup_price:.4g} (ratio={ratio:.3g}, "
                    f"dec={decimals}) fuori range → scartato"
                )
                continue

            log_dist = abs(math.log10(ratio))   # 0 = ratio esattamente 1.0
            candidates.append((log_dist, jup_price, decimals))

        except Exception as e:
            log.debug(f"[Jupiter] {token_address[:16]}… errore fetch: {e}")
            return None

    if not candidates:
        return None

    # Scegli il prezzo con ratio più vicino a 1.0 (decimali più probabilmente corretti)
    candidates.sort()
    best_log_dist, best_price, best_dec = candidates[0]

    # Caching: se i decimali erano sconosciuti, salva quelli vincenti
    if known_dec is None and token_address not in _token_dec_cache:
        _token_dec_cache[token_address] = best_dec
        log.debug(f"[Jupiter] {token_address[:12]}… decimali determinati: {best_dec} "
                  f"(ratio={best_price/entry_price_usd:.3g})")

    return best_price


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


def _fetch_price_pumpfun(mint: str) -> Optional[tuple]:
    """
    Prezzo dalla bonding curve pump.fun (usato per posizioni pre_grad non ancora graduate).
    Ritorna (price_usd, 0, 1.0, liq_usd, mint) oppure None.
    Imposta pos["pair_address"] se il token è già graduato (side effect tramite dict ritornato).
    """
    try:
        r = requests.get(
            f"https://frontend-api.pump.fun/coins/{mint}",
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return None
        d = r.json()

        # Token graduato → non usare più questo endpoint
        if d.get("complete"):
            raydium_pool = d.get("raydium_pool") or ""
            # Segnala con un valore speciale: price=0 ma pair_address valorizzato
            return (0.0, 0, 1.0, 0, mint, raydium_pool)  # 6-tuple speciale: graduated

        v_sol = float(d.get("virtual_sol_reserves", 0) or 0) / 1e9
        v_tok = float(d.get("virtual_token_reserves", 0) or 0) / 1e6
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
        log.debug(f"[pre_grad] pump.fun price {mint[:8]}: {e}")
        return None


def _log_trade(row: dict):
    """Appende una riga a live_trades.csv."""
    exists = os.path.exists(LIVE_LOG_CSV)
    with open(LIVE_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LIVE_COLUMNS)
        if not exists:
            w.writeheader()
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
        if not self._load_state():    # prova prima il JSON di stato
            self._load_existing()     # fallback: ricostruzione da CSV
        self._purge_stale()           # rimuovi fantasmi senza pair_address
        self._load_new_signals()

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

        # Cooldown per token_symbol (cross-sistema).
        # Traccia entry, loss exits e liq_collapse con cooldown differenziati.
        TOKEN_COOLDOWN_H        = 8    # dopo entry normale
        LIQ_COLLAPSE_COOLDOWN_H = 24   # dopo liq_collapse (token ruggato/DLMM)
        HARD_SL_COOLDOWN_H      = 12   # dopo hard_sl (perdita secca)
        LOSS_EXIT_COOLDOWN_H    = 4    # dopo bsr_collapse/vol_crash/exit_adaptive

        # Azioni che avviano cooldown con relativo numero di ore
        _COOLDOWN_MAP = {
            "entry":               TOKEN_COOLDOWN_H,
            "liq_collapse":        LIQ_COLLAPSE_COOLDOWN_H,
            "hard_sl":             HARD_SL_COOLDOWN_H,
            "exit_bsr_collapse":   LOSS_EXIT_COOLDOWN_H,
            "exit_vol_crash":      LOSS_EXIT_COOLDOWN_H,
            "exit_adaptive":       LOSS_EXIT_COOLDOWN_H,
            "exit_momentum":       LOSS_EXIT_COOLDOWN_H,
        }

        recent_tokens: dict = {}        # sym_upper → (ts, action)
        recent_taddrs: dict = {}        # token_address_lower → (ts, action)
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
                            if sym and (sym not in recent_tokens or ts > recent_tokens[sym]):
                                recent_tokens[sym] = (ts, action)
                            if ta and (ta not in recent_taddrs or ts > recent_taddrs[ta]):
                                recent_taddrs[ta] = (ts, action)
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

        new_count = 0
        for system, path in [("defi", DEFI_SIGNALS), ("v3", V3_SIGNALS), ("pump_grad", PUMP_GRAD_SIGNALS), ("mirror", MIRROR_SIGNALS), ("pre_grad", PRE_GRAD_SIGNALS), ("base_pump", BASE_PUMP_SIGNALS)]:
        # BNF (Binance Futures) disabilitato: logica futures incompatibile con bot spot
            if not os.path.exists(path):
                continue
            try:
                df = pd.read_csv(path, on_bad_lines="skip")
                if "gem_id" in df.columns:
                    df = df.rename(columns={"gem_id": "signal_id"})
                for _, row in df.iterrows():
                    sid = str(row.get("signal_id", ""))
                    if not sid or sid in known:
                        continue
                    # Skip se pair_address in permanent blacklist
                    pair_addr_check = str(row.get("pair_address","") or "").strip()
                    if pair_addr_check and pair_addr_check in _perm_bl:
                        known.add(sid)
                        continue
                    # Skip se stessa pair_address già in portafoglio
                    if pair_addr_check and pair_addr_check in active_pairs:
                        known.add(sid)
                        continue
                    # Skip se stesso token_address già aperto (cross-sistema: es. EUP/v3 = ReelRush/defi)
                    tok_addr_check = str(row.get("token_address","") or "").strip().lower()
                    if tok_addr_check and tok_addr_check in active_taddrs:
                        log.debug(f"[live/{system}] {sid}: token_address {tok_addr_check[:12]}… già in portafoglio (altro sistema) → skip")
                        known.add(sid)
                        continue
                    # Routing sistema
                    effective_system = system
                    if system == "mirror":
                        effective_system = "pump_grad"  # mirror usa config/logica pump_grad
                    elif system == "pre_grad":
                        pass  # mantiene "pre_grad" — ha config e logica dedicata
                    elif system == "v3":
                        sig_src  = str(row.get("source", "") or "")
                        sig_mcap = float(row.get("market_cap_usd", 0) or 0)
                        sig_dex  = str(row.get("dex_id", "") or "")
                        if sig_src in ("coingecko_midcap", "coingecko_trending") and sig_mcap >= 5_000_000:
                            # Promozione a v3_large: token CoinGecko mcap>$10M con BSR forte su DEX
                            # Sostituisce il proxy "inflow_wallet_count=10" con un gate reale on-chain
                            _cg_bsr  = float(row.get("cg_dex_bsr", 0) or 0)
                            _cg_liq  = float(row.get("cg_dex_liq", 0) or 0)
                            _cg_chg  = float(row.get("cg_price_chg24", 0) or 0)
                            if (sig_mcap > 10_000_000
                                    and _cg_bsr >= 0.60
                                    and _cg_liq >= 30_000
                                    and _cg_chg >= 5.0
                                    and chain == "solana"):
                                effective_system = "v3_large"
                                log.info(f"[routing] {sid}: CoinGecko mid-cap promosso a v3_large "
                                         f"(mcap=${sig_mcap/1e6:.1f}M bsr={_cg_bsr:.2f} liq=${_cg_liq:,.0f} chg={_cg_chg:+.1f}%)")
                            else:
                                # v3_midcap disabilitato: sostituito da midcap_scanner (BB Squeeze)
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
                            if (_tier_vl not in ("DIAMOND", "GOLD")
                                    or _score_vl < 65
                                    or _bsr_vl < 0.6
                                    or _inflow_vl < 15
                                    or _pnl_vl < 10):
                                log.debug(f"[live/v3_large] {sid}: qualità insufficiente "
                                          f"(tier={_tier_vl} score={_score_vl:.0f} bsr={_bsr_vl:.2f} "
                                          f"inflow={_inflow_vl} pnl={_pnl_vl:.0f}%) → skip")
                                known.add(sid)
                                continue
                        elif sig_dex in ("pumpswap", "pump.fun") or sig_mcap < 1_000_000:
                            # Token pumpswap o micro-cap: comportamento da memecoin → usa config defi
                            effective_system = "defi"
                            log.debug(f"[routing] {sid}: pumpswap/microcap → defi (mcap=${sig_mcap:,.0f}, dex={sig_dex})")

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
                                    f"identico a segnale precedente) → skip"
                                )
                                known.add(sid)
                                continue

                    # Cooldown cross-sistema per symbol e token_address.
                    # Differenziato per tipo di uscita: hard_sl=12h, loss=4h, entry=8h, liq=24h
                    sym_check = str(row.get("token_symbol", "") or "").upper()
                    _cd_sym = recent_tokens.get(sym_check)
                    if _cd_sym:
                        _cd_ts, _cd_action = _cd_sym
                        _cd_limit = _COOLDOWN_MAP.get(_cd_action, TOKEN_COOLDOWN_H)
                        if (now - _cd_ts).total_seconds() / 3600 < _cd_limit:
                            known.add(sid)
                            log.debug(f"[live/{effective_system}] {sym_check} cooldown {_cd_action} "
                                      f"({_cd_ts.strftime('%H:%M')}, <{_cd_limit}h) → skip")
                            continue
                    _cd_ta = recent_taddrs.get(tok_addr_check) if tok_addr_check else None
                    if _cd_ta:
                        _cd_ts, _cd_action = _cd_ta
                        _cd_limit = _COOLDOWN_MAP.get(_cd_action, TOKEN_COOLDOWN_H)
                        if (now - _cd_ts).total_seconds() / 3600 < _cd_limit:
                            known.add(sid)
                            log.debug(f"[live/{effective_system}] {tok_addr_check[:12]}… taddr cooldown "
                                      f"{_cd_action} ({_cd_ts.strftime('%H:%M')}, <{_cd_limit}h) → skip")
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
                        ts    = datetime.fromisoformat(ts_str)
                        age_h = (now - ts).total_seconds() / 3600
                        max_age = MAX_SIGNAL_AGE_H.get(effective_system, MAX_SIGNAL_AGE_H_DEFAULT)
                        if age_h > max_age:
                            continue
                    except:
                        continue
                    entry_price = float(row.get("price_entry_usd", row.get("price_usd", 0)) or 0)
                    if entry_price <= 0:
                        continue
                    pair_addr = str(row.get("pair_address", "") or "")
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
                        # Guardia volume: token senza liquidità attiva
                        _sig_vol = float(row.get("volume_1h_usd", 0) or 0)
                        if _sig_vol < 10_000:
                            log.info(f"[live/defi] {sid}: vol_h1=${_sig_vol:.0f}<10K → skip")
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
                                log.info(f"[live/defi] {sid}: prepump_score={_score:.2f}<0.55 → skip")
                                known.add(sid)
                                continue

                    token_address = str(row.get("token_address", "") or "")

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
                    _ENTRY_DROP_THRESH = {"pump_grad": 0.20, "pre_grad": 0.20, "defi": 0.08}
                    _entry_drop_max = _ENTRY_DROP_THRESH.get(effective_system, 0.08)
                    if chain == "solana" and token_address and entry_price > 0:
                        live_price = _fetch_price_jupiter(token_address, entry_price)
                        if live_price and live_price > 0:
                            drift = (live_price - entry_price) / entry_price
                            if drift < -_entry_drop_max:
                                log.info(
                                    f"[live/{effective_system}] {sid}: prezzo calato "
                                    f"{drift*100:.1f}% dal segnale (>{_entry_drop_max*100:.0f}%) → skip (stantio)"
                                )
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

                    _log_trade({
                        "ts": now.isoformat(), "signal_id": sid, "system": effective_system,
                        "token_symbol": str(row.get("token_symbol", "")), "chain": chain,
                        "pair_address": pair_addr,
                        "action": "entry", "price": f"{entry_price:.8g}",
                        "change_pct": "+0.00", "vol_h1": f"{entry_vol:.0f}",
                        "bsr": "1.000", "remaining": "1.00",
                        "pnl_eur": "+0.00", "exit_reason": "open", "note": "vol_na",
                    })
                    known.add(sid)
                    if pair_addr and pair_addr not in ("", "0"*len(pair_addr) if pair_addr else ""):
                        active_pairs.add(pair_addr)
                    new_count += 1
                    recent_tokens[sym_check] = (now, "entry")
                    if tok_addr_check:
                        recent_taddrs[tok_addr_check] = (now, "entry")
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

    # ── Logica di exit ──────────────────────────────────────────────────────

    def _process_position(self, sid: str, pos: dict):
        if pos["remaining"] <= 0 or not pos.get("pair_address"):
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
            # pre_grad / pump_grad senza prezzo: controlla il time_limit
            # (pool morta o bonding curve non risponde → exit_time_limit non scatterebbe mai)
            _time_limits = {"pump_grad": 45.0, "pre_grad": 20.0}
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
                except Exception:
                    pass
            return
        cur_price, cur_vol, cur_bsr, cur_liq = fetch[0], fetch[1], fetch[2], fetch[3]
        if cur_price <= 0:
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
            jup_price = _fetch_price_jupiter(pos["token_address"], pos["entry_price"])
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
            log.warning(f"[live] {sid}: prezzo anomalo ignorato "
                        f"cur={cur_price:.8g} ep={ep:.8g} chg={chg:+.1f}% — skip ciclo")
            return
        pos["current_pct"]   = chg
        pos["current_vol"]   = cur_vol
        pos["current_bsr"]   = cur_bsr
        pos["price_is_live"] = True
        pos["last_fetch"]    = datetime.now().isoformat()
        # Primo fetch su segnale stantio (entry_vol=0): calibra entry_vol sul volume reale attuale
        if pos.get("entry_vol", 0) == 0 and cur_vol > 0:
            pos["entry_vol"] = cur_vol
        if chg > pos["peak_pct"]: pos["peak_pct"] = chg
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
            pnl_new = _capital * remaining * max(min(chg if chg_val is None else chg_val, MAX_CAP), -100) / 100
            pnl    += pnl_new
            remaining = 0.0
            pos["remaining"] = 0.0; pos["pnl_eur"] = pnl; pos["exit_reason"] = exit_r
            _log_trade({
                "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                "action": action, "price": f"{cur_price:.8g}",
                "change_pct": f"{(chg if chg_val is None else chg_val):+.2f}",
                "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                "remaining": "0.00", "pnl_eur": f"{pnl:+.2f}",
                "exit_reason": exit_r, "note": note,
            })

        snap1_exit = cfg["adaptive_snap1_exit"]
        # 0a. Max hold time zombie: chiude solo posizioni che non hanno MAI ricevuto un prezzo
        #     oltre il limite del sistema. Lascia aperte quelle che avevano prezzi e li hanno persi
        #     (potrebbero recuperare o essere chiuse da altri trigger).
        if pos.get("last_fetch") is None:
            try:
                _pos_age_h = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600
                _max_hold_h = MAX_SIGNAL_AGE_H.get(pos.get("system", ""), MAX_SIGNAL_AGE_H_DEFAULT) * 3
                if _pos_age_h > _max_hold_h:
                    _exit("exit_max_age",
                          f"nessun prezzo da {_pos_age_h:.1f}h > {_max_hold_h:.0f}h max",
                          exit_r="exit_max_age")
                    pos["last_update"] = now
                    return
            except Exception:
                pass
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
            _log_trade({
                "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                "action": "tp1", "price": f"{cur_price:.8g}",
                "change_pct": f"{cfg['tp1_pct']:+.2f}",
                "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                "remaining": f"{remaining:.2f}", "pnl_eur": f"{pnl:+.2f}",
                "exit_reason": "open",
                "note": f"Δ={chg:.1f}% >= TP1={cfg['tp1_pct']}% | bsr_tp1={cur_bsr:.3f} vol_tp1={cur_vol:.0f}",
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
                _log_trade({
                    "ts": now.isoformat(), "signal_id": sid, "system": pos["system"],
                    "token_symbol": pos["token_symbol"], "chain": pos["chain"],
                    "action": "tp2", "price": f"{cur_price:.8g}",
                    "change_pct": f"{cfg['tp2_pct']:+.2f}",
                    "vol_h1": f"{cur_vol:.0f}", "bsr": f"{cur_bsr:.3f}",
                    "remaining": "0.00", "pnl_eur": f"{pnl:+.2f}",
                    "exit_reason": "tp1_tp2", "note": f"Δ={chg:.1f}% >= TP2={cfg['tp2_pct']}%",
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
                    if pos["remaining"] > 0 and pos.get("pair_address"):
                        try: self._process_position(sid, pos)
                        except Exception as e: log.debug(f"[live] {sid}: {e}")
                try: self._generate_html()
                except Exception as e: log.warning(f"[live] HTML: {e}")
                try: self._load_new_signals()
                except Exception as e: log.debug(f"[live] nuovi segnali: {e}")
                try: self._save_state()
                except Exception as e: log.warning(f"[live] salvataggio stato: {e}")
            except Exception as e:
                log.error(f"[live] loop error: {e}")
            self._stop.wait(REFRESH_SEC)

    def stop(self):
        self._save_state()
        self._stop.set()

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

        html = _build_live_html(open_list, closed_list, all_states, entry_prices)
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

    sys_colors = {"defi":"#1f6feb","v3":"#e3b341","v2":"#8b949e","v3_large":"#a371f7","v3_midcap":"#8b949e","pump_grad":"#f0883e","pre_grad":"#58a6ff","mirror":"#bc8cff"}

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
        pump_table_rows += (
            f'<tr>'
            f'<td><a href="{r["dex_link"]}" target="_blank" style="color:#58a6ff;font-weight:600">{r["sym"]}</a></td>'
            f'<td style="color:#484f58;font-size:.8rem">{date_str}</td>'
            f'<td style="color:{dur_color}">{dur_str}</td>'
            f'<td style="color:#f0883e;font-size:.8rem">{action_short}</td>'
            f'<td style="color:{chg_color}">{r["exit_chg"]:+.1f}%</td>'
            f'<td style="color:{pnl_color};font-weight:600">{r["pnl"]:+.2f}€</td>'
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
        for s in ("defi", "v3", "v3_midcap", "v3_large", "pump_grad", "pre_grad")
        if s in CONFIGS
    )
    bsr_thresh_note = _bsr_parts or "0.50"
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
  — Impostazioni correnti: grace <strong>{ENTRY_GRACE_MIN:.0f} min</strong> · BSR conferme <strong>5 (DEFI) / 4 (altri)</strong> · vol_crash guard BSR≤0.65<br>
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
        if v <= 0: return "—"
        if v < 0.0001:  return f"{v:.3e}"
        if v < 0.01:    return f"{v:.6f}"
        if v < 1:       return f"{v:.4f}"
        if v < 10000:   return f"{v:,.2f}"
        return f"{v:,.0f}"
    except: return "—"


def _build_live_html(open_list, closed_list, all_states, entry_prices: dict = None):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Stats per sistema ───────────────────────────────────────────────────
    sys_stats = {}
    for sys_name in ("defi", "v3", "v3_large", "pump_grad", "pre_grad", "mirror"):
        open_n   = sum(1 for _, s in open_list if s.get("system") == sys_name)
        closed_s = [(sid, s) for sid, s in all_states.items()
                    if s.get("system") == sys_name and float(s.get("remaining", 0) or 0) <= 0]
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
    total_closed = sum(s["closed"] for s in sys_stats.values())
    total_pnl    = sum(float(s.get("pnl_eur","0").replace("+","") or 0)
                       for _, s in all_states.items()
                       if float(s.get("remaining", 0) or 0) <= 0)
    # WR reale (esclude vol_na)
    real_closed_all = [(sid, s) for sid, s in all_states.items()
                       if float(s.get("remaining", 0) or 0) <= 0 and s.get("note","") != "vol_na"]
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
            f'<div class="sys-lbl">{name.upper()}</div>'
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

    # ── Righe tabella aperte ─────────────────────────────────────────────────
    def open_row(sid, s):
        sys_name       = s.get("system", "?")
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

        dex_link = f"https://dexscreener.com/{chain}/{pair}" if pair else "#"
        sys_colors = {"defi": "#1f6feb", "v3": "#e3b341", "v3_large": "#a371f7", "pump_grad": "#f0883e", "pre_grad": "#58a6ff", "mirror": "#bc8cff"}
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
            entry_time = f"{_t[:2]}:{_t[2:4]}"
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
        return (
            f'<tr class="orow"{opacity} {data_attrs}>'
            f'<td><a href="{dex_link}" target="_blank" style="color:#58a6ff;font-weight:600">{sym}</a>'
            f'<span class="dup-badge" data-sym="{sym_lower}"></span>'
            f'<br><span class="tag" style="background:{sc}22;color:{sc}">{sys_name.upper()}</span>'
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
        sys_name = s.get("system","?")
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
        sys_colors = {"defi": "#1f6feb", "v3": "#e3b341", "v3_large": "#a371f7", "pump_grad": "#f0883e", "pre_grad": "#58a6ff", "mirror": "#bc8cff"}
        sc = sys_colors.get(sys_name, "#8b949e")
        # Estrai data e ora di apertura da signal_id (SYM_YYYYMMDD_HHMMSS)
        sig_date = ""; entry_time = ""
        try:
            _parts = sid.rsplit("_", 2)
            _d = _parts[-2]; _t = _parts[-1]
            sig_date   = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]}"
            entry_time = f"{_t[:2]}:{_t[2:4]}"
        except: pass
        # Escludi righe con exit_reason da non mostrare
        EXCL = {"purged_stale","archiviato_pre_2026-05-20","duplicate_pair",
                "no_pair_address","expired_max_age"}
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
        return (
            f'<tr class="crow" style="opacity:.8" data-sigdate="{sig_date}" data-sys="{sys_name}" '
            f'data-chain="{chain_c}" data-exitr="{exit_r}" '
            f'data-pnl="{pnl:.2f}" data-pct="{chg:.2f}" data-win="{win_flag}" data-token="{sym_lower}">'
            f'<td style="font-weight:600">{sym} '
            f'<span class="dup-badge" data-sym="{sym_lower}"></span>'
            f'<span class="tag" style="background:{sc}22;color:{sc}">{sys_name.upper()}</span>'
            f'{entry_tag}</td>'
            f'<td style="color:#8b949e;font-size:.8rem">{age}</td>'
            f'<td style="color:{pnl_c};font-weight:600">{pnl:+.2f}€</td>'
            f'<td style="color:#8b949e;font-size:.8rem">{reason}</td>'
            f'<td style="color:{chg_c};font-size:.8rem">{chg:+.1f}%</td>'
            f'<td style="font-size:.72rem;color:#8b949e;font-family:monospace">{ep_str}</td>'
            f'<td style="font-size:.72rem;color:#8b949e;font-family:monospace">{ex_str}</td>'
            f'<td style="font-size:.75rem;color:#484f58">{note[:40]}</td>'
            f'</tr>'
        )

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
        f'{sys_card("v3","#e3b341")}'
        f'{sys_card("v3_large","#a371f7")}'
        f'{sys_card("pump_grad","#f0883e")}'
        f'{sys_card("pre_grad","#58a6ff")}'
        f'{sys_card("mirror","#bc8cff")}'
        f'</div>\n'
        f'<div class="filters">\n'
        f'  <span class="lbl2">Dal:</span>\n'
        f'  <div class="btn-grp">\n'
        f'    <button id="btn24h" class="preset-btn" data-v="24h" onclick="setPreset(this,dateNDaysAgo(1),dateToday())">Ultime 24h</button>\n'
        f'    <button class="preset-btn" data-v="7d" onclick="setPreset(this,dateNDaysAgo(7),\'\')">7 giorni</button>\n'
        f'    <button class="preset-btn" data-v="all" onclick="setPreset(this,\'\',\'\')">Tutto</button>\n'
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
        f'    <button class="sys-btn" onclick="setSys(this,\'v3\')">V3</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'v3_large\')">V3L</button>\n'
        f'    <button class="sys-btn" onclick="setSys(this,\'v3_midcap\')">V3M</button>\n'
        f'    <button class="sys-btn pump-btn" onclick="setSys(this,\'pump_grad\')">🚀 PUMP</button>\n'
        f'    <button class="sys-btn pre-btn" onclick="setSys(this,\'pre_grad\')">⚡ PRE</button>\n'
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
        f'<div class="section-title">Trade chiusi (<span id="vis_c">—</span> visibili su {len(closed_list)})</div>\n'
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
        f'var _sys="", _chain="", _outcome="", _exitr="", _tokSearch="", _sortCol="", _sortDir=-1;\n'
        f'function dateNDaysAgo(n){{\n'
        f'  var d=new Date(); d.setDate(d.getDate()-n);\n'
        f'  return d.toISOString().slice(0,10);\n'
        f'}}\n'
        f'function dateToday(){{\n'
        f'  return new Date().toISOString().slice(0,10);\n'
        f'}}\n'
        f'function setPreset(btn,df,dt){{\n'
        f'  document.getElementById("df").value=df;\n'
        f'  document.getElementById("dt").value=dt;\n'
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
        f'  var sysStat={{defi:{{o:0,c:0,wins:0,pnl:0}},v3:{{o:0,c:0,wins:0,pnl:0}},v3_large:{{o:0,c:0,wins:0,pnl:0}},bnf:{{o:0,c:0,wins:0,pnl:0}},pump_grad:{{o:0,c:0,wins:0,pnl:0}},pre_grad:{{o:0,c:0,wins:0,pnl:0}}}};\n'
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
        f'    var sd=r.dataset.sigdate||"";\n'
        f'    var win=parseInt(r.dataset.win||0);\n'
        f'    var tok=(r.dataset.token||"");\n'
        f'    var ok_df  = !df         || sd>=df;\n'
        f'    var ok_dt  = !dt         || sd<=dt;\n'
        f'    var ok_sys = !_sys       || r.dataset.sys===_sys;\n'
        f'    var ok_ch  = !_chain     || r.dataset.chain===_chain;\n'
        f'    var ok_out = !_outcome   || (_outcome==="win"&&win>0) || (_outcome==="loss"&&win===0);\n'
        f'    var ok_er  = !_exitr     || r.dataset.exitr===_exitr;\n'
        f'    var ok_tok = !_tokSearch || tok.indexOf(_tokSearch)>=0;\n'
        f'    var show   = ok_df && ok_dt && ok_sys && ok_ch && ok_out && ok_er && ok_tok;\n'
        f'    r.style.display=show?"":"none";\n'
        f'    if(show){{\n'
        f'      vc++;\n'
        f'      var p=parseFloat(r.dataset.pnl||0); tot_pnl+=p;\n'
        f'      if(win>0) wins++; else losses++;\n'
        f'      var sys=r.dataset.sys||""; if(sysStat[sys]){{\n'
        f'        sysStat[sys].c++; sysStat[sys].pnl+=p;\n'
        f'        if(win>0) sysStat[sys].wins++;\n'
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
        f'  ["defi","v3","v3_large","bnf","pump_grad","pre_grad","mirror"].forEach(function(sys){{\n'
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
        f'  if(b) setPreset(b,dateNDaysAgo(1),dateToday());\n'
        f'  else applyFilter();\n'
        f'}});\n'
        '</script>\n'
        '</div>\n</body></html>'
    )
    return html

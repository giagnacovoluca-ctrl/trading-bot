"""
run_demo.py — Bot BTC Aggressivo su Bitget Demo (SUSDT-FUTURES)
===============================================================
Parametri aggressivi su conto demo $1000:
  - Rischio 15%/trade ($150) → 3× più del live
  - ADX minimo 20 (più entries)
  - Trail attivazione 8% (più aggressivo)
  - ATR minimo 50 (più entries in range leggero)
  - Cooldown 30 barre dopo circuit breaker (da 60)
  - Capital iniziale $1000

Avvio:
    cd trade && python run_demo.py

Prerequisiti:
    1. Inserire BITGET_DEMO_API_KEY / SECRET / PASSPHRASE in trade/.env
       (da Bitget → Demo Trading → Gestione API)
    2. Il conto demo parte con fondi virtuali (richiedi da Bitget UI)
"""

import os
import sys
import logging
from pathlib import Path

# ── Override parametri PRIMA di importare structural_bot ──────────────────────
os.environ.setdefault("EXECUTOR", "bitget")
os.environ["BITGET_DEMO"]           = "true"
os.environ["BITGET_LEVERAGE"]       = "20"    # 20x — aggressivo ma gestibile
os.environ["BITGET_TRADE_SIZE_USD"] = "90"    # 3% di $3000
os.environ["BITGET_DRY_RUN"]        = "false"
os.environ["BITGET_TRADING_HOURS"]  = "0-24"

# ── Logging ───────────────────────────────────────────────────────────────────
(Path(__file__).parent / "reports_demo").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "reports_demo" / "demo.log",
                            mode="a", encoding="utf-8"),
    ]
)

# ── Path ──────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

# ── Import e patch parametri ──────────────────────────────────────────────────
import structural_bot as bot
from pathlib import Path as _Path

# Parametri aggressivi demo (override dei modulo-level constants)
bot.RISK_PCT               = 0.03   # 3% per trade → $90 rischio → ~0.27 BTC → $890 margine a 20x
bot.INITIAL_CAPITAL        = 1.0    # override da saldo reale prima di run()
bot.ADX_MIN_TREND          = 15     # live=24 → 15: trada anche trend deboli
bot.ATR_MIN_ENTRY          = 15.0   # live=65 → 15: entra anche in range compressi
bot.MAX_CONSECUTIVE_LOSSES = 5      # più tolleranza prima del circuit breaker
bot.COOLDOWN_BARS          = 10     # 50 min recovery (live = 5h)
bot.MIN_RR                 = 2.0    # invariato
bot.TRAIL_ACTIVATE_PCT     = 0.30   # era 0.65 → arma trail al 30% verso TP (scalping)
bot.TRAIL_ATR_DIST         = 0.7    # era 1.0 → trail più stretto (scalping)

# config defi aggressiva
_defi_cfg = {
    "trail_activate_pct": 8.0,   # live = 12%
    "trail_drop_pct":     8.0,
    "hard_sl_pct":       -8.0,
}

# Cartelle e file separati → non interferisce con il bot live
_DEMO_DIR = _HERE / "reports_demo"
_DEMO_DIR.mkdir(exist_ok=True)
bot.REPORTS_DIR      = _DEMO_DIR
bot.STATE_FILE       = _DEMO_DIR / "demo_state.json"
bot.TRADES_LOG_FILE  = _DEMO_DIR / "demo_trades_log.json"
bot.DASHBOARD_DATA   = _DEMO_DIR / "demo_trades_data.js"

# ── Avvio ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log = logging.getLogger("run_demo")
    log.info("=" * 60)
    log.info("  BOT DEMO — Bitget Simulated Trading (SUSDT-FUTURES)")
    log.info(f"  Capital:   ${bot.INITIAL_CAPITAL:,.0f}")
    log.info(f"  Risk/trade:{bot.RISK_PCT*100:.0f}% = ${bot.INITIAL_CAPITAL*bot.RISK_PCT:.0f}")
    log.info(f"  ADX min:   {bot.ADX_MIN_TREND}")
    log.info(f"  Reports:   {_DEMO_DIR}")
    log.info("=" * 60)

    from bitget_futures_executor import BitgetFuturesExecutor
    executor = BitgetFuturesExecutor()

    # Scrivi lo state file con il saldo reale del demo PRIMA di chiamare run()
    # run() chiamerà load_state() e troverà il capitale corretto
    real_bal = executor._get_balance() if hasattr(executor, '_get_balance') else 0.0
    start_capital = real_bal if real_bal > 10.0 else 3000.0  # fallback $3000 se fetch fallisce
    log.info(f"[demo] Capital iniziale: ${start_capital:.2f} USDT (dal saldo demo)")
    fresh_state = bot.BotState()
    fresh_state.capital = start_capital
    bot.save_state(fresh_state)

    bot.run(executor=executor)

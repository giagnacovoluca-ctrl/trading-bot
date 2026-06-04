"""
run.py – Avvia tutti i componenti defi in un unico processo.

Componenti avviati come thread daemon:
  1. LiveEngine (trade_simulator)   — gestione posizioni, prezzi, HTML
  2. PumpGraduationScanner          — graduation events pump.fun → segnali
  3. defi_optimized.main()          — gem hunter defi (Solana/ETH)
  4. gemmeV3.main_loop()            — gem hunter V3 multi-chain
  5. solana_executor.main()         — executor acquisti/vendite su Solana

Non incluso (processo separato):
  • structural_bot.py               — BTC trading bot su Bitget (diverso dominio)

Uso:
    python run.py              # avvia tutto
    python run.py --report-only  # rigenera solo HTML e poi esci
    python run.py --no-defi      # salta defi_optimized
    python run.py --no-v3        # salta gemmeV3
    python run.py --no-executor  # salta solana_executor

Ctrl+C:
    • Primo  → pausa pulita (chiude posizioni aperte) + stop tutti i thread
    • Secondo (entro 5s) → stop immediato senza chiudere posizioni
"""
import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE   = Path(__file__).parent                   # defi/
_ROOT   = _HERE.parent                            # GIT/
_GEMME  = _ROOT / "gemme"
_EXEC   = _ROOT / "executor"

for _p in [str(_HERE), str(_ROOT), str(_GEMME), str(_EXEC)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Carica executor/.env per leggere EXECUTOR_CHAINS e altre variabili
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=_EXEC / ".env", override=False)
except ImportError:
    pass

# ── Logging centralizzato (PRIMA di ogni import) ──────────────────────────────
# Tutti i componenti (gemmeV3, defi_optimized, executor, pump scanner)
# condividono il root logger → unico file + console.
_LOG_DIR  = _HERE / "reports"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "run.log"

_fmt     = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
_console = logging.StreamHandler()
_console.setFormatter(_fmt)

# RotatingFileHandler: max 10 MB per file, mantieni ultimi 5 file (~50 MB totali)
_fhandler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_fhandler.setFormatter(_fmt)

# Configura il root logger con force=True (sovrascrive eventuali basicConfig successivi)
logging.basicConfig(level=logging.INFO, handlers=[_console, _fhandler], force=True)
log = logging.getLogger("run")

import trade_simulator as ts

# ── Parametri ─────────────────────────────────────────────────────────────────
NO_CONN_TIMEOUT_H = 1.0

# ── Argomenti CLI ─────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Bot defi unificato")
parser.add_argument("--report-only",  action="store_true")
parser.add_argument("--no-defi",      action="store_true", help="Salta defi_optimized")
parser.add_argument("--no-v3",        action="store_true", help="Salta gemmeV3")
parser.add_argument("--no-executor",  action="store_true", help="Salta tutti gli executor")
parser.add_argument("--no-solana",    action="store_true", help="Salta solana_executor")
parser.add_argument("--no-base",      action="store_true", help="Salta base_executor")
parser.add_argument("--no-pump",      action="store_true", help="Salta pump graduation scanner")
parser.add_argument("--no-midcap",    action="store_true", help="Salta midcap scanner")
args = parser.parse_args()

# ── Helper: thread con auto-restart ──────────────────────────────────────────
_stop_event = threading.Event()


def _start_component(name: str, target_fn, restart_delay: int = 30):
    """
    Avvia `target_fn` in un thread daemon.
    Se crasha, riprova dopo `restart_delay` secondi (finché _stop_event non è set).
    """
    def _wrapper():
        while not _stop_event.is_set():
            try:
                log.info(f"[run] ▶ {name} avviato")
                target_fn()
            except SystemExit:
                break
            except Exception as e:
                log.error(f"[run] ✗ {name} crashato: {e}", exc_info=False)
            if not _stop_event.is_set():
                log.info(f"[run] {name} riavvio tra {restart_delay}s...")
                _stop_event.wait(restart_delay)
        log.info(f"[run] ■ {name} fermato")

    t = threading.Thread(target=_wrapper, name=name, daemon=True)
    t.start()
    return t


# ── Report-only ───────────────────────────────────────────────────────────────
if args.report_only:
    engine = ts.LiveEngine.__new__(ts.LiveEngine)
    engine.positions = {}
    engine._lock     = threading.Lock()
    engine._stop     = threading.Event()
    engine._load_state()
    engine._generate_html()
    log.info("[run] Report generato.")
    raise SystemExit(0)

# ── 1. LiveEngine ─────────────────────────────────────────────────────────────
engine = ts.LiveEngine()

# ── 2. Pump graduation scanner ────────────────────────────────────────────────
if not args.no_pump:
    try:
        from pump_graduation_scanner import PumpGraduationScanner
        _pump = PumpGraduationScanner()
        _pump.start()
        log.info("[run] ▶ Pump graduation scanner avviato")
    except Exception as e:
        log.warning(f"[run] Pump scanner non avviato: {e}")

# ── 2b. Pre-graduation monitor (intercetta PRIMA del pump) ────────────────────
if not args.no_pump:
    try:
        from pre_grad_monitor import PreGradMonitor
        _pre_grad = PreGradMonitor()
        _pre_grad.start()
        log.info("[run] ▶ Pre-graduation monitor avviato")
    except Exception as e:
        log.warning(f"[run] Pre-grad monitor non avviato: {e}")

# ── 2c. Base pump scanner (nuove pool Uniswap V3 + Aerodrome su Base) ─────────
if not args.no_pump:
    try:
        from base_pump_scanner import BasePumpScanner
        _base_pump = BasePumpScanner()
        _base_pump.start()
        log.info("[run] ▶ Base pump scanner avviato")
    except Exception as e:
        log.warning(f"[run] Base pump scanner non avviato: {e}")

# ── 3. defi_optimized ────────────────────────────────────────────────────────
if not args.no_defi:
    try:
        import defi_optimized as _defi_mod
        _start_component("defi_optimized", _defi_mod.main, restart_delay=60)
    except Exception as e:
        log.warning(f"[run] defi_optimized non avviato: {e}")

# ── 4. gemmeV3 ────────────────────────────────────────────────────────────────
if not args.no_v3:
    try:
        import gemmeV3 as _v3_mod
        _start_component("gemmeV3", _v3_mod.main_loop, restart_delay=60)
    except Exception as e:
        log.warning(f"[run] gemmeV3 non avviato: {e}")

# ── 5. solana_executor ────────────────────────────────────────────────────────
_exec_chains = {c.strip().lower() for c in os.environ.get("EXECUTOR_CHAINS", "solana,base").split(",")}
if not args.no_executor and not args.no_solana and "solana" in _exec_chains:
    try:
        import solana_executor as _exec_mod
        _start_component("solana_executor",
                         lambda: _exec_mod.main(stop_event=_stop_event),
                         restart_delay=30)
    except Exception as e:
        log.warning(f"[run] solana_executor non avviato: {e}")
else:
    log.info(f"[run] solana_executor non attivo (EXECUTOR_CHAINS={os.environ.get('EXECUTOR_CHAINS','solana,base')})")

# ── 6. midcap scanner ────────────────────────────────────────────────────────
if not args.no_midcap:
    try:
        import midcap_scanner as _midcap_mod
        _start_component("midcap_scanner",
                         lambda: _midcap_mod.main(stop_event=_stop_event),
                         restart_delay=300)
        log.info("[run] ▶ Midcap scanner avviato (BB Squeeze + reversal)")
    except Exception as e:
        log.warning(f"[run] midcap_scanner non avviato: {e}")

# ── 7. base_executor ──────────────────────────────────────────────────────────
if not args.no_executor and not args.no_base and "base" in _exec_chains:
    try:
        import base_executor as _base_exec_mod
        _start_component("base_executor",
                         lambda: _base_exec_mod.main(stop_event=_stop_event),
                         restart_delay=30)
        log.info("[run] ▶ Base executor avviato (oracle on-chain + swap)")
    except Exception as e:
        log.warning(f"[run] base_executor non avviato: {e}")

# ── Ctrl+C ────────────────────────────────────────────────────────────────────
_last_ctrl_c   = 0.0
_abort_pause   = [False]


def _on_sigint(sig, frame):
    global _last_ctrl_c
    now = time.time()
    if now - _last_ctrl_c < 6.0:
        log.info("[run] Stop immediato — posizioni lasciate aperte.")
        _abort_pause[0] = True
        _stop_event.set()
        engine.stop()
        raise SystemExit(0)
    _last_ctrl_c = now
    log.info("[run] ⏸  Ctrl+C — Ctrl+C ancora entro 5s per stop immediato.")
    log.info("[run]    Altrimenti chiusura pulita tra 5 secondi...")

    def _countdown():
        for i in range(5, 0, -1):
            if _abort_pause[0]:
                return
            log.info(f"[run]    Chiusura in {i}s...")
            time.sleep(1)
        if _abort_pause[0]:
            return
        log.info("[run] Chiusura pulita in corso...")
        _stop_event.set()
        engine.pause_all_positions()
        engine.stop()

    threading.Thread(target=_countdown, daemon=True).start()


signal.signal(signal.SIGINT, _on_sigint)

# ── Loop principale LiveEngine ────────────────────────────────────────────────
no_conn_since = 0.0
log.info(f"[run] ✅ Tutti i componenti avviati. Auto-pausa dopo {NO_CONN_TIMEOUT_H:.0f}h senza connessione.")

while not engine._stop.is_set():
    with engine._lock:
        open_count = sum(1 for p in engine.positions.values() if p.get("remaining", 0) > 0)
        for p in engine.positions.values():
            p["price_is_live"] = False

    try:
        with engine._lock:
            pos_list = list(engine.positions.items())
        for sid, pos in pos_list:
            if pos["remaining"] > 0 and pos.get("pair_address"):
                try:
                    engine._process_position(sid, pos)
                except Exception as e:
                    ts.log.warning(f"[live] {sid}: EXCEPTION {e}")

        with engine._lock:
            fetched = sum(1 for p in engine.positions.values()
                          if p.get("remaining", 0) > 0 and p.get("price_is_live", False))

        if open_count > 0:
            if fetched == 0:
                now_t = time.time()
                if no_conn_since == 0.0:
                    no_conn_since = now_t
                elapsed_h = (now_t - no_conn_since) / 3600.0
                ts.log.warning(f"[live] ⚠ Nessun prezzo ricevuto ({elapsed_h:.2f}h/{NO_CONN_TIMEOUT_H:.0f}h)")
                if elapsed_h >= NO_CONN_TIMEOUT_H:
                    ts.log.warning(f"[live] 🔌 Connessione assente da {elapsed_h:.1f}h — pausa automatica.")
                    _stop_event.set()
                    engine.pause_all_positions()
                    engine.stop()
                    break
            else:
                if no_conn_since != 0.0:
                    ts.log.info("[live] ✅ Connessione ripristinata.")
                no_conn_since = 0.0

        try:    engine._generate_html()
        except Exception as e: ts.log.warning(f"[live] HTML: {e}")
        try:    engine._load_new_signals()
        except Exception as e: ts.log.debug(f"[live] segnali: {e}")
        try:    engine._save_state()
        except Exception as e: ts.log.warning(f"[live] stato: {e}")

    except Exception as e:
        ts.log.error(f"[live] loop error: {e}")

    engine._stop.wait(ts.REFRESH_SEC)

log.info("[run] Bot fermato.")

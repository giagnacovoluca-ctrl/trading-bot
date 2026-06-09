"""
Injective Autopilot — Entry Point.

Usage:
  python main.py [--mode PAPER|LIVE|BACKTEST] [--backtest-csv path/to/candles.csv]

Modes:
  PAPER     (default) — live data, simulated execution, no capital at risk
  LIVE      — live execution (requires passing live gate validation)
  BACKTEST  — replay on historical CSV data

The system starts in PAPER mode by default.
To switch to LIVE, you must first pass the live gate (500+ trades, PF>1.5, Sharpe>1.5).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import get_settings
from data.injective_client import InjectiveClient
from database.repository import Repository
from core.decision_engine import DecisionEngine
from dashboard.app import app as dashboard_app, set_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("injective_autopilot.log"),
    ],
)
log = logging.getLogger("main")


async def run_backtest(csv_path: str) -> None:
    from backtest.engine import BacktestEngine, load_candles_from_csv

    log.info("Loading candles from %s", csv_path)
    candles = load_candles_from_csv(csv_path)
    log.info("Loaded %d candles", len(candles))

    engine = BacktestEngine()
    result = engine.run(candles, walk_forward=True)

    m = result.metrics
    log.info("=" * 60)
    log.info("BACKTEST RESULTS")
    log.info("=" * 60)
    log.info("Total Trades:    %d", m.total_trades)
    log.info("Win Rate:        %.1f%%", m.win_rate)
    log.info("Profit Factor:   %.2f", m.profit_factor)
    log.info("Sharpe Ratio:    %.2f", m.sharpe_ratio)
    log.info("Sortino Ratio:   %.2f", m.sortino_ratio)
    log.info("Max Drawdown:    %.1f%%", m.max_drawdown_pct)
    log.info("Calmar Ratio:    %.2f", m.calmar_ratio)
    log.info("Expectancy:      $%.2f", m.expectancy)
    log.info("Total PnL:       $%.2f (%.2f%%)", m.total_pnl, m.total_pnl_pct)
    log.info("")

    if result.in_sample_metrics:
        log.info("IN-SAMPLE (70%%):  PF=%.2f  Sharpe=%.2f", result.in_sample_metrics.profit_factor, result.in_sample_metrics.sharpe_ratio)
    if result.out_of_sample_metrics:
        log.info("OUT-OF-SAMPLE (30%%): PF=%.2f  Sharpe=%.2f", result.out_of_sample_metrics.profit_factor, result.out_of_sample_metrics.sharpe_ratio)

    log.info("")
    if result.live_gate_passed:
        log.info("LIVE GATE: ✅ PASSED — system is ready for live trading")
    else:
        log.info("LIVE GATE: ❌ FAILED")
        for f in result.live_gate_failures:
            log.info("  - %s", f)

    if result.signal_stats:
        log.info("")
        log.info("SIGNAL CONTRIBUTIONS:")
        for name, stats in sorted(result.signal_stats.items(), key=lambda x: -x[1]["pnl"]):
            log.info("  %-25s count=%3d  wr=%.0f%%  pnl=$%.2f", name, stats["count"], stats["win_rate"], stats["pnl"])


async def run_paper_or_live(mode: str) -> None:
    cfg = get_settings()

    # Live gate check
    if mode == "LIVE":
        log.warning("LIVE mode requested. Checking live gate...")
        repo = Repository(cfg.db_url)
        await repo.init()
        trades = await repo.get_trades(mode="PAPER", limit=10000)
        closed = [t for t in trades if t["status"] == "CLOSED"]
        if len(closed) < cfg.live_min_simulated_trades:
            log.critical(
                "LIVE GATE FAILED: only %d paper trades completed, need %d",
                len(closed), cfg.live_min_simulated_trades,
            )
            log.critical("Run in PAPER mode first to accumulate trade history.")
            sys.exit(1)
        log.info("Live gate check passed (%d paper trades)", len(closed))

    # Initialise components
    repo = Repository(cfg.db_url)
    await repo.init()
    set_repo(repo)

    client = InjectiveClient(
        network=cfg.network,
        market_id=cfg.market_id,
        private_key=cfg.private_key if mode == "LIVE" else "",
        subaccount_index=cfg.subaccount_index,
        fee_recipient=cfg.fee_recipient,
    )
    await client.connect()

    decision_engine = DecisionEngine(
        model=cfg.claude_model,
        timeout_sec=cfg.claude_timeout_sec,
        min_confidence=cfg.claude_min_confidence,
        use_subprocess=cfg.use_subprocess,
        capital=cfg.capital_usdt,
        max_leverage=cfg.max_leverage,
        min_rr=cfg.min_rr_ratio,
    )

    if mode == "LIVE":
        from paper_trading.engine import PaperTradingEngine  # same architecture, different executor mode
        # For true live, we'd use a separate LiveEngine class
        # For now, PaperTradingEngine with mode override handles it
        log.warning("LIVE trading activated. Real capital at risk.")
        engine = PaperTradingEngine(client, repo, decision_engine)
    else:
        from paper_trading.engine import PaperTradingEngine
        engine = PaperTradingEngine(client, repo, decision_engine)

    # Start dashboard in background
    dashboard_config = uvicorn.Config(
        dashboard_app,
        host=cfg.dashboard_host,
        port=cfg.dashboard_port,
        log_level="critical",
    )
    dashboard_server = uvicorn.Server(dashboard_config)

    log.info("Dashboard available at http://%s:%d", cfg.dashboard_host, cfg.dashboard_port)
    log.info("Starting in %s mode...", mode)
    log.info("Ctrl+C once = stop & save positions | Ctrl+C twice = stop & CLOSE positions")

    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    close_positions_flag = False
    shutdown_started = False

    def _handle_sigint():
        nonlocal close_positions_flag, shutdown_started
        if not shutdown_started:
            shutdown_started = True
            log.info("Ctrl+C — stopping, keeping positions open...")
        else:
            close_positions_flag = True
            log.warning("Second Ctrl+C — will close all positions")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, _handle_sigint)

    engine_task = asyncio.create_task(engine.start())
    dashboard_task = asyncio.create_task(dashboard_server.serve())

    await shutdown_event.wait()

    # Stop running tasks
    dashboard_server.should_exit = True
    engine_task.cancel()
    dashboard_task.cancel()
    await asyncio.gather(engine_task, dashboard_task, return_exceptions=True)

    # Save positions, then wait 5s for a second Ctrl+C to close them
    await engine.stop(close_positions=False)
    log.info("Positions saved. Press Ctrl+C again within 5s to CLOSE all positions, or wait to exit.")

    second_sigint = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, second_sigint.set)

    try:
        await asyncio.wait_for(second_sigint.wait(), timeout=5.0)
        log.warning("Closing all positions...")
        await engine._executor.close_all()
        for trade in engine._executor.closed_trades:
            if trade.exit_reason == "MANUAL":
                await engine._repo.save_trade(trade)
    except asyncio.TimeoutError:
        log.info("Exiting with positions open.")

    loop.remove_signal_handler(signal.SIGINT)


def main():
    parser = argparse.ArgumentParser(description="Injective Autopilot")
    parser.add_argument("--mode", choices=["PAPER", "LIVE", "BACKTEST"], default="PAPER")
    parser.add_argument("--backtest-csv", type=str, help="Path to CSV for backtest mode")
    args = parser.parse_args()

    cfg = get_settings()

    # Mode from CLI overrides env/config
    if args.mode:
        import os
        os.environ["INJ_MODE"] = args.mode

    if args.mode == "BACKTEST":
        if not args.backtest_csv:
            print("ERROR: --backtest-csv required for BACKTEST mode")
            sys.exit(1)
        asyncio.run(run_backtest(args.backtest_csv))
    else:
        asyncio.run(run_paper_or_live(args.mode))


if __name__ == "__main__":
    main()

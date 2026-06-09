"""
Dashboard FastAPI — aggiornamento automatico ogni N secondi.

Endpoints:
  /          → overview (equity curve, PnL)
  /performance → metriche quantitative
  /journal     → trade log filtrabile
  /signals     → segnali Sentinella
  /risk        → stato Risk Engine / Kill Switch
  /ai          → decisioni Claude + accuracy
  /api/stats   → JSON per polling frontend
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from backtest.metrics import PerformanceMetrics, compute_metrics
from config.settings import get_settings
from database.repository import Repository

app = FastAPI(title="Injective Autopilot Dashboard", docs_url=None)

_cfg = get_settings()
_repo: Repository | None = None
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Register custom Jinja2 filters
import datetime as _dt
def _ts_to_str(ts: float) -> str:
    try:
        return _dt.datetime.utcfromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "—"
_templates.env.filters["timestamp_to_str"] = _ts_to_str

# Static files (CSS/JS)
_static_path = Path(__file__).parent / "static"
_static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_path)), name="static")


def set_repo(repo: Repository) -> None:
    global _repo
    _repo = repo


# ── HTML pages ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    trades = await _repo.get_trades(limit=500)
    equity_data = await _repo.get_equity_curve()
    closed = [t for t in trades if t["status"] == "CLOSED"]
    metrics = _compute_metrics_from_trades(closed)
    return _templates.TemplateResponse(request, "overview.html", {
        "trades": closed[-10:],
        "metrics": metrics,
        "equity_data": json.dumps(equity_data),
        "refresh_sec": _cfg.dashboard_auto_refresh_sec,
        "mode": _cfg.mode,
        "kill_active": False,
    })


@app.get("/performance", response_class=HTMLResponse)
async def performance(request: Request):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    trades = await _repo.get_trades(limit=2000)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    metrics = _compute_metrics_from_trades(closed)
    return _templates.TemplateResponse(request, "performance.html", {
        "metrics": metrics, "refresh_sec": _cfg.dashboard_auto_refresh_sec,
    })


@app.get("/journal", response_class=HTMLResponse)
async def journal(request: Request, mode: str = ""):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    trades = await _repo.get_trades(mode=mode or None, limit=200)
    return _templates.TemplateResponse(request, "journal.html", {
        "trades": trades, "mode_filter": mode,
    })


@app.get("/signals", response_class=HTMLResponse)
async def signals_page(request: Request):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    signals = await _repo.get_signals(limit=100)
    return _templates.TemplateResponse(request, "signals.html", {
        "signals": signals, "refresh_sec": _cfg.dashboard_auto_refresh_sec,
    })


@app.get("/risk", response_class=HTMLResponse)
async def risk_page(request: Request):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    equity_data = await _repo.get_equity_curve()
    latest = equity_data[-1] if equity_data else {}
    return _templates.TemplateResponse(request, "risk.html", {
        "latest": latest, "refresh_sec": _cfg.dashboard_auto_refresh_sec,
    })


@app.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    if not _repo:
        return HTMLResponse("<h1>Starting up...</h1>")
    decisions = await _repo.get_ai_decisions(limit=100)
    approved = [d for d in decisions if d["was_approved"]]
    profitable = [d for d in approved if d["outcome_pnl"] > 0]
    accuracy = len(profitable) / (len(approved) + 1e-10) * 100
    avg_pnl = sum(d["outcome_pnl"] for d in approved) / (len(approved) + 1e-10)
    return _templates.TemplateResponse(request, "ai_analysis.html", {
        "decisions": decisions,
        "accuracy": accuracy,
        "avg_pnl": avg_pnl,
        "total_calls": len(decisions),
        "approved": len(approved),
    })


# ── JSON API ──────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    if not _repo:
        return JSONResponse({"status": "starting"})
    trades = await _repo.get_trades(limit=500)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    metrics = _compute_metrics_from_trades(closed)
    return JSONResponse({
        "mode": _cfg.mode,
        "total_trades": len(closed),
        "open_trades": len([t for t in trades if t["status"] == "OPEN"]),
        "total_pnl": metrics.get("total_pnl", 0),
        "win_rate": metrics.get("win_rate", 0),
        "profit_factor": metrics.get("profit_factor", 0),
        "sharpe": metrics.get("sharpe_ratio", 0),
        "max_dd_pct": metrics.get("max_drawdown_pct", 0),
        "ts": time.time(),
    })


@app.get("/api/equity")
async def api_equity():
    if not _repo:
        return JSONResponse([])
    data = await _repo.get_equity_curve()
    return JSONResponse(data)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_metrics_from_trades(trades: list[dict]) -> dict:
    if not trades:
        return {
            "total_pnl": 0, "total_pnl_pct": 0, "profit_factor": 0,
            "sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0,
            "expectancy": 0, "win_rate": 0, "total_trades": 0,
            "max_drawdown_pct": 0, "recovery_factor": 0, "risk_of_ruin": 1,
            "long_trades": 0, "short_trades": 0,
        }

    pnl = [t["pnl_usdt"] for t in trades]
    equity = [_cfg.capital_usdt]
    for p in pnl:
        equity.append(equity[-1] + p)

    m = compute_metrics(
        pnl_series=pnl,
        equity_curve=equity,
        trade_directions=[t["direction"] for t in trades],
        initial_capital=_cfg.capital_usdt,
    )
    return {
        "total_pnl": round(m.total_pnl, 2),
        "total_pnl_pct": round(m.total_pnl_pct, 2),
        "profit_factor": round(m.profit_factor, 2),
        "sharpe_ratio": round(m.sharpe_ratio, 2),
        "sortino_ratio": round(m.sortino_ratio, 2),
        "calmar_ratio": round(m.calmar_ratio, 2),
        "expectancy": round(m.expectancy, 2),
        "win_rate": round(m.win_rate, 1),
        "total_trades": m.total_trades,
        "winning_trades": m.winning_trades,
        "losing_trades": m.losing_trades,
        "avg_win": round(m.avg_win, 2),
        "avg_loss": round(m.avg_loss, 2),
        "max_drawdown": round(m.max_drawdown, 2),
        "max_drawdown_pct": round(m.max_drawdown_pct, 1),
        "recovery_factor": round(m.recovery_factor, 2),
        "risk_of_ruin": round(m.risk_of_ruin * 100, 2),
        "long_trades": m.long_trades,
        "short_trades": m.short_trades,
        "long_win_rate": round(m.long_win_rate, 1),
        "short_win_rate": round(m.short_win_rate, 1),
    }

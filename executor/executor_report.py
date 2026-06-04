"""
executor_report.py
==================
Genera reports/executor_dashboard.html confrontando:
  - real_state.json      (executor: P&L reale USDC, trade eseguiti)
  - real_executions.csv  (executor: log operazioni)
  - reports/live_trades.csv (simulator: P&L simulato EUR)

Uso standalone:
    python executor_report.py

Uso da codice:
    from Produzione.executor_report import build_executor_report
    build_executor_report()
"""

from __future__ import annotations
import json, csv, os
from datetime import datetime
from pathlib import Path

# ── Percorsi ──────────────────────────────────────────────────────────────────
DEFI_ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE        = Path(__file__).parent
_DEFI        = Path(os.path.join(DEFI_ROOT, "defi"))
STATE_JSON      = _HERE / "real_state.json"
EXEC_CSV        = _HERE / "real_executions.csv"
BSC_STATE_JSON  = _HERE / "bsc_real_state.json"
BSC_EXEC_CSV    = _HERE / "bsc_executions.csv"
BASE_STATE_JSON = _HERE / "base_real_state.json"
BASE_EXEC_CSV   = _HERE / "base_executions.csv"
SIM_CSV         = _DEFI / "reports" / "live_trades.csv"
OUT_HTML        = _DEFI / "reports" / "executor_dashboard.html"

BSC_ENABLED  = False
BASE_ENABLED = True


# ── Lettura dati ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    merged = {}
    if STATE_JSON.exists():
        with open(STATE_JSON, encoding="utf-8") as f:
            for sid, v in json.load(f).items():
                merged[sid] = _normalize_state(v, "solana")
    if BSC_ENABLED and BSC_STATE_JSON.exists():
        with open(BSC_STATE_JSON, encoding="utf-8") as f:
            for sid, v in json.load(f).items():
                merged[sid] = _normalize_state(v, "bsc")
    if BASE_ENABLED and BASE_STATE_JSON.exists():
        with open(BASE_STATE_JSON, encoding="utf-8") as f:
            for sid, v in json.load(f).items():
                merged[sid] = _normalize_state(v, "base")
    return merged


def _load_exec_csv() -> list[dict]:
    paths = [EXEC_CSV]
    if BSC_ENABLED:
        paths.append(BSC_EXEC_CSV)
    if BASE_ENABLED:
        paths.append(BASE_EXEC_CSV)
    rows = []
    for path in paths:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def _normalize_state(raw: dict, chain_default: str = "solana") -> dict:
    """Normalizza BSC (usdt_*) e Base (eth_*) verso il formato Solana (usdc_*)."""
    v = dict(raw)
    # BSC: usdt_* → usdc_*
    if "usdt_spent" in v and "usdc_spent" not in v:
        v["usdc_spent"]    = v.pop("usdt_spent", 0)
        v["usdc_received"] = v.pop("usdt_received", 0)
        v["real_pnl_usdc"] = v.pop("real_pnl_usdt", 0)
    # Base: eth_* già convertiti in usdc_* da base_executor (al momento del trade)
    # Se mancano (stato creato prima dell'update), calcola con fallback price
    if "eth_spent" in v and "usdc_spent" not in v:
        _eth_price = 2000.0  # fallback se Chainlink non raggiungibile
        try:
            import sys as _s, os as _o
            _exec_dir = str(Path(__file__).parent)
            if _exec_dir not in _s.path:
                _s.path.insert(0, _exec_dir)
            from base_executor import _get_weth_usd
            _eth_price = _get_weth_usd()
        except Exception:
            pass
        v["usdc_spent"]    = v.get("eth_spent", 0) * _eth_price
        v["usdc_received"] = v.get("eth_received", 0) * _eth_price
        v["real_pnl_usdc"] = v["usdc_received"] - v["usdc_spent"]
    if "chain" not in v:
        v["chain"] = chain_default
    return v


def _load_sim_csv() -> dict[str, dict]:
    """Ritorna {signal_id: ultima riga chiusa} dal simulator."""
    if not SIM_CSV.exists():
        return {}
    by_id: dict[str, list] = {}
    with open(SIM_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            by_id.setdefault(r["signal_id"], []).append(r)
    result = {}
    for sid, rows in by_id.items():
        # Prendi entry e ultima riga (exit o stato corrente)
        entry = next((r for r in rows if r["action"] == "entry"), rows[0])
        closed = [r for r in rows if r.get("exit_reason", "open") not in ("open", "")]
        last   = closed[-1] if closed else rows[-1]
        def _sf(v):
            try: return float(v or 0)
            except: return 0.0
        result[sid] = {
            "entry_price":  _sf(entry.get("price", 0)),
            "entry_ts":     entry.get("ts", ""),
            "exit_reason":  last.get("exit_reason", "open"),
            "pnl_eur":      _sf(last.get("pnl_eur", 0)),
            "change_pct":   _sf(last.get("change_pct", 0)),
            "system":       entry.get("system", ""),
            "token_symbol": entry.get("token_symbol", ""),
            "chain":        entry.get("chain", ""),
            "is_closed":    bool(closed),
        }
    return result


# ── Calcoli aggregati ─────────────────────────────────────────────────────────

def _exec_stats(state: dict) -> dict:
    closed = [v for v in state.values() if v["status"] == "closed"]
    opened = [v for v in state.values() if v["status"] == "open"]
    total_pnl   = sum(v.get("real_pnl_usdc", 0) for v in closed)
    wins        = sum(1 for v in closed if v.get("real_pnl_usdc", 0) > 0)
    win_rate    = (wins / len(closed) * 100) if closed else 0
    total_spent = sum(v.get("usdc_spent", 0) for v in closed)
    return {
        "n_closed": len(closed), "n_open": len(opened),
        "total_pnl": total_pnl, "win_rate": win_rate,
        "total_spent": total_spent, "wins": wins,
        "avg_pnl": (total_pnl / len(closed)) if closed else 0,
    }


def _sim_stats(sim: dict) -> dict:
    closed = [v for v in sim.values() if v["is_closed"]]
    total_pnl = sum(v["pnl_eur"] for v in closed)
    wins      = sum(1 for v in closed if v["pnl_eur"] > 0)
    win_rate  = (wins / len(closed) * 100) if closed else 0
    return {
        "n_closed": len(closed), "total_pnl": total_pnl,
        "win_rate": win_rate, "wins": wins,
        "avg_pnl": (total_pnl / len(closed)) if closed else 0,
    }


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> str:
    if v > 0:   return "#3fb950"
    if v < 0:   return "#f85149"
    return "#8b949e"


def _pnl_cell(v: float, suffix: str = "") -> str:
    c = _pnl_color(v)
    return f'<td style="color:{c};font-weight:500">{v:+.2f}{suffix}</td>'


def _ts_short(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
    except Exception:
        return ts[:16]


# ── Builder HTML ──────────────────────────────────────────────────────────────

def build_executor_report() -> Path:
    state  = _load_state()
    execs  = _load_exec_csv()
    sim    = _load_sim_csv()
    es     = _exec_stats(state)
    ss     = _sim_stats(sim)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    SIM_CAPITAL = 100.0   # CAPITAL_EUR del simulator (trade_simulator.py)

    # ── Segnali solo-simulator (non in executor: pump_grad, defi, v3…) ────────
    sim_only = {sid: s for sid, s in sim.items() if sid not in state}

    # ── Matched trades (signal_id in entrambi) ──────────────────────────────
    matched = []
    for sid, ex in state.items():
        if sid not in sim:
            continue
        s = sim[sid]
        exec_pnl   = ex.get("real_pnl_usdc", 0)
        exec_spent = ex.get("usdc_spent", SIM_CAPITAL) or SIM_CAPITAL
        sim_pnl    = s["pnl_eur"]

        # Converti sim P&L allo stesso capitale dell'executor per confronto equo.
        # Il simulator usa 100€ fissi; l'executor può usare import diverso (es 10$ o 100$).
        # Scalando: sim_pnl_usd = sim_pnl% × exec_spent
        sim_pnl_pct = sim_pnl / SIM_CAPITAL * 100.0       # % P&L del simulator
        exec_pnl_pct = exec_pnl / exec_spent * 100.0       # % P&L dell'executor
        sim_pnl_usd = sim_pnl_pct / 100.0 * exec_spent    # sim P&L riscalato in $
        div_pct = exec_pnl_pct - sim_pnl_pct               # divergenza in punti %

        matched.append({
            "sid":           sid,
            "sym":           ex.get("token_symbol", "?"),
            "system":        ex.get("system", ""),
            "chain":         s["chain"],
            "entry_ts":      ex.get("entry_ts", ""),
            "exec_pnl":      exec_pnl,
            "exec_pnl_pct":  exec_pnl_pct,
            "sim_pnl":       sim_pnl,
            "sim_pnl_usd":   sim_pnl_usd,
            "sim_pnl_pct":   sim_pnl_pct,
            "divergence":    div_pct,
            "exec_status":   ex["status"],
            "sim_exit":      s["exit_reason"],
            "exec_spent":    exec_spent,
            "exec_received": ex.get("usdc_received", 0),
        })
    matched.sort(key=lambda x: x["entry_ts"], reverse=True)

    # Divergenza media (in punti %) solo su trade chiusi in entrambi
    both_closed = [m for m in matched if m["exec_status"] == "closed" and m["sim_exit"] != "open"]
    avg_div = sum(m["divergence"] for m in both_closed) / len(both_closed) if both_closed else 0

    # ── Posizioni aperte (executor) ─────────────────────────────────────────
    open_pos = [(sid, v) for sid, v in state.items() if v["status"] == "open"]
    open_pos.sort(key=lambda x: x[1].get("entry_ts", ""), reverse=True)

    # ── Trade chiusi recenti (executor) ─────────────────────────────────────
    closed_pos = [(sid, v) for sid, v in state.items() if v["status"] == "closed"]
    closed_pos.sort(key=lambda x: x[1].get("close_ts", ""), reverse=True)

    # ── Log esecuzioni recenti ───────────────────────────────────────────────
    recent_execs = execs[-30:][::-1]

    # ── Tabelle HTML ─────────────────────────────────────────────────────────

    def _matched_rows() -> str:
        if not matched:
            return '<tr><td colspan="9" style="color:#8b949e;text-align:center">Nessun trade in comune</td></tr>'
        rows = []
        for m in matched:
            div_c = _pnl_color(m["divergence"])
            status_badge = (
                '<span style="color:#3fb950">✓ closed</span>' if m["exec_status"] == "closed"
                else '<span style="color:#e3b341">⏳ open</span>'
            )
            sigdate = m["entry_ts"][:10] if m["entry_ts"] else ""
            win = 1 if m["exec_pnl"] > 0 else 0
            rows.append(
                f'<tr class="mrow" data-sys="{m["system"]}" data-chain="{m["chain"]}"'
                f' data-sigdate="{sigdate}" data-win="{win}" data-token="{m["sym"].lower()}"'
                f' data-simppct="{m["sim_pnl_pct"]:.4f}" data-execpct="{m["exec_pnl_pct"]:.4f}"'
                f' data-div="{m["divergence"]:.4f}" data-execpnl="{m["exec_pnl"]:.4f}"'
                f' data-simpnl="{m["sim_pnl"]:.4f}">'
                f'<td><b>{m["sym"]}</b><br><small style="color:#8b949e">{m["sid"][-12:]}</small></td>'
                f'<td>{m["system"]}</td>'
                f'<td style="color:#8b949e">{_ts_short(m["entry_ts"])}</td>'
                + _pnl_cell(m["sim_pnl_pct"], "%")
                + _pnl_cell(m["exec_pnl_pct"], "%")
                + f'<td style="color:{div_c};font-weight:600">{m["divergence"]:+.1f}pp</td>'
                + f'<td style="color:#8b949e;font-size:.8rem">{m["sim_exit"]}</td>'
                + f'<td>{status_badge}</td>'
                + f'<td style="color:#8b949e">{m["exec_spent"]:.1f}→{m["exec_received"]:.2f}$</td>'
                + f'</tr>'
            )
        return "\n".join(rows)

    def _open_rows() -> str:
        if not open_pos:
            return '<tr><td colspan="5" style="color:#8b949e;text-align:center">Nessuna posizione aperta</td></tr>'
        rows = []
        for sid, v in open_pos:
            pnl = v.get("real_pnl_usdc", 0)
            sigdate = v.get("entry_ts","")[:10]
            chain   = sim.get(sid, {}).get("chain", "")
            stuck = v.get("status","") == "stuck"
            stuck_badge = ' <span style="background:#f8513322;color:#f85149;font-size:.7rem;padding:1px 5px;border-radius:3px">⚠ STUCK</span>' if stuck else ""
            rows.append(
                f'<tr class="orow" data-sys="{v.get("system","")}" data-chain="{chain}"'
                f' data-sigdate="{sigdate}" data-token="{v.get("token_symbol","").lower()}">'
                f'<td><b>{v.get("token_symbol","?")}</b>{stuck_badge}</td>'
                f'<td style="color:#8b949e">{v.get("system","")}</td>'
                f'<td style="color:#8b949e">{_ts_short(v.get("entry_ts",""))}</td>'
                f'<td>{v.get("usdc_spent",0):.2f}$</td>'
                + _pnl_cell(pnl, "$")
                + f'</tr>'
            )
        return "\n".join(rows)

    def _closed_rows() -> str:
        if not closed_pos:
            return '<tr><td colspan="6" style="color:#8b949e;text-align:center">Nessun trade chiuso</td></tr>'
        rows = []
        for sid, v in closed_pos:
            pnl = v.get("real_pnl_usdc", 0)
            sim_row = sim.get(sid)
            sim_cell = (
                _pnl_cell(sim_row["pnl_eur"], "€") if sim_row
                else '<td style="color:#8b949e">—</td>'
            )
            sigdate   = v.get("entry_ts","")[:10]
            closedate = v.get("close_ts","")[:10]
            chain     = sim_row.get("chain","") if sim_row else ""
            win       = 1 if pnl > 0 else 0
            sim_pnl_v = sim_row["pnl_eur"] if sim_row else 0.0
            rows.append(
                f'<tr class="crow" data-sys="{v.get("system","")}" data-chain="{chain}"'
                f' data-sigdate="{sigdate}" data-closedate="{closedate}"'
                f' data-win="{win}" data-token="{v.get("token_symbol","").lower()}"'
                f' data-execpnl="{pnl:.4f}" data-simpnl="{sim_pnl_v:.4f}">'
                f'<td><b>{v.get("token_symbol","?")}</b></td>'
                f'<td style="color:#8b949e">{v.get("system","")}</td>'
                f'<td style="color:#8b949e">{_ts_short(v.get("entry_ts",""))}</td>'
                f'<td style="color:#8b949e">{_ts_short(v.get("close_ts",""))}</td>'
                f'<td>{v.get("usdc_spent",0):.2f}→{v.get("usdc_received",0):.2f}$</td>'
                + _pnl_cell(pnl, "$")
                + sim_cell
                + f'</tr>'
            )
        return "\n".join(rows)

    def _simonly_rows() -> str:
        rows_so = sorted(sim_only.items(), key=lambda x: x[1].get("entry_ts",""), reverse=True)
        if not rows_so:
            return '<tr><td colspan="6" style="color:#8b949e;text-align:center">Nessun segnale solo-simulator</td></tr>'
        rows = []
        for sid, s in rows_so:
            exit_r  = s.get("exit_reason","open")
            is_open = exit_r in ("open","")
            status_badge = (
                '<span style="color:#e3b341">⏳ aperto</span>' if is_open
                else '<span style="color:#8b949e">✓ chiuso</span>'
            )
            pnl    = s.get("pnl_eur", 0)
            chg    = s.get("change_pct", 0)
            win    = 1 if pnl > 0 else 0
            sigdate = s.get("entry_ts","")[:10]
            rows.append(
                f'<tr class="sorow" data-sys="{s.get("system","")}" data-chain="{s.get("chain","")}"'
                f' data-sigdate="{sigdate}" data-win="{win}" data-token="{s.get("token_symbol","").lower()}"'
                f' data-simpnl="{pnl:.4f}">'
                f'<td><b>{s.get("token_symbol","?")}</b><br>'
                f'<small style="color:#8b949e">{sid[-14:]}</small></td>'
                f'<td style="color:#8b949e">{s.get("system","")}</td>'
                f'<td style="color:#8b949e">{_ts_short(s.get("entry_ts",""))}</td>'
                f'<td style="color:#8b949e">{exit_r}</td>'
                + _pnl_cell(chg, "%")
                + _pnl_cell(pnl, "€")
                + f'<td>{status_badge}</td>'
                + f'</tr>'
            )
        return "\n".join(rows)

    def _exec_log_rows() -> str:
        if not recent_execs:
            return '<tr><td colspan="6" style="color:#8b949e;text-align:center">Nessuna esecuzione</td></tr>'
        rows = []
        for r in recent_execs:
            action = r.get("action", "")
            a_color = "#3fb950" if action == "buy" else "#f85149" if action == "sell" else "#8b949e"
            status = r.get("status","")
            s_color = "#8b949e" if status == "dry_run" else "#e3b341"
            rows.append(
                f'<tr>'
                f'<td style="color:#8b949e;font-size:.8rem">{_ts_short(r.get("ts",""))}</td>'
                f'<td><b>{r.get("token_symbol","?")}</b></td>'
                f'<td style="color:{a_color};font-weight:600">{action.upper()}</td>'
                f'<td>{float(r.get("usdc_amount") or r.get("eth_amount") or 0):.4g}</td>'
                f'<td style="color:#8b949e">{float(r.get("price_actual",0) or 0):.6g}</td>'
                f'<td style="color:{s_color}">{status}</td>'
                f'</tr>'
            )
        return "\n".join(rows)

    # ── KPI Cards ────────────────────────────────────────────────────────────
    def _kpi(label: str, value: str, color: str = "#e6edf3", kid: str = "") -> str:
        id_attr = f' id="{kid}"' if kid else ""
        return (
            f'<div class="kpi">'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-value" style="color:{color}"{id_attr}>{value}</div>'
            f'</div>'
        )

    exec_pnl_color = _pnl_color(es["total_pnl"])
    sim_pnl_color  = _pnl_color(ss["total_pnl"])
    div_color      = _pnl_color(avg_div)

    # Controlla se ci sono trade reali o solo DRY_RUN
    all_dry = all(
        any(e.get("status","") == "dry_run" for e in execs if e.get("signal_id") == sid)
        for sid in state
    ) if state else True
    dry_banner = (
        '<div style="background:#1c2128;border:1px solid #e3b341;border-radius:6px;'
        'padding:8px 14px;margin-bottom:16px;font-size:.82rem;color:#e3b341">'
        '⚠️ <strong>MODALITÀ SIMULAZIONE (DRY_RUN)</strong> — nessuna transazione reale eseguita. '
        'I P&L mostrati sono simulati con prezzi DEX al momento dell\'exit.</div>'
    ) if all_dry else ""

    # ── HTML finale ──────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>Executor Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;padding:20px 24px;font-size:13px}}
h2{{font-size:1rem;font-weight:600;color:#e6edf3;margin:20px 0 10px}}
h3{{font-size:.85rem;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin:24px 0 8px}}
table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
th{{font-size:.72rem;color:#8b949e;font-weight:500;text-transform:uppercase;letter-spacing:.4px;
    padding:8px 12px;border-bottom:1px solid #21262d;background:#161b22;text-align:left}}
td{{padding:7px 12px;border-bottom:1px solid #161b22;font-size:.85rem;vertical-align:middle}}
tr:hover td{{background:#161b22}}
.kpi-row{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}
.kpi{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;min-width:130px;flex:1}}
.kpi-label{{font-size:.68rem;color:#8b949e;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}}
.kpi-value{{font-size:1.4rem;font-weight:600}}
.section{{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:20px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:500}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;
         border-bottom:1px solid #21262d;padding-bottom:12px}}
.refresh-note{{font-size:.75rem;color:#8b949e}}
.divider{{border:none;border-top:1px solid #21262d;margin:20px 0}}
small{{font-size:.78rem}}
.filters{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
.btn-grp{{display:flex}}
.btn-grp button{{background:#21262d;border:1px solid #30363d;color:#8b949e;font-size:.75rem;padding:5px 12px;cursor:pointer;transition:.15s}}
.btn-grp button:first-child{{border-radius:5px 0 0 5px}}
.btn-grp button:last-child{{border-radius:0 5px 5px 0;margin-left:-1px}}
.btn-grp button.active{{background:#1f6feb;border-color:#1f6feb;color:white}}
.chain-btn.active,.sol-btn.active{{background:#9945ff;border-color:#9945ff;color:white}}
.base-btn.active{{background:#0052ff;border-color:#0052ff;color:white}}
.bsc-btn.active{{background:#f3ba2f;border-color:#f3ba2f;color:#000}}
.eth-btn.active{{background:#627eea;border-color:#627eea;color:white}}
.outcome-btn.active{{background:#3fb950;border-color:#3fb950;color:#000}}
.loss-btn.active{{background:#f85149;border-color:#f85149;color:white}}
input[type=date],input[type=search]{{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.75rem}}
th.sortable{{cursor:pointer;user-select:none}}
th.sortable:hover{{color:#e6edf3}}
th.sorted{{color:#58a6ff}}
.sort-ind{{font-size:.65rem;opacity:.4;margin-left:3px}}
th.sorted .sort-ind{{opacity:1}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h2>⚡ Executor Dashboard</h2>
    <span class="refresh-note">{now_str} · auto-refresh 30s</span>
  </div>
</div>
{dry_banner}

<!-- KPI EXECUTOR -->
<h3>Executor (USDC reali)</h3>
<div class="kpi-row">
  {_kpi("P&L Totale", f"{es['total_pnl']:+.2f} $", exec_pnl_color, "kpi-exec-pnl")}
  {_kpi("Win Rate", f"{es['win_rate']:.0f}%", "#3fb950" if es['win_rate']>=50 else "#f85149", "kpi-exec-wr")}
  {_kpi("Trade Chiusi", str(es['n_closed']), kid="kpi-exec-closed")}
  {_kpi("Aperti", str(es['n_open']), "#e3b341" if es['n_open']>0 else "#8b949e", "kpi-exec-open")}
  {_kpi("Avg P&L", f"{es['avg_pnl']:+.2f} $", exec_pnl_color, "kpi-exec-avg")}
  {_kpi("Vol. Scambiato", f"{es['total_spent']:.0f} $", "#8b949e")}
</div>

<!-- KPI SIMULATOR -->
<h3>Simulator (EUR simulati)</h3>
<div class="kpi-row">
  {_kpi("P&L Totale", f"{ss['total_pnl']:+.2f} €", sim_pnl_color, "kpi-sim-pnl")}
  {_kpi("Win Rate", f"{ss['win_rate']:.0f}%", "#3fb950" if ss['win_rate']>=50 else "#f85149", "kpi-sim-wr")}
  {_kpi("Trade Chiusi", str(ss['n_closed']), kid="kpi-sim-closed")}
  {_kpi("Avg P&L", f"{ss['avg_pnl']:+.2f} €", sim_pnl_color, "kpi-sim-avg")}
</div>

<!-- KPI DIVERGENZA -->
<h3>Divergenza % Simulator → Executor (trade in comune: {len(both_closed)})</h3>
<div class="kpi-row">
  {_kpi("Avg Diverg. (%)", f"{avg_div:+.1f} pp", div_color, "kpi-avg-div")}
  {_kpi("Sim vs Exec", f"sim {'sovrastima' if avg_div>0 else 'sottostima'} di {abs(avg_div):.1f}pp", _pnl_color(-avg_div), "kpi-div-label")}
  {_kpi("Trade confrontati", str(len(both_closed)), kid="kpi-div-count")}
</div>
<p style="color:#8b949e;font-size:.78rem;margin-bottom:20px">
  Divergenza in punti percentuale (pp) = %P&amp;L Executor − %P&amp;L Simulator (entrambi normalizzati al capitale investito).
  Negativo = simulator ottimista (DexScreener ≠ Jupiter / entry time diversi).
</p>

<!-- FILTRI -->
<div class="filters">
  <div class="btn-grp">
    <button id="btn24h" class="preset-btn" onclick="setPreset(this,dateNDaysAgo(1),dateToday())">24h</button>
    <button class="preset-btn" onclick="setPreset(this,dateNDaysAgo(7),'')">7g</button>
    <button class="preset-btn active" onclick="setPreset(this,'','')">Tutto</button>
  </div>
  <input type="date" id="df" onchange="clearPreset();applyFilter()">
  <span style="color:#484f58;font-size:.8rem">→</span>
  <input type="date" id="dt" onchange="clearPreset();applyFilter()">
  <div class="btn-grp">
    <button class="active sys-btn" onclick="setSys(this,'')">Tutti</button>
    <button class="sys-btn" onclick="setSys(this,'pump_grad')">PUMP</button>
    <button class="sys-btn" onclick="setSys(this,'defi')">DEFI</button>
    <button class="sys-btn" onclick="setSys(this,'v3')">V3</button>
    <button class="sys-btn" onclick="setSys(this,'v3_large')">V3L</button>
    <button class="sys-btn" onclick="setSys(this,'v3_midcap')">V3M</button>
  </div>
  <div class="btn-grp">
    <button class="active chain-btn" onclick="setChain(this,'')">Tutte</button>
    <button class="chain-btn sol-btn" onclick="setChain(this,'solana')">◎ SOL</button>
    <button class="chain-btn base-btn" onclick="setChain(this,'base')">🔵 BASE</button>
    <button class="chain-btn bsc-btn" onclick="setChain(this,'bsc')">◈ BSC</button>
    <button class="chain-btn eth-btn" onclick="setChain(this,'ethereum')">⬡ ETH</button>
  </div>
  <div class="btn-grp">
    <button class="active outcome-btn" onclick="setOutcome(this,'')">W+L</button>
    <button class="outcome-btn" onclick="setOutcome(this,'win')">✅ Win</button>
    <button class="outcome-btn loss-btn" onclick="setOutcome(this,'loss')">❌ Loss</button>
  </div>
  <input type="search" id="tok-search" placeholder="cerca token…" oninput="setTokSearch(this.value)" style="width:130px">
</div>
<div style="font-size:.78rem;color:#8b949e;margin-bottom:12px">
  Visibili: <b id="vis-m">—</b> confronto · <b id="vis-o">—</b> aperte · <b id="vis-c">—</b> chiuse
</div>

<!-- CONFRONTO TRADE -->
<div class="section">
<h3>Confronto Simulator vs Executor (trade in comune)</h3>
<table>
<thead><tr>
  <th class="sortable" data-sortcol="token" onclick="sortTable('token','matched-tbody')">Token <span class="sort-ind">&#8645;</span></th>
  <th>Sistema</th>
  <th class="sortable" data-sortcol="sigdate" onclick="sortTable('sigdate','matched-tbody')">Entrata <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="simppct" onclick="sortTable('simppct','matched-tbody')">Sim P&L % <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="execpct" onclick="sortTable('execpct','matched-tbody')">Exec P&L % <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="div" onclick="sortTable('div','matched-tbody')">Diverg. (pp) <span class="sort-ind">&#8645;</span></th>
  <th>Exit Sim</th><th>Exec Status</th><th>Spent→Recv</th>
</tr></thead>
<tbody id="matched-tbody">{_matched_rows()}</tbody>
</table>
</div>

<!-- POSIZIONI APERTE -->
<div class="section">
<h3>Posizioni Aperte (<span id="vis-o2">{len(open_pos)}</span>)</h3>
<table>
<thead><tr><th>Token</th><th>Sistema</th><th>Entrata</th><th>Investito</th><th>P&L Attuale</th></tr></thead>
<tbody id="open-tbody">{_open_rows()}</tbody>
</table>
</div>

<!-- TRADE CHIUSI -->
<div class="section">
<h3>Trade Chiusi (<span id="vis-c2">{len(closed_pos)}</span>)</h3>
<table>
<thead><tr>
  <th class="sortable" data-sortcol="token" onclick="sortTable('token','closed-tbody')">Token <span class="sort-ind">&#8645;</span></th>
  <th>Sistema</th>
  <th class="sortable" data-sortcol="sigdate" onclick="sortTable('sigdate','closed-tbody')">Entrata <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="closedate" onclick="sortTable('closedate','closed-tbody')">Chiusura <span class="sort-ind">&#8645;</span></th>
  <th>Spent→Recv</th>
  <th class="sortable" data-sortcol="execpnl" onclick="sortTable('execpnl','closed-tbody')">Exec P&L <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="simpnl" onclick="sortTable('simpnl','closed-tbody')">Sim P&L <span class="sort-ind">&#8645;</span></th>
</tr></thead>
<tbody id="closed-tbody">{_closed_rows()}</tbody>
</table>
</div>

<!-- SOLO SIMULATOR (pump_grad, defi, v3 senza executor reale) -->
<div class="section">
<h3>Solo Simulator — non eseguiti dall'executor (<span id="vis-so">{len(sim_only)}</span>)</h3>
<table>
<thead><tr>
  <th class="sortable" data-sortcol="token" onclick="sortTable('token','simonly-tbody')">Token <span class="sort-ind">&#8645;</span></th>
  <th>Sistema</th>
  <th class="sortable" data-sortcol="sigdate" onclick="sortTable('sigdate','simonly-tbody')">Entrata <span class="sort-ind">&#8645;</span></th>
  <th>Exit</th>
  <th class="sortable" data-sortcol="simpnl" onclick="sortTable('simpnl','simonly-tbody')">Δ% <span class="sort-ind">&#8645;</span></th>
  <th class="sortable" data-sortcol="simpnl" onclick="sortTable('simpnl','simonly-tbody')">P&L Sim <span class="sort-ind">&#8645;</span></th>
  <th>Status</th>
</tr></thead>
<tbody id="simonly-tbody">{_simonly_rows()}</tbody>
</table>
</div>

<!-- LOG ESECUZIONI -->
<div class="section">
<h3>Log Esecuzioni Recenti</h3>
<table>
<thead><tr><th>Timestamp</th><th>Token</th><th>Azione</th><th>USDC</th><th>Prezzo</th><th>Status</th></tr></thead>
<tbody>{_exec_log_rows()}</tbody>
</table>
</div>

<script>
var _sys="",_chain="",_outcome="",_tokSearch="",_sortCol="",_sortDir=-1;
function dateNDaysAgo(n){{var d=new Date();d.setDate(d.getDate()-n);return d.toISOString().slice(0,10);}}
function dateToday(){{return new Date().toISOString().slice(0,10);}}
function setPreset(btn,df,dt){{
  document.getElementById("df").value=df;
  document.getElementById("dt").value=dt;
  document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");applyFilter();}}
function clearPreset(){{document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));}}
function setSys(btn,v){{_sys=v;btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));btn.classList.add("active");applyFilter();}}
function setChain(btn,v){{_chain=v;btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));btn.classList.add("active");applyFilter();}}
function setOutcome(btn,v){{_outcome=v;btn.closest(".btn-grp").querySelectorAll("button").forEach(b=>b.classList.remove("active"));btn.classList.add("active");applyFilter();}}
function setTokSearch(v){{_tokSearch=v.toLowerCase().trim();applyFilter();}}
var _numCols=["simppct","execpct","div","execpnl","simpnl","closedate"];
function sortTable(col,tbodyId){{
  if(_sortCol===col){{_sortDir=-_sortDir;}}else{{_sortCol=col;_sortDir=-1;}}
  var tbody=document.getElementById(tbodyId);
  var rows=Array.from(tbody.querySelectorAll("tr"));
  rows.sort(function(a,b){{
    var av=a.dataset[col]||"",bv=b.dataset[col]||"";
    if(_numCols.indexOf(col)>=0){{return(parseFloat(av||0)-parseFloat(bv||0))*_sortDir;}}
    return(av<bv?-1:av>bv?1:0)*_sortDir;
  }});
  rows.forEach(r=>tbody.appendChild(r));
  document.querySelectorAll("th.sortable").forEach(function(th){{
    var sorted=th.dataset.sortcol===col;
    th.classList.toggle("sorted",sorted);
    var ind=th.querySelector(".sort-ind");
    if(ind)ind.innerHTML=sorted?(_sortDir>0?"&#9650;":"&#9660;"):"&#8645;";
  }});
  applyFilter();
}}
function _set(id,txt,color){{var el=document.getElementById(id);if(el){{el.textContent=txt;if(color)el.style.color=color;}}}}
function _pcolor(v){{return v>0?"#3fb950":v<0?"#f85149":"#8b949e";}}
function matchRow(r,df,dt){{
  var sd=r.dataset.sigdate||"";
  var ok_df=!df||sd>=df;var ok_dt=!dt||sd<=dt;
  var ok_sys=!_sys||r.dataset.sys===_sys;
  var ok_ch=!_chain||r.dataset.chain===_chain;
  var ok_out=!_outcome||(_outcome==="win"&&r.dataset.win==="1")||(_outcome==="loss"&&r.dataset.win==="0");
  var ok_tok=!_tokSearch||(r.dataset.token||"").indexOf(_tokSearch)>=0;
  return ok_df&&ok_dt&&ok_sys&&ok_ch&&ok_out&&ok_tok;
}}
function applyFilter(){{
  var df=document.getElementById("df").value;
  var dt=document.getElementById("dt").value;
  var vm=0,vo=0,vc=0;
  var exec_pnl=0,exec_wins=0,exec_losses=0;
  var sim_pnl=0,sim_wins=0,sim_closed=0;
  var div_sum=0,div_n=0;
  document.querySelectorAll("#matched-tbody tr.mrow").forEach(function(r){{
    var s=matchRow(r,df,dt);r.style.display=s?"":"none";if(s)vm++;
  }});
  document.querySelectorAll("#open-tbody tr.orow").forEach(function(r){{
    var s=matchRow(r,df,dt);r.style.display=s?"":"none";if(s)vo++;
  }});
  document.querySelectorAll("#closed-tbody tr.crow").forEach(function(r){{
    var s=matchRow(r,df,dt);r.style.display=s?"":"none";
    if(s){{
      vc++;
      var ep=parseFloat(r.dataset.execpnl||0);
      var sp=parseFloat(r.dataset.simpnl||0);
      exec_pnl+=ep;
      sim_pnl+=sp;
      if(ep>0)exec_wins++;else if(ep<0)exec_losses++;
      if(sp>0)sim_wins++;
      sim_closed++;
      if(r.dataset.execpnl&&r.dataset.simpnl&&parseFloat(r.dataset.simpnl)!==0){{
        div_sum+=(parseFloat(r.dataset.execpnl)-parseFloat(r.dataset.simpnl));div_n++;
      }}
    }}
  }});
  var vso=0,so_pnl=0,so_wins=0,so_closed=0;
  document.querySelectorAll("#simonly-tbody tr.sorow").forEach(function(r){{
    var s=matchRow(r,df,dt);r.style.display=s?"":"none";
    if(s){{
      vso++;
      var sp=parseFloat(r.dataset.simpnl||0);
      so_pnl+=sp;if(sp>0)so_wins++;
      if(r.dataset.win!==undefined)so_closed++;
      sim_pnl+=sp;if(sp>0)sim_wins++;sim_closed++;
    }}
  }});
  _set("vis-so",vso);
  // Contatori visibili
  _set("vis-m",vm);_set("vis-o",vo);_set("vis-c",vc);
  _set("vis-o2",vo);_set("vis-c2",vc);
  // KPI Executor
  _set("kpi-exec-pnl",(exec_pnl>=0?"+":"")+exec_pnl.toFixed(2)+" $",_pcolor(exec_pnl));
  var tot=exec_wins+exec_losses;
  var wr=tot>0?Math.round(exec_wins/tot*100):0;
  _set("kpi-exec-wr",wr+"%",wr>=50?"#3fb950":"#f85149");
  _set("kpi-exec-closed",vc);
  _set("kpi-exec-open",vo,"#e3b341");
  _set("kpi-exec-avg",(vc>0?(exec_pnl/vc>=0?"+":"")+( exec_pnl/vc).toFixed(2)+" $":"—"),_pcolor(exec_pnl));
  // KPI Simulator
  _set("kpi-sim-pnl",(sim_pnl>=0?"+":"")+sim_pnl.toFixed(2)+" €",_pcolor(sim_pnl));
  var swr=sim_closed>0?Math.round(sim_wins/sim_closed*100):0;
  _set("kpi-sim-wr",swr+"%",swr>=50?"#3fb950":"#f85149");
  _set("kpi-sim-closed",sim_closed);
  _set("kpi-sim-avg",(sim_closed>0?(sim_pnl/sim_closed>=0?"+":"")+( sim_pnl/sim_closed).toFixed(2)+" €":"—"),_pcolor(sim_pnl));
  // KPI Divergenza
  var avg_div=div_n>0?div_sum/div_n:0;
  _set("kpi-avg-div",(avg_div>=0?"+":"")+avg_div.toFixed(2)+" pp",_pcolor(avg_div));
  _set("kpi-div-count",div_n);
}}
applyFilter();
</script>

</body>
</html>"""

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    return OUT_HTML


if __name__ == "__main__":
    out = build_executor_report()
    print(f"Report generato: {out}")

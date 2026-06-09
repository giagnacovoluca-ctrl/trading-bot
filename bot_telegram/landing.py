"""
landing.py — genera bot_telegram/landing/index.html dalla stats.json.
Chiamato da track_record.post_recap() ad ogni ciclo daily.
Se LANDING_PAGES_REPO_PATH è impostato nel .env, fa auto-commit+push su quel
repo Git (GitHub Pages) dopo ogni rigenerazione.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import config
import store

log = logging.getLogger("landing")

OUT_DIR  = config.BASE_DIR / "landing"
OUT_FILE = OUT_DIR / "index.html"

_SYS_LABEL = {
    "defi": "DeFi Gem", "pump_grad": "Pump Graduation", "v3": "Gem Hunter V3",
    "v3_large": "Gem Hunter V3 Large",
    "pre_grad": "Pre-Graduation", "mirror": "Wallet Mirror", "midcap": "Mid-Cap",
}


def _sign_color(v: float) -> str:
    return "#00e676" if v >= 0 else "#ff5252"


def _pnl_str(v: float) -> str:
    return f"{v:+.2f}€"


def _wr_color(wr: float) -> str:
    if wr >= 70:
        return "#00e676"
    if wr >= 50:
        return "#ffeb3b"
    return "#ff5252"


def _system_rows(by_system: dict) -> str:
    rows = []
    # Solo sistemi attivi (vedi _SYS_LABEL): esclude v2/v3/bnf/ecc. — disabilitati,
    # presenti in live_trades.csv solo come storico, non vanno mostrati in landing.
    active = {k: d for k, d in by_system.items() if k in _SYS_LABEL}
    for key, d in sorted(active.items(), key=lambda kv: -kv[1].get("pnl_eur", 0)):
        label = _SYS_LABEL[key]
        pnl   = d.get("pnl_eur", 0.0)
        wr    = d.get("win_rate", 0.0)
        n     = d.get("n", 0)
        rows.append(
            f'<tr>'
            f'<td>{label}</td>'
            f'<td style="color:{_wr_color(wr)}">{wr:.1f}%</td>'
            f'<td>{n}</td>'
            f'<td style="color:{_sign_color(pnl)};font-weight:600">{_pnl_str(pnl)}</td>'
            f'</tr>'
        )
    return "\n".join(rows) if rows else '<tr><td colspan="4">—</td></tr>'


def generate(stats: dict | None = None) -> Path:
    """Genera landing/index.html. Restituisce il path del file generato."""
    if stats is None:
        stats = store.load("stats.json", {})

    if not stats.get("available"):
        log.info("[landing] stats non disponibili, landing non generata")
        return OUT_FILE

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    wr    = stats.get("win_rate", 0.0)
    n     = stats.get("trades_closed", 0)
    pnl   = stats.get("total_pnl_eur", 0.0)
    p24   = stats.get("pnl_24h_eur", 0.0)
    avg   = stats.get("avg_pnl_eur", 0.0)
    best  = stats.get("best",  {"symbol": "—", "pnl_eur": 0.0})
    worst = stats.get("worst", {"symbol": "—", "pnl_eur": 0.0})
    upd   = stats.get("updated_iso", "—")
    sys_rows = _system_rows(stats.get("by_system", {}))

    bot_url     = f"https://t.me/{config.BOT_USERNAME or 'YourBot'}"
    premium_cta = f"{bot_url}?start=premium"
    free_url    = f"https://t.me/{config.FREE_CHANNEL_USERNAME}" if config.FREE_CHANNEL_USERNAME else ""
    premium_url = f"https://t.me/{config.PREMIUM_CHANNEL_USERNAME}" if config.PREMIUM_CHANNEL_USERNAME else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Crypto Signal Bot — Track Record</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0d0d0d; --card: #161616; --border: #2a2a2a;
    --text: #e0e0e0; --muted: #888; --accent: #00e676;
    --font: 'Inter', system-ui, sans-serif;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--font); line-height: 1.6; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── HERO ── */
  .hero {{ text-align: center; padding: 72px 24px 48px; }}
  .hero .badge {{ display: inline-block; background: #1a2e1a; color: var(--accent);
    font-size: .75rem; font-weight: 600; letter-spacing: .1em; padding: 4px 12px;
    border-radius: 99px; border: 1px solid var(--accent); margin-bottom: 24px; }}
  .hero h1 {{ font-size: clamp(2rem, 5vw, 3.5rem); font-weight: 800; line-height: 1.15;
    background: linear-gradient(135deg, #fff 40%, var(--accent));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .hero p {{ margin-top: 16px; color: var(--muted); font-size: 1.1rem; max-width: 560px;
    margin-left: auto; margin-right: auto; }}

  /* ── STATS GRID ── */
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px; max-width: 900px; margin: 0 auto 48px; padding: 0 24px; }}
  .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; text-align: center; }}
  .stat-card .value {{ font-size: 2rem; font-weight: 800; }}
  .stat-card .label {{ font-size: .8rem; color: var(--muted); margin-top: 4px; text-transform: uppercase;
    letter-spacing: .05em; }}

  /* ── TABLE ── */
  .section {{ max-width: 900px; margin: 0 auto 48px; padding: 0 24px; }}
  .section h2 {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 16px;
    color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
  th {{ text-align: left; padding: 12px 16px; font-size: .8rem; color: var(--muted);
    text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--border); }}
  td {{ padding: 12px 16px; font-size: .95rem; border-bottom: 1px solid #1e1e1e; }}
  tr:last-child td {{ border-bottom: none; }}

  /* ── BEST/WORST ── */
  .bw-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .bw-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; }}
  .bw-card .bw-label {{ font-size: .75rem; color: var(--muted); text-transform: uppercase;
    letter-spacing: .08em; margin-bottom: 8px; }}
  .bw-card .bw-sym {{ font-size: 1.3rem; font-weight: 700; }}
  .bw-card .bw-pnl {{ font-size: 1rem; font-weight: 600; margin-top: 4px; }}

  /* ── CTA ── */
  .cta {{ text-align: center; padding: 48px 24px 72px; }}
  .cta h2 {{ font-size: 1.6rem; font-weight: 800; margin-bottom: 8px; }}
  .cta p {{ color: var(--muted); margin-bottom: 32px; }}
  .btn-row {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }}
  .btn {{ display: inline-block; padding: 14px 28px; border-radius: 10px;
    font-weight: 700; font-size: 1rem; transition: opacity .15s; }}
  .btn:hover {{ opacity: .85; text-decoration: none; }}
  .btn-free    {{ background: #1e1e1e; border: 1px solid var(--border); color: var(--text); }}
  .btn-premium {{ background: var(--accent); color: #000; }}

  /* ── FOOTER ── */
  footer {{ text-align: center; padding: 24px; color: var(--muted); font-size: .8rem;
    border-top: 1px solid var(--border); }}
</style>
</head>
<body>

<section class="hero">
  <span class="badge">LIVE TRACK RECORD</span>
  <h1>AI-powered crypto signals<br/>on Solana &amp; Base</h1>
  <p>Multi-chain scanner with automated AI signals. Verifiable P&amp;L, published in real time.</p>
</section>

<div class="stats">
  <div class="stat-card">
    <div class="value" style="color:{_wr_color(wr)}">{wr:.1f}%</div>
    <div class="label">Win Rate</div>
  </div>
  <div class="stat-card">
    <div class="value">{n}</div>
    <div class="label">Closed trades</div>
  </div>
  <div class="stat-card">
    <div class="value" style="color:{_sign_color(pnl)}">{_pnl_str(pnl)}</div>
    <div class="label">Total P&amp;L</div>
  </div>
  <div class="stat-card">
    <div class="value" style="color:{_sign_color(avg)}">{_pnl_str(avg)}</div>
    <div class="label">Avg / trade</div>
  </div>
  <div class="stat-card">
    <div class="value" style="color:{_sign_color(p24)}">{_pnl_str(p24)}</div>
    <div class="label">Last 24h</div>
  </div>
</div>

<section class="section">
  <h2>Performance by system</h2>
  <table>
    <thead><tr><th>System</th><th>Win Rate</th><th>Trades</th><th>P&amp;L</th></tr></thead>
    <tbody>
{sys_rows}
    </tbody>
  </table>
</section>

<section class="section">
  <h2>Best &amp; Worst trade</h2>
  <div class="bw-grid">
    <div class="bw-card">
      <div class="bw-label">🏆 Best trade</div>
      <div class="bw-sym">${best['symbol']}</div>
      <div class="bw-pnl" style="color:#00e676">{_pnl_str(best['pnl_eur'])}</div>
    </div>
    <div class="bw-card">
      <div class="bw-label">📉 Worst trade</div>
      <div class="bw-sym">${worst['symbol']}</div>
      <div class="bw-pnl" style="color:#ff5252">{_pnl_str(worst['pnl_eur'])}</div>
    </div>
  </div>
</section>

<section class="cta">
  <h2>Follow our signals</h2>
  <p>Free channel: closed winning trades published publicly.<br/>Premium: real-time entry signals with price, liquidity, BSR and charts.</p>
  <div class="btn-row">
    {"" if not free_url else f'<a class="btn btn-free" href="{free_url}">Free Channel</a>'}
    <a class="btn btn-premium" href="{premium_cta}">Premium — ${int(config.PRICE_PREMIUM_USD)}/mo</a>
  </div>
</section>

<footer>
  Updated: {upd} &nbsp;·&nbsp; Past results do not guarantee future returns. DYOR.
</footer>

</body>
</html>"""

    OUT_FILE.write_text(html, encoding="utf-8")
    log.info("[landing] generata → %s", OUT_FILE)
    _push_to_pages(OUT_FILE, upd)
    return OUT_FILE


def _push_to_pages(html_file: Path, label: str) -> None:
    """Se LANDING_PAGES_REPO_PATH è impostato, copia index.html nel repo e fa push."""
    repo_path = config._env("LANDING_PAGES_REPO_PATH")
    if not repo_path:
        return
    repo = Path(repo_path)
    if not repo.exists():
        log.warning("[landing] LANDING_PAGES_REPO_PATH non trovato: %s", repo)
        return
    dest = repo / "index.html"
    dest.write_text(html_file.read_text(encoding="utf-8"), encoding="utf-8")
    cmds = [
        ["git", "-C", str(repo), "add", "index.html"],
        ["git", "-C", str(repo), "commit", "-m", f"track record {label}"],
        ["git", "-C", str(repo), "push"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            # commit fallisce se non ci sono modifiche — è ok, continua
            if "nothing to commit" in r.stdout + r.stderr:
                break
            log.warning("[landing] git: %s", r.stderr.strip())
            return
    log.info("[landing] push GitHub Pages completato")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    p = generate()
    print(f"Landing generata: {p}")

"""
funding_farmer.py — Monitora funding rates Injective e segnala opportunità farming.
Ogni 4h: se funding > soglia → log CSV + email diretta.
Strategia: quando funding positivo (long pagano short) → apri SHORT per raccogliere il rendimento.
Avvio standalone: cd injective_autopilot && python funding_farmer.py
NO trading autonomo — solo segnali con alert email.
"""
import asyncio
import csv
import logging
import os
import signal
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_EXEC = _ROOT / "executor"

for _p in [str(_HERE), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_EXEC / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("funding_farmer")

POLL_HOURS        = 4        # funding round ogni 8h, controlliamo ogni 4h per non perderne uno
FUNDING_THRESHOLD = 0.0003   # 0.03%/8h → APY ~13% — livello minimo interessante
FUNDING_GREAT     = 0.0008   # 0.08%/8h → APY ~35% — ottimo

_REPORTS = _HERE / "reports"
_CSV_OUT  = _REPORTS / "funding_opportunities.csv"


def _get_market_names() -> dict[str, str]:
    try:
        from config.settings import MARKET_NAMES
        return MARKET_NAMES
    except Exception:
        return {}


async def _scan_all_markets() -> list[dict]:
    """Fetch funding rate per tutti i market configurati, filtra sopra soglia."""
    from config.settings import Settings
    from data.injective_client import InjectiveClient

    cfg    = Settings()
    client = InjectiveClient(cfg)
    names  = _get_market_names()

    try:
        await client.connect()
    except Exception as e:
        log.error(f"[funding] connect error: {e}")
        return []

    results = []
    for market_id in cfg.market_ids:
        try:
            snap = await asyncio.wait_for(
                client.fetch_market_snapshot(market_id=market_id), timeout=10.0
            )
            if not snap:
                continue
            fr = snap.funding_rate   # cumulativo come decimal (es. 0.0005 = 0.05%/8h)
            if abs(fr) < FUNDING_THRESHOLD:
                continue
            ticker  = names.get(market_id, market_id[:16])
            side    = "SHORT" if fr > 0 else "LONG"
            apy_pct = abs(fr) * 3 * 365 * 100   # 3 round/gg × 365
            results.append({
                "market_id":       market_id,
                "ticker":          ticker,
                "funding_rate_8h": fr,
                "pct_8h":          fr * 100,
                "apy_pct":         apy_pct,
                "side":            side,
                "rating":          "★★★" if abs(fr) >= FUNDING_GREAT else "★★",
                "mid_price":       snap.mid if snap else 0,
            })
            log.info(
                f"[funding] {ticker:10s}  fr={fr*100:+.4f}%/8h  APY≈{apy_pct:.1f}%  → {side}  {results[-1]['rating']}"
            )
        except asyncio.TimeoutError:
            log.warning(f"[funding] {market_id[:16]}: fetch timeout (10s)")
        except Exception as e:
            log.debug(f"[funding] {market_id[:16]}: {e}")

    try:
        await client.close()
    except Exception:
        pass

    return sorted(results, key=lambda x: abs(x["funding_rate_8h"]), reverse=True)


def _append_csv(opportunities: list[dict]):
    if not opportunities:
        return
    _REPORTS.mkdir(parents=True, exist_ok=True)
    fields = ["ts", "ticker", "market_id", "pct_8h", "apy_pct", "side", "rating", "mid_price"]
    new_file = not _CSV_OUT.exists()
    with open(_CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        ts = datetime.now().isoformat()
        for o in opportunities:
            w.writerow({k: o.get(k, "") for k in fields} | {"ts": ts})


def _send_email(opportunities: list[dict]):
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    smtp_to   = os.getenv("SMTP_TO", smtp_user)
    if not smtp_user or not smtp_pass:
        log.debug("[funding] SMTP non configurato, skip email")
        return
    top = opportunities[:8]
    rows = "".join(
        f"<tr><td><b>{o['ticker']}</b></td>"
        f"<td style='color:{'red' if o['side']=='SHORT' else 'green'}'>{o['side']}</td>"
        f"<td>{o['pct_8h']:+.4f}%</td>"
        f"<td>{o['apy_pct']:.1f}%</td>"
        f"<td>{o['rating']}</td></tr>"
        for o in top
    )
    best = top[0]
    body = (
        f"<h3>Funding Farming Injective — {datetime.now().strftime('%d/%m/%H:%M')}</h3>"
        f"<table border='1' cellpadding='5' style='border-collapse:collapse'>"
        f"<tr><th>Ticker</th><th>Lato</th><th>Rate/8h</th><th>APY est.</th><th>Rating</th></tr>"
        f"{rows}</table>"
        f"<br><b>Strategia:</b> apri il lato indicato su injective_autopilot in PAPER mode, "
        f"chiudi dopo il prossimo funding round (~8h). "
        f"SL consigliato: -1.5% per coprire rischio direzionale.<br>"
        f"<i>Nota: APY calcolato su 3 round/gg — il funding può cambiare tra ora e il prossimo round.</i>"
    )
    msg = MIMEText(body, "html")
    msg["Subject"] = (
        f"[FUNDING] {len(opportunities)} opp. Injective — "
        f"{best['ticker']} APY {best['apy_pct']:.0f}% ({best['side']})"
    )
    msg["From"] = smtp_user
    msg["To"]   = smtp_to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        log.info(f"[funding] email inviata ({len(opportunities)} opportunità)")
    except Exception as e:
        log.warning(f"[funding] email error: {e}")


async def _loop():
    log.info(
        f"[funding] ▶ avviato (poll {POLL_HOURS}h, soglia {FUNDING_THRESHOLD*100:.4f}%/8h)"
    )
    while True:
        try:
            opps = await _scan_all_markets()
            if opps:
                _append_csv(opps)
                _send_email(opps)
                log.info(
                    f"[funding] {len(opps)} opportunità — top: {opps[0]['ticker']} "
                    f"APY {opps[0]['apy_pct']:.1f}% ({opps[0]['side']})"
                )
            else:
                log.info("[funding] nessuna opportunità sopra soglia in questo ciclo")
        except Exception as e:
            log.error(f"[funding] loop error: {e}")
        await asyncio.sleep(POLL_HOURS * 3600)


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    _REPORTS.mkdir(parents=True, exist_ok=True)
    log_file = _REPORTS / "funding_farmer.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
    )
    asyncio.run(_loop())


if __name__ == "__main__":
    main()

"""
email_digest.py — coda email → riepiloghi periodici.

Gli scanner NON inviano più una mail per segnale: chiamano queue_email(), che
accoda su JSONL. Un thread (digest_loop, avviato da run.py) invia un'unica
mail di riepilogo agli orari DIGEST_HOURS (default 8,14,20 → 3/giorno) con i
corpi HTML originali raggruppati per sistema + P&L 24h da live_trades.csv.

Le call real-time restano sul canale Telegram PREMIUM (bot_telegram/publisher).
Gli alert operativi di run.py (_send_alert crash) restano email immediate.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("email_digest")

_HERE        = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH   = os.path.join(_HERE, "reports", "email_digest_queue.jsonl")
TRADES_CSV   = os.path.join(_HERE, "reports", "live_trades.csv")
_queue_lock  = threading.Lock()

_SYS_LABEL = {
    "defi": "DeFi Pre-Pump", "v3": "Gem Hunter V3", "pump_grad": "Pump Graduation",
    "pre_grad": "Pre-Graduation", "mirror": "Wallet Mirror", "midcap": "Mid-Cap Squeeze",
    "base_pump": "Base Pump",
}


def _digest_hours() -> list[int]:
    raw = os.environ.get("DIGEST_HOURS", "8,14,20")
    try:
        hours = sorted({int(h) % 24 for h in raw.split(",") if h.strip()})
        return hours or [8, 14, 20]
    except ValueError:
        return [8, 14, 20]


def queue_email(system: str, subject: str, html_body: str) -> bool:
    """Accoda una mail-segnale al digest. Ritorna False solo su errore I/O."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "system": system,
        "subject": subject,
        "html": html_body,
    }
    try:
        with _queue_lock:
            with open(QUEUE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info(f"[digest] 📥 accodata [{system}] {subject[:80]}")
        return True
    except OSError as e:
        log.warning(f"[digest] errore accodamento: {e}")
        return False


def _read_and_clear_queue() -> list[dict]:
    with _queue_lock:
        if not os.path.exists(QUEUE_PATH):
            return []
        try:
            with open(QUEUE_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            open(QUEUE_PATH, "w").close()
        except OSError as e:
            log.warning(f"[digest] errore lettura coda: {e}")
            return []
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return entries


def _extract_body(html: str) -> str:
    """Estrae il contenuto di <body> per imbustare più mail in una sola."""
    m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else html


def _pnl_24h_by_system() -> dict[str, float]:
    """pnl_eur è CUMULATIVO per segnale: il valore 24h è il delta tra l'ultima
    riga nella finestra e l'ultima riga pre-finestra (stesso fix di
    track_record.compute(), mai propagato qui — sommare le righe raw
    double-conta le uscite parziali)."""
    out: dict[str, float] = {}
    try:
        import csv as _csv
        cutoff = datetime.now() - timedelta(hours=24)
        last_in_window: dict[str, float] = {}
        base_before_window: dict[str, float] = {}
        sys_by_sid: dict[str, str] = {}
        with open(TRADES_CSV, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(row["ts"])
                    pnl = float((row.get("pnl_eur") or "0").replace("+", ""))
                except (ValueError, KeyError):
                    continue
                sid = row.get("signal_id") or row.get("token_symbol") or "?"
                sys_by_sid[sid] = row.get("system", "?")
                if ts < cutoff:
                    base_before_window[sid] = pnl
                else:
                    last_in_window[sid] = pnl
        for sid, pnl in last_in_window.items():
            delta = pnl - base_before_window.get(sid, 0.0)
            if delta:
                sys_name = sys_by_sid[sid]
                out[sys_name] = out.get(sys_name, 0.0) + delta
    except OSError:
        pass
    return out


def flush_digest(force: bool = False) -> int:
    """Invia la mail di riepilogo con tutti i segnali accodati. Ritorna quanti."""
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASSWORD", "")
    if not user or not pwd:
        log.warning("[digest] SMTP_USER/SMTP_PASSWORD mancanti — coda lasciata intatta")
        return 0

    entries = _read_and_clear_queue()
    if not entries and not force:
        log.info("[digest] coda vuota — nessun riepilogo da inviare")
        return 0

    by_sys: dict[str, list[dict]] = {}
    for e in entries:
        by_sys.setdefault(e.get("system", "?"), []).append(e)

    counts = " · ".join(f"{_SYS_LABEL.get(s, s)} {len(v)}" for s, v in sorted(by_sys.items()))
    now_s  = datetime.now().strftime("%d/%m %H:%M")
    subject = (f"📊 Riepilogo segnali {now_s} — {len(entries)} segnali"
               + (f" ({counts})" if counts else ""))

    parts = [
        "<html><body style='font-family:Arial,sans-serif'>",
        f"<h1 style='font-size:18px'>📊 Riepilogo segnali — {now_s}</h1>",
    ]
    pnl = _pnl_24h_by_system()
    if pnl:
        tot = sum(pnl.values())
        rows = "".join(
            f"<tr><td style='padding:2px 10px'>{_SYS_LABEL.get(s, s)}</td>"
            f"<td style='padding:2px 10px;text-align:right'>{v:+.2f}€</td></tr>"
            for s, v in sorted(pnl.items(), key=lambda kv: -kv[1])
        )
        parts.append(
            "<h2 style='font-size:15px'>P&amp;L ultime 24h</h2>"
            f"<table style='border-collapse:collapse'>{rows}"
            f"<tr><td style='padding:2px 10px'><b>Totale</b></td>"
            f"<td style='padding:2px 10px;text-align:right'><b>{tot:+.2f}€</b></td></tr></table>"
        )
    if not entries:
        parts.append("<p><i>Nessun nuovo segnale nel periodo.</i></p>")
    for system in sorted(by_sys):
        items = by_sys[system]
        parts.append(f"<h2 style='font-size:16px;border-bottom:1px solid #ccc'>"
                     f"{_SYS_LABEL.get(system, system)} ({len(items)})</h2>")
        for e in items:
            ts_short = (e.get("ts") or "")[11:16]
            parts.append(f"<h3 style='font-size:14px;margin-bottom:4px'>"
                         f"[{ts_short}] {e.get('subject', '')}</h3>")
            parts.append(f"<div>{_extract_body(e.get('html', ''))}</div><hr>")
    parts.append("</body></html>")
    html = "\n".join(parts)

    try:
        import smtplib
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = os.environ.get("SMTP_FROM", user)
        msg["To"]      = os.environ.get("SMTP_TO", user)
        msg.attach(MIMEText(html, "html", "utf-8"))
        # Istantanea della dashboard del simulator in allegato (~1MB)
        dash = os.path.join(_HERE, "reports", "sim_report.html")
        if os.path.exists(dash):
            try:
                with open(dash, "rb") as f:
                    att = MIMEApplication(f.read(), _subtype="html")
                fname = f"sim_dashboard_{datetime.now():%Y%m%d_%H%M}.html"
                att.add_header("Content-Disposition", "attachment", filename=fname)
                msg.attach(att)
            except OSError as e:
                log.warning(f"[digest] dashboard non allegata: {e}")
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(user, pwd)
            srv.sendmail(msg["From"], [msg["To"]], msg.as_string())
        log.info(f"[digest] ✅ riepilogo inviato: {len(entries)} segnali ({counts})")
        return len(entries)
    except Exception as e:
        log.error(f"[digest] ❌ invio fallito: {e} — riaccodo {len(entries)} segnali")
        for entry in entries:   # non perdere i segnali: torneranno al prossimo giro
            try:
                with _queue_lock, open(QUEUE_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                break
        return 0


def _next_fire(now: datetime) -> datetime:
    for h in _digest_hours():
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    return (now + timedelta(days=1)).replace(
        hour=_digest_hours()[0], minute=0, second=0, microsecond=0)


def digest_loop(stop_event: threading.Event | None = None):
    """Thread daemon: invia il riepilogo agli orari DIGEST_HOURS."""
    log.info(f"[digest] loop attivo — orari: {_digest_hours()} "
             f"(coda: {QUEUE_PATH})")
    while stop_event is None or not stop_event.is_set():
        target = _next_fire(datetime.now())
        while datetime.now() < target:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(30.0, max(1.0, (target - datetime.now()).total_seconds())))
        try:
            flush_digest()
        except Exception as e:
            log.exception(f"[digest] errore flush: {e}")

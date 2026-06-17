"""
==============================================================================
signal_tracker.py  — v4
Tracking segnali con monitoraggio prezzi 4h (8 snapshot × 30 min).

Fix v4:
  - Notifica email HTML quando un token passa i filtri (registra_segnale)
  - EMAIL_CONFIG configurabile in testa al file
  - Supporto Gmail (app password) e qualsiasi SMTP
==============================================================================
"""

import atexit
import csv
import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ==============================================================================
# ── Configurazione Email ───────────────────────────────────────────────────────
# Imposta EMAIL_ENABLED = True e compila le credenziali per ricevere notifiche.
#
# Gmail: vai su https://myaccount.google.com/apppasswords
#        crea una "App password" (non la password normale!) e usala qui.
# ==============================================================================
EMAIL_CONFIG = {
    "EMAIL_ENABLED":    False,           # ← email gestita da defi_optimized.py (evita duplicati)
    "SMTP_HOST":        "smtp.gmail.com",
    "SMTP_PORT":        587,
    "SMTP_USER":        "giagnacovo.luca@gmail.com",  # ← la tua email mittente
    "SMTP_PASSWORD":    os.environ.get("SMTP_PASSWORD", ""),  # ← App Password Gmail (16 char)
    "EMAIL_TO":         "giagnacovo.luca@gmail.com",  # ← dove ricevere (può essere uguale)
    "EMAIL_SUBJECT_PREFIX": "🚨 Crypto Signal",
}

# Path assoluto della directory reports — SEMPRE relativo a questo file.
# Così funziona indipendentemente da dove viene lanciato defi_optimized.py.
_REPORTS_DIR = Path(__file__).parent / "reports"

TRACKER_CONFIG = {
    "REPORT_DIR":             str(_REPORTS_DIR),
    "SIGNALS_CSV":            str(_REPORTS_DIR / "signals_log.csv"),
    "FOLLOWUP_CSV":           str(_REPORTS_DIR / "price_followup.csv"),
    "HTML_REPORT":            str(_REPORTS_DIR / "signal_report.html"),
    "STATE_FILE":             str(_REPORTS_DIR / "tracker_state.json"),
    # Early-dense schedule: snapshot fitti nelle prime 2h poi ogni ora fino a 4h.
    # snap_num 1..10 corrisponde a questi minuti nell'ordine.
    "SNAPSHOT_SCHEDULE_MIN":  [5, 10, 20, 30, 45, 60, 90, 120, 180, 240],
    "SNAPSHOT_INTERVAL_SEC":  1800,   # fallback se SNAPSHOT_SCHEDULE_MIN è vuoto
    "NUM_SNAPSHOTS":          10,     # = len(SNAPSHOT_SCHEDULE_MIN)
    "SCHEDULER_POLL_SEC":     20,
    "PRICE_FETCH_TIMEOUT":    10,
    # Milestone: snap_num 100 = +12h, snap_num 200 = +24h
    "MILESTONE_HOURS":        [12, 24],
    "MILESTONE_SNAP_NUMS":    {12: 100, 24: 200},
    "MILESTONE_MAX_HOURS":    25,   # rimuovi dalla memoria dopo 25h
}

SIGNAL_COLUMNS = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pump_probability", "buy_tax", "sell_tax", "lp_locked", "is_honeypot",
    "top_features",
    # NUOVO 14/06 (PRIORITÀ #4): feature pre-pump come colonne reali, prima
    # disponibili solo dentro la stringa libera top_features (non backtestabili
    # senza parsing). vol_accel/composite/wallet_confluence sono gli input
    # principali dello score; bsr_* sono "sperimentali in raccolta dati"
    # (vedi project_via_gemmeV3_lp_gate_fix_14_06 e report quant 14/06).
    "vol_accel_5m_vs_1h", "prepump_composite_score", "wallet_confluence_score",
    "bsr_5m", "bsr_recent_shift", "bsr_trend_per_min", "bsr_trend_samples",
    "score_top_component",
]

FOLLOWUP_COLUMNS = [
    "signal_id", "token_symbol", "chain", "pair_address",
    "price_entry_usd", "snapshot_num", "timestamp_snapshot",
    "minutes_since_entry", "price_snapshot_usd", "change_pct", "status",
]


# ==============================================================================
# ── Utility ────────────────────────────────────────────────────────────────────

def _safe_float(v, default: float = 0.0) -> float:
    """Converte v in float ignorando valori non numerici (es. timestamp string)."""
    try:
        return float(v or default)
    except (ValueError, TypeError):
        return default


# ── Funzione email ─────────────────────────────────────────────────────────────
# ==============================================================================

def _build_email_html(riga: dict) -> str:
    """Costruisce il corpo HTML della notifica email per un segnale."""
    sym        = riga.get("token_symbol", "?")
    name       = riga.get("token_name", "")
    addr       = riga.get("token_address", "")
    chain      = riga.get("chain", "").upper()
    pair       = riga.get("pair_address", "")
    price      = float(riga.get("price_entry_usd", 0) or 0)
    vol1h      = float(riga.get("volume_1h_usd", 0) or 0)
    liq        = float(riga.get("liquidity_usd", 0) or 0)
    bsr        = float(riga.get("buy_sell_ratio_1h", 0) or 0)
    chg1h      = float(riga.get("change_1h_pct", 0) or 0)
    prob       = float(riga.get("pump_probability", 0) or 0)
    buy_tax    = float(riga.get("buy_tax", 0) or 0)
    sell_tax   = float(riga.get("sell_tax", 0) or 0)
    lp_locked  = riga.get("lp_locked", "0") == "1"
    honeypot   = riga.get("is_honeypot", "0") == "1"
    top_feat   = riga.get("top_features", "")
    ts         = riga.get("timestamp_entry", "")[:19].replace("T", " ")
    sig_id     = riga.get("signal_id", "")

    dex_url = f"https://dexscreener.com/{chain.lower()}/{pair}" if pair else "#"

    prob_color = "#27ae60" if prob >= 0.7 else "#e67e22" if prob >= 0.5 else "#c0392b"
    chg_color  = "#27ae60" if chg1h >= 0 else "#c0392b"
    chg_sign   = "+" if chg1h >= 0 else ""

    lp_badge  = '<span style="background:#27ae60;color:white;padding:2px 8px;border-radius:12px;font-size:12px">🔒 LP Locked</span>' if lp_locked else '<span style="background:#c0392b;color:white;padding:2px 8px;border-radius:12px;font-size:12px">⚠️ LP Unlocked</span>'
    hp_badge  = '<span style="background:#c0392b;color:white;padding:2px 8px;border-radius:12px;font-size:12px">🍯 HONEYPOT</span>' if honeypot else '<span style="background:#27ae60;color:white;padding:2px 8px;border-radius:12px;font-size:12px">✅ Non Honeypot</span>'

    return f"""
<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0d1117; color: #e6edf3; margin: 0; padding: 0; }}
  .wrap {{ max-width: 620px; margin: 0 auto; padding: 24px 16px; }}
  .header {{ background: linear-gradient(135deg, #1f6feb 0%, #388bfd 100%);
             border-radius: 12px 12px 0 0; padding: 24px; text-align: center; }}
  .header h1 {{ margin: 0; font-size: 26px; color: white; }}
  .header p  {{ margin: 6px 0 0; color: rgba(255,255,255,0.8); font-size: 14px; }}
  .body  {{ background: #161b22; border: 1px solid #30363d; border-top: none;
            border-radius: 0 0 12px 12px; padding: 24px; }}
  .prob-badge {{ display: inline-block; background: {prob_color}; color: white;
                 font-size: 22px; font-weight: 700; padding: 8px 20px;
                 border-radius: 50px; margin: 12px 0; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 20px 0; }}
  .card {{ background: #21262d; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }}
  .card .label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .value {{ font-size: 18px; font-weight: 700; margin-top: 4px; }}
  .addr {{ font-family: monospace; font-size: 12px; color: #8b949e; word-break: break-all;
           background: #21262d; border-radius: 6px; padding: 10px; margin: 16px 0; }}
  .badges {{ margin: 16px 0; display: flex; gap: 8px; flex-wrap: wrap; }}
  .feat {{ background: #21262d; border-radius: 8px; padding: 12px; font-size: 13px; color: #8b949e; }}
  .cta {{ display: block; text-align: center; background: #1f6feb; color: white !important;
          text-decoration: none; padding: 14px; border-radius: 8px; font-weight: 700;
          font-size: 16px; margin: 20px 0; }}
  .footer {{ text-align: center; font-size: 11px; color: #484f58; margin-top: 20px; }}
  .disclaimer {{ background: #1c1a00; border: 1px solid #6e5908; border-radius: 8px;
                 padding: 12px; font-size: 12px; color: #e3c84a; margin-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>🚨 Nuovo Segnale Crypto</h1>
    <p>{ts} &nbsp;|&nbsp; ID: {sig_id}</p>
  </div>
  <div class="body">
    <div style="text-align:center">
      <span style="font-size:28px;font-weight:900">{sym}</span>
      {"&nbsp;<span style='color:#8b949e;font-size:16px'>" + name + "</span>" if name else ""}
      &nbsp;<span style="background:#30363d;color:#8b949e;padding:2px 10px;border-radius:12px;font-size:13px">{chain}</span>
      <br>
      <div class="prob-badge">P(pump) = {prob:.1%}</div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">💲 Prezzo Entrata</div>
        <div class="value" style="font-family:monospace">${price:.8f}</div>
      </div>
      <div class="card">
        <div class="label">📈 Variazione 1h</div>
        <div class="value" style="color:{chg_color}">{chg_sign}{chg1h:.2f}%</div>
      </div>
      <div class="card">
        <div class="label">💧 Liquidità</div>
        <div class="value">${liq:,.0f}</div>
      </div>
      <div class="card">
        <div class="label">📊 Volume 1h</div>
        <div class="value">${vol1h:,.0f}</div>
      </div>
      <div class="card">
        <div class="label">⚖️ Buy/Sell Ratio 1h</div>
        <div class="value">{bsr:.2f}</div>
      </div>
      <div class="card">
        <div class="label">🏷️ Tax Buy / Sell</div>
        <div class="value">{buy_tax:.1f}% / {sell_tax:.1f}%</div>
      </div>
    </div>

    <div class="badges">
      {lp_badge}
      {hp_badge}
    </div>

    <div class="addr">
      <strong>Token:</strong> {addr}<br>
      <strong>Pair:</strong>  {pair}
    </div>

    {"<div class='feat'><strong>🔍 Top Features:</strong><br>" + top_feat.replace("|", "<br>") + "</div>" if top_feat else ""}

    <a href="{dex_url}" class="cta">🔗 Apri su DexScreener</a>

    <div class="disclaimer">
      ⚠️ <strong>AVVISO:</strong> Solo a scopo educativo. NON consigli finanziari.
      Il trading crypto comporta rischi elevati di perdita del capitale.
    </div>
  </div>
  <div class="footer">Generato da crypto-signal-bot &nbsp;·&nbsp; {ts}</div>
</div>
</body>
</html>
"""


def send_signal_email(riga: dict) -> bool:
    """
    Invia una notifica email per un segnale appena generato.
    Ritorna True se l'invio ha avuto successo, False altrimenti.
    Non lancia eccezioni (fail silenzioso con log di warning).
    """
    if not EMAIL_CONFIG.get("EMAIL_ENABLED"):
        return False

    sym = riga.get("token_symbol", "?")
    chain = riga.get("chain", "").upper()
    prob  = float(riga.get("pump_probability", 0) or 0)

    subject = (
        f"{EMAIL_CONFIG['EMAIL_SUBJECT_PREFIX']} — "
        f"{sym} ({chain}) · P(pump)={prob:.0%}"
    )

    try:
        import email_digest
        email_digest.queue_email("defi", subject, _build_email_html(riga))
        log.info(f"[email] 📥 {sym} ({chain}) accodata al digest")
        return True
    except ImportError:
        pass   # standalone: invio diretto

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_CONFIG["SMTP_USER"]
    msg["To"]      = EMAIL_CONFIG["EMAIL_TO"]

    # Parte plain text (fallback)
    plain = (
        f"Nuovo segnale: {sym} ({chain})\n"
        f"P(pump): {prob:.1%}\n"
        f"Prezzo: ${float(riga.get('price_entry_usd',0) or 0):.8f}\n"
        f"Liquidità: ${float(riga.get('liquidity_usd',0) or 0):,.0f}\n"
        f"Volume 1h: ${float(riga.get('volume_1h_usd',0) or 0):,.0f}\n"
        f"Buy/Sell ratio: {float(riga.get('buy_sell_ratio_1h',0) or 0):.2f}\n"
        f"Token: {riga.get('token_address','')}\n"
        f"Pair: {riga.get('pair_address','')}\n"
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(_build_email_html(riga), "html"))

    try:
        with smtplib.SMTP(EMAIL_CONFIG["SMTP_HOST"], EMAIL_CONFIG["SMTP_PORT"], timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_CONFIG["SMTP_USER"], EMAIL_CONFIG["SMTP_PASSWORD"])
            smtp.sendmail(
                EMAIL_CONFIG["SMTP_USER"],
                EMAIL_CONFIG["EMAIL_TO"],
                msg.as_string(),
            )
        log.info(f"[email] ✅ Notifica inviata per {sym} ({chain})")
        return True
    except smtplib.SMTPAuthenticationError:
        log.warning("[email] ❌ Autenticazione fallita — controlla SMTP_USER/SMTP_PASSWORD in EMAIL_CONFIG")
    except smtplib.SMTPException as e:
        log.warning(f"[email] ❌ Errore SMTP: {e}")
    except Exception as e:
        log.warning(f"[email] ❌ Errore invio: {e}")
    return False


# ==============================================================================
# ── SignalTracker ──────────────────────────────────────────────────────────────
# ==============================================================================

class SignalTracker:
    def __init__(self):
        self._lock        = threading.Lock()
        self._active_tracks: dict[str, dict] = {}
        self._stop_event  = threading.Event()

        self._ensure_dirs()
        self._ensure_csv_headers()

        # Salva stato automaticamente all'uscita (gestisce Ctrl+C, exit normale, crash)
        atexit.register(self._save_state)

        # Scheduler: daemon=True → non blocca l'uscita del processo
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="signal-scheduler",
        )
        self._scheduler_thread.start()
        log.info("[tracker] Scheduler avviato.")

        # Recovery in background: non blocca l'avvio del bot
        self._recovery_thread = threading.Thread(
            target=self._recover_on_startup,
            daemon=True,
            name="signal-recovery",
        )
        self._recovery_thread.start()

    # ── Setup ──────────────────────────────────────────────────────────────

    def _ensure_dirs(self):
        Path(TRACKER_CONFIG["REPORT_DIR"]).mkdir(parents=True, exist_ok=True)

    def _ensure_csv_headers(self):
        for path, cols in [
            (TRACKER_CONFIG["SIGNALS_CSV"],  SIGNAL_COLUMNS),
            (TRACKER_CONFIG["FOLLOWUP_CSV"], FOLLOWUP_COLUMNS),
        ]:
            p = Path(path)
            if not p.exists():
                with p.open("w", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()
                continue
            # Migrazione schema (14/06): se mancano colonne nuove, riscrive il
            # CSV con l'header aggiornato; le righe esistenti ottengono "" per
            # le colonne nuove (DictReader le restituisce mancanti).
            with p.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_cols = reader.fieldnames or []
                if list(existing_cols) == cols:
                    continue
                rows = list(reader)
            if set(existing_cols) - set(cols):
                # colonne rimosse rispetto allo schema attuale: non tocca il file,
                # evita perdita dati su uno schema non previsto
                log.warning(f"[tracker] {path}: header ha colonne extra non in "
                             f"SIGNAL_COLUMNS/FOLLOWUP_COLUMNS, migrazione saltata")
                continue
            with p.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=cols)
                writer.writeheader()
                for row in rows:
                    writer.writerow({c: row.get(c, "") for c in cols})
            log.info(f"[tracker] {path}: schema migrato ({len(existing_cols)}→{len(cols)} colonne, "
                     f"{len(rows)} righe preservate)")

    # ── Persistenza stato ──────────────────────────────────────────────────

    def _save_state(self):
        state = {}
        with self._lock:
            for sid, meta in self._active_tracks.items():
                ts = meta["timestamp_entry"]
                state[sid] = {
                    "signal_id":         meta["signal_id"],
                    "token_symbol":      meta.get("token_symbol", ""),
                    "token_name":        meta.get("token_name", ""),
                    "chain":             meta.get("chain", ""),
                    "pair_address":      meta.get("pair_address", ""),
                    "token_address":     meta.get("token_address", ""),
                    "price_entry_usd":   meta.get("price_entry_usd", 0),
                    "volume_1h_usd":     meta.get("volume_1h_usd", 0),
                    "liquidity_usd":     meta.get("liquidity_usd", 0),
                    "buy_sell_ratio_1h": meta.get("buy_sell_ratio_1h", 0),
                    "change_1h_pct":     meta.get("change_1h_pct", 0),
                    "pump_probability":  meta.get("pump_probability", 0),
                    "buy_tax":           meta.get("buy_tax", 0),
                    "sell_tax":          meta.get("sell_tax", 0),
                    "lp_locked":         meta.get("lp_locked", "0"),
                    "is_honeypot":       meta.get("is_honeypot", "0"),
                    "top_features":      meta.get("top_features", ""),
                    "timestamp_entry":   ts.isoformat() if isinstance(ts, datetime) else ts,
                    "snapshots_done":    meta.get("snapshots_done", 0),
                }
        try:
            # Scrittura atomica: scrivi su file temporaneo poi rinomina
            # Evita corruzioni se il processo crasha durante la scrittura
            target = Path(TRACKER_CONFIG["STATE_FILE"])
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(target)
            if state:
                log.info(f"[tracker] 💾 Stato salvato ({len(state)} track attive).")
        except Exception as e:
            log.warning(f"[tracker] Errore salvataggio stato: {e}")

    def _load_state(self) -> dict:
        state_file = Path(TRACKER_CONFIG["STATE_FILE"])
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[tracker] Impossibile leggere stato: {e}")
            return {}

    # ── Recovery all'avvio (gira in background, non blocca) ───────────────

    def _load_signals_csv(self) -> dict:
        result = {}
        csv_path = Path(TRACKER_CONFIG["SIGNALS_CSV"])
        if not csv_path.exists():
            return result
        try:
            with csv_path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid = row.get("signal_id", "").strip()
                    if not sid:
                        continue
                    result[sid] = {
                        "signal_id":       sid,
                        "token_symbol":    row.get("token_symbol", ""),
                        "chain":           row.get("chain", ""),
                        "pair_address":    row.get("pair_address", ""),
                        "price_entry_usd": row.get("price_entry_usd", 0),
                        "timestamp_entry": row.get("timestamp_entry", ""),
                    }
        except Exception as e:
            log.warning(f"[tracker] Errore lettura signals_log.csv: {e}")
        return result

    def _recover_on_startup(self):
        saved    = self._load_state()
        csv_sigs = self._load_signals_csv()
        merged = {**csv_sigs, **saved}
        if not merged:
            return

        log.info(f"[tracker] 🔄 Recovery in background: {len(merged)} segnali "
                 f"(JSON:{len(saved)}, CSV:{len(csv_sigs)})...")
        done_map = self._load_done_snapshots()
        now      = datetime.now()
        recovered_total = 0

        for sid, meta in merged.items():
            ts_raw = meta.get("timestamp_entry", "")
            try:
                entry_ts = datetime.fromisoformat(ts_raw)
            except (ValueError, TypeError):
                log.warning(f"[tracker] Timestamp non valido per {sid}, skip.")
                continue

            done_snaps   = done_map.get(sid, set())
            schedule_min = TRACKER_CONFIG.get("SNAPSHOT_SCHEDULE_MIN", [])
            n_tot        = len(schedule_min) if schedule_min else TRACKER_CONFIG["NUM_SNAPSHOTS"]
            interval     = TRACKER_CONFIG["SNAPSHOT_INTERVAL_SEC"]   # fallback
            elapsed_min  = (now - entry_ts).total_seconds() / 60

            def _snap_time_min(sn):
                if schedule_min and sn <= len(schedule_min):
                    return schedule_min[sn - 1]
                return sn * (interval // 60)

            for snap_num in range(1, n_tot + 1):
                if snap_num in done_snaps:
                    continue
                snap_time = entry_ts + timedelta(minutes=_snap_time_min(snap_num))
                if snap_time > now:
                    break
                try:
                    self._recover_snapshot(sid, meta, snap_num, snap_time)
                    recovered_total += 1
                except Exception as _re:
                    log.warning(f"[tracker] Recovery snap {snap_num} per {sid}: {_re} — skip.")

            max_minutes = TRACKER_CONFIG.get("MILESTONE_MAX_HOURS", 25) * 60
            if elapsed_min <= max_minutes:
                snaps_done = len(done_map.get(sid, set())) + sum(
                    1 for sn in range(1, n_tot + 1)
                    if sn not in done_snaps
                    and (entry_ts + timedelta(minutes=_snap_time_min(sn))) <= now
                )
                milestone_snap_map = TRACKER_CONFIG.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})
                milestones_done_set = set()
                for mh, msnap in milestone_snap_map.items():
                    if msnap in done_snaps:
                        milestones_done_set.add(mh)
                meta_obj = {
                    "signal_id":       sid,
                    "token_symbol":    meta.get("token_symbol", ""),
                    "chain":           meta.get("chain", ""),
                    "pair_address":    meta.get("pair_address", ""),
                    "token_address":   meta.get("token_address", ""),
                    "price_entry_usd": _safe_float(meta.get("price_entry_usd", 0)),
                    "timestamp_entry": entry_ts,
                    "snapshots_done":  min(snaps_done, n_tot),
                    "milestones_done": milestones_done_set,
                }
                with self._lock:
                    self._active_tracks[sid] = meta_obj
                log.info(f"[tracker] ▶️  {sid} riattivato "
                         f"({meta_obj['snapshots_done']}/{n_tot} snap)")

        if recovered_total:
            log.info(f"[tracker] ↩️  Recovery completato: {recovered_total} snapshot recuperati.")
        else:
            log.info("[tracker] ℹ️  Recovery: nessun snapshot mancante da recuperare.")

        # Aggiorna sempre l'HTML all'avvio — anche senza nuovi snapshot,
        # così il report riflette i segnali già nel CSV.
        try:
            self.genera_report_html()
            log.info("[tracker] 📄 HTML aggiornato all'avvio.")
        except Exception as e:
            log.warning(f"[tracker] Errore HTML post-recovery: {e}")

    def _recover_snapshot(self, signal_id, meta, snap_num, snap_time):
        entry_price  = _safe_float(meta.get("price_entry_usd", 0))
        schedule_min = TRACKER_CONFIG.get("SNAPSHOT_SCHEDULE_MIN", [])
        if schedule_min and 1 <= snap_num <= len(schedule_min):
            minutes = schedule_min[snap_num - 1]
        else:
            minutes = snap_num * (TRACKER_CONFIG["SNAPSHOT_INTERVAL_SEC"] // 60)

        price, status = self._fetch_historical_price(
            pair_address=meta.get("pair_address", ""),
            chain=meta.get("chain", ""),
            target_ts=snap_time,
            signal_id=signal_id,
        )

        change_pct = ""
        if price is not None and entry_price > 0:
            change_pct = round((price - entry_price) / entry_price * 100, 4)

        row = {
            "signal_id":           signal_id,
            "token_symbol":        meta.get("token_symbol", ""),
            "chain":               meta.get("chain", ""),
            "pair_address":        meta.get("pair_address", ""),
            "price_entry_usd":     entry_price,
            "snapshot_num":        snap_num,
            "timestamp_snapshot":  snap_time.isoformat(),
            "minutes_since_entry": minutes,
            "price_snapshot_usd":  price if price is not None else "",
            "change_pct":          change_pct,
            "status":              status,
        }
        with self._lock:
            with Path(TRACKER_CONFIG["FOLLOWUP_CSV"]).open(
                "a", newline="", encoding="utf-8"
            ) as f:
                csv.DictWriter(f, fieldnames=FOLLOWUP_COLUMNS).writerow(row)

    def _fetch_historical_price(self, pair_address, chain, target_ts, signal_id):
        if not pair_address:
            return None, "missed_no_pair"

        now     = datetime.now()
        age_sec = (now - target_ts).total_seconds()

        if age_sec < 7200:
            price, status = self._fetch_current_price(
            pair_address, chain, signal_id,
            token_address=meta.get("token_address", "") if isinstance(meta, dict) else "",
        )
            if price is not None:
                return price, "recovered_proxy"

        try:
            chain_map = {"solana": "solana", "bsc": "bsc", "ethereum": "ethereum"}
            dex_chain = chain_map.get(chain.lower(), chain.lower())
            from_ts   = int((target_ts - timedelta(minutes=15)).timestamp())
            to_ts     = int((target_ts + timedelta(minutes=15)).timestamp())
            url = (f"https://api.dexscreener.com/latest/dex/pairs"
                   f"/{dex_chain}/{pair_address}/chart")
            resp = requests.get(url, params={"from": from_ts, "to": to_ts, "res": "15"},
                                timeout=TRACKER_CONFIG["PRICE_FETCH_TIMEOUT"],
                                headers={"User-Agent": "crypto-tracker/4.0"})
            if resp.status_code == 200:
                candles = resp.json().get("candles") or []
                if candles:
                    best = min(candles,
                               key=lambda c: abs(c.get("t", 0) - target_ts.timestamp()))
                    price = float(best.get("c", 0))
                    if price > 0:
                        return price, "recovered_historical"
        except Exception:
            pass

        return None, "missed"

    # ── Snapshot già completati ────────────────────────────────────────────

    def _load_done_snapshots(self) -> dict[str, set]:
        done: dict[str, set] = {}
        fu_path = Path(TRACKER_CONFIG["FOLLOWUP_CSV"])
        if not fu_path.exists():
            return done
        with fu_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid  = row.get("signal_id", "")
                snum = int(row.get("snapshot_num", 0) or 0)
                if sid:
                    done.setdefault(sid, set()).add(snum)
        return done

    # ── Validazione indirizzi ──────────────────────────────────────────────

    @staticmethod
    def _is_valid_address(token_address: str, chain: str) -> bool:
        """
        Filtra token con indirizzi palesemente sbagliati per la chain.
        Solana usa base58 (32-44 char alfanumerici senza 0x).
        Ethereum/BSC usano hex 0x + 40 char.
        """
        if not token_address:
            return False
        addr = token_address.strip()
        chain_low = (chain or "").lower()
        # Indirizzo Ethereum-style (0x...) su Solana = sicuramente sbagliato
        if chain_low == "solana" and addr.lower().startswith("0x"):
            return False
        # Indirizzo troppo corto per qualsiasi chain
        if len(addr) < 32:
            return False
        return True

    # ── Registrazione segnale ──────────────────────────────────────────────

    def registra_segnale(self, segnale: dict) -> str:
        ts        = datetime.now()
        signal_id = f"{segnale.get('token_symbol','UNK')}_{ts.strftime('%Y%m%d_%H%M%S')}"

        # Valida l'indirizzo prima di registrare
        token_addr = segnale.get("token_address", "")
        chain      = segnale.get("chain", "")
        if not self._is_valid_address(token_addr, chain):
            log.warning(
                f"[tracker] ⚠️  Indirizzo non valido per chain '{chain}': "
                f"'{token_addr}' — segnale scartato."
            )
            return ""

        riga = {
            "signal_id":         signal_id,
            "timestamp_entry":   ts.isoformat(),
            "token_symbol":      segnale.get("token_symbol", ""),
            "token_name":        segnale.get("token_name", ""),
            "token_address":     segnale.get("token_address", ""),
            "chain":             segnale.get("chain", ""),
            "pair_address":      segnale.get("pair_address", ""),
            "price_entry_usd":   segnale.get("price_usd", 0),
            "volume_1h_usd":     segnale.get("volume_1h_usd", 0),
            "liquidity_usd":     segnale.get("liquidity_usd", 0),
            "buy_sell_ratio_1h": segnale.get("buy_sell_ratio_1h", 0),
            "change_1h_pct":     segnale.get("change_1h_pct", 0),
            "pump_probability":  segnale.get("pump_probability", 0),
            "buy_tax":           segnale.get("buy_tax", 0),
            "sell_tax":          segnale.get("sell_tax", 0),
            "lp_locked":         int(bool(segnale.get("lp_locked", False))),
            "is_honeypot":       int(bool(segnale.get("is_honeypot", False))),
            "top_features":      segnale.get("top_features", ""),
            "vol_accel_5m_vs_1h":      segnale.get("vol_accel_5m_vs_1h", 0),
            "prepump_composite_score": segnale.get("prepump_composite_score", 0),
            "wallet_confluence_score": segnale.get("wallet_confluence_score", 0),
            "bsr_5m":                  segnale.get("bsr_5m", 0),
            "bsr_recent_shift":        segnale.get("bsr_recent_shift", 0),
            "bsr_trend_per_min":       segnale.get("bsr_trend_per_min", 0),
            "bsr_trend_samples":       segnale.get("bsr_trend_samples", 0),
            "score_top_component":     segnale.get("score_top_component", ""),
        }

        with self._lock:
            with Path(TRACKER_CONFIG["SIGNALS_CSV"]).open(
                "a", newline="", encoding="utf-8"
            ) as f:
                csv.DictWriter(f, fieldnames=SIGNAL_COLUMNS).writerow(riga)

        log.info(f"[tracker] ✅ Segnale registrato: {signal_id} | "
                 f"entry=${float(riga['price_entry_usd']):.8f}")

        with self._lock:
            self._active_tracks[signal_id] = {
                "signal_id":         signal_id,
                "token_symbol":      riga["token_symbol"],
                "token_name":        riga.get("token_name", ""),
                "chain":             riga["chain"],
                "pair_address":      riga["pair_address"],
                "token_address":     riga.get("token_address", ""),
                "price_entry_usd":   _safe_float(riga.get("price_entry_usd", 0)),
                "volume_1h_usd":     _safe_float(riga.get("volume_1h_usd", 0)),
                "liquidity_usd":     _safe_float(riga.get("liquidity_usd", 0)),
                "buy_sell_ratio_1h": _safe_float(riga.get("buy_sell_ratio_1h", 0)),
                "change_1h_pct":     _safe_float(riga.get("change_1h_pct", 0)),
                "pump_probability":  _safe_float(riga.get("pump_probability", 0)),
                "buy_tax":           _safe_float(riga.get("buy_tax", 0)),
                "sell_tax":          _safe_float(riga.get("sell_tax", 0)),
                "lp_locked":         riga.get("lp_locked", "0"),
                "is_honeypot":       riga.get("is_honeypot", "0"),
                "top_features":      riga.get("top_features", ""),
                "timestamp_entry":   ts,
                "snapshots_done":    0,
                "milestones_done":   set(),
            }
        self._save_state()

        # ── Aggiorna subito l'HTML report ─────────────────────────────────
        try:
            self.genera_report_html()
        except Exception as e:
            log.warning(f"[tracker] Errore generazione HTML: {e}")

        # ── Notifica email (in thread separato: non blocca il bot) ─────────
        if EMAIL_CONFIG.get("EMAIL_ENABLED"):
            threading.Thread(
                target=send_signal_email,
                args=(riga,),
                daemon=True,
                name=f"email-{signal_id}",
            ).start()

        return signal_id

    # ── Scheduler daemon ───────────────────────────────────────────────────

    def _scheduler_loop(self):
        poll               = TRACKER_CONFIG["SCHEDULER_POLL_SEC"]
        interval           = TRACKER_CONFIG["SNAPSHOT_INTERVAL_SEC"]
        n_tot              = TRACKER_CONFIG["NUM_SNAPSHOTS"]
        schedule_min       = TRACKER_CONFIG.get("SNAPSHOT_SCHEDULE_MIN", [])
        milestone_hours    = TRACKER_CONFIG.get("MILESTONE_HOURS", [12, 24])
        milestone_snap_map = TRACKER_CONFIG.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})
        max_active_hours   = TRACKER_CONFIG.get("MILESTONE_MAX_HOURS", 25)

        while not self._stop_event.is_set():
            now = datetime.now()

            with self._lock:
                track_list = list(self._active_tracks.items())

            for signal_id, meta in track_list:
                entry_ts          = meta["timestamp_entry"]
                elapsed_sec       = (now - entry_ts).total_seconds()
                elapsed_min       = elapsed_sec / 60
                hours_since_entry = elapsed_sec / 3600
                snaps_done        = meta.get("snapshots_done", 0)
                milestones_done   = meta.get("milestones_done", set())

                # Rimuovi solo dopo MILESTONE_MAX_HOURS (25h)
                if hours_since_entry > max_active_hours:
                    with self._lock:
                        self._active_tracks.pop(signal_id, None)
                    log.info(f"[tracker] 🏁 {signal_id} scaduto (>{max_active_hours}h).")
                    self._save_state()
                    continue

                # ── Snapshot regolari (early-dense schedule o fallback 30min) ──
                if snaps_done < n_tot:
                    next_snap = snaps_done + 1
                    # Calcola il minuto target per il prossimo snapshot
                    if schedule_min and 1 <= next_snap <= len(schedule_min):
                        target_min = schedule_min[next_snap - 1]
                        due = elapsed_min >= target_min
                    else:
                        due = elapsed_sec >= next_snap * interval

                    if due:
                        self._take_snapshot(signal_id, meta, next_snap)
                        meta["snapshots_done"] = next_snap
                        self._save_state()
                        try:
                            self.genera_report_html()
                        except Exception as e:
                            log.warning(f"[tracker] Errore HTML: {e}")

                # ── Milestone +12h e +24h ──────────────────────────────────
                for mh in milestone_hours:
                    if mh in milestones_done:
                        continue
                    if hours_since_entry >= mh:
                        snap_num = milestone_snap_map[mh]
                        self._take_snapshot(signal_id, meta, snap_num)
                        milestones_done = milestones_done | {mh}
                        meta["milestones_done"] = milestones_done
                        with self._lock:
                            if signal_id in self._active_tracks:
                                self._active_tracks[signal_id]["milestones_done"] = milestones_done
                        log.info(f"[tracker] ⏱️  {meta['token_symbol']} milestone +{mh}h completata.")
                        self._save_state()
                        try:
                            self.genera_report_html()
                        except Exception as e:
                            log.warning(f"[tracker] Errore HTML milestone: {e}")

            time.sleep(poll)

        log.info("[tracker] Scheduler fermato.")

    def _take_snapshot(self, signal_id: str, meta: dict, snap_num: int):
        entry_ts    = meta["timestamp_entry"]
        entry_price = meta["price_entry_usd"]
        now         = datetime.now()
        minutes     = round((now - entry_ts).total_seconds() / 60)
        n_tot       = TRACKER_CONFIG["NUM_SNAPSHOTS"]

        price, status = self._fetch_current_price(
            pair_address=meta.get("pair_address", ""),
            chain=meta.get("chain", ""),
            signal_id=signal_id,
            token_address=meta.get("token_address", ""),
        )

        change_pct = ""
        if price is not None and entry_price > 0:
            change_pct = round((price - entry_price) / entry_price * 100, 4)

        row = {
            "signal_id":           signal_id,
            "token_symbol":        meta["token_symbol"],
            "chain":               meta["chain"],
            "pair_address":        meta["pair_address"],
            "price_entry_usd":     entry_price,
            "snapshot_num":        snap_num,
            "timestamp_snapshot":  now.isoformat(),
            "minutes_since_entry": minutes,
            "price_snapshot_usd":  price if price is not None else "",
            "change_pct":          change_pct,
            "status":              status,
        }

        with self._lock:
            with Path(TRACKER_CONFIG["FOLLOWUP_CSV"]).open(
                "a", newline="", encoding="utf-8"
            ) as f:
                csv.DictWriter(f, fieldnames=FOLLOWUP_COLUMNS).writerow(row)

        if change_pct != "":
            emoji = "📈" if float(change_pct) >= 0 else "📉"
            log.info(f"[tracker] {emoji} {signal_id} | snap {snap_num}/{n_tot} | "
                     f"+{minutes}min | ${price:.8f} | Δ={float(change_pct):+.2f}%")
        else:
            log.info(f"[tracker] ⚠️  {signal_id} | snap {snap_num}/{n_tot} | "
                     f"fetch fallito ({status})")

    # ── Fetch prezzo ───────────────────────────────────────────────────────

    def _fetch_current_price(self, pair_address, chain, signal_id,
                              token_address: str = ""):
        """
        Fetcha il prezzo corrente via DexScreener.
        Tentativo 1: pair_address endpoint (veloce, preciso).
        Tentativo 2: token_address endpoint (fallback se pair non trovata o migrata).
        """
        chain_map = {"solana": "solana", "bsc": "bsc", "ethereum": "ethereum"}
        dex_chain = chain_map.get((chain or "").lower(), (chain or "").lower())
        timeout   = TRACKER_CONFIG["PRICE_FETCH_TIMEOUT"]
        headers   = {"User-Agent": "crypto-tracker/4.0"}

        # ── Tentativo 1: pair_address ─────────────────────────────────────
        if pair_address:
            try:
                url  = f"https://api.dexscreener.com/latest/dex/pairs/{dex_chain}/{pair_address}"
                resp = requests.get(url, timeout=timeout, headers=headers)
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    if pairs:
                        try:
                            price = float(pairs[0].get("priceUsd", "") or "")
                            if price > 0:
                                return price, "ok"
                        except (TypeError, ValueError):
                            return None, "price_parse_error"
                    # pair_not_found → prova fallback token_address
                elif resp.status_code == 429:
                    log.warning(f"[tracker] Rate limit DexScreener ({signal_id})")
                    return None, "rate_limit"
                elif resp.status_code != 200:
                    return None, f"http_{resp.status_code}"
            except requests.exceptions.Timeout:
                return None, "timeout"
            except requests.exceptions.RequestException:
                return None, "network_error"

        # ── Tentativo 2: token_address fallback ───────────────────────────
        # Scatta quando la pair_address non è più valida (token migrato / rimosso).
        if token_address:
            try:
                url2  = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                resp2 = requests.get(url2, timeout=timeout, headers=headers)
                if resp2.status_code == 200:
                    pairs2 = resp2.json().get("pairs") or []
                    # Filtra per chain, poi prendi il pair con più liquidità
                    chain_pairs = [
                        p for p in pairs2
                        if p.get("chainId", "").lower() == dex_chain
                    ]
                    if chain_pairs:
                        best = max(
                            chain_pairs,
                            key=lambda p: float(
                                (p.get("liquidity") or {}).get("usd", 0) or 0
                            ),
                        )
                        try:
                            price = float(best.get("priceUsd", "") or "")
                            if price > 0:
                                log.info(
                                    f"[tracker] 🔄 {signal_id}: prezzo via token_address"
                                    f" (pair={best.get('pairAddress','')})"
                                )
                                return price, "ok_token_fallback"
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass

        if not pair_address and not token_address:
            return None, "no_pair_address"
        return None, "pair_not_found"

    # ── Stop ──────────────────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()
        self._save_state()
        log.info("[tracker] Stop richiesto. Stato salvato.")

    # ── Generazione report HTML ────────────────────────────────────────────

    def genera_report_html(self) -> str:
        signals: dict[str, dict] = {}
        sig_path = Path(TRACKER_CONFIG["SIGNALS_CSV"])
        if sig_path.exists():
            with sig_path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    signals[row["signal_id"]] = row

        # Merge con _active_tracks: segnali attivi non ancora nel CSV (solo JSON state)
        with self._lock:
            for sid, track in self._active_tracks.items():
                if sid not in signals:
                    ts = track.get("timestamp_entry", "")
                    if hasattr(ts, "isoformat"):
                        ts = ts.isoformat()
                    signals[sid] = {
                        "signal_id":         sid,
                        "token_symbol":      track.get("token_symbol", ""),
                        "token_name":        track.get("token_name", ""),
                        "chain":             track.get("chain", ""),
                        "pair_address":      track.get("pair_address", ""),
                        "token_address":     track.get("token_address", ""),
                        "price_entry_usd":   str(track.get("price_entry_usd", 0)),
                        "volume_1h_usd":     str(track.get("volume_1h_usd", 0)),
                        "liquidity_usd":     str(track.get("liquidity_usd", 0)),
                        "buy_sell_ratio_1h": str(track.get("buy_sell_ratio_1h", 0)),
                        "change_1h_pct":     str(track.get("change_1h_pct", 0)),
                        "pump_probability":  str(track.get("pump_probability", 0)),
                        "buy_tax":           str(track.get("buy_tax", 0)),
                        "sell_tax":          str(track.get("sell_tax", 0)),
                        "lp_locked":         str(track.get("lp_locked", "0")),
                        "is_honeypot":       str(track.get("is_honeypot", "0")),
                        "top_features":      track.get("top_features", ""),
                        "timestamp_entry":   str(ts),
                    }

        followups: dict[str, dict[int, dict]] = {}
        fu_path = Path(TRACKER_CONFIG["FOLLOWUP_CSV"])
        if fu_path.exists():
            with fu_path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid  = row["signal_id"]
                    snum = int(row.get("snapshot_num", 0) or 0)
                    followups.setdefault(sid, {})[snum] = row

        # Self-healing: aggiungi segnali presenti nel followup ma assenti nel CSV
        # (es. crash durante registrazione, state corrotto, riavvio)
        for sid in followups:
            if sid not in signals:
                # Ricostruisci metadata dal signal_id e dal primo followup
                first_fu = followups[sid].get(min(followups[sid].keys()), {})
                sym   = first_fu.get("token_symbol", sid.split("_")[0])
                chain = first_fu.get("chain", "")
                pair  = first_fu.get("pair_address", "")
                price = first_fu.get("price_entry_usd", "0")
                # Timestamp dal signal_id: SYM_YYYYMMDD_HHMMSS
                parts = sid.rsplit("_", 2)
                try:
                    ts_str = f"{parts[-2][:4]}-{parts[-2][4:6]}-{parts[-2][6:8]}T{parts[-1][:2]}:{parts[-1][2:4]}:{parts[-1][4:6]}"
                except Exception:
                    ts_str = ""
                signals[sid] = {
                    "signal_id":         sid,
                    "token_symbol":      sym,
                    "token_name":        sym,
                    "chain":             chain,
                    "pair_address":      pair,
                    "token_address":     "",
                    "price_entry_usd":   price,
                    "volume_1h_usd":     "0",
                    "liquidity_usd":     "0",
                    "buy_sell_ratio_1h": "0",
                    "change_1h_pct":     "0",
                    "pump_probability":  "0",
                    "buy_tax":           "0",
                    "sell_tax":          "0",
                    "lp_locked":         "0",
                    "is_honeypot":       "0",
                    "top_features":      "(recuperato da followup)",
                    "timestamp_entry":   ts_str,
                }

        n_snapshots       = TRACKER_CONFIG["NUM_SNAPSHOTS"]
        interval_min      = TRACKER_CONFIG["SNAPSHOT_INTERVAL_SEC"] // 60
        milestone_hours   = TRACKER_CONFIG.get("MILESTONE_HOURS", [12, 24])
        milestone_snap_map = TRACKER_CONFIG.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})

        def _sf(v, default=0.0):
            """safe float — ignora valori non numerici (es. timestamp nel campo prezzo)."""
            try:
                return float(v or default)
            except (ValueError, TypeError):
                return default

        # Ordina per timestamp_entry DECRESCENTE (più recente in cima)
        def _parse_ts(sig):
            try:
                return datetime.fromisoformat(sig.get("timestamp_entry", "1970-01-01"))
            except Exception:
                return datetime.min

        sorted_signals = sorted(signals.items(), key=lambda kv: _parse_ts(kv[1]), reverse=True)

        tbody_rows = []
        active_ids = set(self._active_tracks.keys())
        live_st    = ("ok", "recovered_proxy", "recovered_historical",
                      "ok_low_liq", "ok_token_fallback")
        sched_min  = TRACKER_CONFIG.get("SNAPSHOT_SCHEDULE_MIN", [])

        for idx, (sid, sig) in enumerate(sorted_signals):
          try:
            entry_price = _sf(sig.get("price_entry_usd", 0))
            pump_prob   = _sf(sig.get("pump_probability", 0))
            ts_entry    = sig.get("timestamp_entry", "")[:16].replace("T", " ")
            token_addr  = sig.get("token_address", "") or ""
            pair_addr   = sig.get("pair_address",  "") or ""
            chain_str   = sig.get("chain", "").upper()
            chain_low   = sig.get("chain", "").lower()
            sym         = sig.get("token_symbol", "")
            vol_1h      = _sf(sig.get("volume_1h_usd",  0))
            liq         = _sf(sig.get("liquidity_usd",  0))
            bsr         = _sf(sig.get("buy_sell_ratio_1h", 0))
            chg1h       = _sf(sig.get("change_1h_pct",  0))
            btax        = _sf(sig.get("buy_tax",  0))
            stax        = _sf(sig.get("sell_tax", 0))
            dex_url     = (f"https://dexscreener.com/{chain_low}/{pair_addr}"
                           if pair_addr else "#")
            try:
                gem_entry_ts = datetime.fromisoformat(sig.get("timestamp_entry",""))
            except Exception:
                gem_entry_ts = None

            # ── Snapshot pills ─────────────────────────────────────────
            snap_pills = ""
            for sn in range(1, n_snapshots + 1):
                fu   = followups.get(sid, {}).get(sn)
                slbl = (f"+{sched_min[sn-1]}m" if sn <= len(sched_min)
                        else f"+snap{sn}")
                if fu:
                    chg_raw = fu.get("change_pct", "")
                    status  = fu.get("status", "")
                    if chg_raw != "" and status in live_st:
                        chg  = _sf(chg_raw)
                        sign = "+" if chg >= 0 else ""
                        cls  = "pill-pos" if chg >= 0 else "pill-neg"
                        snap_pills += (f'<span class="{cls}"'
                                       f' title="{slbl}: {sign}{chg:.2f}%">'
                                       f'{sign}{chg:.1f}%</span>')
                    elif status == "missed":
                        snap_pills += (f'<span class="pill-miss"'
                                       f' title="{slbl}: offline">-</span>')
                    else:
                        snap_pills += (f'<span class="pill-warn"'
                                       f' title="{slbl}: {status}">?</span>')
                else:
                    snap_pills += f'<span class="pill-wait" title="{slbl}">·</span>'

            # ── Snapshot rows for detail panel ──────────────────────────
            det_rows = ""
            for sn in range(1, n_snapshots + 1):
                fu   = followups.get(sid, {}).get(sn)
                slbl = (f"+{sched_min[sn-1]}m" if sn <= len(sched_min)
                        else f"+snap{sn}")
                if fu:
                    cr  = fu.get("change_pct", "")
                    pr  = fu.get("price_snapshot_usd", "")
                    st  = fu.get("status", "")
                    tss = fu.get("timestamp_snapshot","")[:16].replace("T"," ")
                    if cr != "" and st in live_st:
                        chg  = _sf(cr); pv = _sf(pr)
                        sign = "+" if chg >= 0 else ""
                        dc   = "d-pos" if chg >= 0 else "d-neg"
                        det_rows += (f'<tr><td class="d-lbl">{slbl}</td>'
                                     f'<td class="d-ts">{tss}</td>'
                                     f'<td class="d-price">${pv:.8f}</td>'
                                     f'<td class="{dc}">{sign}{chg:.2f}%</td>'
                                     f'<td class="d-st">{st}</td></tr>')
                    else:
                        det_rows += (f'<tr><td class="d-lbl">{slbl}</td>'
                                     f'<td colspan="4" class="d-st">{st}</td></tr>')
                else:
                    det_rows += (f'<tr><td class="d-lbl">{slbl}</td>'
                                 f'<td colspan="4" style="color:#484f58">'
                                 f'in attesa</td></tr>')
            for mh in milestone_hours:
                snap_num = milestone_snap_map[mh]
                fu = followups.get(sid, {}).get(snap_num)
                slbl = f"+{mh}h"
                if fu:
                    cr  = fu.get("change_pct", "")
                    pr  = fu.get("price_snapshot_usd", "")
                    st  = fu.get("status", "")
                    tss = fu.get("timestamp_snapshot","")[:16].replace("T"," ")
                    if cr != "" and st in live_st:
                        chg = _sf(cr); pv = _sf(pr)
                        sign = "+" if chg >= 0 else ""
                        dc   = "d-pos" if chg >= 0 else "d-neg"
                        det_rows += (f'<tr><td class="d-lbl ms-lbl">{slbl}</td>'
                                     f'<td class="d-ts">{tss}</td>'
                                     f'<td class="d-price">${pv:.8f}</td>'
                                     f'<td class="{dc}">{sign}{chg:.2f}%</td>'
                                     f'<td class="d-st">{st}</td></tr>')
                    else:
                        det_rows += (f'<tr><td class="d-lbl ms-lbl">{slbl}</td>'
                                     f'<td colspan="4" class="d-st">{st}</td></tr>')

            # ── Milestone cells ─────────────────────────────────────────
            milestone_cells = ""
            for mh in milestone_hours:
                snap_num = milestone_snap_map[mh]
                fu = followups.get(sid, {}).get(snap_num)
                if fu:
                    cr = fu.get("change_pct", ""); st = fu.get("status", "")
                    if cr != "" and st in live_st:
                        chg  = _sf(cr)
                        sign = "+" if chg >= 0 else ""
                        cls  = "ms-pos" if chg >= 0 else "ms-neg"
                        milestone_cells += f'<td class="{cls}">{sign}{chg:.2f}%</td>'
                    elif st == "missed":
                        milestone_cells += '<td class="ms-wait">-</td>'
                    else:
                        milestone_cells += '<td class="ms-wait">?</td>'
                else:
                    eta_str = ""
                    if gem_entry_ts:
                        eta_dt   = gem_entry_ts + timedelta(hours=mh)
                        diff_min = int((eta_dt - datetime.now()).total_seconds() / 60)
                        if diff_min > 0:
                            eta_str = (f"{diff_min//60}h{diff_min%60}m"
                                       if diff_min >= 60 else f"{diff_min}m")
                        else:
                            eta_str = "..."
                    milestone_cells += (f'<td class="ms-wait">'
                                        f'<small>{eta_str}</small></td>')

            # ── Best / Worst ────────────────────────────────────────────
            all_chg = [
                _sf(v.get("change_pct", 0))
                for v in followups.get(sid, {}).values()
                if v.get("change_pct","") != "" and v.get("status","") in live_st
            ]
            best_c  = max(all_chg) if all_chg else None
            worst_c = min(all_chg) if all_chg else None
            best_str = (f'<span class="best">+{best_c:.1f}%</span>'
                        if best_c is not None and best_c > 0
                        else (f'<span style="color:#8b949e">{best_c:.1f}%</span>'
                              if best_c is not None else "—"))
            worst_str = (f'<span class="worst">{worst_c:.1f}%</span>'
                         if worst_c is not None and worst_c < 0
                         else (f'<span style="color:#8b949e">+{worst_c:.1f}%</span>'
                               if worst_c is not None else "—"))

            lp = ("&#128274;" if sig.get("lp_locked") == "1"
                  else '<span style="color:#f85149">&#10007;</span>')
            hp = ('<span style="color:#f85149">&#x1F36F;</span>'
                  if sig.get("is_honeypot") == "1"
                  else '<span style="color:#3fb950">&#10003;</span>')
            prob_color = ("#1f6feb" if pump_prob < 0.7
                          else "#e3b341" if pump_prob < 0.85 else "#3fb950")
            is_newest = idx == 0
            is_active = sid in active_ids
            row_cls   = "main-row newest-row" if is_newest else "main-row"
            new_badge = '<span class="new-badge">NEW</span>' if is_newest else ""
            act_badge = '<span class="act-badge">LIVE</span>' if is_active else ""
            safe_sym  = sym.replace("'", "").replace('"', "")
            safe_sid  = sid.replace("'", "")

            # ── Detail panel ────────────────────────────────────────────
            cp_js = f"navigator.clipboard.writeText('{token_addr}');event.stopPropagation()"
            detail_panel = (
                '<div class="detail-panel">'
                '<div class="det-meta">'
                f'<span><b>Token</b> {sym} {sig.get("token_name","")}</span>'
                f'<span><b>Chain</b> {chain_str}</span>'
                f'<span><b>Entry</b> ${entry_price:.8f}</span>'
                f'<span><b>Vol 1h</b> ${vol_1h:,.0f}</span>'
                f'<span><b>Liq</b> ${liq:,.0f}</span>'
                f'<span><b>BSR</b> {bsr:.2f}</span>'
                f'<span><b>Chg 1h</b> {chg1h:+.2f}%</span>'
                f'<span><b>Tax</b> {btax:.1f}% / {stax:.1f}%</span>'
                '</div>'
                f'<div class="det-addr"><b>Addr:</b> <code>{token_addr}</code>'
                f' <button class="copy-btn" onclick="{cp_js}">copy</button></div>'
                f'<div class="det-addr"><b>Pair:</b> <code>{pair_addr}</code></div>'
                f'<a class="dex-link" href="{dex_url}" target="_blank"'
                f' onclick="event.stopPropagation()">DexScreener &#8599;</a>'
                '<table class="det-table">'
                '<thead><tr><th>Snap</th><th>Timestamp</th>'
                '<th>Prezzo</th><th>Change</th><th>Status</th></tr></thead>'
                f'<tbody>{det_rows}</tbody></table>'
                '</div>'
            )

            tbody_rows.append(
                f'<tr class="{row_cls}" onclick="toggleDetail(\'{safe_sid}\')"'
                f' data-chain="{chain_low}" data-active="{1 if is_active else 0}"'
                f' data-sym="{safe_sym}" data-sid="{safe_sid}">'
                f'<td class="td-date">{ts_entry}</td>'
                f'<td class="td-token"><span class="sym">{sym}</span>'
                f'{act_badge}{new_badge}<br>'
                f'<span class="chain-tag">{chain_str}</span></td>'
                f'<td class="td-nums"><span class="lbl2">V</span> ${vol_1h:,.0f}<br>'
                f'<span class="lbl2">L</span> ${liq:,.0f}</td>'
                f'<td class="td-price">${entry_price:.8f}</td>'
                f'<td style="text-align:center">'
                f'<span class="prob" style="background:{prob_color}">'
                f'{pump_prob:.0%}</span></td>'
                f'<td style="text-align:center;font-size:.9rem">{lp}&nbsp;{hp}</td>'
                f'<td class="td-snaps"><div class="snaps-bar">{snap_pills}</div></td>'
                f'{milestone_cells}'
                f'<td style="text-align:center">{best_str}</td>'
                f'<td style="text-align:center">{worst_str}</td>'
                f'</tr>'
                f'<tr class="detail-row" id="detail-{safe_sid}" style="display:none">'
                f'<td colspan="10" style="padding:0">{detail_panel}</td>'
                f'</tr>'
            )
          except Exception as _row_e:
            log.debug(f"[tracker] riga {sid} saltata nel report HTML: {_row_e}")
            continue

        # ── Stats globali ───────────────────────────────────────────────────
        live_st2 = ("ok","recovered_proxy","recovered_historical",
                    "ok_low_liq","ok_token_fallback")
        all_bests, n_tp1 = [], 0
        for sid, _ in sorted_signals:
            chgs = [_sf(v.get("change_pct",0))
                    for v in followups.get(sid,{}).values()
                    if v.get("change_pct","") != "" and v.get("status","") in live_st2]
            if chgs:
                b = max(chgs)
                all_bests.append(b)
                if b >= 20:
                    n_tp1 += 1
        nwd = len(all_bests)
        tp1_rate_str = f"{n_tp1/nwd*100:.0f}%" if nwd else "—"
        avg_best_str = f"+{sum(all_bests)/nwd:.1f}%" if nwd else "—"

        milestone_headers = "".join(
            f'<th class="ms-hdr">+{mh}h</th>' for mh in milestone_hours)
        now_str      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n_active     = len(self._active_tracks)
        n_total      = len(signals)
        tbody_html   = "".join(tbody_rows)
        schedule_str = (", ".join(f"+{m}m" for m in sched_min) or "ogni 30min")
        chains_set   = sorted(set(sig.get("chain","").lower()
                               for _, sig in sorted_signals if sig.get("chain","")))
        chain_opts   = "".join(
            f'<option value="{c}">{c.upper()}</option>' for c in chains_set)

        html = (
            '<!DOCTYPE html>\n<html lang="it">\n<head>\n'
            '  <meta charset="UTF-8">\n'
            '  <meta http-equiv="refresh" content="30">\n'
            '  <title>Crypto Signal Tracker</title>\n'
            '  <style>\n'
            '    *{box-sizing:border-box;margin:0;padding:0}\n'
            '    body{font-family:\'Segoe UI\',system-ui,sans-serif;'
            'background:#0d1117;color:#e6edf3;padding:20px 24px;font-size:13px}\n'
            '    a{color:#58a6ff;text-decoration:none}\n'
            '    .hdr{display:flex;align-items:baseline;gap:16px;margin-bottom:6px}\n'
            '    .hdr h1{font-size:1.2rem;font-weight:600}\n'
            '    .meta{font-size:.78rem;color:#8b949e;margin-bottom:14px}\n'
            '    /* Stats bar */\n'
            '    .stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}\n'
            '    .stat{background:#161b22;border:1px solid #30363d;border-radius:6px;'
            'padding:8px 14px;min-width:110px}\n'
            '    .stat .lbl{font-size:.65rem;color:#8b949e;text-transform:uppercase;'
            'letter-spacing:.4px}\n'
            '    .stat .val{font-size:1.2rem;font-weight:700;margin-top:2px}\n'
            '    /* Filter bar */\n'
            '    .filters{display:flex;gap:8px;align-items:center;'
            'flex-wrap:wrap;margin-bottom:14px}\n'
            '    .filters select,.filters input{background:#161b22;'
            'border:1px solid #30363d;border-radius:5px;'
            'color:#e6edf3;font-size:.8rem;padding:5px 9px}\n'
            '    .filters input{width:160px}\n'
            '    .filters label{font-size:.75rem;color:#8b949e}\n'
            '    .btn-grp{display:flex;gap:0}\n'
            '    .btn-grp button{background:#21262d;border:1px solid #30363d;'
            'color:#8b949e;font-size:.75rem;padding:5px 12px;cursor:pointer;transition:.15s}\n'
            '    .btn-grp button:first-child{border-radius:5px 0 0 5px}\n'
            '    .btn-grp button:last-child{border-radius:0 5px 5px 0;margin-left:-1px}\n'
            '    .btn-grp button.active{background:#1f6feb;border-color:#1f6feb;'
            'color:white}\n'
            '    #vis-count{color:#e3b341;font-weight:600}\n'
            '    /* Table */\n'
            '    .wrap{overflow-x:auto;border-radius:8px;border:1px solid #30363d}\n'
            '    table{width:100%;border-collapse:collapse;background:#161b22;'
            'font-size:.8rem;white-space:nowrap}\n'
            '    thead th{background:#21262d;color:#8b949e;font-size:.65rem;'
            'text-transform:uppercase;letter-spacing:.5px;padding:8px 8px;'
            'border-bottom:1px solid #30363d;position:sticky;top:0;z-index:2}\n'
            '    td{padding:6px 8px;border-bottom:1px solid #21262d;'
            'vertical-align:middle}\n'
            '    tr.main-row{cursor:pointer}\n'
            '    tr.main-row:hover td{background:#1c2128}\n'
            '    tr:last-child td{border-bottom:none}\n'
            '    /* Token */\n'
            '    .sym{font-weight:700;font-size:.88rem;letter-spacing:.3px}\n'
            '    .chain-tag{display:inline-block;font-size:.62rem;background:#21262d;'
            'border:1px solid #30363d;border-radius:3px;padding:0 4px;margin-top:2px;'
            'color:#8b949e;text-transform:uppercase}\n'
            '    /* Nums cell */\n'
            '    .td-nums{font-size:.75rem;color:#c9d1d9;line-height:1.6}\n'
            '    .lbl2{font-size:.62rem;color:#484f58;text-transform:uppercase;'
            'margin-right:2px}\n'
            '    /* Price */\n'
            '    .td-price{font-family:monospace;font-size:.72rem;text-align:right}\n'
            '    /* Snapshot pills */\n'
            '    .td-snaps{min-width:200px}\n'
            '    .snaps-bar{display:flex;flex-wrap:wrap;gap:2px;padding:2px 0}\n'
            '    .pill-pos{background:#0d2616;color:#3fb950;border-radius:3px;'
            'padding:1px 4px;font-size:.7rem;font-weight:600;cursor:default}\n'
            '    .pill-neg{background:#2d0f0f;color:#f85149;border-radius:3px;'
            'padding:1px 4px;font-size:.7rem;font-weight:600;cursor:default}\n'
            '    .pill-wait{color:#484f58;font-size:.8rem;padding:1px 3px}\n'
            '    .pill-miss{color:#e67e22;font-size:.75rem;padding:1px 3px}\n'
            '    .pill-warn{color:#6e5908;font-size:.75rem;padding:1px 3px}\n'
            '    /* Milestone */\n'
            '    th.ms-hdr{border-left:2px solid #30363d;color:#e3b341}\n'
            '    .ms-pos{background:#0d2616;color:#3fb950;font-weight:700;'
            'text-align:center;border-left:2px solid #3fb95033}\n'
            '    .ms-neg{background:#2d0f0f;color:#f85149;font-weight:700;'
            'text-align:center;border-left:2px solid #f8514933}\n'
            '    .ms-wait{color:#484f58;text-align:center;'
            'border-left:2px solid #30363d;font-size:.75rem}\n'
            '    /* Prob badge */\n'
            '    .prob{display:inline-block;color:white;padding:2px 7px;'
            'border-radius:10px;font-size:.73rem;font-weight:700}\n'
            '    /* Best / worst */\n'
            '    .best{color:#3fb950;font-weight:600}\n'
            '    .worst{color:#f85149;font-weight:600}\n'
            '    /* NEW / LIVE badges */\n'
            '    .new-badge{display:inline-block;font-size:.58rem;background:#1f6feb;'
            'color:white;padding:1px 5px;border-radius:3px;margin-left:4px;'
            'vertical-align:middle}\n'
            '    .act-badge{display:inline-block;font-size:.58rem;background:#3fb950;'
            'color:#0d1117;padding:1px 5px;border-radius:3px;margin-left:4px;'
            'vertical-align:middle;font-weight:700}\n'
            '    tr.newest-row td{border-left:2px solid #1f6feb}\n'
            '    tr.newest-row td:first-child{padding-left:6px}\n'
            '    /* Detail panel */\n'
            '    .detail-row td{background:#0d1117;padding:0!important}\n'
            '    .detail-panel{padding:14px 20px;border-top:1px solid #30363d}\n'
            '    .det-meta{display:flex;flex-wrap:wrap;gap:14px;'
            'font-size:.78rem;margin-bottom:10px;color:#c9d1d9}\n'
            '    .det-meta span b{color:#8b949e;font-weight:500;margin-right:3px}\n'
            '    .det-addr{font-size:.72rem;color:#8b949e;margin-bottom:6px;'
            'font-family:monospace;word-break:break-all}\n'
            '    .det-addr b{font-family:sans-serif;color:#c9d1d9}\n'
            '    .dex-link{display:inline-block;margin:8px 0;background:#1f6feb;'
            'color:white;padding:4px 12px;border-radius:5px;font-size:.78rem;'
            'font-weight:600}\n'
            '    .copy-btn{background:none;border:1px solid #30363d;border-radius:3px;'
            'color:#8b949e;cursor:pointer;font-size:.65rem;padding:1px 5px;'
            'margin-left:6px;transition:.15s}\n'
            '    .copy-btn:hover{border-color:#8b949e;color:#e6edf3}\n'
            '    /* Snapshot detail table */\n'
            '    .det-table{margin-top:10px;border-collapse:collapse;'
            'font-size:.75rem;width:auto}\n'
            '    .det-table th{background:#21262d;color:#8b949e;'
            'font-size:.65rem;text-transform:uppercase;'
            'padding:5px 10px;border-bottom:1px solid #30363d}\n'
            '    .det-table td{padding:4px 10px;border-bottom:1px solid #21262d}\n'
            '    .d-lbl{font-weight:600;color:#e3b341;white-space:nowrap}\n'
            '    .ms-lbl{color:#58a6ff!important}\n'
            '    .d-ts{color:#8b949e}\n'
            '    .d-price{font-family:monospace}\n'
            '    .d-pos{color:#3fb950;font-weight:700}\n'
            '    .d-neg{color:#f85149;font-weight:700}\n'
            '    .d-st{color:#484f58;font-size:.7rem}\n'
            '    /* Disclaimer */\n'
            '    .disclaimer{background:#161005;border:1px solid #6e5908;'
            'border-radius:6px;padding:10px 14px;margin-top:18px;'
            'font-size:.72rem;color:#b39a2e}\n'
            '  </style>\n'
            '</head>\n'
            '<body>\n'
            '  <div style="max-width:2400px;margin:0 auto">\n'
            f'    <div class="hdr"><h1>&#128202; Crypto Signal Tracker</h1>'
            f'<span style="font-size:.75rem;color:#8b949e">— {now_str} · refresh 30s</span></div>\n'
            f'    <div class="meta">Schedule: {schedule_str} &nbsp;|&nbsp; Milestone: +12h +24h</div>\n'
            '    <div class="stats">\n'
            f'      <div class="stat"><div class="lbl">Totali</div>'
            f'<div class="val">{n_total}</div></div>\n'
            f'      <div class="stat"><div class="lbl">Attivi</div>'
            f'<div class="val" style="color:#3fb950">{n_active}</div></div>\n'
            f'      <div class="stat"><div class="lbl">Completati</div>'
            f'<div class="val" style="color:#8b949e">{n_total - n_active}</div></div>\n'
            f'      <div class="stat"><div class="lbl">Hit &#x2265;+20%</div>'
            f'<div class="val" style="color:#e3b341">{tp1_rate_str}</div></div>\n'
            f'      <div class="stat"><div class="lbl">Avg Best</div>'
            f'<div class="val" style="color:#3fb950">{avg_best_str}</div></div>\n'
            '    </div>\n'
            '    <div class="filters">\n'
            '      <span class="lbl2">Stato:</span>\n'
            '      <div class="btn-grp">\n'
            '        <button class="active" onclick="setStatus(this,\'\')">'
            'Tutti</button>\n'
            '        <button onclick="setStatus(this,\'1\')">Attivi</button>\n'
            '        <button onclick="setStatus(this,\'0\')">Completati</button>\n'
            '      </div>\n'
            '      <span class="lbl2">Chain:</span>\n'
            f'      <select id="fc" onchange="applyFilter()">'
            f'<option value="">Tutte</option>{chain_opts}</select>\n'
            '      <span class="lbl2">Cerca:</span>\n'
            '      <input id="fq" type="text" placeholder="simbolo..." '
            'oninput="applyFilter()">\n'
            '      <span style="font-size:.75rem;color:#8b949e">'
            'Visibili: <span id="vis-count">'
            f'{n_total}</span></span>\n'
            '    </div>\n'
            '    <div class="wrap">\n'
            '    <table>\n'
            '      <thead><tr>\n'
            '        <th>Data/Ora</th><th>Token</th><th>Vol / Liq</th>\n'
            '        <th style="text-align:right">Entry $</th>\n'
            '        <th style="text-align:center">P(pump)</th>\n'
            '        <th style="text-align:center">LP&#183;HP</th>\n'
            '        <th>Snapshots (hover = valore)</th>\n'
            f'        {milestone_headers}\n'
            '        <th style="text-align:center">Best</th>\n'
            '        <th style="text-align:center">Worst</th>\n'
            '      </tr></thead>\n'
            f'      <tbody id="tbody">{tbody_html}</tbody>\n'
            '    </table>\n'
            '    </div>\n'
            '  </div>\n'
            '  <div class="disclaimer">\n'
            '    <strong>Solo a scopo educativo.</strong> '
            'Non costituisce consulenza finanziaria. '
            'Il trading crypto comporta rischi elevati di perdita del capitale.\n'
            '  </div>\n'
            '  <script>\n'
            '    var _statusFilter = "";\n'
            '    function setStatus(btn, val) {\n'
            '      _statusFilter = val;\n'
            '      document.querySelectorAll(".btn-grp button")'
            '.forEach(function(b){b.classList.remove("active")});\n'
            '      btn.classList.add("active");\n'
            '      applyFilter();\n'
            '    }\n'
            '    function applyFilter() {\n'
            '      var chain = document.getElementById("fc").value;\n'
            '      var q = document.getElementById("fq").value.toUpperCase().trim();\n'
            '      var vis = 0;\n'
            '      document.querySelectorAll("tr.main-row").forEach(function(row) {\n'
            '        var mc = !chain || row.dataset.chain === chain;\n'
            '        var ms = !_statusFilter || row.dataset.active === _statusFilter;\n'
            '        var mq = !q || row.dataset.sym.includes(q);\n'
            '        var show = mc && ms && mq;\n'
            '        row.style.display = show ? "" : "none";\n'
            '        var det = document.getElementById("detail-" + row.dataset.sid);\n'
            '        if (det && !show) det.style.display = "none";\n'
            '        if (show) vis++;\n'
            '      });\n'
            '      document.getElementById("vis-count").textContent = vis;\n'
            '    }\n'
            '    function toggleDetail(sid) {\n'
            '      var row = document.getElementById("detail-" + sid);\n'
            '      if (row) row.style.display = (row.style.display === "none") ? "" : "none";\n'
            '    }\n'
            '  </script>\n'
            '</body></html>\n'
        )

        out = Path(TRACKER_CONFIG["HTML_REPORT"])
        out.write_text(html, encoding="utf-8")
        log.info(f"[tracker] Report aggiornato: {out} ({n_total} segnali, {n_active} attivi)")
        return str(out)


# ── Singleton ─────────────────────────────────────────────
_tracker_instance: Optional["SignalTracker"] = None
_tracker_lock = threading.Lock()


def get_tracker() -> "SignalTracker":
    """Ritorna l'istanza singleton del SignalTracker (thread-safe)."""
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_lock:
            if _tracker_instance is None:
                _tracker_instance = SignalTracker()
    return _tracker_instance

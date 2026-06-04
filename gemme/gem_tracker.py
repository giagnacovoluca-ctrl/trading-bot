"""
==============================================================================
gem_tracker.py — Gem Tracker v1
Traccia ogni gemma segnalata con dati dettagliati: prezzi, social, TVL,
smart money inflow — e genera un report HTML ricco per ogni token.

Differenze da signal_tracker:
  • Report per-gemma con tutti i fondamentali
  • Traccia social score e TVL nel tempo (oltre ai prezzi)
  • Snapshot ogni 30min × 8 (4h totali) — stesso schema
  • Recovery automatica da CSV all'avvio
==============================================================================
"""

import atexit
import csv
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

GEM_TRACKER_AVAILABLE = True

# Path assoluti ancorati alla directory di gem_tracker.py — invarianti rispetto al CWD
_HERE = Path(__file__).parent
_REPORTS = _HERE / "reports"

GEM_TRACKER_CONFIG = {
    "REPORT_DIR":            str(_REPORTS),
    "GEMS_CSV":              str(_REPORTS / "gems_log.csv"),
    "FOLLOWUP_CSV":          str(_REPORTS / "gems_followup.csv"),
    "HTML_REPORT":           str(_REPORTS / "gem_report.html"),
    "STATE_FILE":            str(_REPORTS / "gem_tracker_state.json"),
    "SNAPSHOT_INTERVAL_SEC": 7200,
    "NUM_SNAPSHOTS":         5,
    "SCHEDULER_POLL_SEC":    60,
    "PRICE_FETCH_TIMEOUT":   10,
    "MIN_POOL_LIQUIDITY_USD": 50_000,
    "FLAT_TIMEOUT_HOURS":     3,
    "FLAT_THRESHOLD_PCT":     1.5,
    "MILESTONE_HOURS":       [12, 24, 36, 48, 60, 72],
    "MILESTONE_SNAP_NUMS":   {12: 100, 24: 200, 36: 300, 48: 400, 60: 500, 72: 600},
    "MILESTONE_MAX_HOURS":   73,
}

# Config dedicata per gemmeV3 — path separati per evitare conflitti di scrittura
GEM_TRACKER_CONFIG_V3 = {
    "REPORT_DIR":            str(_REPORTS),
    "GEMS_CSV":              str(_REPORTS / "gems_log_v3.csv"),
    "FOLLOWUP_CSV":          str(_REPORTS / "gems_followup_v3.csv"),
    "HTML_REPORT":           str(_REPORTS / "gem_report_v3.html"),
    "STATE_FILE":            str(_REPORTS / "gem_tracker_state_v3.json"),
    "SNAPSHOT_INTERVAL_SEC": 7200,
    "NUM_SNAPSHOTS":         5,
    "SCHEDULER_POLL_SEC":    60,
    "PRICE_FETCH_TIMEOUT":   10,
    "MIN_POOL_LIQUIDITY_USD": 50_000,
    "FLAT_TIMEOUT_HOURS":     11,   # dopo tutti i 5 snapshot (5×2h=10h) — non bloccare prima
    "FLAT_THRESHOLD_PCT":     2.0,  # soglia flat alzata: molti meme token oscillano poco
    "MILESTONE_HOURS":       [12, 24, 36, 48, 60, 72],
    "MILESTONE_SNAP_NUMS":   {12: 100, 24: 200, 36: 300, 48: 400, 60: 500, 72: 600},
    "MILESTONE_MAX_HOURS":   73,
}

GEM_COLUMNS = [
    "gem_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "dex_id",
    "price_entry_usd", "market_cap_usd", "liquidity_usd",
    "volume_1h_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pair_age_hours", "gem_probability",
    # Smart money
    "inflow_usd", "inflow_wallet_count", "avg_wallet_pnl_pct",
    # Social
    "social_score", "social_tweet_count",
    # TVL
    "tvl_usd",
    # Top features ML
    "top_features",
]

FOLLOWUP_COLUMNS = [
    "gem_id", "token_symbol", "chain", "pair_address",
    "price_entry_usd", "snapshot_num", "timestamp_snapshot",
    "minutes_since_entry", "price_snapshot_usd", "change_pct",
    "social_score_snapshot", "tvl_snapshot", "status",
]


class GemTracker:
    """
    Traccia le gemme segnalate con snapshot di prezzo ogni 30min.
    Genera un report HTML dettagliato per ogni gemma.
    """

    def __init__(self, config: dict | None = None):
        self._cfg          = config if config is not None else GEM_TRACKER_CONFIG
        self._lock         = threading.Lock()
        self._active: dict[str, dict] = {}  # gem_id → meta
        self._stop_event   = threading.Event()

        self._ensure_dirs()
        self._ensure_csv_headers()
        atexit.register(self._save_state)

        # Scheduler prezzi
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="gem-scheduler"
        )
        self._scheduler_thread.start()
        log.info("[gem_tracker] Scheduler avviato.")

        # Recovery da CSV
        self._recovery_thread = threading.Thread(
            target=self._recover_on_startup, daemon=True, name="gem-recovery"
        )
        self._recovery_thread.start()

    # ── Setup ──────────────────────────────────────────────────────────────

    def _ensure_dirs(self):
        Path(self._cfg["REPORT_DIR"]).mkdir(parents=True, exist_ok=True)

    def _ensure_csv_headers(self):
        for path, cols in [
            (self._cfg["GEMS_CSV"],     GEM_COLUMNS),
            (self._cfg["FOLLOWUP_CSV"], FOLLOWUP_COLUMNS),
        ]:
            p = Path(path)
            if not p.exists():
                with p.open("w", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()

    # ── Persistenza ────────────────────────────────────────────────────────

    def _save_state(self):
        state = {}
        with self._lock:
            for gid, meta in self._active.items():
                ts = meta["timestamp_entry"]
                state[gid] = {
                    **{k: v for k, v in meta.items() if k != "timestamp_entry"},
                    "timestamp_entry": ts.isoformat() if isinstance(ts, datetime) else ts,
                }
        def _json_default(obj):
            if isinstance(obj, set):
                return list(obj)
            if isinstance(obj, datetime):
                return obj.isoformat()
            return str(obj)

        try:
            # Scrittura atomica: .tmp → rename — evita corruzioni su crash
            target = Path(self._cfg["STATE_FILE"])
            tmp    = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, default=_json_default), encoding="utf-8")
            tmp.replace(target)
        except Exception as e:
            log.warning(f"[gem_tracker] Errore salvataggio stato: {e}")

    def _load_state(self) -> dict:
        p = Path(self._cfg["STATE_FILE"])
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_gems_csv(self) -> dict:
        """Legge gems_log.csv per recovery storica."""
        result = {}
        p = Path(self._cfg["GEMS_CSV"])
        if not p.exists():
            return result
        try:
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gid = row.get("gem_id", "").strip()
                    if gid:
                        result[gid] = row
        except Exception as e:
            log.warning(f"[gem_tracker] Errore lettura gems_log.csv: {e}")
        return result

    def _load_done_snapshots(self) -> dict[str, set]:
        done: dict[str, set] = {}
        p = Path(self._cfg["FOLLOWUP_CSV"])
        if not p.exists():
            return done
        try:
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gid  = row.get("gem_id", "")
                    snum = int(row.get("snapshot_num", 0) or 0)
                    if gid:
                        done.setdefault(gid, set()).add(snum)
        except Exception:
            pass
        return done

    # ── Recovery ───────────────────────────────────────────────────────────

    def _recover_on_startup(self):
        saved    = self._load_state()
        csv_gems = self._load_gems_csv()
        merged   = {**csv_gems, **saved}
        if not merged:
            return

        log.info(f"[gem_tracker] 🔄 Recovery: {len(merged)} gemme "
                 f"(state:{len(saved)}, csv:{len(csv_gems)})...")
        done_map = self._load_done_snapshots()
        now      = datetime.now()
        recovered = 0

        for gid, meta in merged.items():
            # V3 CSV usa "timestamp", V2/state usa "timestamp_entry" — supporta entrambi
            ts_raw = meta.get("timestamp_entry", "") or meta.get("timestamp", "")
            try:
                entry_ts = datetime.fromisoformat(str(ts_raw))
            except (ValueError, TypeError):
                continue

            done_snaps = done_map.get(gid, set())
            interval   = self._cfg["SNAPSHOT_INTERVAL_SEC"]
            n_tot      = self._cfg["NUM_SNAPSHOTS"]

            for snap_num in range(1, n_tot + 1):
                if snap_num in done_snaps:
                    continue
                snap_time = entry_ts + timedelta(seconds=interval * snap_num)
                if snap_time > now:
                    break
                self._recover_snapshot(gid, meta, snap_num, snap_time)
                recovered += 1

            # Rimette in tracking se ancora attivo (fino a MILESTONE_MAX_HOURS)
            elapsed_min = (now - entry_ts).total_seconds() / 60
            max_min     = self._cfg.get("MILESTONE_MAX_HOURS", 25) * 60
            if elapsed_min <= max_min:
                snaps_done = len(done_snaps) + sum(
                    1 for sn in range(1, n_tot + 1)
                    if sn not in done_snaps
                    and (entry_ts + timedelta(seconds=interval * sn)) <= now
                )
                milestone_snap_map = self._cfg.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})
                milestones_done_set = set()
                for mh, msnap in milestone_snap_map.items():
                    if msnap in done_snaps:
                        milestones_done_set.add(mh)
                m = {
                    "gem_id":          gid,
                    "token_symbol":    meta.get("token_symbol", ""),
                    "chain":           meta.get("chain", ""),
                    "pair_address":    meta.get("pair_address", ""),
                    "token_address":   meta.get("token_address", ""),
                    "price_entry_usd": float(meta.get("price_entry_usd", 0) or meta.get("price_usd", 0) or 0),
                    "timestamp_entry": entry_ts,
                    "snapshots_done":  min(snaps_done, n_tot),
                    "milestones_done": milestones_done_set,
                    "social_score":    float(meta.get("social_score", 0) or 0),
                    "tvl_usd":         float(meta.get("tvl_usd", 0) or 0),
                    "inflow_usd":      float(meta.get("inflow_usd", 0) or 0),
                }
                with self._lock:
                    self._active[gid] = m

        if recovered:
            log.info(f"[gem_tracker] ↩️  Recuperati {recovered} snapshot.")
            try:
                self.genera_report_html()
            except Exception:
                pass
        else:
            log.info("[gem_tracker] ℹ️  Recovery: nessun snapshot mancante.")

    def _recover_snapshot(self, gem_id, meta, snap_num, snap_time):
        pair = meta.get("pair_address", "")
        chain = meta.get("chain", "")
        price, status = self._fetch_price(pair, chain, gem_id, snap_time)
        entry_price = float(meta.get("price_entry_usd", 0) or 0)
        change_pct = ""
        if price is not None and entry_price > 0:
            change_pct = round((price - entry_price) / entry_price * 100, 4)
        minutes = snap_num * (self._cfg["SNAPSHOT_INTERVAL_SEC"] // 60)
        row = {
            "gem_id":             gem_id,
            "token_symbol":       meta.get("token_symbol", ""),
            "chain":              chain,
            "pair_address":       pair,
            "price_entry_usd":    entry_price,
            "snapshot_num":       snap_num,
            "timestamp_snapshot": snap_time.isoformat(),
            "minutes_since_entry": minutes,
            "price_snapshot_usd": price if price is not None else "",
            "change_pct":         change_pct,
            "social_score_snapshot": "",
            "tvl_snapshot":       "",
            "status":             status,
        }
        with self._lock:
            with Path(self._cfg["FOLLOWUP_CSV"]).open(
                "a", newline="", encoding="utf-8"
            ) as f:
                csv.DictWriter(f, fieldnames=FOLLOWUP_COLUMNS).writerow(row)

    # ── Registrazione gemma ────────────────────────────────────────────────

    def registra_gemma(self, gem: dict) -> str:
        ts     = datetime.now()
        # Usa gem_id già assegnato da gemmeV3 (save_gem_to_csv), altrimenti ne crea uno
        gem_id = gem.get("gem_id") or f"{gem.get('token_symbol','UNK')}_{ts.strftime('%Y%m%d_%H%M%S')}"
        price_entry = float(gem.get("price_usd", gem.get("price_entry_usd", 0)) or 0)

        # Scrivi su GEMS_CSV solo se non già fatto da gemmeV3.save_gem_to_csv()
        # (gemmeV3 imposta gem["_tracker_csv_written"] = True dopo aver scritto).
        # Per gemmeV2 il flag è assente → scriviamo qui con GEM_COLUMNS.
        if not gem.get("_tracker_csv_written"):
            row = {col: gem.get(col, "") for col in GEM_COLUMNS}
            row["gem_id"]            = gem_id
            row["timestamp_entry"]   = ts.isoformat(timespec="seconds")
            row["price_entry_usd"]   = price_entry
            row["gem_probability"]   = gem.get("gem_probability", gem.get("score", ""))
            row["inflow_usd"]        = gem.get("inflow_usd", "")
            row["inflow_wallet_count"] = gem.get("inflow_wallet_count", "")
            row["social_score"]      = gem.get("social_score", "")
            row["tvl_usd"]           = gem.get("tvl_usd", "")
            p_csv = Path(self._cfg["GEMS_CSV"])
            write_hdr = not p_csv.exists()
            with p_csv.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=GEM_COLUMNS, extrasaction="ignore")
                if write_hdr:
                    w.writeheader()
                w.writerow(row)

        with self._lock:
            self._active[gem_id] = {
                "gem_id":          gem_id,
                "token_symbol":    gem.get("token_symbol", ""),
                "chain":           gem.get("chain", ""),
                "pair_address":    gem.get("pair_address", ""),
                "token_address":   gem.get("token_address", ""),
                "price_entry_usd": price_entry,
                "timestamp_entry": ts,
                "snapshots_done":  0,
                "social_score":    float(gem.get("social_score", 0) or 0),
                "tvl_usd":         float(gem.get("tvl_usd", 0) or 0),
                "inflow_usd":      float(gem.get("inflow_usd", 0) or 0),
            }

        self._save_state()
        log.info(f"[gem_tracker] 💎 Gemma registrata: {gem_id}")
        return gem_id

    # ── Scheduler snapshots ────────────────────────────────────────────────

    def _scheduler_loop(self):
        while not self._stop_event.is_set():
            time.sleep(self._cfg["SCHEDULER_POLL_SEC"])
            now = datetime.now()
            with self._lock:
                candidates = list(self._active.items())

            milestone_hours    = self._cfg.get("MILESTONE_HOURS", [12, 24])
            milestone_snap_map = self._cfg.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})
            max_active_hours   = self._cfg.get("MILESTONE_MAX_HOURS", 25)

            for gem_id, meta in candidates:
                entry_ts  = meta["timestamp_entry"]
                snap_done = meta.get("snapshots_done", 0)
                interval  = self._cfg["SNAPSHOT_INTERVAL_SEC"]
                n_tot     = self._cfg["NUM_SNAPSHOTS"]
                hours_since_entry = (now - entry_ts).total_seconds() / 3600

                # Rimuovi dalla memoria solo dopo MILESTONE_MAX_HOURS (25h)
                if hours_since_entry > max_active_hours:
                    with self._lock:
                        self._active.pop(gem_id, None)
                    continue

                # ── Snapshot regolari (ogni 30min × 8 = 4h) ───────────────
                if snap_done < n_tot:
                    # Flat-timeout: non rimuovere dalla memoria (serve ancora per milestone),
                    # ma smetti di fare snapshot regolari se il token è piatto dopo 3h.
                    flat_hours = self._cfg.get("FLAT_TIMEOUT_HOURS", 8)
                    flat_thr   = self._cfg.get("FLAT_THRESHOLD_PCT", 1.5)
                    skip_regular = False
                    if hours_since_entry >= flat_hours and snap_done >= 2:
                        entry_price = meta.get("price_entry_usd", 0)
                        if entry_price > 0:
                            last_price = meta.get("last_price_usd", entry_price)
                            change = abs((last_price - entry_price) / entry_price * 100)
                            if change < flat_thr:
                                log.info(f"[gem_tracker] ⏹ {meta['token_symbol']} piatto "
                                         f"({change:.1f}% in {hours_since_entry:.1f}h) "
                                         f"— snapshot regolari fermi (milestone continua).")
                                skip_regular = True

                    if not skip_regular:
                        next_snap_time = entry_ts + timedelta(seconds=interval * (snap_done + 1))
                        if now >= next_snap_time:
                            self._take_snapshot(gem_id, meta, snap_done + 1)
                            with self._lock:
                                if gem_id in self._active:
                                    self._active[gem_id]["snapshots_done"] = snap_done + 1
                                    if hasattr(self, "_last_snap_price") and gem_id in self._last_snap_price:
                                        self._active[gem_id]["last_price_usd"] = self._last_snap_price.pop(gem_id)

                # ── Milestone snapshot (+12h e +24h) ──────────────────────
                milestones_done = meta.get("milestones_done", set())
                for mh in milestone_hours:
                    snap_num = milestone_snap_map[mh]
                    if mh in milestones_done:
                        continue
                    milestone_time = entry_ts + timedelta(hours=mh)
                    if now >= milestone_time:
                        self._take_snapshot(gem_id, meta, snap_num)
                        milestones_done = milestones_done | {mh}
                        with self._lock:
                            if gem_id in self._active:
                                self._active[gem_id]["milestones_done"] = milestones_done
                        log.info(f"[gem_tracker] ⏱️  {meta['token_symbol']} milestone +{mh}h completata.")

            try:
                self.genera_report_html()
            except Exception:
                pass

    def _take_snapshot(self, gem_id: str, meta: dict, snap_num: int):
        entry_price = meta["price_entry_usd"]
        now         = datetime.now()
        minutes     = round((now - meta["timestamp_entry"]).total_seconds() / 60)
        n_tot       = self._cfg["NUM_SNAPSHOTS"]

        price, status = self._fetch_price(
            meta["pair_address"], meta["chain"], gem_id,
            token_address=meta.get("token_address", "")
        )
        # Salva ultimo prezzo per flat-timeout check
        if price is not None:
            if not hasattr(self, "_last_snap_price"):
                self._last_snap_price = {}
            self._last_snap_price[gem_id] = price
        change_pct = ""
        if price is not None and entry_price > 0:
            change_pct = round((price - entry_price) / entry_price * 100, 4)

        row = {
            "gem_id":             gem_id,
            "token_symbol":       meta["token_symbol"],
            "chain":              meta["chain"],
            "pair_address":       meta["pair_address"],
            "price_entry_usd":    entry_price,
            "snapshot_num":       snap_num,
            "timestamp_snapshot": now.isoformat(),
            "minutes_since_entry": minutes,
            "price_snapshot_usd": price if price is not None else "",
            "change_pct":         change_pct,
            "social_score_snapshot": meta.get("social_score", ""),
            "tvl_snapshot":       meta.get("tvl_usd", ""),
            "status":             status,
        }

        with self._lock:
            with Path(self._cfg["FOLLOWUP_CSV"]).open(
                "a", newline="", encoding="utf-8"
            ) as f:
                csv.DictWriter(f, fieldnames=FOLLOWUP_COLUMNS).writerow(row)

        if change_pct != "":
            emoji = "📈" if float(change_pct) >= 0 else "📉"
            log.info(f"[gem_tracker] {emoji} {gem_id} snap {snap_num}/{n_tot} "
                     f"+{minutes}min | ${price:.8f} | Δ={float(change_pct):+.2f}%")
        else:
            log.info(f"[gem_tracker] ⚠️  {gem_id} snap {snap_num}/{n_tot} fetch fallito ({status})")

    # ── Fetch prezzo ────────────────────────────────────────────────────────

    def _fetch_price(self, pair_address: str, chain: str,
                     gem_id: str = "", target_ts: datetime = None,
                     token_address: str = ""
                     ) -> tuple[Optional[float], str]:
        if not pair_address:
            return None, "no_pair_address"

        chain_map = {"solana": "solana", "bsc": "bsc", "base": "base", "ethereum": "ethereum"}
        dex_chain = chain_map.get(chain.lower(), chain.lower())
        min_liq   = self._cfg["MIN_POOL_LIQUIDITY_USD"]

        # Prezzo corrente (snapshot real-time)
        if target_ts is None or (datetime.now() - target_ts).total_seconds() < 7200:
            # Se abbiamo il token_address, cerca sempre il pool con più liquidità
            # (evita di leggere prezzi da pool secondari/illiquidi)
            best_price = None
            best_liq   = 0.0

            if token_address:
                try:
                    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                    resp = requests.get(url, timeout=self._cfg["PRICE_FETCH_TIMEOUT"],
                                        headers={"User-Agent": "gem-tracker/1.0"})
                    if resp.status_code == 200:
                        all_pairs = resp.json().get("pairs") or []
                        chain_pairs = [p for p in all_pairs
                                       if (p.get("chainId") or "").lower() == dex_chain.lower()]
                        if not chain_pairs:
                            chain_pairs = all_pairs
                        for p in chain_pairs:
                            liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                            px  = float(p.get("priceUsd") or 0)
                            if px > 0 and liq > best_liq:
                                best_liq   = liq
                                best_price = px
                        if best_price and best_liq >= min_liq:
                            return best_price, "ok"
                        elif best_price:
                            log.debug(f"[gem_tracker] {gem_id}: liquidità pool ${best_liq:,.0f} "
                                      f"< min ${min_liq:,.0f} — prezzo potenzialmente inaffidabile")
                            return best_price, "ok_low_liq"
                except Exception:
                    pass

            # Fallback: usa pair_address diretto
            url = f"https://api.dexscreener.com/latest/dex/pairs/{dex_chain}/{pair_address}"
            try:
                resp = requests.get(url, timeout=self._cfg["PRICE_FETCH_TIMEOUT"],
                                    headers={"User-Agent": "gem-tracker/1.0"})
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    if pairs:
                        price = float(pairs[0].get("priceUsd") or 0)
                        if price > 0:
                            return price, "ok"
            except Exception:
                pass

        # Storico: DexScreener chart API
        if target_ts is not None:
            try:
                from_ts = int((target_ts - timedelta(minutes=15)).timestamp())
                to_ts   = int((target_ts + timedelta(minutes=15)).timestamp())
                url = (f"https://api.dexscreener.com/latest/dex/pairs"
                       f"/{dex_chain}/{pair_address}/chart")
                resp = requests.get(url, params={"from": from_ts, "to": to_ts, "res": "15"},
                                    timeout=self._cfg["PRICE_FETCH_TIMEOUT"])
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

    # ── Stop ───────────────────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()
        self._save_state()
        log.info("[gem_tracker] Tracker fermato e stato salvato.")

    # ── Report HTML ────────────────────────────────────────────────────────

    def genera_report_html(self) -> str:
        """Genera report HTML ricco con card per ogni gemma."""
        # Carica dati
        gems: dict[str, dict] = {}
        p = Path(self._cfg["GEMS_CSV"])
        if p.exists():
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gems[row["gem_id"]] = row

        followups: dict[str, dict[int, dict]] = {}
        fp = Path(self._cfg["FOLLOWUP_CSV"])
        if fp.exists():
            with fp.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gid  = row["gem_id"]
                    snum = int(row.get("snapshot_num", 0) or 0)
                    followups.setdefault(gid, {})[snum] = row

        n_snap            = self._cfg["NUM_SNAPSHOTS"]
        interval_min      = self._cfg["SNAPSHOT_INTERVAL_SEC"] // 60
        milestone_hours   = self._cfg.get("MILESTONE_HOURS", [12, 24])
        milestone_snap_map = self._cfg.get("MILESTONE_SNAP_NUMS", {12: 100, 24: 200})
        now_str           = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n_gems            = len(gems)

        # ── Genera card per ogni gemma ──
        cards_html = ""
        for gid, gem in reversed(list(gems.items())):
            sym    = gem.get("token_symbol", "?")
            name   = gem.get("token_name", sym) or sym
            chain  = gem.get("chain", "?").upper()
            addr   = gem.get("token_address", "") or ""
            pair   = gem.get("pair_address", "") or ""
            ts     = (gem.get("timestamp", "") or gem.get("timestamp_entry", ""))[:16].replace("T", " ")
            tier   = gem.get("tier", "")
            score  = float(gem.get("score", 0) or 0)
            prob   = float(gem.get("gem_probability", 0) or 0)
            bsr    = float(gem.get("buy_sell_ratio_1h", 0) or 0)
            ch1h   = float(gem.get("change_1h_pct", 0) or 0)
            age    = float(gem.get("pair_age_hours", 0) or 0)
            ramp   = float(gem.get("volume_ramp_ratio", 1) or 1)
            cex_e  = gem.get("cex_exchanges", "")
            sigs_raw = gem.get("signals", "")

            ep     = float(gem.get("price_usd", gem.get("price_entry_usd", 0)) or 0)
            mcap   = float(gem.get("market_cap_usd", 0) or 0)
            liq    = float(gem.get("liquidity_usd", 0) or 0)
            vol1h  = float(gem.get("volume_1h_usd", 0) or 0)
            inflow = float(gem.get("inflow_usd", 0) or 0)
            wallets = int(float(gem.get("inflow_wallet_count", 0) or 0))
            pnl_w  = float(gem.get("avg_wallet_pnl_pct", 0) or 0)
            social = float(gem.get("social_score", 0) or 0)
            tweets = int(float(gem.get("social_tweet_count", 0) or 0))
            tvl    = float(gem.get("tvl_usd", 0) or 0)
            top_f  = gem.get("top_features", "")

            # Tier badge colore
            tier_color = {"DIAMOND": "#2980B9", "GOLD": "#F39C12",
                          "SILVER": "#7F8C8D", "BRONZE": "#A04000"}.get(tier, "#334155")
            tier_emoji = {"DIAMOND": "💎", "GOLD": "🥇", "SILVER": "🥈", "BRONZE": "🥉"}.get(tier, "📊")

            # Social score bar
            ss_color = "#facc15" if social >= 50 else ("#4ade80" if social >= 20 else "#64748b")
            ss_bar = (f'<div style="background:#1e293b;border-radius:4px;height:8px;margin:4px 0">'
                      f'<div style="width:{min(social,100):.0f}%;height:8px;background:{ss_color};'
                      f'border-radius:4px"></div></div>')

            # Prezzo snapshot timeline
            snap_cells = ""
            best_chg = None
            for sn in range(1, n_snap + 1):
                fu = followups.get(gid, {}).get(sn)
                snap_hours = (sn * self._cfg["SNAPSHOT_INTERVAL_SEC"]) // 3600
                label = f"+{snap_hours}h"
                if fu:
                    chg_raw = fu.get("change_pct", "")
                    price_raw = fu.get("price_snapshot_usd", "")
                    status  = fu.get("status", "")
                    live = status in ("ok", "recovered_proxy", "recovered_historical")
                    if chg_raw != "" and live:
                        chg = float(chg_raw)
                        pv  = float(price_raw) if price_raw else 0
                        col = "#4ade80" if chg >= 0 else "#f87171"
                        sign = "+" if chg >= 0 else ""
                        if best_chg is None or chg > best_chg:
                            best_chg = chg
                        snap_cells += (
                            f'<div style="background:#1e293b;border-radius:6px;'
                            f'padding:6px 8px;min-width:70px;text-align:center">'
                            f'<div style="color:#64748b;font-size:10px">{label}</div>'
                            f'<div style="color:{col};font-weight:600;font-size:13px">'
                            f'{sign}{chg:.1f}%</div>'
                            f'<div style="color:#94a3b8;font-size:10px;font-family:monospace">'
                            f'${pv:.6f}</div></div>'
                        )
                    elif status == "missed":
                        snap_cells += (
                            f'<div style="background:#1e293b;border-radius:6px;'
                            f'padding:6px 8px;min-width:70px;text-align:center">'
                            f'<div style="color:#64748b;font-size:10px">{label}</div>'
                            f'<div style="color:#475569;font-size:16px">📵</div></div>'
                        )
                    else:
                        snap_cells += (
                            f'<div style="background:#1e293b;border-radius:6px;'
                            f'padding:6px 8px;min-width:70px;text-align:center">'
                            f'<div style="color:#64748b;font-size:10px">{label}</div>'
                            f'<div style="color:#475569;font-size:16px">⏳</div></div>'
                        )
                else:
                    snap_cells += (
                        f'<div style="background:#1e293b;border-radius:6px;'
                        f'padding:6px 8px;min-width:70px;text-align:center">'
                        f'<div style="color:#64748b;font-size:10px">{label}</div>'
                        f'<div style="color:#475569;font-size:16px">⏳</div></div>'
                    )

            # Milestone cells (+12h, +24h)
            gem_entry_ts_str = gem.get("timestamp_entry", "")
            try:
                gem_entry_ts = datetime.fromisoformat(gem_entry_ts_str)
            except Exception:
                gem_entry_ts = None

            milestone_cells = ""
            for mh in milestone_hours:
                snap_num = milestone_snap_map[mh]
                label = f"+{mh}h"
                fu = followups.get(gid, {}).get(snap_num)
                if fu:
                    chg_raw   = fu.get("change_pct", "")
                    price_raw = fu.get("price_snapshot_usd", "")
                    status    = fu.get("status", "")
                    live = status in ("ok", "recovered_proxy", "recovered_historical", "ok_low_liq")
                    if chg_raw != "" and live:
                        chg = float(chg_raw)
                        pv  = float(price_raw) if price_raw else 0
                        col = "#4ade80" if chg >= 0 else "#f87171"
                        sign = "+" if chg >= 0 else ""
                        bdr = f"border:1px solid {col}44"
                        milestone_cells += (
                            f'<div style="background:#0f172a;{bdr};border-radius:8px;'
                            f'padding:8px 14px;min-width:90px;text-align:center">'
                            f'<div style="color:#94a3b8;font-size:11px;font-weight:600">{label}</div>'
                            f'<div style="color:{col};font-weight:700;font-size:18px;margin:2px 0">'
                            f'{sign}{chg:.1f}%</div>'
                            f'<div style="color:#64748b;font-size:10px;font-family:monospace">'
                            f'${pv:.6g}</div></div>'
                        )
                    elif status == "missed":
                        milestone_cells += (
                            f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;'
                            f'padding:8px 14px;min-width:90px;text-align:center">'
                            f'<div style="color:#94a3b8;font-size:11px;font-weight:600">{label}</div>'
                            f'<div style="color:#475569;font-size:20px;margin:2px 0">📵</div>'
                            f'<div style="color:#334155;font-size:10px">dati mancanti</div></div>'
                        )
                    else:
                        milestone_cells += (
                            f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;'
                            f'padding:8px 14px;min-width:90px;text-align:center">'
                            f'<div style="color:#94a3b8;font-size:11px;font-weight:600">{label}</div>'
                            f'<div style="color:#475569;font-size:20px;margin:2px 0">⏳</div>'
                            f'<div style="color:#334155;font-size:10px">in attesa</div></div>'
                        )
                else:
                    # Calcola quando arriverà
                    eta_str = ""
                    if gem_entry_ts:
                        eta_dt = gem_entry_ts + timedelta(hours=mh)
                        now_dt = datetime.now()
                        if eta_dt > now_dt:
                            diff_min = int((eta_dt - now_dt).total_seconds() / 60)
                            if diff_min >= 60:
                                eta_str = f"tra {diff_min//60}h {diff_min%60}m"
                            else:
                                eta_str = f"tra {diff_min}m"
                        else:
                            eta_str = "in arrivo..."
                    milestone_cells += (
                        f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;'
                        f'padding:8px 14px;min-width:90px;text-align:center">'
                        f'<div style="color:#94a3b8;font-size:11px;font-weight:600">{label}</div>'
                        f'<div style="color:#475569;font-size:20px;margin:2px 0">⏳</div>'
                        f'<div style="color:#334155;font-size:10px">{eta_str}</div></div>'
                    )

            # Badge best performance
            best_badge = ""
            if best_chg is not None:
                bc = "#4ade80" if best_chg >= 0 else "#f87171"
                bs = "+" if best_chg >= 0 else ""
                best_badge = (f'<span style="background:{bc}22;color:{bc};'
                              f'border:1px solid {bc}44;border-radius:4px;'
                              f'padding:2px 8px;font-size:12px;font-weight:600">'
                              f'Max: {bs}{best_chg:.1f}%</span>')

            # Chain badge
            chain_colors = {"SOLANA": "#9945FF", "BSC": "#F0B90B", "BASE": "#0052FF"}
            cc = chain_colors.get(chain, "#64748b")

            dex_url = f"https://dexscreener.com/{chain.lower()}/{pair or addr}"
            cards_html += f"""
<div style="background:#0f172a;border:1px solid {tier_color}55;border-radius:12px;
            padding:20px;margin-bottom:16px">
  <!-- Header -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;
              flex-wrap:wrap;gap:8px;margin-bottom:16px">
    <div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">
        <span style="background:{tier_color}22;color:{tier_color};border:1px solid {tier_color}66;
                     border-radius:6px;padding:3px 10px;font-size:13px;font-weight:700">{tier_emoji} {tier}</span>
        <span style="font-size:20px;font-weight:800;color:#e2e8f0">${sym}</span>
        <span style="background:{cc}22;color:{cc};border:1px solid {cc}44;
                     border-radius:4px;padding:2px 8px;font-size:11px">{chain}</span>
        <span style="background:#1e293b;color:#94a3b8;border-radius:4px;
                     padding:2px 8px;font-size:12px;font-weight:600">Score {score:.0f}/100</span>
        {best_badge}
      </div>
      <div style="color:#64748b;font-size:12px">{name}</div>
      <div style="color:#334155;font-size:11px;font-family:monospace;
                  word-break:break-all;margin-top:4px">{addr}
        <button onclick="navigator.clipboard.writeText('{addr}')"
                style="background:#1e293b;border:none;color:#64748b;
                       cursor:pointer;border-radius:4px;padding:1px 6px;
                       margin-left:4px;font-size:10px">copy</button>
      </div>
    </div>
    <div style="text-align:right">
      <div style="color:#475569;font-size:12px">🕐 {ts}</div>
      <a href="{dex_url}" target="_blank"
         style="color:#7c3aed;font-size:11px;text-decoration:none">→ DexScreener</a>
    </div>
  </div>

  <!-- Metriche principali -->
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
              gap:10px;margin-bottom:16px">
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">Prezzo Entry</div>
      <div style="color:#e2e8f0;font-weight:600;font-family:monospace">${ep:.8f}</div>
    </div>
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">Market Cap</div>
      <div style="color:#e2e8f0;font-weight:600">${mcap:,.0f}</div>
    </div>
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">Liquidità</div>
      <div style="color:#e2e8f0;font-weight:600">${liq:,.0f}</div>
    </div>
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">Volume 1h</div>
      <div style="color:#e2e8f0;font-weight:600">${vol1h:,.0f}</div>
    </div>
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">🐋 Smart Money</div>
      <div style="color:#a78bfa;font-weight:600">${inflow:,.0f}</div>
      <div style="color:#64748b;font-size:10px">{wallets} wallet | avg {pnl_w:+.0f}%</div>
    </div>
    <div style="background:#1e293b;border-radius:8px;padding:10px">
      <div style="color:#64748b;font-size:11px">💬 Social Score</div>
      <div style="color:{ss_color};font-weight:700;font-size:18px">{social:.0f}<span style="font-size:11px;color:#64748b">/100</span></div>
      {ss_bar}
      <div style="color:#64748b;font-size:10px">{tweets} tweet analizzati</div>
    </div>
    {'<div style="background:#1e293b;border-radius:8px;padding:10px"><div style="color:#64748b;font-size:11px">📊 TVL (DefiLlama)</div><div style="color:#34d399;font-weight:600">$' + f'{tvl:,.0f}</div></div>' if tvl > 0 else ''}
  </div>

  <!-- Timeline prezzi -->
  <div style="margin-bottom:8px">
    <div style="color:#64748b;font-size:11px;margin-bottom:8px">
      📈 Tracking prezzi (ogni 2h × 5 check → fino a +10h)
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      {snap_cells}
    </div>
  </div>

  <!-- Milestone +12h / +24h -->
  <div style="margin-top:10px;margin-bottom:8px">
    <div style="color:#64748b;font-size:11px;margin-bottom:8px">
      🎯 Milestone performance
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      {milestone_cells}
    </div>
  </div>

  {'<div style="color:#475569;font-size:11px;margin-top:8px">🔑 ' + top_f + '</div>' if top_f else ''}
</div>"""

        if not cards_html:
            cards_html = """<div style="text-align:center;padding:60px;color:#475569">
              💎 Nessuna gemma ancora trovata.<br>
              <small>Il bot cercherà gemme ad ogni ciclo (ogni 5 minuti).</small>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>💎 Gem Hunter Report</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #020617; color: #e2e8f0; min-height: 100vh; padding: 20px;
    }}
    .header {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 16px 0; margin-bottom: 24px;
      border-bottom: 1px solid #1e293b;
    }}
    .stats-bar {{
      display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px;
    }}
    .stat {{
      background: #0f172a; border: 1px solid #1e293b; border-radius: 8px;
      padding: 10px 16px; text-align: center;
    }}
    .stat-val {{ font-size: 20px; font-weight: 700; color: #e2e8f0; }}
    .stat-lbl {{ font-size: 11px; color: #64748b; text-transform: uppercase; margin-top: 2px; }}
    .disclaimer {{
      margin-top: 32px; padding: 12px 16px;
      background: #1c1a00; border: 1px solid #6e5908;
      border-radius: 8px; font-size: 13px; color: #a16207;
    }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1 style="font-size:24px;color:#e2e8f0">
        💎 Quality Gem Hunter
      </h1>
      <div style="color:#64748b;font-size:13px;margin-top:4px">
        Smart Money + Social + Fondamentali | Solana · BSC · Base
      </div>
    </div>
    <div style="text-align:right;color:#475569;font-size:12px">
      Aggiornato: {now_str}<br>
      <span style="font-size:10px">Auto-refresh 60s</span>
    </div>
  </div>

  <div class="stats-bar">
    <div class="stat">
      <div class="stat-val">{n_gems}</div>
      <div class="stat-lbl">Gemme trovate</div>
    </div>
    <div class="stat">
      <div class="stat-val">{len(self._active)}</div>
      <div class="stat-lbl">In tracking</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#9945FF">{sum(1 for g in gems.values() if g.get('chain','').lower()=='solana')}</div>
      <div class="stat-lbl">Solana</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#F0B90B">{sum(1 for g in gems.values() if g.get('chain','').lower()=='bsc')}</div>
      <div class="stat-lbl">BSC</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#0052FF">{sum(1 for g in gems.values() if g.get('chain','').lower()=='base')}</div>
      <div class="stat-lbl">Base</div>
    </div>
  </div>

  {cards_html}

  <div class="disclaimer">
    ⚠️ <strong>AVVISO:</strong> Solo a scopo educativo. NON costituisce consiglio finanziario.
    Il trading di criptovalute comporta rischi molto elevati di perdita del capitale.
  </div>
</body>
</html>"""

        out = Path(self._cfg["HTML_REPORT"])
        out.write_text(html, encoding="utf-8")
        log.debug(f"[gem_tracker] 📄 Report aggiornato: {out}")
        return str(out)


# ── Singleton (multi-config) ─────────────────────────────────────────────────
# Supporta gemmeV2 e gemmeV3 in esecuzione simultanea con config separate.
# Ogni config identifica un'istanza diversa tramite STATE_FILE.

_tracker_instances: dict = {}
_tracker_lock = threading.Lock()

GEM_TRACKER_AVAILABLE = True


def get_gem_tracker(config: dict | None = None) -> "GemTracker":
    """
    Ritorna l'istanza GemTracker per la config fornita (lazy init).
    Se config=None usa GEM_TRACKER_CONFIG (v2).
    Passare GEM_TRACKER_CONFIG_V3 da gemmeV3.py per istanza dedicata.
    """
    cfg = config if config is not None else GEM_TRACKER_CONFIG
    key = cfg.get("STATE_FILE", "default")
    if key not in _tracker_instances:
        with _tracker_lock:
            if key not in _tracker_instances:
                _tracker_instances[key] = GemTracker(config=cfg)
    return _tracker_instances[key]

"""
bitget_futures_executor.py
==========================
Execution layer per structural_bot.py su Bitget Futures (BTCUSDT USDT-M).

Usa ccxt (libreria multi-exchange) — stessa logica di bybit_futures_executor
ma adattata alle specifiche Bitget:
  • Richiede API passphrase (terza credenziale)
  • Symbol: "BTC/USDT:USDT" in ccxt unified format
  • TP/SL tramite parametri extra nell'ordine di apertura

Setup in trade/.env:
    BITGET_API_KEY=...
    BITGET_API_SECRET=...
    BITGET_PASSPHRASE=...        ← obbligatorio su Bitget
    BITGET_DRY_RUN=true
    BITGET_LEVERAGE=1
    BITGET_TRADE_SIZE_USD=50
    BITGET_TRADING_HOURS=8-22
    BITGET_TESTNET=false

Avvio:
    EXECUTOR=bitget python structural_bot.py
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False

log = logging.getLogger("bitget_exec")

def _env(k, default=""):
    return os.environ.get(k, default)

DRY_RUN        = _env("BITGET_DRY_RUN", "true").lower() != "false"
LEVERAGE       = int(_env("BITGET_LEVERAGE", "1"))
TRADE_SIZE_USD = float(_env("BITGET_TRADE_SIZE_USD", "50"))
TESTNET        = _env("BITGET_TESTNET", "false").lower() == "true"
DEMO_MODE      = _env("BITGET_DEMO", "false").lower() == "true"

# ccxt usa sempre BTC/USDT:USDT come simbolo unificato (anche per demo)
# SBTCUSDT è il simbolo nativo Bitget usato solo nelle chiamate privatePost
SYMBOL    = "BTC/USDT:USDT"
SYMBOL_ID = "SBTCSUSDT" if DEMO_MODE else "BTCUSDT"   # Simulated Trading (SUSDT-FUTURES)

_hours_raw = _env("BITGET_TRADING_HOURS", "0-24")
try:
    _h_start, _h_end = map(int, _hours_raw.split("-"))
except Exception:
    _h_start, _h_end = 0, 24


def _in_trading_hours() -> bool:
    if _h_start == 0 and _h_end == 24:
        return True
    h = datetime.now(timezone.utc).hour
    return _h_start <= h < _h_end


class BitgetFuturesExecutor:
    """
    Executor Bitget Futures USDT-M per structural_bot.py.
    LONG e SHORT supportati.
    SL+TP inclusi nell'ordine di apertura tramite ccxt params.
    """

    def __init__(self):
        self._exchange: Optional["ccxt.bitget"] = None
        self._position_side: str  = ""
        self._entry_price: float  = 0.0
        self._entry_qty: float    = 0.0
        self._tp: float           = 0.0
        self._sl: float           = 0.0
        self._sl_order_id: Optional[str]       = None
        self._trailing_order_id: Optional[str] = None   # trailing stop nativo Bitget

        if not CCXT_AVAILABLE:
            log.warning("[BITGET] ccxt non installato — pip install ccxt")
            return

        if DRY_RUN:
            log.info("[BITGET] DRY_RUN=True — nessuna tx reale")
            return

        # Demo richiede chiavi API create nell'ambiente Simulated Trading di Bitget
        # (diverse dalle chiavi del conto reale — errore 40099 se si usano le stesse)
        if DEMO_MODE:
            api_key    = _env("BITGET_DEMO_API_KEY")
            api_secret = _env("BITGET_DEMO_API_SECRET")
            passphrase = _env("BITGET_DEMO_PASSPHRASE")
        else:
            api_key    = _env("BITGET_API_KEY")
            api_secret = _env("BITGET_API_SECRET")
            passphrase = _env("BITGET_PASSPHRASE")

        if not api_key or not api_secret or not passphrase:
            mode = "DEMO (Simulated Trading)" if DEMO_MODE else "LIVE"
            log.error(f"[BITGET] Credenziali {mode} mancanti nel .env")
            return

        product_type = "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES"
        exchange_cfg: dict = {
            "apiKey":     api_key,
            "secret":     api_secret,
            "password":   passphrase,
            "sandbox":    TESTNET,
            "timeout":    30000,
            "options": {
                "defaultType":        "swap",
                "defaultProductType": product_type,
            },
        }
        # Demo: Bitget richiede header paptrading=1 su OGNI richiesta
        if DEMO_MODE:
            exchange_cfg["headers"] = {"paptrading": "1"}

        self._exchange = ccxt.bitget(exchange_cfg)
        self._net_fail_streak = 0   # timeout/connessione consecutivi

        # Imposta leva (passa productType per demo SUSDT-FUTURES)
        try:
            self._exchange.set_leverage(LEVERAGE, SYMBOL, params={
                "productType": product_type,
                "marginCoin":  "SUSDT" if DEMO_MODE else "USDT",
            })
            mode = "DEMO" if DEMO_MODE else ("TESTNET" if TESTNET else "LIVE")
            log.info(f"[BITGET] Connesso {mode} ({product_type}) | "
                     f"leva={LEVERAGE}x | saldo: {self._get_balance():.2f} USDT")
        except Exception as e:
            log.warning(f"[BITGET] Setup leva: {e}")

        self._reconcile()

    # ── Filtro orario ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        if not _in_trading_hours():
            log.debug(f"[BITGET] Fuori orario ({_h_start}-{_h_end} UTC)")
            return False
        return True

    # ── Entry con SL+TP ──────────────────────────────────────────────────────

    def enter(self, signal_enum, entry_price: float, sl: float, tp: float,
              size_contracts: float, atr: float = 0.0) -> bool:
        from structural_bot import Signal

        is_long   = signal_enum == Signal.LONG
        side      = "buy" if is_long else "sell"
        direction = "LONG" if is_long else "SHORT"

        # Bitget: min 0.001 BTC, step 0.001
        qty = max(0.001, round(size_contracts, 3))

        # Cap margine: se qty × price / leverage > saldo disponibile → riduci
        if not DRY_RUN:
            _avail = self._get_balance()
            if _avail > 0 and entry_price > 0:
                import math as _math
                _max_qty = _math.floor((_avail * LEVERAGE) / entry_price * 0.92 * 1000) / 1000  # floor a 0.001 BTC
                if qty > _max_qty:
                    log.warning(
                        f"[BITGET] Margine insufficiente: qty={qty} BTC richiede "
                        f"${qty*entry_price/LEVERAGE:.2f} ma saldo=${_avail:.2f} lev={LEVERAGE}x "
                        f"→ ridotto a {_max_qty} BTC"
                    )
                    qty = _max_qty
                if qty < 0.001:
                    log.error(
                        f"[BITGET] Saldo insufficiente anche per lotto minimo 0.001 BTC "
                        f"(min_margin=${0.001*entry_price/LEVERAGE:.2f} > saldo=${_avail:.2f})"
                    )
                    return False

        log.info(f"[BITGET] {direction} | qty={qty} BTC | "
                 f"entry≈{entry_price:.1f} | SL={sl:.1f} | TP={tp:.1f}")

        if DRY_RUN:
            self._position_side = direction
            self._entry_price   = entry_price
            self._entry_qty     = qty
            self._tp = tp; self._sl = sl
            log.info(f"[DRY BITGET] {direction} {qty} BTC @ ~{entry_price:.1f} | SL={sl:.1f} TP={tp:.1f}")
            return True

        if not self._exchange:
            return False

        # Circuit breaker connessione: se 4+ timeout consecutivi → API irraggiungibile
        NET_FAIL_MAX = 4
        if getattr(self, "_net_fail_streak", 0) >= NET_FAIL_MAX:
            log.error(
                f"[BITGET] ⛔ Circuit breaker connessione: {self._net_fail_streak} timeout "
                f"consecutivi — Bitget API irraggiungibile. Skip entry."
            )
            return False

        params = {
            "tradeSide":              "open",
            "presetStopSurplusPrice": str(round(tp, 1)),
            "presetStopLossPrice":    str(round(sl, 1)),
        }

        # Retry interno: 3 tentativi con 4s di pausa (cattura blip temporanei <30s)
        last_exc = None
        for attempt in range(1, 4):
            try:
                order = self._exchange.create_order(
                    symbol  = SYMBOL,
                    type    = "market",
                    side    = side,
                    amount  = qty,
                    params  = params,
                )
                self._net_fail_streak = 0
                self._position_side = direction
                self._entry_price   = float(order.get("average") or entry_price)
                self._entry_qty     = qty
                self._tp = tp; self._sl = sl
                log.info(f"[BITGET] ✅ {direction} aperto | id={order.get('id','?')} | "
                         f"fill≈{self._entry_price:.1f} | SL={sl:.1f} TP={tp:.1f}")
                # Piazza trailing stop nativo immediatamente dopo l'apertura
                if atr > 0:
                    self._trailing_order_id = self._place_trailing_stop(
                        direction, qty, self._entry_price, tp, atr
                    )
                return True
            except Exception as e:
                last_exc = e
                is_timeout = "Timeout" in type(e).__name__ or "timeout" in str(e).lower()
                if is_timeout and attempt < 3:
                    log.warning(f"[BITGET] Timeout tentativo {attempt}/3 — retry in 4s...")
                    time.sleep(4)
                    # Verifica se il timeout ha comunque aperto la posizione
                    try:
                        pos_check = self._get_open_position()
                        if pos_check and abs(float(pos_check.get("contracts", 0) or 0)) >= 0.001:
                            side_c = pos_check.get("side", "")
                            self._position_side = "LONG" if side_c == "long" else "SHORT"
                            self._entry_price   = float(pos_check.get("entryPrice", 0) or entry_price)
                            self._entry_qty     = float(pos_check.get("contracts", 0))
                            self._tp = tp; self._sl = sl
                            self._net_fail_streak = 0
                            log.warning(
                                f"[BITGET] ⚠ Timeout ma posizione già aperta: "
                                f"{self._position_side} {self._entry_qty} BTC @ {self._entry_price:.1f} "
                                f"— tracciata, annullo retry"
                            )
                            return True
                    except Exception:
                        pass
                else:
                    break

        log.error(f"[BITGET] Errore entry: {type(last_exc).__name__}: {last_exc}", exc_info=True)

        # Timeout: il server potrebbe aver processato l'ordine comunque.
        try:
            pos = self._get_open_position()
            if pos and abs(float(pos.get("contracts", 0) or 0)) >= 0.001:
                side_str = pos.get("side", "")
                self._position_side = "LONG" if side_str == "long" else "SHORT"
                self._entry_price   = float(pos.get("entryPrice", 0) or entry_price)
                self._entry_qty     = float(pos.get("contracts", 0))
                self._tp = tp; self._sl = sl
                self._net_fail_streak = 0
                log.warning(
                    f"[BITGET] ⚠ Timeout ma posizione trovata sul exchange: "
                    f"{self._position_side} {self._entry_qty} BTC @ {self._entry_price:.1f} — tracciata"
                )
                return True
        except Exception as e2:
            log.debug(f"[BITGET] check post-timeout: {e2}")

        self._net_fail_streak = getattr(self, "_net_fail_streak", 0) + 1
        if self._net_fail_streak >= NET_FAIL_MAX:
            log.error(
                f"[BITGET] ⛔ {self._net_fail_streak} timeout consecutivi — "
                f"Bitget API irraggiungibile. Prossimi entry bloccati finché la connessione non si ripristina."
            )
        return False

    # ── Trailing stop nativo Bitget ──────────────────────────────────────────

    def _place_trailing_stop(self, direction: str, qty: float,
                              entry: float, tp: float, atr: float) -> Optional[str]:
        """
        Piazza un trailing stop nativo su Bitget (planType=track_plan).

        Logica allineata al bot Python:
          - triggerPrice: entry × TRAIL_ACTIVATE_PCT verso TP (65% del percorso)
          - callbackRatio: TRAIL_ATR_DIST × ATR / entry_price [0.1% – 5%]

        Vantaggi vs Python trailing (ogni 5m):
          - Esecuzione intra-candela al millisecondo
          - Zero latenza Python: Bitget gestisce lo stop autonomamente
        """
        from structural_bot import TRAIL_ACTIVATE_PCT, TRAIL_ATR_DIST

        hold_side = "long" if direction == "LONG" else "short"

        # Prezzo di attivazione: TRAIL_ACTIVATE_PCT verso TP
        if direction == "LONG":
            trigger = round(entry + (tp - entry) * TRAIL_ACTIVATE_PCT, 1)
        else:
            trigger = round(entry - (entry - tp) * TRAIL_ACTIVATE_PCT, 1)

        # Callback ratio: distanza trail in % del prezzo entry
        # Bitget accetta valori tra 0.001 (0.1%) e 0.05 (5%)
        raw_ratio = TRAIL_ATR_DIST * atr / entry if entry > 0 else 0.002
        callback  = round(max(0.001, min(0.05, raw_ratio)), 4)

        log.info(
            f"[BITGET] Trailing nativo: trigger={trigger:.1f} "
            f"callback={callback*100:.2f}% (ATR={atr:.1f}×{TRAIL_ATR_DIST})"
        )

        if DRY_RUN:
            log.info(f"[DRY BITGET] Trailing stop simulato: trigger={trigger:.1f} cb={callback*100:.2f}%")
            return "DRY_TRAIL"

        if not self._exchange:
            return None

        try:
            resp = self._exchange.privatePostApiV2MixOrderPlaceTpslOrder({
                "symbol":        "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                "productType":   "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                "marginCoin":    "SUSDT" if DEMO_MODE else "USDT",
                "planType":      "track_plan",
                "triggerPrice":  str(trigger),
                "triggerType":   "mark_price",
                "callbackRatio": str(callback),
                "holdSide":      hold_side,
                "size":          str(qty),
            })
            order_id = (resp.get("data") or {}).get("orderId", "?")
            log.info(f"[BITGET] ✅ Trailing stop piazzato | id={order_id}")
            return str(order_id)
        except Exception as e:
            log.debug(f"[BITGET] Trailing stop nativo non disponibile ({e}) — Python trailing attivo come backup")
            return None

    def _cancel_trailing_stop(self) -> None:
        """Cancella il trailing stop order nativo (chiamato da on_close)."""
        if not self._trailing_order_id or self._trailing_order_id == "DRY_TRAIL":
            self._trailing_order_id = None
            return
        if not self._exchange:
            return
        try:
            self._exchange.privatePostApiV2MixOrderCancelPlanOrder({
                "symbol":      "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                "productType": "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                "orderId":     self._trailing_order_id,
                "planType":    "track_plan",
            })
            log.info(f"[BITGET] Trailing stop {self._trailing_order_id} cancellato")
        except Exception as e:
            log.debug(f"[BITGET] Cancel trailing stop: {e} (già eseguito o inesistente)")
        finally:
            self._trailing_order_id = None

    # ── Trailing SL (Python-side backup) ─────────────────────────────────────

    def update_sl(self, new_sl: float) -> bool:
        """Modifica lo stop-loss della posizione aperta."""
        if not self._position_side:
            return False

        log.info(f"[BITGET] TRAIL SL → {new_sl:.1f}")

        if DRY_RUN:
            self._sl = new_sl
            return True

        if not self._exchange:
            return False

        try:
            # ccxt unified: edit_order per modificare SL esistente
            # In alternativa: usa l'endpoint Bitget nativo per modificare TPSL
            self._exchange.privatePostApiV2MixOrderModifyTpslOrder({
                "symbol":           "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                "productType":      "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                "marginCoin":       "SUSDT" if DEMO_MODE else "USDT",
                "planType":         "loss_plan",
                "triggerPrice":     str(round(new_sl, 1)),
                "executePrice":     "0",   # market execution
                "triggerType":      "mark_price",
            })
            self._sl = new_sl
            log.info(f"[BITGET] SL aggiornato a {new_sl:.1f}")
            return True

        except Exception as e:
            log.warning(f"[BITGET] update_sl: {e} — tenterò close+reopen SL")
            # Fallback: chiudi il vecchio piano SL e apri uno nuovo
            try:
                self._exchange.privatePostApiV2MixOrderCancelPlanOrder({
                    "symbol":      "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                    "productType": "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                    "planType":    "loss_plan",
                })
                self._exchange.privatePostApiV2MixOrderPlaceTpslOrder({
                    "symbol":       "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                    "productType":  "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                    "marginCoin":   "SUSDT" if DEMO_MODE else "USDT",
                    "planType":     "loss_plan",
                    "triggerPrice": str(round(new_sl, 1)),
                    "holdSide":     "long" if self._position_side == "LONG" else "short",
                })
                self._sl = new_sl
                log.info(f"[BITGET] SL ripiazzato a {new_sl:.1f}")
                return True
            except Exception as e2:
                log.error(f"[BITGET] Fallback update_sl: {e2}")
                return False

    # ── Chiusura ─────────────────────────────────────────────────────────────

    def on_close(self, reason: str):
        """Chiude la posizione con ordine market reduceOnly se ancora aperta."""
        log.info(f"[BITGET] Posizione chiusa ({reason})")
        self._cancel_trailing_stop()   # rimuove trailing order residuo sull'exchange

        if DRY_RUN:
            self._position_side = ""
            return

        if not self._exchange or not self._position_side:
            self._position_side = ""
            return

        try:
            pos = self._get_open_position()
            if pos and abs(float(pos.get("contracts", 0) or 0)) > 0:
                close_side = "sell" if self._position_side == "LONG" else "buy"
                self._exchange.create_order(
                    symbol  = SYMBOL,
                    type    = "market",
                    side    = close_side,
                    amount  = self._entry_qty,
                    params  = {"tradeSide": "close"},
                )
                log.info(f"[BITGET] Posizione chiusa manualmente ({reason})")
        except Exception as e:
            log.warning(f"[BITGET] on_close: {e}")
        finally:
            self._position_side = ""
            self._entry_qty     = 0.0

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync_position(self) -> Optional[float]:
        """Verifica se Bitget ha chiuso la posizione (SL/TP colpito)."""
        if DRY_RUN or not self._exchange or not self._position_side:
            return None
        try:
            pos = self._get_open_position()
            if pos is None or abs(float(pos.get("contracts", 0) or 0)) < 0.001:
                # Possibile chiusura — conferma con un secondo check dopo 4s
                # (evita falsi positivi da glitch momentaneo di fetch_positions)
                time.sleep(4)
                pos2 = self._get_open_position()
                if pos2 and abs(float(pos2.get("contracts", 0) or 0)) >= 0.001:
                    log.debug("[BITGET] Posizione ancora aperta (falso positivo al primo check) — ignoro")
                    return None

                # Confermato chiusa — retry su P&L (Bitget aggiorna achievedProfits con ritardo)
                pnl = 0.0
                for _attempt in range(3):
                    time.sleep(3)
                    pnl = self._get_last_closed_pnl()
                    if pnl != 0.0:
                        break
                    log.debug(f"[BITGET] P&L=0 al tentativo {_attempt+1}/3 — riprovo...")
                log.info(f"[BITGET] Posizione chiusa da exchange | P&L≈{pnl:+.2f} USDT")
                self._position_side = ""
                self._entry_qty     = 0.0
                return pnl
        except Exception as e:
            log.debug(f"[BITGET] sync: {e}")
        return None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_open_position(self) -> Optional[dict]:
        try:
            positions = self._exchange.fetch_positions([SYMBOL])
            self._net_fail_streak = 0   # connessione OK → reset streak
            for p in positions:
                if float(p.get("contracts", 0) or 0) > 0:
                    return p
        except Exception:
            pass
        return None

    def _get_last_closed_pnl(self) -> float:
        # 1. Storico posizioni Bitget (fonte più affidabile per P&L realizzato)
        try:
            resp = self._exchange.private_get_api_v2_mix_position_history_position({
                "productType": "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES",
                "symbol":      "SBTCSUSDT" if DEMO_MODE else "BTCUSDT",
                "limit":       "1",
            })
            items = (resp.get("data", {}) or {}).get("list") or []
            if items:
                pnl = float(items[0].get("achievedProfits", 0) or 0)
                log.debug(f"[BITGET] PnL da history-position: {pnl:+.4f}")
                return pnl
        except Exception as e:
            log.debug(f"[BITGET] history-position: {e}")

        # 2. Fallback: fetch_my_trades → realizedPnl o profit
        try:
            trades = self._exchange.fetch_my_trades(SYMBOL, limit=1)
            if trades:
                info = trades[0].get("info", {})
                for field in ("realizedPnl", "profit", "pnl", "achievedProfits"):
                    val = info.get(field)
                    if val not in (None, "", "0", 0):
                        return float(val)
        except Exception as e:
            log.debug(f"[BITGET] fetch_my_trades pnl: {e}")

        return 0.0

    def _get_balance(self) -> float:
        # Demo usa SUSDT (Simulated USDT) come marginCoin — chiave diversa nel balance
        coin = "SUSDT" if DEMO_MODE else "USDT"
        pt   = "SUSDT-FUTURES" if DEMO_MODE else "USDT-FUTURES"
        try:
            balance = self._exchange.fetch_balance({
                "type": "swap", "productType": pt, "marginCoin": coin,
            })
            val = float(balance.get(coin, {}).get("free", 0) or 0)
            if val == 0:
                # Fallback: prova anche con USDT nel dict (ccxt normalizza)
                val = float(balance.get("USDT", {}).get("free", 0) or 0)
            return val
        except Exception:
            pass
        return 0.0

    def _reconcile(self):
        """Al restart verifica se c'è già una posizione aperta."""
        pos = self._get_open_position()
        if pos:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts > 0:
                side = pos.get("side", "")
                self._position_side = "LONG" if side == "long" else "SHORT"
                self._entry_price   = float(pos.get("entryPrice", 0) or 0)
                self._entry_qty     = contracts
                log.warning(
                    f"[BITGET] ⚠ Posizione aperta trovata: "
                    f"{self._position_side} {contracts} BTC @ {self._entry_price:.1f}"
                )

    @property
    def has_open_position(self) -> bool:
        return bool(self._position_side)

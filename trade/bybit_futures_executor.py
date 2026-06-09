"""
bybit_futures_executor.py
==========================
Execution layer per structural_bot.py su Bybit Futures (BTCUSDT perp).

Vantaggi rispetto a Binance:
  • SL e TP settati direttamente nell'ordine di apertura (una sola API call)
  • Nessun interesse su posizioni short (è futures, non margin)
  • Fee 0.055% taker (vs 0.1% Binance spot/margin)
  • Disponibile in EU/Italia

Setup in trade/.env:
    BYBIT_API_KEY=...
    BYBIT_API_SECRET=...
    BYBIT_DRY_RUN=true
    BYBIT_LEVERAGE=1
    BYBIT_TRADE_SIZE_USD=50
    BYBIT_TRADING_HOURS=8-22     # solo 08-22 UTC (0-24 = sempre)
    BYBIT_TESTNET=false          # true = usa testnet.bybit.com
    BYBIT_EXECUTOR=true

Avvio:
    BYBIT_EXECUTOR=true python structural_bot.py
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    from pybit.unified_trading import HTTP
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False

log = logging.getLogger("bybit_exec")

SYMBOL   = "BTCUSDT"
CATEGORY = "linear"   # USDT perpetual

def _env(k, default=""):
    return os.environ.get(k, default)

DRY_RUN        = _env("BYBIT_DRY_RUN", "true").lower() != "false"
LEVERAGE       = int(_env("BYBIT_LEVERAGE", "1"))
TRADE_SIZE_USD = float(_env("BYBIT_TRADE_SIZE_USD", "50"))
TESTNET        = _env("BYBIT_TESTNET", "false").lower() == "true"

_hours_raw = _env("BYBIT_TRADING_HOURS", "0-24")
try:
    _h_start, _h_end = map(int, _hours_raw.split("-"))
except Exception:
    _h_start, _h_end = 0, 24


def _in_trading_hours() -> bool:
    if _h_start == 0 and _h_end == 24:
        return True
    h = datetime.now(timezone.utc).hour
    return _h_start <= h < _h_end


class BybitFuturesExecutor:
    """
    Executor Bybit Futures per structural_bot.py.
    LONG e SHORT supportati.
    SL+TP inclusi nell'ordine di apertura (singola API call).
    """

    def __init__(self):
        self._session: Optional["HTTP"] = None
        self._position_side: str  = ""   # "LONG" | "SHORT" | ""
        self._entry_price: float  = 0.0
        self._entry_qty: float    = 0.0
        self._tp: float           = 0.0
        self._sl: float           = 0.0

        if not PYBIT_AVAILABLE:
            log.warning("[BYBIT] pybit non installato — pip install pybit")
            return

        if DRY_RUN:
            log.info("[BYBIT] DRY_RUN=True — nessuna tx reale")
            return

        api_key    = _env("BYBIT_API_KEY")
        api_secret = _env("BYBIT_API_SECRET")
        if not api_key or not api_secret:
            log.error("[BYBIT] BYBIT_API_KEY / BYBIT_API_SECRET mancanti in .env")
            return

        self._session = HTTP(
            testnet    = TESTNET,
            api_key    = api_key,
            api_secret = api_secret,
        )

        # Imposta leva e modalità posizione (one-way)
        try:
            self._session.set_leverage(
                category     = CATEGORY,
                symbol       = SYMBOL,
                buyLeverage  = str(LEVERAGE),
                sellLeverage = str(LEVERAGE),
            )
            self._session.switch_position_mode(
                category = CATEGORY,
                symbol   = SYMBOL,
                mode     = 0,   # 0 = one-way (più semplice da gestire)
            )
            log.info(f"[BYBIT] Connesso {'TESTNET' if TESTNET else 'LIVE'} | "
                     f"leva={LEVERAGE}x | saldo: {self._get_balance():.2f} USDT")
        except Exception as e:
            log.warning(f"[BYBIT] Setup leva/modalità: {e}")

        # Riconcilia posizione aperta su Bybit (es. restart dopo crash)
        self._reconcile()

    # ── Filtro orario ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        if not _in_trading_hours():
            log.debug(f"[BYBIT] Fuori orario ({_h_start}-{_h_end} UTC) — skip")
            return False
        return True

    # ── Entry con SL+TP in un solo ordine ────────────────────────────────────

    def enter(self, signal_enum, entry_price: float, sl: float, tp: float,
              size_contracts: float) -> bool:
        """
        Apre posizione LONG o SHORT con SL e TP inclusi nell'ordine.
        size_contracts = quantità BTC (calcolata da calculate_trade in structural_bot)
        """
        # Confronto per nome (non identità): `from structural_bot import Signal` qui
        # creerebbe una classe Enum diversa da __main__.Signal → confronto sempre False
        # e ogni LONG verrebbe inviato all'exchange come SHORT.
        is_long  = signal_enum.name == "LONG"
        side     = "Buy" if is_long else "Sell"
        direction = "LONG" if is_long else "SHORT"

        # Bybit: quantità minima 0.001 BTC, step 0.001
        qty = max(0.001, round(size_contracts, 3))

        log.info(
            f"[BYBIT] {direction} | qty={qty} BTC | "
            f"entry≈{entry_price:.1f} | SL={sl:.1f} | TP={tp:.1f}"
        )

        if DRY_RUN:
            self._position_side = direction
            self._entry_price   = entry_price
            self._entry_qty     = qty
            self._tp            = tp
            self._sl            = sl
            log.info(f"[DRY BYBIT] {direction} {qty} BTC @ ~{entry_price:.1f} | SL={sl:.1f} TP={tp:.1f}")
            return True

        if not self._session:
            return False

        try:
            resp = self._session.place_order(
                category    = CATEGORY,
                symbol      = SYMBOL,
                side        = side,
                orderType   = "Market",
                qty         = str(qty),
                # SL e TP inclusi nell'ordine di apertura — il vantaggio di Bybit
                takeProfit      = f"{tp:.1f}",
                stopLoss        = f"{sl:.1f}",
                tpTriggerBy     = "LastPrice",
                slTriggerBy     = "LastPrice",
                tpOrderType     = "Market",
                slOrderType     = "Market",
            )

            if resp.get("retCode") != 0:
                log.error(f"[BYBIT] Errore apertura: {resp.get('retMsg')}")
                return False

            order_id = resp.get("result", {}).get("orderId", "?")
            self._position_side = direction
            self._entry_price   = entry_price
            self._entry_qty     = qty
            self._tp            = tp
            self._sl            = sl
            log.info(f"[BYBIT] ✅ {direction} aperto | orderId={order_id} | SL={sl:.1f} TP={tp:.1f}")
            return True

        except Exception as e:
            log.error(f"[BYBIT] Errore entry: {e}")
            return False

    # ── Aggiorna trailing SL ─────────────────────────────────────────────────

    def update_sl(self, new_sl: float) -> bool:
        """
        Aggiorna il trailing stop tramite set_trading_stop.
        Non serve cancellare/ricreare ordini — una sola chiamata API.
        """
        if not self._position_side:
            return False

        log.info(f"[BYBIT] TRAIL SL → {new_sl:.1f}")

        if DRY_RUN:
            self._sl = new_sl
            return True

        if not self._session:
            return False

        try:
            resp = self._session.set_trading_stop(
                category    = CATEGORY,
                symbol      = SYMBOL,
                stopLoss    = f"{new_sl:.1f}",
                slTriggerBy = "LastPrice",
                slOrderType = "Market",
                positionIdx = 0,   # one-way mode
            )
            if resp.get("retCode") != 0:
                log.error(f"[BYBIT] Errore update SL: {resp.get('retMsg')}")
                return False
            self._sl = new_sl
            log.info(f"[BYBIT] SL aggiornato a {new_sl:.1f}")
            return True

        except Exception as e:
            log.error(f"[BYBIT] Errore update SL: {e}")
            return False

    # ── Chiusura ─────────────────────────────────────────────────────────────

    def on_close(self, reason: str):
        """
        Chiamato dal bot quando chiude il trade.
        Se la posizione è ancora aperta su Bybit (es. trail hit dal bot ma non
        ancora da Bybit), la chiude con ordine market reduce-only.
        """
        log.info(f"[BYBIT] Posizione chiusa ({reason})")

        if DRY_RUN:
            self._position_side = ""
            return

        if not self._session or not self._position_side:
            self._position_side = ""
            return

        try:
            # Verifica se c'è ancora posizione aperta su Bybit
            pos = self._get_open_position()
            if pos and abs(float(pos.get("size", 0))) > 0:
                close_side = "Sell" if self._position_side == "LONG" else "Buy"
                qty        = pos.get("size", str(self._entry_qty))
                self._session.place_order(
                    category    = CATEGORY,
                    symbol      = SYMBOL,
                    side        = close_side,
                    orderType   = "Market",
                    qty         = str(qty),
                    reduceOnly  = True,
                )
                log.info(f"[BYBIT] Posizione chiusa manualmente ({reason})")
        except Exception as e:
            log.warning(f"[BYBIT] Chiusura: {e}")
        finally:
            self._position_side = ""
            self._entry_qty     = 0.0

    # ── Sync con Bybit ───────────────────────────────────────────────────────

    def sync_position(self) -> Optional[float]:
        """
        Verifica se Bybit ha chiuso la posizione (SL o TP colpito).
        Ritorna il P&L realizzato se chiuso, None se ancora aperta.
        """
        if DRY_RUN or not self._session or not self._position_side:
            return None

        try:
            pos = self._get_open_position()
            if pos is None:
                return None

            size = float(pos.get("size", 1))
            if size < 0.001:
                # Posizione chiusa da Bybit (SL o TP colpito)
                pnl = float(pos.get("unrealisedPnl", 0) or 0)
                # Recupera P&L realizzato dagli ultimi trade chiusi
                closed = self._session.get_closed_pnl(
                    category = CATEGORY,
                    symbol   = SYMBOL,
                    limit    = 1,
                )
                if closed.get("retCode") == 0:
                    items = closed.get("result", {}).get("list", [])
                    if items:
                        pnl = float(items[0].get("closedPnl", pnl))

                log.info(f"[BYBIT] Posizione chiusa da exchange | P&L={pnl:+.2f} USDT")
                self._position_side = ""
                self._entry_qty     = 0.0
                return pnl
        except Exception as e:
            log.debug(f"[BYBIT] sync_position: {e}")
        return None

    # ── Helpers privati ──────────────────────────────────────────────────────

    def _get_open_position(self) -> Optional[dict]:
        try:
            resp = self._session.get_positions(category=CATEGORY, symbol=SYMBOL)
            if resp.get("retCode") == 0:
                positions = resp.get("result", {}).get("list", [])
                for p in positions:
                    if p.get("symbol") == SYMBOL and float(p.get("size", 0)) > 0:
                        return p
        except Exception:
            pass
        return None

    def _get_balance(self) -> float:
        try:
            resp = self._session.get_wallet_balance(
                accountType = "UNIFIED",
                coin        = "USDT",
            )
            if resp.get("retCode") == 0:
                coins = resp.get("result", {}).get("list", [{}])[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        return float(c.get("availableToWithdraw", 0))
        except Exception:
            pass
        return 0.0

    def _reconcile(self):
        """Al restart: verifica se c'è già una posizione aperta su Bybit."""
        pos = self._get_open_position()
        if pos:
            size = float(pos.get("size", 0))
            if size > 0:
                side = pos.get("side", "")
                self._position_side = "LONG" if side == "Buy" else "SHORT"
                self._entry_price   = float(pos.get("avgPrice", 0))
                self._entry_qty     = size
                log.warning(
                    f"[BYBIT] ⚠ Posizione aperta trovata: "
                    f"{self._position_side} {size} BTC @ {self._entry_price:.1f}"
                )

    @property
    def has_open_position(self) -> bool:
        return bool(self._position_side)

"""
binance_spot_executor.py
=========================
Execution layer per structural_bot.py su Binance SPOT o CROSS MARGIN (BTCUSDT).

Modalità SPOT  (BINANCE_MODE=spot):
  • Solo segnali LONG (SHORT skippati)
  • OCO orders per SL+TP

Modalità MARGIN (BINANCE_MODE=margin):
  • LONG e SHORT entrambi supportati
  • Binance AUTO_BORROW_REPAY gestisce il prestito BTC automaticamente
  • OCO su margin per SL+TP

Setup in trade/.env:
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...
    BINANCE_DRY_RUN=true
    BINANCE_TRADE_SIZE_USD=50
    BINANCE_TRADING_HOURS=8-22
    BINANCE_EXECUTOR=true
    BINANCE_MODE=margin            ← spot | margin
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
    from binance.client import Client
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
    from binance.exceptions import BinanceAPIException
    BINANCE_AVAILABLE = True
except ImportError:
    BINANCE_AVAILABLE = False

log = logging.getLogger("bnx_spot")

SYMBOL    = "BTCUSDT"

def _env(k, default=""):
    return os.environ.get(k, default)

DRY_RUN        = _env("BINANCE_DRY_RUN", "true").lower() != "false"
TRADE_SIZE_USD = float(_env("BINANCE_TRADE_SIZE_USD", "50"))
TESTNET        = _env("BINANCE_TESTNET", "false").lower() == "true"
MARGIN_MODE    = _env("BINANCE_MODE", "spot").lower() == "margin"

_hours_raw = _env("BINANCE_TRADING_HOURS", "0-24")
try:
    _h_start, _h_end = map(int, _hours_raw.split("-"))
except Exception:
    _h_start, _h_end = 0, 24


def _in_trading_hours() -> bool:
    if _h_start == 0 and _h_end == 24:
        return True
    h = datetime.now(timezone.utc).hour
    return _h_start <= h < _h_end


class BinanceSpotExecutor:
    """
    Executor spot Binance per structural_bot.py.
    Esegue solo segnali LONG, skippa SHORT con warning.
    SL+TP gestiti tramite OCO order (cancellazione automatica reciproca).
    """

    def __init__(self):
        self._client: Optional["Client"] = None
        self._oco_order_list_id: Optional[int] = None
        self._btc_qty: float    = 0.0
        self._entry_price: float = 0.0
        self._position_open: bool = False
        self._is_long: bool      = True

        if not BINANCE_AVAILABLE:
            log.warning("[BNX-SPOT] python-binance non installato — pip install python-binance")
            return

        if DRY_RUN:
            log.info("[BNX-SPOT] DRY_RUN=True — nessuna tx reale")
            return

        api_key    = _env("BINANCE_API_KEY")
        api_secret = _env("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            log.error("[BNX-SPOT] API key/secret mancanti in .env")
            return

        self._client = Client(api_key, api_secret, testnet=TESTNET)
        mode_str = "CROSS MARGIN" if MARGIN_MODE else "SPOT"
        log.info(f"[BNX] Connesso in modalità {mode_str} — saldo USDT: {self._get_usdt_balance():.2f}")

    def can_trade(self) -> bool:
        if not _in_trading_hours():
            log.debug(f"[BNX-SPOT] Fuori orario ({_h_start}-{_h_end} UTC)")
            return False
        return True

    def enter(self, signal_enum, entry_price: float, sl: float, tp: float,
              size_btc: float) -> bool:
        """
        Apre una posizione LONG spot con OCO order (SL+TP).
        Skippa i segnali SHORT (non eseguibili su spot senza margin).
        """
        from structural_bot import Signal

        is_long  = signal_enum == Signal.LONG
        is_short = signal_enum == Signal.SHORT

        if is_short and not MARGIN_MODE:
            log.warning(
                "[BNX] ⚠ Segnale SHORT skippato — non eseguibile su spot. "
                "Imposta BINANCE_MODE=margin per abilitare gli short."
            )
            return False

        qty = round(size_btc, 5)
        if qty < 0.00001:
            qty = 0.00001

        direction = "LONG" if is_long else "SHORT"
        log.info(f"[BNX] {direction} | qty={qty} BTC | entry≈{entry_price:.1f} | SL={sl:.1f} | TP={tp:.1f}"
                 + (" [MARGIN]" if MARGIN_MODE else ""))

        if DRY_RUN:
            self._btc_qty       = qty
            self._entry_price   = entry_price
            self._position_open = True
            self._is_long       = is_long
            log.info(f"[DRY] {direction} {qty} BTC @ ~{entry_price:.1f} | SL={sl:.1f} TP={tp:.1f}")
            return True

        if not self._client:
            return False

        try:
            entry_side = SIDE_BUY if is_long else SIDE_SELL
            close_side = SIDE_SELL if is_long else SIDE_BUY

            if MARGIN_MODE:
                # ── MARGIN: AUTO_BORROW_REPAY gestisce il prestito BTC per lo short ──
                resp = self._client.create_margin_order(
                    symbol         = SYMBOL,
                    side           = entry_side,
                    type           = ORDER_TYPE_MARKET,
                    quantity       = qty,
                    sideEffectType = "AUTO_BORROW_REPAY",
                )
            else:
                # ── SPOT: solo LONG ──────────────────────────────────────────────────
                resp = self._client.order_market_buy(symbol=SYMBOL, quantity=qty)

            fills    = resp.get("fills", [])
            avg_fill = (sum(float(f["price"]) * float(f["qty"]) for f in fills) /
                        sum(float(f["qty"]) for f in fills)) if fills else entry_price
            self._btc_qty     = float(resp.get("executedQty", qty))
            self._entry_price = avg_fill
            self._position_open = True
            self._is_long     = is_long
            log.info(f"[BNX] ✅ {direction} eseguito: {self._btc_qty:.5f} BTC @ {avg_fill:.1f}")

            time.sleep(0.3)

            # ── OCO: SL+TP come ordine unico (auto-cancellazione reciproca) ──────
            oco_params = dict(
                symbol               = SYMBOL,
                quantity             = round(self._btc_qty, 5),
                price                = f"{tp:.2f}",
                stopPrice            = f"{sl:.2f}",
                stopLimitPrice       = f"{sl * (0.999 if is_long else 1.001):.2f}",
                stopLimitTimeInForce = "GTC",
            )

            if MARGIN_MODE:
                oco_params["sideEffectType"] = "AUTO_REPAY"
                oco = self._client.create_margin_oco_order(
                    side=close_side, **oco_params)
            else:
                oco = self._client.order_oco_sell(**oco_params)

            self._oco_order_list_id = oco.get("orderListId")
            log.info(f"[BNX] OCO piazzato — TP={tp:.1f} SL={sl:.1f} (listId={self._oco_order_list_id})")
            return True

        except BinanceAPIException as e:
            log.error(f"[BNX] Errore entry: {e}")
            return False

    def update_sl(self, new_sl: float) -> bool:
        """
        Aggiorna il trailing stop: cancella OCO, ne piazza uno nuovo.
        Il TP rimane invariato — va recuperato dallo stato del bot.
        """
        if not self._position_open:
            return False

        log.info(f"[BNX] TRAIL SL → {new_sl:.1f}")

        if DRY_RUN:
            return True

        if not self._client:
            return False

        # Recupera TP dall'OCO ancora aperto
        tp_price = None
        try:
            if self._oco_order_list_id:
                get_fn = (self._client.get_margin_oco if MARGIN_MODE
                          else self._client.get_order_list)
                oco_info = get_fn(orderListId=self._oco_order_list_id)
                for o in oco_info.get("orders", []):
                    if o.get("type") == "LIMIT_MAKER":
                        tp_price = float(o.get("price", 0))
                        break
        except Exception:
            pass

        if not tp_price:
            log.warning("[BNX] TP non recuperabile — trailing SL non aggiornato")
            return False

        try:
            # Cancella OCO vecchio
            if self._oco_order_list_id:
                cancel_fn = (self._client.cancel_margin_order_list if MARGIN_MODE
                             else self._client.cancel_order_list)
                cancel_fn(symbol=SYMBOL, orderListId=self._oco_order_list_id)

            # Piazza nuovo OCO con SL aggiornato
            close_side = SIDE_SELL if self._is_long else SIDE_BUY
            sl_limit   = new_sl * (0.999 if self._is_long else 1.001)
            oco_params = dict(
                symbol               = SYMBOL,
                quantity             = round(self._btc_qty, 5),
                price                = f"{tp_price:.2f}",
                stopPrice            = f"{new_sl:.2f}",
                stopLimitPrice       = f"{sl_limit:.2f}",
                stopLimitTimeInForce = "GTC",
            )
            if MARGIN_MODE:
                oco_params["sideEffectType"] = "AUTO_REPAY"
                oco = self._client.create_margin_oco_order(side=close_side, **oco_params)
            else:
                oco = self._client.order_oco_sell(**oco_params)
            self._oco_order_list_id = oco.get("orderListId")
            log.info(f"[BNX] OCO aggiornato: SL={new_sl:.1f} TP={tp_price:.1f}")
            return True

        except BinanceAPIException as e:
            log.error(f"[BNX-SPOT] Errore aggiornamento OCO: {e}")
            return False

    def on_close(self, reason: str):
        """Chiamato quando il bot chiude il trade — cancella OCO residuo."""
        log.info(f"[BNX-SPOT] Posizione chiusa ({reason})")
        if DRY_RUN:
            self._position_open = False
            return

        if self._client and self._oco_order_list_id:
            try:
                self._client.cancel_order_list(
                    symbol=SYMBOL, orderListId=self._oco_order_list_id)
            except Exception:
                pass  # già eseguito/cancellato

        self._oco_order_list_id = None
        self._position_open     = False
        self._btc_qty           = 0.0

    def sync_position(self) -> Optional[float]:
        """
        Verifica se l'OCO è stato eseguito (SL o TP colpito da Binance).
        Ritorna il P&L stimato se chiuso, None altrimenti.
        """
        if DRY_RUN or not self._client or not self._position_open:
            return None
        if not self._oco_order_list_id:
            return None
        try:
            oco = self._client.get_order_list(orderListId=self._oco_order_list_id)
            status = oco.get("listStatusType", "")
            if status == "ALL_DONE":
                # Uno dei due ordini è stato eseguito
                for o in oco.get("orders", []):
                    detail = self._client.get_order(
                        symbol=SYMBOL, orderId=o["orderId"])
                    if detail.get("status") == "FILLED":
                        fill_price = float(detail.get("price", 0) or
                                           detail.get("cummulativeQuoteQty", 0) /
                                           float(detail.get("executedQty", 1) or 1))
                        pnl = (fill_price - self._entry_price) * self._btc_qty
                        log.info(f"[BNX-SPOT] OCO eseguito: fill={fill_price:.1f} | P&L≈{pnl:+.2f} USDT")
                        self._oco_order_list_id = None
                        self._position_open     = False
                        return pnl
        except Exception as e:
            log.debug(f"[BNX-SPOT] sync: {e}")
        return None

    def _get_usdt_balance(self) -> float:
        if not self._client:
            return 0.0
        try:
            account = self._client.get_account()
            for b in account.get("balances", []):
                if b["asset"] == "USDT":
                    return float(b["free"])
        except Exception:
            pass
        return 0.0

    @property
    def has_open_position(self) -> bool:
        return self._position_open

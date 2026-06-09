"""
binance_futures_executor.py
============================
Execution layer per structural_bot.py su Binance Futures (BTCUSDT perp).

Viene istanziato da structural_bot.py e chiamato nei punti chiave:
  • enter()            — apre posizione + piazza SL + TP come bracket orders
  • update_sl()        — aggiorna il trailing stop (cancel vecchio, piazza nuovo)
  • on_close()         — cancella gli ordini residui a posizione chiusa
  • sync_position()    — riconcilia stato locale con Binance a ogni ciclo

DRY_RUN = True  → nessuna chiamata reale, tutto simulato
DRY_RUN = False → trade reali (ATTENZIONE: usa prima testnet)

Setup:
  Aggiungi a trade/.env (o esporta come env vars):
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...
    BINANCE_DRY_RUN=true
    BINANCE_LEVERAGE=1
    BINANCE_TRADE_SIZE_USD=50       # capitale per trade in USD
    BINANCE_TRADING_HOURS=8-22      # solo tra 08:00 e 22:00 UTC (0-24 = sempre)
    BINANCE_TESTNET=false           # true = usa testnet Binance Futures
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
    from binance.enums import (
        SIDE_BUY, SIDE_SELL,
        ORDER_TYPE_MARKET,
        FUTURE_ORDER_TYPE_STOP_MARKET,
        FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    )
    from binance.exceptions import BinanceAPIException
    BINANCE_AVAILABLE = True
except ImportError:
    BINANCE_AVAILABLE = False

log = logging.getLogger("bnx_exec")

SYMBOL   = "BTCUSDT"

def _env(k, default=""):
    return os.environ.get(k, default)

DRY_RUN       = _env("BINANCE_DRY_RUN", "true").lower() != "false"
LEVERAGE      = int(_env("BINANCE_LEVERAGE", "1"))
TRADE_SIZE_USD = float(_env("BINANCE_TRADE_SIZE_USD", "50"))
TESTNET       = _env("BINANCE_TESTNET", "false").lower() == "true"

# Filtro orario UTC: "8-22" → opera solo tra 08:00 e 22:00 UTC
_hours_raw = _env("BINANCE_TRADING_HOURS", "0-24")
try:
    _h_start, _h_end = map(int, _hours_raw.split("-"))
except Exception:
    _h_start, _h_end = 0, 24


def _in_trading_hours() -> bool:
    """Restituisce True se siamo nell'orario operativo configurato."""
    if _h_start == 0 and _h_end == 24:
        return True
    h = datetime.now(timezone.utc).hour
    return _h_start <= h < _h_end


class BinanceFuturesExecutor:
    """
    Gestisce l'esecuzione di un trade alla volta su BTCUSDT perp.
    Bracket order: MARKET entry + STOP_MARKET SL + TAKE_PROFIT_MARKET TP.
    """

    def __init__(self):
        self._client: Optional["Client"] = None
        self._sl_order_id:  Optional[int] = None
        self._tp_order_id:  Optional[int] = None
        self._entry_qty:    float         = 0.0
        self._entry_price:  float         = 0.0
        self._position_side: str          = ""   # "LONG" | "SHORT" | ""

        if not BINANCE_AVAILABLE:
            log.warning("[BNX] python-binance non installato — pip install python-binance")
            return

        if DRY_RUN:
            log.info("[BNX] DRY_RUN=True — nessuna tx reale")
            return

        api_key    = _env("BINANCE_API_KEY")
        api_secret = _env("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            log.error("[BNX] BINANCE_API_KEY / BINANCE_API_SECRET mancanti in .env")
            return

        self._client = Client(api_key, api_secret, testnet=TESTNET)
        # Imposta leva
        try:
            self._client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
            log.info(f"[BNX] Leva impostata: {LEVERAGE}x")
        except Exception as e:
            log.warning(f"[BNX] Errore impostazione leva: {e}")

        # Riconcilia posizione aperta su Binance (es. restart dopo crash)
        self._reconcile()

    # ── Filtro orario ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        if not _in_trading_hours():
            log.debug(f"[BNX] Fuori orario operativo ({_h_start}-{_h_end} UTC) — skip entry")
            return False
        return True

    # ── Entry ────────────────────────────────────────────────────────────────

    def enter(self, signal_enum, entry_price: float, sl: float, tp: float,
              size_contracts: float) -> bool:
        """
        Apre una posizione con bracket order SL+TP.

        signal_enum: Signal.LONG o Signal.SHORT (dall'enum di structural_bot)
        size_contracts: dimensione in BTC (calcolata da calculate_trade)
        """
        # Confronto per nome (non identità): `from structural_bot import Signal` qui
        # creerebbe una classe Enum diversa da __main__.Signal → confronto sempre False
        # e ogni LONG verrebbe inviato all'exchange come SHORT.
        is_long = signal_enum.name == "LONG"
        side = SIDE_BUY if is_long else SIDE_SELL
        sl_side = SIDE_SELL if is_long else SIDE_BUY
        direction = "LONG" if is_long else "SHORT"

        # Arrotonda la quantità a 3 decimali (step size BTCUSDT = 0.001)
        qty = round(size_contracts, 3)
        if qty < 0.001:
            qty = 0.001
            log.warning(f"[BNX] Quantità minima impostata a 0.001 BTC")

        log.info(f"[BNX] ENTER {direction} | qty={qty} BTC | entry≈{entry_price:.1f} "
                 f"| SL={sl:.1f} | TP={tp:.1f}")

        if DRY_RUN:
            self._position_side = direction
            self._entry_qty     = qty
            self._entry_price   = entry_price
            log.info(f"[DRY BNX] {direction} {qty} BTC @ ~{entry_price:.1f} | SL={sl:.1f} TP={tp:.1f}")
            return True

        if not self._client:
            return False
        try:
            # 1. Market order entry
            resp = self._client.futures_create_order(
                symbol   = SYMBOL,
                side     = side,
                type     = ORDER_TYPE_MARKET,
                quantity = qty,
            )
            self._entry_qty   = qty
            self._entry_price = float(resp.get("avgPrice", entry_price) or entry_price)
            self._position_side = direction
            log.info(f"[BNX] ✅ Entry eseguita: {resp.get('orderId')} | fill≈{self._entry_price:.1f}")

            # 2. Stop-loss (STOP_MARKET closePosition)
            sl_resp = self._client.futures_create_order(
                symbol        = SYMBOL,
                side          = sl_side,
                type          = FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice     = round(sl, 1),
                closePosition = "true",
                timeInForce   = "GTE_GTC",
            )
            self._sl_order_id = sl_resp["orderId"]
            log.info(f"[BNX] SL piazzato a {sl:.1f} (id={self._sl_order_id})")

            # 3. Take-profit (TAKE_PROFIT_MARKET closePosition)
            tp_resp = self._client.futures_create_order(
                symbol        = SYMBOL,
                side          = sl_side,
                type          = FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice     = round(tp, 1),
                closePosition = "true",
                timeInForce   = "GTE_GTC",
            )
            self._tp_order_id = tp_resp["orderId"]
            log.info(f"[BNX] TP piazzato a {tp:.1f} (id={self._tp_order_id})")
            return True

        except BinanceAPIException as e:
            log.error(f"[BNX] Errore apertura posizione: {e}")
            self._cancel_all()
            return False

    # ── Trailing SL update ───────────────────────────────────────────────────

    def update_sl(self, new_sl: float) -> bool:
        """Cancella il vecchio SL e piazza il nuovo trailing SL."""
        if not self._position_side:
            return False

        log.info(f"[BNX] TRAIL SL aggiornato → {new_sl:.1f}")

        if DRY_RUN:
            return True

        if not self._client:
            return False
        try:
            # Cancella vecchio SL
            if self._sl_order_id:
                try:
                    self._client.futures_cancel_order(
                        symbol=SYMBOL, orderId=self._sl_order_id)
                except Exception:
                    pass   # potrebbe essere già eseguito

            # Piazza nuovo SL
            sl_side = SIDE_SELL if self._position_side == "LONG" else SIDE_BUY
            resp = self._client.futures_create_order(
                symbol        = SYMBOL,
                side          = sl_side,
                type          = FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice     = round(new_sl, 1),
                closePosition = "true",
                timeInForce   = "GTE_GTC",
            )
            self._sl_order_id = resp["orderId"]
            log.info(f"[BNX] Nuovo SL: {new_sl:.1f} (id={self._sl_order_id})")
            return True
        except BinanceAPIException as e:
            log.error(f"[BNX] Errore aggiornamento SL: {e}")
            return False

    # ── Close ────────────────────────────────────────────────────────────────

    def on_close(self, reason: str):
        """
        Chiamato quando il bot chiude il trade (trail hit, SL, TP, manual).
        Cancella tutti gli ordini aperti residui.
        """
        log.info(f"[BNX] Posizione chiusa ({reason}) — cancello ordini residui")
        self._cancel_all()
        self._position_side = ""
        self._sl_order_id   = None
        self._tp_order_id   = None
        self._entry_qty     = 0.0

    def _cancel_all(self):
        if DRY_RUN or not self._client:
            return
        try:
            self._client.futures_cancel_all_open_orders(symbol=SYMBOL)
            log.debug("[BNX] Tutti gli ordini cancellati")
        except Exception as e:
            log.warning(f"[BNX] Errore cancellazione ordini: {e}")

    # ── Sync / riconciliazione ───────────────────────────────────────────────

    def sync_position(self) -> Optional[float]:
        """
        Verifica se Binance ha chiuso la posizione autonomamente (SL/TP hit).
        Ritorna il P&L realizzato se la posizione è stata chiusa, None altrimenti.
        """
        if DRY_RUN or not self._client or not self._position_side:
            return None
        try:
            positions = self._client.futures_position_information(symbol=SYMBOL)
            for pos in positions:
                if pos["symbol"] == SYMBOL:
                    qty = float(pos["positionAmt"])
                    if abs(qty) < 0.001 and self._position_side:
                        # Posizione chiusa su Binance
                        pnl = float(pos.get("realizedPnl", 0) or 0)
                        log.info(f"[BNX] Posizione chiusa da Binance | P&L={pnl:+.2f} USDT")
                        self._cancel_all()
                        self._position_side = ""
                        self._sl_order_id   = None
                        self._tp_order_id   = None
                        return pnl
        except Exception as e:
            log.debug(f"[BNX] sync_position: {e}")
        return None

    def _reconcile(self):
        """Al restart, verifica se c'è già una posizione aperta su Binance."""
        if not self._client:
            return
        try:
            positions = self._client.futures_position_information(symbol=SYMBOL)
            for pos in positions:
                if pos["symbol"] == SYMBOL:
                    qty = float(pos["positionAmt"])
                    if abs(qty) > 0.001:
                        self._entry_qty    = abs(qty)
                        self._position_side = "LONG" if qty > 0 else "SHORT"
                        self._entry_price  = float(pos.get("entryPrice", 0))
                        log.warning(
                            f"[BNX] ⚠ Posizione aperta trovata su Binance: "
                            f"{self._position_side} {self._entry_qty} BTC @ {self._entry_price:.1f}"
                        )
        except Exception as e:
            log.debug(f"[BNX] reconcile: {e}")

    # ── Info ─────────────────────────────────────────────────────────────────

    def get_balance_usdt(self) -> float:
        """Saldo USDT disponibile nel futures wallet."""
        if DRY_RUN or not self._client:
            return 0.0
        try:
            balances = self._client.futures_account_balance()
            for b in balances:
                if b["asset"] == "USDT":
                    return float(b["availableBalance"])
        except Exception:
            pass
        return 0.0

    @property
    def has_open_position(self) -> bool:
        return bool(self._position_side)

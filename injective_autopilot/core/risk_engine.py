"""
Livello 3 — Risk Engine.

Responsabilità:
  1. Validare ogni decisione di Claude prima dell'esecuzione
  2. Ricalcolare position sizing (override se necessario)
  3. Gestire Stop Loss e Take Profit dinamici
  4. Kill Switch automatico
  5. Calcolare il net R:R (incluso funding cost)

Il Risk Engine è l'ultimo gate prima dell'esecuzione.
Se qualcosa non torna, blocca il trade. Mai bypassare.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config.settings import get_settings
from core.decision_engine import TradeDecision
from signals.derivatives import DerivativesAnalyzer

log = logging.getLogger(__name__)


@dataclass
class RiskValidation:
    approved: bool
    reason: str
    adjusted_decision: TradeDecision | None = None


@dataclass
class KillSwitchState:
    active: bool = False
    reason: str = ""
    activated_at: float = 0.0


@dataclass
class EquityState:
    capital: float = 1000.0
    peak_equity: float = 1000.0
    current_equity: float = 1000.0
    daily_start_equity: float = 1000.0
    weekly_start_equity: float = 1000.0
    daily_start_ts: float = field(default_factory=time.time)
    weekly_start_ts: float = field(default_factory=time.time)

    @property
    def daily_drawdown_pct(self) -> float:
        if self.daily_start_equity < 1e-10:
            return 0.0
        return max(0.0, (self.daily_start_equity - self.current_equity) / self.daily_start_equity)

    @property
    def weekly_drawdown_pct(self) -> float:
        if self.weekly_start_equity < 1e-10:
            return 0.0
        return max(0.0, (self.weekly_start_equity - self.current_equity) / self.weekly_start_equity)

    @property
    def max_drawdown_from_peak(self) -> float:
        if self.peak_equity < 1e-10:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity)


class RiskEngine:
    def __init__(self, deriv_analyzer: DerivativesAnalyzer | None = None) -> None:
        self._cfg = get_settings()
        self._deriv = deriv_analyzer or DerivativesAnalyzer()
        self.kill_switch = KillSwitchState()
        self.equity = EquityState(
            capital=self._cfg.capital_usdt,
            peak_equity=self._cfg.capital_usdt,
            current_equity=self._cfg.capital_usdt,
            daily_start_equity=self._cfg.capital_usdt,
            weekly_start_equity=self._cfg.capital_usdt,
        )
        self._consecutive_errors = 0
        self._open_positions: list[Any] = []

    def validate_decision(
        self,
        decision: TradeDecision,
        margin_available: float,
        current_positions: list[Any],
    ) -> RiskValidation:
        """
        Final validation gate. Checks:
          1. Kill switch state
          2. Existing positions (no duplicate direction)
          3. R:R net of funding ≥ min_rr
          4. Position size within limits
          5. SL/TP sanity
          6. Margin sufficiency
        """
        if self.kill_switch.active:
            return RiskValidation(False, f"Kill switch ACTIVE: {self.kill_switch.reason}")

        if decision.action == "NO_TRADE":
            return RiskValidation(False, "Claude returned NO_TRADE")

        # Max concurrent positions across all markets
        if len(current_positions) >= self._cfg.max_open_positions:
            return RiskValidation(
                False,
                f"Max open positions reached ({self._cfg.max_open_positions})",
            )

        # No duplicate position in the same market
        decision_market = getattr(decision, "market_id", "")
        for pos in current_positions:
            pos_market = getattr(pos, "market_id", "")
            if pos_market and decision_market and pos_market == decision_market:
                return RiskValidation(
                    False,
                    f"Position already open in market {getattr(decision, 'ticker', decision_market)}",
                )
            # Fallback: no duplicate direction globally if market_id not available
            if not pos_market and pos.direction == decision.action.lower():
                return RiskValidation(
                    False,
                    f"Position already open in {decision.action} direction",
                )

        # SL/TP sanity
        if decision.action == "LONG":
            if decision.stop_loss >= decision.entry:
                return RiskValidation(False, f"LONG: SL {decision.stop_loss} >= entry {decision.entry}")
            if decision.take_profit <= decision.entry:
                return RiskValidation(False, f"LONG: TP {decision.take_profit} <= entry {decision.entry}")
        elif decision.action == "SHORT":
            if decision.stop_loss <= decision.entry:
                return RiskValidation(False, f"SHORT: SL {decision.stop_loss} <= entry {decision.entry}")
            if decision.take_profit >= decision.entry:
                return RiskValidation(False, f"SHORT: TP {decision.take_profit} >= entry {decision.entry}")

        # Gross R:R
        risk = abs(decision.entry - decision.stop_loss)
        reward = abs(decision.take_profit - decision.entry)
        if risk < 1e-10:
            return RiskValidation(False, "Zero risk (SL = entry)")

        gross_rr = reward / risk
        if gross_rr < self._cfg.min_rr_ratio * 0.85:  # 15% tolerance on gross
            return RiskValidation(
                False,
                f"R:R too low: {gross_rr:.2f} < {self._cfg.min_rr_ratio}",
            )

        # Net R:R including funding cost
        net_rr = self._deriv.compute_net_rr_with_funding(
            direction=decision.action,
            entry=decision.entry,
            sl=decision.stop_loss,
            tp=decision.take_profit,
            hold_hours=8.0,
        )
        if net_rr < self._cfg.min_rr_ratio - 0.01:
            return RiskValidation(
                False,
                f"Net R:R (with funding) too low: {net_rr:.2f} < {self._cfg.min_rr_ratio}",
            )

        # Position size validation and adjustment
        adjusted = self._adjust_size(decision, margin_available)

        if adjusted.position_size < 1e-6:
            return RiskValidation(False, "Position size too small after risk adjustment")

        # Check margin requirement (rough estimate: entry × qty / leverage)
        required_margin = adjusted.entry * adjusted.position_size / self._cfg.max_leverage
        if required_margin > margin_available * 0.9:
            return RiskValidation(
                False,
                f"Insufficient margin: need {required_margin:.2f} USDT, have {margin_available:.2f}",
            )

        return RiskValidation(approved=True, reason="OK", adjusted_decision=adjusted)

    def _adjust_size(self, decision: TradeDecision, margin_available: float) -> TradeDecision:
        """
        Override position size using Risk Engine's own calculation.
        Uses volatility-adjusted Kelly-inspired sizing.

        size = (capital × max_position_pct × confidence) / ATR_equivalent
        """
        risk_per_trade = abs(decision.entry - decision.stop_loss)
        if risk_per_trade < 1e-10:
            return decision

        max_dollar_risk = self.equity.current_equity * self._cfg.max_position_pct * decision.confidence
        qty_by_risk = max_dollar_risk / risk_per_trade

        # Cap by leverage
        max_qty_by_leverage = (margin_available * self._cfg.max_leverage) / (decision.entry + 1e-10)

        # Cap by margin available (conservative: 80% of available)
        max_qty_by_margin = (margin_available * 0.80 * self._cfg.max_leverage) / (decision.entry + 1e-10)

        final_qty = min(qty_by_risk, max_qty_by_leverage, max_qty_by_margin, decision.position_size or 1e9)

        from dataclasses import replace
        return replace(decision, position_size=round(final_qty, 6))

    def check_kill_switch(self, margin_used_pct: float = 0.0) -> bool:
        """
        Returns True if kill switch should activate.
        Call this every tick to monitor circuit breakers.
        """
        if self.kill_switch.active:
            return True

        dd_limit = (
            self._cfg.paper_max_daily_drawdown_pct
            if self._cfg.mode in ("PAPER", "BACKTEST")
            else self._cfg.max_daily_drawdown_pct
        )
        if self.equity.daily_drawdown_pct >= dd_limit:
            self._activate_kill(
                f"Daily DD {self.equity.daily_drawdown_pct*100:.1f}% >= {dd_limit*100:.1f}%"
            )
            return True

        weekly_limit = (
            self._cfg.paper_max_weekly_drawdown_pct
            if self._cfg.mode in ("PAPER", "BACKTEST")
            else self._cfg.max_weekly_drawdown_pct
        )
        if self.equity.weekly_drawdown_pct >= weekly_limit:
            self._activate_kill(
                f"Weekly DD {self.equity.weekly_drawdown_pct*100:.1f}% >= {weekly_limit*100:.1f}%"
            )
            return True

        if margin_used_pct >= self._cfg.max_margin_used_pct:
            self._activate_kill(
                f"Margin used {margin_used_pct*100:.1f}% >= {self._cfg.max_margin_used_pct*100:.1f}%"
            )
            return True

        if self._consecutive_errors >= self._cfg.max_consecutive_errors:
            self._activate_kill(f"{self._consecutive_errors} consecutive errors")
            return True

        return False

    def _activate_kill(self, reason: str) -> None:
        self.kill_switch = KillSwitchState(
            active=True,
            reason=reason,
            activated_at=time.time(),
        )
        log.critical("KILL SWITCH ACTIVATED: %s", reason)

    def reset_kill_switch(self) -> None:
        """Manual reset — requires human confirmation.

        Riallinea anche le baseline daily/weekly all'equity corrente: senza
        questo, con equity già sotto soglia il DD resta >= limite e il kill
        switch si riattiva al primo check (trappola permanente, visto 10/06
        con equity 884/1000 → weekly DD 11.5% perenne)."""
        self.kill_switch = KillSwitchState()
        now = time.time()
        self.equity.daily_start_equity = self.equity.current_equity
        self.equity.daily_start_ts = now
        self.equity.weekly_start_equity = self.equity.current_equity
        self.equity.weekly_start_ts = now
        log.warning(
            "Kill switch manually reset — baseline daily/weekly riallineate a %.2f",
            self.equity.current_equity,
        )

    def update_equity(self, current_value: float) -> None:
        """Call after each trade close or periodically."""
        now = time.time()
        self.equity.current_equity = current_value

        if current_value > self.equity.peak_equity:
            self.equity.peak_equity = current_value

        # Reset daily tracking
        if now - self.equity.daily_start_ts >= 86400:
            self.equity.daily_start_equity = current_value
            self.equity.daily_start_ts = now

        # Reset weekly tracking
        if now - self.equity.weekly_start_ts >= 604800:
            self.equity.weekly_start_equity = current_value
            self.equity.weekly_start_ts = now

    def record_error(self) -> None:
        self._consecutive_errors += 1

    def clear_errors(self) -> None:
        self._consecutive_errors = 0

    def dynamic_sl(self, entry: float, atr: float, direction: str) -> float:
        """
        ATR-based stop loss.
        LONG: entry - ATR × multiplier
        SHORT: entry + ATR × multiplier
        """
        offset = atr * self._cfg.atr_sl_multiplier
        if direction == "LONG":
            return entry - offset
        return entry + offset

    def dynamic_tp(self, entry: float, atr: float, direction: str) -> float:
        """ATR-based take profit (minimum 2:1 R:R built in via multiplier ratio)."""
        offset = atr * self._cfg.atr_tp_multiplier
        if direction == "LONG":
            return entry + offset
        return entry - offset

    def should_trail_stop(
        self,
        current_price: float,
        entry: float,
        current_sl: float,
        direction: str,
        atr: float,
        trail_activate_pct: float = 0.50,
    ) -> float:
        """
        Trailing stop: once in profit by trail_activate_pct × ATR,
        move SL to break even + ATR × 0.5 behind price.
        Returns new SL (or current SL if no trail).
        """
        if direction == "LONG":
            pnl_pct = (current_price - entry) / (entry + 1e-10)
            if pnl_pct >= trail_activate_pct * atr / (entry + 1e-10):
                new_sl = current_price - atr * 0.5
                return max(current_sl, new_sl)
        elif direction == "SHORT":
            pnl_pct = (entry - current_price) / (entry + 1e-10)
            if pnl_pct >= trail_activate_pct * atr / (entry + 1e-10):
                new_sl = current_price + atr * 0.5
                return min(current_sl, new_sl)
        return current_sl

    @property
    def risk_dashboard(self) -> dict:
        return {
            "current_equity": self.equity.current_equity,
            "peak_equity": self.equity.peak_equity,
            "daily_drawdown_pct": self.equity.daily_drawdown_pct * 100,
            "weekly_drawdown_pct": self.equity.weekly_drawdown_pct * 100,
            "max_drawdown_from_peak_pct": self.equity.max_drawdown_from_peak * 100,
            "kill_switch_active": self.kill_switch.active,
            "kill_switch_reason": self.kill_switch.reason,
            "consecutive_errors": self._consecutive_errors,
        }

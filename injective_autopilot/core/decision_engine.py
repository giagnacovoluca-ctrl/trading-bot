"""
Livello 2 — Motore Decisionale (Claude Code).

Viene invocato SOLO quando la Sentinella genera un trigger.
Supporta due modalità di invocazione:
  1. subprocess — chiama il CLI `claude` (default)
  2. SDK        — chiama direttamente l'API Anthropic

Restituisce SOLO JSON strutturato. Mai testo libero.

Costo computazionale: ~3-15s per invocazione.
Frequenza attesa: 0-3 volte/ora in condizioni normali.

IMPORTANTE: Claude NON è nel critical path del timing dell'ordine.
Claude decide SE entrare e DOVE (entry/SL/TP).
Il momento esatto dell'esecuzione è gestito dal Risk Engine locale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# ── Decision dataclass ───────────────────────────────────────────────────────


@dataclass
class TradeDecision:
    action: str             # "LONG" | "SHORT" | "NO_TRADE"
    confidence: float       # [0, 1]
    entry: float
    stop_loss: float
    take_profit: float
    position_size: float    # in base asset units
    risk_score: float       # [0, 1]; higher = riskier
    reason: str
    market_id: str = ""
    ticker: str = ""
    raw_response: str = ""
    latency_ms: float = 0.0
    model: str = ""


_NO_TRADE = TradeDecision(
    action="NO_TRADE",
    confidence=0.0,
    entry=0.0,
    stop_loss=0.0,
    take_profit=0.0,
    position_size=0.0,
    risk_score=1.0,
    reason="Default no-trade",
)

# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an expert quantitative trading analyst for Injective Protocol perpetual markets.

You receive structured market data from a quantitative sentinel system that has already filtered for statistical anomalies. Your job is to make the final trading decision.

CRITICAL RULES:
1. Return ONLY valid JSON. No prose, no markdown, no explanations outside the JSON.
2. Be conservative: when in doubt, action = "NO_TRADE".
3. Risk score reflects execution risk (liquidity, timing, market conditions).
4. Confidence must be based on signal convergence, not gut feeling.
5. Stop loss must be below entry for LONG, above entry for SHORT.
6. Take profit must give minimum 2.0 R:R net of funding.
7. Position size must be calibrated to risk_pct × capital, never exceed max_leverage.

Required JSON schema:
{
  "action": "LONG|SHORT|NO_TRADE",
  "confidence": <float 0.0-1.0>,
  "entry": <float>,
  "stop_loss": <float>,
  "take_profit": <float>,
  "position_size": <float>,
  "risk_score": <float 0.0-1.0>,
  "reason": "<concise explanation, max 200 chars>"
}"""

# ── Prompt builder ───────────────────────────────────────────────────────────


def _build_prompt(
    trigger: Any,
    positions: list[Any],
    margin_available: float,
    capital: float,
    max_leverage: float,
    min_rr: float,
) -> str:
    """Builds the user prompt with all market context."""

    ob = trigger.orderbook_snapshot
    mkt = trigger.market_snapshot
    sv = trigger.signal_values

    pos_info = "FLAT"
    if positions:
        pos = positions[0]
        pos_info = (
            f"OPEN {pos.direction.upper()} qty={pos.quantity:.4f} "
            f"entry={pos.entry_price:.4f} upnl={pos.unrealized_pnl:.2f}$"
        )

    ticker = getattr(trigger, "ticker", trigger.market_id[:8])
    prompt = f"""MARKET: {ticker}/USDC PERP  [{trigger.market_id}]
TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(trigger.ts))}

SENTINEL TRIGGER:
  Direction bias: {trigger.direction_bias}
  Active signals: {', '.join(trigger.active_signals)}
  Tier-S (funding extreme): {trigger.tier_s}

MARKET SNAPSHOT:
  Mark price: {mkt['mark_price']:.4f}
  Oracle price: {mkt['oracle_price']:.4f}
  Funding rate (hourly): {mkt['funding_rate']:.6f} ({mkt['funding_rate']*100:.4f}%)
  Open interest: {mkt['open_interest']:.2f}

ORDERBOOK (top 5):
  Best bid: {ob['bids'][0][0] if ob['bids'] else 'N/A'}
  Best ask: {ob['asks'][0][0] if ob['asks'] else 'N/A'}
  Spread: {ob['spread_bps']:.2f} bps
  Bids: {ob['bids'][:5]}
  Asks: {ob['asks'][:5]}

SIGNAL VALUES:
  OBI: {sv['obi']:.3f} (threshold ±0.60)
  Funding Z-Score: {sv['funding_zscore']:.2f}
  CVD divergence: {sv['cvd_divergence']:.3f}
  ATR: {sv['atr']:.4f} ({sv['atr_pct']*100:.3f}% of price)
  Vol regime: {sv['vol_regime']} (ratio {sv['vol_ratio']:.2f})
  OI divergence pattern: {sv['oi_div_pattern']}
  Z-Score: {sv['zscore']:.2f}
  Spread Z-Score: {sv['spread_zscore']:.2f}
  Vote balance: LONG={sv['votes_long']} SHORT={sv['votes_short']}

CURRENT POSITION: {pos_info}
MARGIN AVAILABLE: {margin_available:.2f} USDT
CAPITAL: {capital:.2f} USDT
MAX LEVERAGE: {max_leverage}x
MIN R:R REQUIRED: {min_rr}:1 (net of funding)

Analyse the above data and return your trading decision as JSON.
If a position is already open in the same direction, action should be NO_TRADE.
"""
    return prompt


# ── Decision engine ──────────────────────────────────────────────────────────


class DecisionEngine:
    def __init__(
        self,
        model: str = "claude-opus-4-8",
        timeout_sec: int = 45,
        min_confidence: float = 0.65,
        use_subprocess: bool = True,
        capital: float = 1000.0,
        max_leverage: float = 5.0,
        min_rr: float = 2.0,
    ) -> None:
        self.model = model
        self.timeout_sec = timeout_sec
        self.min_confidence = min_confidence
        self.use_subprocess = use_subprocess
        self.capital = capital
        self.max_leverage = max_leverage
        self.min_rr = min_rr

        self._total_calls = 0
        self._approved = 0
        self._rejected = 0

    async def decide(
        self,
        trigger: Any,
        positions: list[Any],
        margin_available: float,
    ) -> TradeDecision:
        """
        Core decision method. Returns TradeDecision.
        On any error, returns NO_TRADE (fail-safe).
        """
        t0 = time.monotonic()
        self._total_calls += 1

        prompt = _build_prompt(
            trigger=trigger,
            positions=positions,
            margin_available=margin_available,
            capital=self.capital,
            max_leverage=self.max_leverage,
            min_rr=self.min_rr,
        )

        try:
            if self.use_subprocess:
                raw = await self._call_subprocess(prompt)
            else:
                raw = await self._call_sdk(prompt)
        except asyncio.TimeoutError:
            log.warning("Claude decision timeout after %ds", self.timeout_sec)
            return _NO_TRADE
        except Exception as exc:
            log.error("Claude decision error: %s", exc)
            return _NO_TRADE

        latency_ms = (time.monotonic() - t0) * 1000.0
        decision = self._parse_response(raw, latency_ms)

        # Stamp market context from trigger
        from dataclasses import replace as _replace
        decision = _replace(
            decision,
            market_id=getattr(trigger, "market_id", ""),
            ticker=getattr(trigger, "ticker", ""),
        )

        if decision.action != "NO_TRADE" and decision.confidence < self.min_confidence:
            log.info(
                "Decision rejected: confidence %.2f < threshold %.2f",
                decision.confidence,
                self.min_confidence,
            )
            self._rejected += 1
            return TradeDecision(
                action="NO_TRADE",
                confidence=decision.confidence,
                entry=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                position_size=0.0,
                risk_score=decision.risk_score,
                reason=f"Confidence too low ({decision.confidence:.2f})",
                latency_ms=latency_ms,
                model=self.model,
            )

        if decision.action != "NO_TRADE":
            self._approved += 1
            log.info(
                "Decision APPROVED: %s conf=%.2f entry=%.4f sl=%.4f tp=%.4f",
                decision.action,
                decision.confidence,
                decision.entry,
                decision.stop_loss,
                decision.take_profit,
            )
        else:
            self._rejected += 1

        return decision

    async def _call_subprocess(self, prompt: str) -> str:
        """Invoke `claude` CLI via subprocess."""
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--model", self.model,
                "--print",
                "--max-turns", "1",
                "--system-prompt", _SYSTEM_PROMPT,
                prompt,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_sec
            )
            if stderr:
                log.debug("Claude stderr: %s", stderr.decode()[:200])
            return stdout.decode().strip()
        finally:
            os.unlink(prompt_file)

    async def _call_sdk(self, prompt: str) -> str:
        """Invoke Anthropic SDK directly."""
        import anthropic

        client = anthropic.AsyncAnthropic()
        msg = await asyncio.wait_for(
            client.messages.create(
                model=self.model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=self.timeout_sec,
        )
        return msg.content[0].text.strip()

    def _parse_response(self, raw: str, latency_ms: float) -> TradeDecision:
        """Extract JSON from Claude's response. Robust to markdown code blocks."""
        try:
            # Strip markdown code fences if present
            text = raw
            if "```" in text:
                parts = text.split("```")
                for part in parts:
                    stripped = part.strip().lstrip("json").strip()
                    if stripped.startswith("{"):
                        text = stripped
                        break

            # Find JSON object
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found in response")

            data: dict[str, Any] = json.loads(text[start:end])

            action = str(data.get("action", "NO_TRADE")).upper()
            if action not in ("LONG", "SHORT", "NO_TRADE"):
                action = "NO_TRADE"

            return TradeDecision(
                action=action,
                confidence=float(data.get("confidence", 0.0)),
                entry=float(data.get("entry", 0.0)),
                stop_loss=float(data.get("stop_loss", 0.0)),
                take_profit=float(data.get("take_profit", 0.0)),
                position_size=float(data.get("position_size", 0.0)),
                risk_score=float(data.get("risk_score", 1.0)),
                reason=str(data.get("reason", ""))[:500],
                raw_response=raw[:2000],
                latency_ms=latency_ms,
                model=self.model,
            )

        except Exception as exc:
            log.error("Failed to parse Claude response: %s | raw: %s", exc, raw[:200])
            return TradeDecision(
                action="NO_TRADE",
                confidence=0.0,
                entry=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                position_size=0.0,
                risk_score=1.0,
                reason=f"Parse error: {exc}",
                raw_response=raw[:2000],
                latency_ms=latency_ms,
            )

    @property
    def stats(self) -> dict[str, Any]:
        approval_rate = self._approved / (self._total_calls + 1e-10)
        return {
            "total_calls": self._total_calls,
            "approved": self._approved,
            "rejected": self._rejected,
            "approval_rate": approval_rate,
        }

import asyncio
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
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

from config.settings import Settings
from data.injective_client import InjectiveClient
import defi.tg_alert as tg_alert

log = logging.getLogger("funding_farmer_auto")

POLL_HOURS = 4
FUNDING_THRESHOLD = 0.0003   # 0.03%/8h → APY ~13%
FUNDING_CLOSE_THRESHOLD = 0.0001 # 0.01%/8h
SL_PCT = -0.02 # -2% Stop Loss
MAX_HOLD_HOURS = 72
FUNDING_GREAT = 0.0008

_REPORTS = _HERE / "reports"
_CSV_OUT = _REPORTS / "funding_trades.csv"

def _get_market_names() -> dict[str, str]:
    try:
        from config.settings import MARKET_NAMES
        return MARKET_NAMES
    except Exception:
        return {}


class Position:
    def __init__(self, market_id: str, ticker: str, side: str, entry_price: float, size_usd: float, entry_cum_funding: float):
        self.market_id = market_id
        self.ticker = ticker
        self.side = side  # "SHORT" o "LONG"
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.size_qty = size_usd / entry_price if entry_price > 0 else 0
        self.entry_cum_funding = entry_cum_funding
        
        self.entry_ts = time.time()
        self.funding_collected = 0.0
        self.unrealized_pnl = 0.0
        self.hedge_pnl = 0.0
        self.last_cum_funding = entry_cum_funding

    def update_pnl(self, current_price: float, current_cum_funding: float):
        # PnL price
        if self.side == "LONG":
            self.unrealized_pnl = (current_price - self.entry_price) / self.entry_price
            self.hedge_pnl = (self.entry_price - current_price) / self.entry_price # Short spot
        else:
            self.unrealized_pnl = (self.entry_price - current_price) / self.entry_price
            self.hedge_pnl = (current_price - self.entry_price) / self.entry_price # Long spot
            
        # Update funding
        delta_funding = current_cum_funding - self.last_cum_funding
        if self.side == "SHORT":
            # If we are short, we receive funding when rate > 0 (which means cumulative increases)
            earned = delta_funding * self.size_usd
        else:
            # If we are long, we receive funding when rate < 0
            earned = -delta_funding * self.size_usd
            
        self.funding_collected += earned
        self.last_cum_funding = current_cum_funding


class FundingFarmerAuto:
    def __init__(self, mode='PAPER', max_positions=3, position_size_usd=50):
        self.mode = mode
        self.max_positions = max_positions
        self.position_size_usd = position_size_usd
        self.positions: dict[str, Position] = {} # market_id -> Position
        self.prev_cumulative: dict[str, float] = {}
        self.prev_poll_ts: float = 0.0
        self.cfg = Settings()
        self.client = InjectiveClient(self.cfg)
        self.market_names = _get_market_names()
        
        _REPORTS.mkdir(parents=True, exist_ok=True)
        if not _CSV_OUT.exists():
            with open(_CSV_OUT, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ts", "market", "direction", "entry_price", "size", "funding_collected", "unrealized_pnl", "hedge_pnl", "status"])

    def _log_trade(self, p: Position, status: str):
        with open(_CSV_OUT, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.now().isoformat(),
                p.ticker,
                p.side,
                f"{p.entry_price:.6f}",
                f"{p.size_usd:.2f}",
                f"{p.funding_collected:.4f}",
                f"{p.unrealized_pnl:.4f}",
                f"{p.hedge_pnl:.4f}",
                status
            ])

    async def scan_funding_rates(self) -> list[dict]:
        """Scan all markets, return opportunities sorted by APY"""
        try:
            await self.client.connect()
        except Exception as e:
            log.error(f"[funding_auto] connect error: {e}")
            return []

        now = time.time()
        elapsed_hours = (now - self.prev_poll_ts) / 3600.0 if self.prev_poll_ts else 0.0
        is_first_poll = self.prev_poll_ts == 0.0

        current_cumulative: dict[str, float] = {}
        raw_snapshots = []

        for market_id in self.cfg.market_ids:
            try:
                snap = await asyncio.wait_for(
                    self.client.fetch_market_snapshot(market_id=market_id), timeout=10.0
                )
                if not snap:
                    continue
                cum = snap.funding_rate
                mid = getattr(snap, "mid", 0) or 0
                ticker = self.market_names.get(market_id, market_id[:16])
                current_cumulative[market_id] = cum
                raw_snapshots.append((market_id, ticker, cum, mid))
                
                # Update positions
                if market_id in self.positions:
                    self.positions[market_id].update_pnl(mid, cum)
                    
            except asyncio.TimeoutError:
                log.warning(f"[funding_auto] {market_id[:16]}: fetch timeout (10s)")
            except Exception as e:
                log.debug(f"[funding_auto] {market_id[:16]}: {e}")

        try:
            await self.client.close()
        except Exception:
            pass

        if is_first_poll:
            log.info("[funding_auto] primo poll — accumulo baseline")
            self.prev_cumulative = current_cumulative
            self.prev_poll_ts = now
            return []

        results = []
        for market_id, ticker, cum, mid in raw_snapshots:
            prev_cum = self.prev_cumulative.get(market_id)
            if prev_cum is None:
                continue
            delta = cum - prev_cum
            rate_8h = delta / elapsed_hours * 8.0 if elapsed_hours > 0.01 else 0.0
            
            # Check thresholds
            if abs(rate_8h) >= FUNDING_THRESHOLD:
                side = "SHORT" if rate_8h > 0 else "LONG"
                apy_pct = abs(rate_8h) * 3 * 365 * 100
                results.append({
                    "market_id": market_id,
                    "ticker": ticker,
                    "funding_rate_8h": rate_8h,
                    "apy_pct": apy_pct,
                    "side": side,
                    "mid_price": mid,
                    "cum_funding": cum
                })
                
        self.prev_cumulative = current_cumulative
        self.prev_poll_ts = now

        return sorted(results, key=lambda x: abs(x["funding_rate_8h"]), reverse=True)

    async def check_exits(self):
        """Check SL, funding threshold, max hold time"""
        to_close = []
        now = time.time()
        for market_id, p in self.positions.items():
            held_hours = (now - p.entry_ts) / 3600.0
            
            # Re-calculate current rate_8h for this market if possible
            current_cum = self.prev_cumulative.get(market_id)
            rate_8h = 0.0
            if current_cum is not None and p.last_cum_funding != current_cum:
                # Approximate last cycle rate (we just updated prev_cumulative in scan)
                pass # Already handled by opportunities logic, but let's check basic conditions

            close_reason = None
            if p.unrealized_pnl <= SL_PCT:
                close_reason = f"SL ({p.unrealized_pnl*100:.2f}%)"
            elif held_hours >= MAX_HOLD_HOURS:
                close_reason = f"Max hold ({held_hours:.1f}h)"
                
            # Delta-Neutral Hedge PnL (teorico)
            hedge_side = "BUY" if p.side == "SHORT" else "SELL"
            
            if close_reason:
                log.info(f"[funding_auto] Closing {p.ticker} {p.side}: {close_reason}. PnL: {p.unrealized_pnl*100:.2f}%, Funding: ${p.funding_collected:.4f}")
                tg_alert.send(
                    f"⚠️ <b>Funding Farmer: Chiusura Posizione ({self.mode})</b>\n\n"
                    f"Mercato: {p.ticker}\n"
                    f"Motivo: {close_reason}\n"
                    f"Funding incassato: ${p.funding_collected:.4f}\n"
                    f"PnL Non Realizzato (Perp): {p.unrealized_pnl*100:.2f}%\n\n"
                    f"<i>Spot Hedge: Chiudere posizione {hedge_side} spot su Binance.</i>"
                )
                self._log_trade(p, f"CLOSED - {close_reason}")
                to_close.append(market_id)

        for mid in to_close:
            del self.positions[mid]

    async def manage_positions(self, opportunities: list[dict]):
        """Open new / close existing positions based on opportunities"""
        
        # Check if existing positions lost their funding opportunity (drop below threshold)
        to_close = []
        for market_id, p in self.positions.items():
            # Find in opportunities
            opp = next((o for o in opportunities if o["market_id"] == market_id), None)
            if not opp:
                # Not in opportunities -> rate is below threshold
                log.info(f"[funding_auto] {p.ticker}: funding rate drop below threshold, closing.")
                self._log_trade(p, "CLOSED - rate drop")
                tg_alert.send(f"⚠️ <b>Funding Farmer: Rate Drop ({self.mode})</b>\n{p.ticker} chiuso per calo funding.\nIncassato: ${p.funding_collected:.4f}")
                to_close.append(market_id)
                continue
            
            # Check if side changed
            if opp["side"] != p.side:
                log.info(f"[funding_auto] {p.ticker}: funding side flip, closing.")
                self._log_trade(p, "CLOSED - side flip")
                tg_alert.send(f"⚠️ <b>Funding Farmer: Side Flip ({self.mode})</b>\n{p.ticker} chiuso per inversione funding.\nIncassato: ${p.funding_collected:.4f}")
                to_close.append(market_id)

        for mid in to_close:
            del self.positions[mid]

        # Open new positions
        for opp in opportunities:
            if len(self.positions) >= self.max_positions:
                break
            
            market_id = opp["market_id"]
            if market_id not in self.positions:
                ticker = opp["ticker"]
                side = opp["side"]
                price = opp["mid_price"]
                cum_funding = opp["cum_funding"]
                
                if price <= 0:
                    continue
                
                p = Position(market_id, ticker, side, price, self.position_size_usd, cum_funding)
                self.positions[market_id] = p
                self._log_trade(p, "OPEN")
                
                hedge_side = "BUY" if side == "SHORT" else "SELL"
                qty = self.position_size_usd / price
                
                log.info(f"[funding_auto] Opened {side} on {ticker} at {price}. Hedge: {hedge_side} {qty:.4f} {ticker.split('/')[0]} spot.")
                
                tg_alert.send(
                    f"🚀 <b>Funding Farmer: Nuova Posizione ({self.mode})</b>\n\n"
                    f"Mercato: {ticker}\n"
                    f"Direzione (Perp): {side}\n"
                    f"Size: ${self.position_size_usd} ({qty:.4f})\n"
                    f"APY Atteso: {opp['apy_pct']:.1f}%\n\n"
                    f"<i>Azione raccomandata Delta-Neutral:\n"
                    f"→ Spot Hedge: {hedge_side} {qty:.4f} su Binance.</i>"
                )

    async def run(self):
        """Main loop: scan → manage → check_exits, every 4h"""
        log.info(f"[funding_auto] ▶ Avviato bot automatico ({self.mode} MODE). Max pos: {self.max_positions}, Size: ${self.position_size_usd}")
        while True:
            try:
                opps = await self.scan_funding_rates()
                
                # Check exits (SL, max hold) for existing positions
                await self.check_exits()
                
                if opps:
                    await self.manage_positions(opps)
                    
                # Logging summary
                if self.positions:
                    log.info(f"--- Posizioni Aperte ({len(self.positions)}/{self.max_positions}) ---")
                    total_funding = 0.0
                    for mid, p in self.positions.items():
                        total_funding += p.funding_collected
                        net_yield = p.funding_collected + (p.unrealized_pnl + p.hedge_pnl)*p.size_usd
                        log.info(f" - {p.ticker} {p.side}: PnL {p.unrealized_pnl*100:.2f}%, Hedge {p.hedge_pnl*100:.2f}%, Funding {p.funding_collected:.4f}$ (Net: {net_yield:.4f}$)")
                    log.info(f"Totale funding accumulato: {total_funding:.4f}$")
                else:
                    log.info("[funding_auto] Nessuna posizione aperta.")
                    
            except Exception as e:
                log.error(f"[funding_auto] Loop error: {e}", exc_info=True)
                
            await asyncio.sleep(POLL_HOURS * 3600)

def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(_REPORTS / "funding_farmer_auto.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
    )
    
    bot = FundingFarmerAuto(mode='PAPER', max_positions=3, position_size_usd=50)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()

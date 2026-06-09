"""
Injective data client — compatibile con injective-py >= 1.15.0 (async_client_v2).

Tutte le risposte SDK sono dict in camelCase (via protobuf MessageToDict).
I prezzi su Injective sono stringhe in unità 1e18 (cosmwasm Dec).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Coroutine

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

# ── Data contracts ──────────────────────────────────────────────────────────


@dataclass
class OrderLevel:
    price: float
    quantity: float


@dataclass
class OrderbookSnapshot:
    ts: float
    market_id: str
    bids: list[OrderLevel]  # sorted desc
    asks: list[OrderLevel]  # sorted asc
    mid: float = 0.0

    def __post_init__(self) -> None:
        if self.bids and self.asks:
            self.mid = (self.bids[0].price + self.asks[0].price) / 2.0

    @property
    def spread(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return self.asks[0].price - self.bids[0].price

    @property
    def spread_bps(self) -> float:
        if self.mid < 1e-10:
            return 0.0
        return self.spread / self.mid * 10_000


@dataclass
class TradeEvent:
    ts: float
    price: float
    quantity: float
    is_buy: bool


@dataclass
class MarketSnapshot:
    ts: float
    market_id: str
    mark_price: float
    oracle_price: float
    funding_rate: float    # hourly cumulative rate as decimal
    open_interest: float   # in base asset units
    volume_24h: float = 0.0


@dataclass
class PositionInfo:
    market_id: str
    subaccount_id: str
    direction: str         # "long" | "short"
    quantity: float
    entry_price: float
    margin: float
    unrealized_pnl: float
    liquidation_price: float


@dataclass
class OrderResult:
    success: bool
    tx_hash: str = ""
    order_hash: str = ""
    error: str = ""


# ── Client ──────────────────────────────────────────────────────────────────


class InjectiveClient:
    def __init__(
        self,
        network: str = "testnet",
        market_id: str = "",
        private_key: str = "",
        subaccount_index: int = 0,
        fee_recipient: str = "",
    ) -> None:
        self.network = network
        self.market_id = market_id
        self.private_key = private_key
        self.subaccount_index = subaccount_index
        self.fee_recipient = fee_recipient

        self._client: Any = None
        self._composer: Any = None
        self._broadcaster: Any = None
        self._address: Any = None
        self._subaccount_id: str = ""

    async def connect(self) -> None:
        try:
            from pyinjective.async_client_v2 import AsyncClient
            from pyinjective.core.network import Network
        except ImportError as exc:
            raise RuntimeError(
                "pyinjective not installed — run: pip install injective-py"
            ) from exc

        net = Network.mainnet() if self.network == "mainnet" else Network.testnet()
        self._client = AsyncClient(network=net)

        # Wallet + broadcaster solo in LIVE mode
        if self.private_key:
            from pyinjective.wallet import PrivateKey
            from pyinjective.core.broadcaster import (
                MsgBroadcasterWithPk,
                StandardAccountBroadcasterConfig,
                SimulatedTransactionFeeCalculator,
            )
            from pyinjective.composer_v2 import Composer

            pk = PrivateKey.from_hex(self.private_key)
            pub_key = pk.to_public_key()
            self._address = pub_key.to_address()
            self._subaccount_id = self._address.get_subaccount_id(index=self.subaccount_index)

            account_config = StandardAccountBroadcasterConfig(private_key=self.private_key)
            composer = Composer(network=net.string())
            fee_calculator = SimulatedTransactionFeeCalculator(
                client=self._client, composer=composer
            )
            self._broadcaster = MsgBroadcasterWithPk(
                network=net,
                account_config=account_config,
                client=self._client,
                fee_calculator=fee_calculator,
            )
            self._composer = await self._client.composer()

        log.info("InjectiveClient connected (network=%s)", self.network)

    # ── Market data ──────────────────────────────────────────────────────────

    async def fetch_orderbook(self, depth: int = 20, market_id: str = "") -> OrderbookSnapshot:
        """Fetch L3 (full) derivative orderbook."""
        mid = market_id or self.market_id
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                res = await self._client.fetch_l3_derivative_orderbook(
                    market_id=mid
                )

        # SDK v1.15 uses capital keys "Bids"/"Asks" (from protobuf MessageToDict)
        bids = [
            OrderLevel(
                price=self._from_chain_price(b.get("price", "0")),
                quantity=self._from_chain_qty(b.get("quantity", "0")),
            )
            for b in (res.get("Bids") or res.get("buys", []))[:depth]
        ]
        asks = [
            OrderLevel(
                price=self._from_chain_price(a.get("price", "0")),
                quantity=self._from_chain_qty(a.get("quantity", "0")),
            )
            for a in (res.get("Asks") or res.get("sells", []))[:depth]
        ]
        return OrderbookSnapshot(
            ts=time.time(),
            market_id=mid,
            bids=sorted(bids, key=lambda x: -x.price),
            asks=sorted(asks, key=lambda x: x.price),
        )

    async def fetch_recent_trades(self, limit: int = 100, market_id: str = "") -> list[TradeEvent]:
        """Fetch recent historical trades."""
        mid = market_id or self.market_id
        try:
            res = await self._client.fetch_historical_trade_records(
                market_id=mid
            )
        except Exception as exc:
            log.warning("fetch_recent_trades failed: %s", exc)
            return []

        events: list[TradeEvent] = []
        for t in res.get("tradeRecords", [])[:limit]:
            price = self._from_chain_price(t.get("price", "0"))
            qty = self._from_chain_qty(t.get("quantity", "0"))
            ts = int(t.get("timestamp", "0")) / 1e3
            # No side info in historical records — use price vs oracle as proxy
            is_buy = True
            events.append(TradeEvent(ts=ts, price=price, quantity=qty, is_buy=is_buy))
        return events

    async def fetch_market_snapshot(self, market_id: str = "") -> MarketSnapshot:
        """Fetch mark price, funding rate, and open interest."""
        mid = market_id or self.market_id
        try:
            # Parallel fetch: funding + mid_price + open_interest
            funding_task = asyncio.create_task(
                self._client.fetch_chain_perpetual_market_funding(market_id=mid)
            )
            mid_task = asyncio.create_task(
                self._client.fetch_derivative_mid_price_and_tob(market_id=mid)
            )
            oi_task = asyncio.create_task(
                self._client.fetch_open_interest(market_id=mid)
            )
            funding_res, mid_res, oi_res = await asyncio.gather(
                funding_task, mid_task, oi_task, return_exceptions=True
            )
        except Exception as exc:
            log.warning("fetch_market_snapshot partial failure: %s", exc)
            funding_res, mid_res, oi_res = {}, {}, {}

        # Mid price: {"midPrice": "5783500000000000000", ...}
        mid_price = 0.0
        if isinstance(mid_res, dict):
            raw_mid = mid_res.get("midPrice", "0") or "0"
            mid_price = self._from_chain_price(raw_mid)

        # Funding: {"state": {"cumulativeFunding": "...", "lastTimestamp": "..."}}
        # We use cumulativeFunding as the value tracked for z-score computation.
        funding_rate = 0.0
        if isinstance(funding_res, dict):
            state = funding_res.get("state", {})
            raw_funding = state.get("cumulativeFunding", "0") or "0"
            funding_rate = self._from_chain_price(raw_funding)

        # Open Interest: {"amount": {"balance": "18188180000000000000000"}}
        open_interest = 0.0
        if isinstance(oi_res, dict):
            amount = oi_res.get("amount", {})
            raw_oi = amount.get("balance", "0") or "0"
            open_interest = self._from_chain_qty(raw_oi)

        return MarketSnapshot(
            ts=time.time(),
            market_id=mid,
            mark_price=mid_price,
            oracle_price=mid_price,
            funding_rate=funding_rate,
            open_interest=open_interest,
        )

    async def fetch_positions(self) -> list[PositionInfo]:
        if not self._subaccount_id:
            return []
        try:
            res = await self._client.fetch_chain_positions_in_market(
                market_id=self.market_id
            )
        except Exception as exc:
            log.warning("fetch_positions failed: %s", exc)
            return []

        out: list[PositionInfo] = []
        for p in res.get("positions", []):
            is_long = p.get("isLong", p.get("is_long", True))
            direction = "long" if is_long else "short"
            out.append(PositionInfo(
                market_id=self.market_id,
                subaccount_id=self._subaccount_id,
                direction=direction,
                quantity=self._from_chain_qty(p.get("quantity", "0")),
                entry_price=self._from_chain_price(p.get("entryPrice", p.get("entry_price", "0"))),
                margin=self._from_chain_price(p.get("margin", "0")),
                unrealized_pnl=0.0,
                liquidation_price=0.0,
            ))
        return out

    async def fetch_subaccount_balance(self) -> float:
        if not self._subaccount_id:
            return 0.0
        try:
            # USDT on Injective: peggy denom
            denom = "peggy0xdAC17F958D2ee523a2206206994597C13D831ec7"
            res = await self._client.fetch_subaccount_deposit(
                subaccount_id=self._subaccount_id,
                denom=denom,
            )
            deposit = res.get("deposits", res)
            if isinstance(deposit, dict):
                total = deposit.get("totalBalance",
                        deposit.get("total_balance", "0"))
            else:
                total = "0"
            return float(total) / 1e6  # USDT 6 decimals
        except Exception as exc:
            log.warning("fetch_subaccount_balance failed: %s", exc)
            return 0.0

    # ── Order execution (LIVE only) ──────────────────────────────────────────

    async def create_limit_order(
        self,
        is_buy: bool,
        price: float,
        quantity: float,
        reduce_only: bool = False,
        market_id: str = "",
    ) -> OrderResult:
        if not self._broadcaster or not self._composer:
            return OrderResult(success=False, error="Broadcaster not initialised (not LIVE mode)")

        mid = market_id or self.market_id
        try:
            order_type = "buy" if is_buy else "sell"
            if reduce_only:
                order_type = "buy_po" if is_buy else "sell_po"

            # Margin required = price * quantity / leverage (simplified)
            margin = Decimal(str(price)) * Decimal(str(quantity)) / Decimal("5")

            msg = self._composer.msg_create_derivative_limit_order(
                market_id=mid,
                sender=str(self._address),
                subaccount_id=self._subaccount_id,
                fee_recipient=self.fee_recipient or str(self._address),
                price=Decimal(str(price)),
                quantity=Decimal(str(quantity)),
                margin=margin,
                order_type=order_type,
                cid="",
            )
            res = await self._broadcaster.broadcast([msg])
            return OrderResult(success=True, tx_hash=getattr(res, "tx_hash", ""))
        except Exception as exc:
            log.error("create_limit_order failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    async def cancel_all_orders(self) -> bool:
        if not self._broadcaster or not self._composer:
            return False
        try:
            msg = self._composer.msg_batch_cancel_derivative_orders(
                sender=str(self._address),
                data=[{"market_id": self.market_id, "subaccount_id": self._subaccount_id}],
            )
            await self._broadcaster.broadcast([msg])
            return True
        except Exception as exc:
            log.error("cancel_all_orders failed: %s", exc)
            return False

    async def close_position(self, direction: str, quantity: float, price: float) -> OrderResult:
        is_buy = direction == "short"
        return await self.create_limit_order(
            is_buy=is_buy,
            price=price,
            quantity=quantity,
            reduce_only=True,
        )

    # ── Streaming ────────────────────────────────────────────────────────────

    async def stream_orderbook(
        self, callback: Callable[[OrderbookSnapshot], Coroutine[Any, Any, None]]
    ) -> None:
        from injective.stream.v2.query_pb2 import OrderbookFilter

        async def _on_update(event: Any) -> None:
            for ob_update in event.get("derivativeOrderbooks", []):
                if ob_update.get("marketId") != self.market_id:
                    continue
                ob = ob_update.get("orderbook", {})
                bids = [
                    OrderLevel(
                        price=self._from_chain_price(b.get("price", "0")),
                        quantity=self._from_chain_qty(b.get("quantity", "0")),
                    )
                    for b in ob.get("buys", [])[:20]
                ]
                asks = [
                    OrderLevel(
                        price=self._from_chain_price(a.get("price", "0")),
                        quantity=self._from_chain_qty(a.get("quantity", "0")),
                    )
                    for a in ob.get("sells", [])[:20]
                ]
                snap = OrderbookSnapshot(
                    ts=time.time(),
                    market_id=self.market_id,
                    bids=sorted(bids, key=lambda x: -x.price),
                    asks=sorted(asks, key=lambda x: x.price),
                )
                await callback(snap)

        await self._client.listen_chain_stream_updates(
            callback=_on_update,
            derivative_orderbooks_filter=OrderbookFilter(market_ids=[self.market_id]),
        )

    # ── Unit conversion ──────────────────────────────────────────────────────

    def _from_chain_price(self, raw: str | float | int) -> float:
        try:
            v = float(raw) if raw else 0.0
            return v / 1e18
        except (ValueError, TypeError):
            return 0.0

    def _from_chain_qty(self, raw: str | float | int) -> float:
        try:
            v = float(raw) if raw else 0.0
            return v / 1e18
        except (ValueError, TypeError):
            return 0.0

    def to_chain_price(self, price: float) -> str:
        return str(int(price * 1e18))

    def to_chain_qty(self, qty: float) -> str:
        return str(int(qty * 1e18))

"""Alpaca paper-trading executor.

Mechanical order routing only — what to trade is decided upstream (policy/risk/
positions). Refuses to construct against a non-paper endpoint. alpaca-py's
TradingClient is sync, so every call runs in a thread; order submission is
therefore off the tick->decision critical path by construction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..config import AlpacaConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float


class PaperExecutor:
    def __init__(self, config: AlpacaConfig, client: Any | None = None) -> None:
        if not config.is_paper:
            raise RuntimeError("Refusing to run: config does not point at paper trading.")
        self.config = config
        self._client = client  # injectable for tests; real client built lazily

    def _get_client(self) -> Any:
        if self._client is None:
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                self.config.api_key, self.config.secret_key, paper=True
            )
        return self._client

    def _submit_market_sync(self, symbol: str, side: str, qty: float) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = self._get_client()
        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,  # crypto requires GTC/IOC
            )
        )
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status.value if hasattr(order.status, "value") else order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=float(order.filled_avg_price or 0),
        )

    async def market_order(self, symbol: str, side: str, qty: float) -> OrderResult:
        return await asyncio.to_thread(self._submit_market_sync, symbol, side, qty)

    async def poll_fill(
        self, order_id: str, attempts: int = 10, delay_s: float = 0.5
    ) -> OrderResult:
        """Poll until the order reaches a terminal state (crypto market fills are fast)."""

        def fetch() -> OrderResult:
            order = self._get_client().get_order_by_id(order_id)
            return OrderResult(
                order_id=str(order.id),
                status=str(order.status.value if hasattr(order.status, "value") else order.status),
                filled_qty=float(order.filled_qty or 0),
                filled_avg_price=float(order.filled_avg_price or 0),
            )

        result = await asyncio.to_thread(fetch)
        for _ in range(attempts):
            if result.status in ("filled", "canceled", "rejected", "expired"):
                break
            await asyncio.sleep(delay_s)
            result = await asyncio.to_thread(fetch)
        return result

    async def equity(self) -> float:
        return float((await asyncio.to_thread(self._get_client().get_account)).equity)

    async def position_qty(self, symbol: str) -> float:
        def fetch() -> float:
            for pos in self._get_client().get_all_positions():
                if pos.symbol.replace("/", "") == symbol.replace("/", ""):
                    return float(pos.qty)
            return 0.0

        return await asyncio.to_thread(fetch)

    async def flatten_all(self) -> None:
        """Kill-switch: cancel open orders and close every position."""
        log.warning("flatten_all: closing all positions")
        await asyncio.to_thread(
            lambda: self._get_client().close_all_positions(cancel_orders=True)
        )

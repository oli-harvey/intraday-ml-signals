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


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: float
    cash: float


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

    def _submit_market_sync(
        self, symbol: str, side: str, qty: float, tif: str = "gtc"
    ) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = self._get_client()
        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                # crypto requires GTC/IOC; US equities market orders use DAY
                time_in_force=TimeInForce.DAY if tif == "day" else TimeInForce.GTC,
            )
        )
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status.value if hasattr(order.status, "value") else order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=float(order.filled_avg_price or 0),
        )

    async def market_order(
        self, symbol: str, side: str, qty: float, tif: str = "gtc"
    ) -> OrderResult:
        return await asyncio.to_thread(self._submit_market_sync, symbol, side, qty, tif)

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

    async def account(self) -> AccountSnapshot:
        """equity AND cash in one call — the basis for the cash/holdings split in
        every bot message (holdings_value = equity - cash, always broker-true)."""
        def fetch() -> AccountSnapshot:
            acct = self._get_client().get_account()
            return AccountSnapshot(equity=float(acct.equity), cash=float(acct.cash))

        return await asyncio.to_thread(fetch)

    async def position_qty(self, symbol: str) -> float:
        def fetch() -> float:
            for pos in self._get_client().get_all_positions():
                if pos.symbol.replace("/", "") == symbol.replace("/", ""):
                    return float(pos.qty)
            return 0.0

        return await asyncio.to_thread(fetch)

    async def flatten_all(self) -> None:
        """Kill-switch: cancel open orders and close every position.

        ⚠ ACCOUNT-WIDE. The paper account is SHARED between the crypto pipeline
        and the stocks paper trader — running strategies must use
        flatten_symbols(their own symbols) or they will close each other's books.
        """
        log.warning("flatten_all: closing all positions")
        await asyncio.to_thread(
            lambda: self._get_client().close_all_positions(cancel_orders=True)
        )

    async def flatten_symbols(self, symbols: list[str]) -> None:
        """Close only THIS strategy's positions (and cancel its open orders).
        The account is shared across strategies, so account-wide close_all is
        reserved for a human kill-switch."""
        wanted = {s.replace("/", "") for s in symbols}

        def run() -> None:
            client = self._get_client()
            for order in client.get_orders():  # default: open orders
                if str(order.symbol).replace("/", "") in wanted:
                    try:
                        client.cancel_order_by_id(order.id)
                    except Exception:  # noqa: BLE001 — already filled/cancelled is fine
                        log.warning("cancel failed for %s", order.id, exc_info=True)
            for pos in client.get_all_positions():
                if str(pos.symbol).replace("/", "") in wanted:
                    log.warning("flatten_symbols: closing %s", pos.symbol)
                    client.close_position(pos.symbol)

        await asyncio.to_thread(run)

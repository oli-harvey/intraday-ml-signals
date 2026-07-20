"""flatten_symbols must report what it closed (a raw broker close bypasses the
normal order-submission path entirely, so without a return value a
reconciliation close is invisible to every alert — this bit us: an ETH
position vanished from the crypto holdings line with no sell alert) and must
never touch a symbol outside its own book (the account is shared)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from signals.config import AlpacaConfig
from signals.execution.alpaca_exec import PaperExecutor


@dataclass
class FakeOrder:
    id: str
    symbol: str


@dataclass
class FakePosition:
    symbol: str
    qty: str
    avg_entry_price: str
    market_value: str
    unrealized_pl: str


@dataclass
class FakeAccount:
    equity: str
    cash: str


class FakeClient:
    def __init__(self, orders, positions, account=None):
        self._orders = list(orders)
        self._positions = list(positions)
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self._account = account or FakeAccount("100000", "50000")

    def get_orders(self):
        return list(self._orders)

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def get_all_positions(self):
        return list(self._positions)

    def close_position(self, symbol):
        self.closed.append(symbol)

    def get_account(self):
        return self._account


def _executor(client: FakeClient) -> PaperExecutor:
    return PaperExecutor(AlpacaConfig(api_key="k", secret_key="s"), client=client)


def test_flatten_symbols_closes_and_reports_only_the_wanted_names():
    client = FakeClient(
        orders=[FakeOrder("o1", "ETH/USD"), FakeOrder("o2", "NVDA")],
        positions=[
            FakePosition("ETH/USD", "0.5", "1800.0", "912.34", "12.34"),
            FakePosition("NVDA", "8", "206.25", "1649.44", "-0.60"),
        ],
    )
    closed = asyncio.run(_executor(client).flatten_symbols(["ETH/USD"]))
    assert client.cancelled == ["o1"]  # NVDA's order untouched
    assert client.closed == ["ETH/USD"]  # NVDA's position untouched
    assert closed == [{
        "symbol": "ETH/USD", "qty": 0.5, "avg_entry_price": 1800.0,
        "market_value": 912.34, "unrealized_pl": 12.34,
    }]


def test_flatten_symbols_reports_nothing_when_flat():
    client = FakeClient(orders=[], positions=[])
    assert asyncio.run(_executor(client).flatten_symbols(["ETH/USD"])) == []


def test_account_returns_equity_and_cash():
    client = FakeClient(orders=[], positions=[],
                        account=FakeAccount("99925.32", "48012.98"))
    snap = asyncio.run(_executor(client).account())
    assert snap.equity == 99_925.32 and snap.cash == 48_012.98

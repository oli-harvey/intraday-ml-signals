"""A flatten_symbols() close must be visible to the alert path.

close_position() is a raw broker call that bypasses market_order()/_execute()
entirely, so without _record_reconciliation_closes() a startup/shutdown flatten
closes a real position with NO trade recorded anywhere — this happened: an ETH
position was sold at a restart and simply vanished from the Telegram holdings
line, with no sell alert. Constructs a bare Pipeline (bypassing __init__, which
needs a live DataSource/DB) since the method under test touches only
orders_submitted and _recent_orders.
"""

from __future__ import annotations

from collections import deque

from signals.pipeline import Pipeline


def _bare_pipeline() -> Pipeline:
    p = object.__new__(Pipeline)
    p.orders_submitted = 0
    p._recent_orders = deque(maxlen=20)
    return p


def test_reconciliation_close_increments_orders_and_is_visible():
    p = _bare_pipeline()
    p._record_reconciliation_closes([
        {"symbol": "ETH/USD", "qty": 0.268585, "avg_entry_price": 1864.79,
         "market_value": 500.94},
    ])
    assert p.orders_submitted == 1
    (order,) = p._recent_orders
    assert order["symbol"] == "ETH/USD"
    assert order["side"] == "sell"  # closing a LONG (positive qty) position
    assert abs(order["qty"] - 0.268585) < 1e-9
    assert order["price"] == 1864.79
    assert order["status"] == "filled"
    assert "RECONCILIATION" in order["note"]


def test_reconciliation_close_of_a_short_position_records_a_buy():
    p = _bare_pipeline()
    p._record_reconciliation_closes([
        {"symbol": "SPY", "qty": -2.0, "avg_entry_price": 744.99,
         "market_value": -1491.66},
    ])
    (order,) = p._recent_orders
    assert order["side"] == "buy" and order["qty"] == 2.0


def test_multiple_closes_each_produce_a_visible_order():
    p = _bare_pipeline()
    p._record_reconciliation_closes([
        {"symbol": "ETH/USD", "qty": 0.5, "avg_entry_price": 1800.0, "market_value": 900.0},
        {"symbol": "BTC/USD", "qty": 0.01, "avg_entry_price": 60_000.0, "market_value": 600.0},
    ])
    assert p.orders_submitted == 2
    assert len(p._recent_orders) == 2

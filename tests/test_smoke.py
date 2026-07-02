"""Phase 0 smoke tests — keep the suite green from day one."""

from signals import __version__
from signals.data.schema import Quote, Side, Tick


def test_version() -> None:
    assert isinstance(__version__, str)


def test_tick_is_frozen_and_slotted() -> None:
    t = Tick(symbol="BTC/USD", ts_ns=1, price=100.0, size=0.5, side=Side.BUY)
    assert t.price == 100.0
    # slots => no __dict__, frozen => immutable
    assert not hasattr(t, "__dict__")


def test_quote_spread_and_mid() -> None:
    q = Quote(symbol="BTC/USD", ts_ns=1, bid=99.0, ask=101.0, bid_size=1.0, ask_size=1.0)
    assert q.spread == 2.0
    assert q.mid == 100.0

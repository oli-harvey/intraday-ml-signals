"""The $100 QQQ trend filter — the only strategy here meant to hold real money,
so its decision function is pinned exhaustively.

decide() is pure and total: four states (above/below the average x holding/flat)
plus the missing-data case. A wrong branch here is a real trade with real money,
which is why this file tests the rule itself rather than the plumbing around it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from trend_bot import decide, sma_from_closes  # noqa: E402

NAN = float("nan")


def test_buys_when_price_rises_above_the_average_and_we_are_flat():
    assert decide(close=110.0, sma=100.0, holding=False) == "buy"


def test_sells_when_price_falls_below_the_average_and_we_hold():
    assert decide(close=90.0, sma=100.0, holding=True) == "sell"


def test_holds_without_retrading_while_the_trend_persists():
    """The rule must be flat-to-hold, not re-buy every day — 4 trades a year is
    the entire cost advantage over buy-and-hold."""
    assert decide(close=110.0, sma=100.0, holding=True) is None


def test_stays_in_cash_while_below_the_average():
    assert decide(close=90.0, sma=100.0, holding=False) is None


def test_exactly_at_the_average_is_not_above_it():
    """Strict inequality: on a tie we do not initiate. Prevents a flat market
    from churning trades on floating-point noise."""
    assert decide(close=100.0, sma=100.0, holding=False) is None
    assert decide(close=100.0, sma=100.0, holding=True) == "sell"


def test_missing_data_never_trades():
    """A NaN average (not enough history) or a NaN close must produce NO action.
    Guessing here would trade $100 on a data outage."""
    assert decide(close=110.0, sma=NAN, holding=False) is None
    assert decide(close=NAN, sma=100.0, holding=True) is None


def test_sma_needs_a_full_window_before_it_will_commit():
    assert sma_from_closes([1.0] * 199, window=200) != sma_from_closes([1.0] * 199, window=200)
    assert sma_from_closes([2.0] * 200, window=200) == 2.0


def test_sma_uses_only_the_most_recent_window():
    closes = [1.0] * 100 + [3.0] * 200          # older values must not leak in
    assert sma_from_closes(closes, window=200) == 3.0


def test_a_realistic_crossover_sequence_trades_exactly_twice():
    """Walk a full round trip: below -> above (buy) -> above (hold) -> below
    (sell) -> below (stay out). Exactly two orders."""
    path = [(95, False), (105, False), (108, True), (98, True), (94, False)]
    actions = [decide(c, 100.0, h) for c, h in path]
    assert actions == [None, "buy", None, "sell", None]

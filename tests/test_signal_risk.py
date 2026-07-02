"""Signal policy thresholds, risk limits, position book transitions, paper guard."""

import pytest

from signals.config import AlpacaConfig
from signals.execution.alpaca_exec import PaperExecutor
from signals.signal.policy import Action, SignalPolicy
from signals.signal.positions import PositionBook
from signals.signal.risk import RiskLimits, RiskManager

# ---- policy ----

def test_policy_threshold_includes_cost_and_half_spread() -> None:
    policy = SignalPolicy(cost_bps=5.0, dead_zone_bps=2.0)
    # threshold = (5 + 10/2 + 2) bps = 12 bps
    sig = policy.decide("BTC/USD", predicted_return=0.0011, spread_bps=10.0)
    assert sig.action is Action.FLAT  # 11 bps < 12 bps
    sig = policy.decide("BTC/USD", predicted_return=0.0013, spread_bps=10.0)
    assert sig.action is Action.LONG
    assert sig.confidence == pytest.approx(0.0013 / 0.0012)
    sig = policy.decide("BTC/USD", predicted_return=-0.0013, spread_bps=10.0)
    assert sig.action is Action.SHORT


def test_wider_spread_raises_the_bar() -> None:
    policy = SignalPolicy(cost_bps=5.0, dead_zone_bps=2.0)
    pred = 0.0015
    assert policy.decide("X", pred, spread_bps=5.0).action is Action.LONG
    assert policy.decide("X", pred, spread_bps=40.0).action is Action.FLAT


# ---- risk ----

def _risk(**kw) -> RiskManager:  # type: ignore[no-untyped-def]
    defaults = dict(
        max_position_usd=1000.0, max_open_positions=2,
        daily_loss_limit_usd=100.0, risk_fraction=0.01, min_notional_usd=10.0,
    )
    defaults.update(kw)
    return RiskManager(RiskLimits(**defaults))


def test_sizing_fixed_fraction_and_cap() -> None:
    r = _risk()
    assert r.size_order(equity=50_000, price=100.0) == pytest.approx(5.0)  # 1% = $500
    # 1% of 500k = $5000 -> capped at $1000
    assert r.size_order(equity=500_000, price=100.0) == pytest.approx(10.0)


def test_sizing_vol_scaling_and_min_notional() -> None:
    r = _risk(vol_target=0.001)
    base = r.size_order(equity=50_000, price=100.0, volatility=0.001)
    halved = r.size_order(equity=50_000, price=100.0, volatility=0.002)
    assert halved == pytest.approx(base / 2)
    tiny = _risk().size_order(equity=500, price=100.0)  # 1% = $5 < min_notional
    assert tiny == 0.0


def test_circuit_breaker_blocks_entries_until_reset() -> None:
    r = _risk(daily_loss_limit_usd=100.0)
    r.record_realized_pnl(-101.0)
    assert r.circuit_breaker_tripped
    assert not r.entry_allowed(open_positions=0)
    assert r.size_order(50_000, 100.0) == 0.0
    r.reset_day()
    assert r.entry_allowed(open_positions=0)


def test_max_open_positions() -> None:
    r = _risk(max_open_positions=1)
    assert r.entry_allowed(open_positions=0)
    assert not r.entry_allowed(open_positions=1)


# ---- position book (long-only mapping) ----

def _signal(action: Action, symbol: str = "BTC/USD"):  # type: ignore[no-untyped-def]
    from signals.signal.policy import Signal

    return Signal(symbol, action, 0.002 * action.value, 0.001, 2.0)


def test_long_entry_then_short_exits_and_books_pnl() -> None:
    risk = _risk()
    book = PositionBook(risk)
    intent = book.on_signal(_signal(Action.LONG), mid=100.0, equity=50_000)
    assert intent is not None and intent.side == "buy"
    assert book.on_fill("BTC/USD", "buy", intent.qty, 100.0) == 0.0
    assert book.open_count == 1
    # LONG again while holding: no-op
    assert book.on_signal(_signal(Action.LONG), 101.0, 50_000) is None
    exit_intent = book.on_signal(_signal(Action.SHORT), 102.0, 50_000)
    assert exit_intent is not None and exit_intent.side == "sell"
    realized = book.on_fill("BTC/USD", "sell", exit_intent.qty, 102.0)
    assert realized == pytest.approx(2.0 * intent.qty)
    assert risk.realized_pnl_today == pytest.approx(realized)
    assert book.open_count == 0


def test_short_while_flat_is_noop_long_only() -> None:
    book = PositionBook(_risk())
    assert book.on_signal(_signal(Action.SHORT), 100.0, 50_000) is None


def test_entry_blocked_by_breaker() -> None:
    risk = _risk(daily_loss_limit_usd=50.0)
    risk.record_realized_pnl(-60.0)
    book = PositionBook(risk)
    assert book.on_signal(_signal(Action.LONG), 100.0, 50_000) is None


# ---- executor guard ----

def test_executor_refuses_non_paper_endpoint() -> None:
    live = AlpacaConfig(api_key="k", secret_key="s", base_url="https://api.alpaca.markets")
    with pytest.raises(RuntimeError, match="paper"):
        PaperExecutor(live)


def test_executor_accepts_paper_endpoint() -> None:
    paper = AlpacaConfig(api_key="k", secret_key="s")
    PaperExecutor(paper, client=object())  # no network with injected client

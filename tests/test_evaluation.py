"""Trade simulator math + classifier model behavior."""

import numpy as np
import pytest

from signals.evaluation import Row, SymbolScore
from signals.model.online import OnlineModel

S = 1_000_000_000


def _row(ts_s: float, pred: float, realized: float, spread_bps: float = 4.0) -> Row:
    return Row(
        ts_ns=int(ts_s * S),
        prediction=pred,
        realized=realized,
        persistence=0.0,
        spread_bps=spread_bps,
    )


def test_sim_charges_costs_and_respects_threshold() -> None:
    score = SymbolScore("X")
    # threshold = (5 + 2 + 2)/1e4 = 9 bps with spread 4
    score.rows = [
        _row(0, pred=0.0008, realized=0.0020),   # 8bps < 9bps: no trade
        _row(20, pred=0.0010, realized=0.0020),  # long: +20bps - (4 + 10) = +6
        _row(40, pred=-0.0010, realized=-0.0030),  # short: +30bps - 14 = +16
        _row(60, pred=0.0010, realized=-0.0010),   # long, wrong: -10 - 14 = -24
    ]
    sim = score.simulate_trading(horizon_ns=10 * S, fee_bps=5.0, dead_zone_bps=2.0)
    assert sim.trades == 3
    assert sim.wins == 2
    assert sim.net_bps_sum == pytest.approx(6 + 16 - 24)


def test_sim_one_position_at_a_time() -> None:
    score = SymbolScore("X")
    # second signal arrives while the first position is still open -> skipped
    score.rows = [
        _row(0, pred=0.0010, realized=0.0020),
        _row(5, pred=0.0010, realized=0.0020),   # within 10s horizon: busy
        _row(11, pred=0.0010, realized=0.0020),  # free again
    ]
    sim = score.simulate_trading(horizon_ns=10 * S, fee_bps=5.0, dead_zone_bps=2.0)
    assert sim.trades == 2


def test_sim_long_only_mode() -> None:
    score = SymbolScore("X")
    score.rows = [_row(0, pred=-0.0010, realized=-0.0030)]
    assert score.simulate_trading(10 * S, allow_short=False).trades == 0
    assert score.simulate_trading(10 * S, allow_short=True).trades == 1


def test_classifier_learns_tail_direction() -> None:
    """Feature 'a' drives tail moves; classifier must recover direction + magnitude."""
    rng = np.random.default_rng(0)
    model = OnlineModel(kind="classifier", band_bps=5.0)
    for _ in range(4000):
        a = rng.choice([-1.0, 0.0, 1.0])
        noise = rng.normal(0, 0.5e-4)
        target = a * 10e-4 + noise  # a=+-1 -> +-10bps move (outside 5bps band)
        model.learn_one({"a": a, "b": rng.normal()}, target)
    up = model.predict_one({"a": 1.0, "b": 0.0})
    down = model.predict_one({"a": -1.0, "b": 0.0})
    flat = model.predict_one({"a": 0.0, "b": 0.0})
    assert up > 5e-4, f"up prediction too weak: {up}"
    assert down < -5e-4, f"down prediction too weak: {down}"
    assert abs(flat) < abs(up) / 2
    m = model.metrics()
    assert m["directional_acc"] > 0.8

def test_fade_and_best_baseline_properties() -> None:
    from signals.evaluation import SegmentScore

    seg = SegmentScore(n=100, mae=1.0, zero_mae=1.0, dir_acc=0.55, dir_persistence=0.42)
    assert seg.dir_fade == pytest.approx(0.58)
    assert seg.dir_best_baseline == pytest.approx(0.58)  # fade wins on reverting data
    trending = SegmentScore(n=100, mae=1.0, zero_mae=1.0, dir_acc=0.55, dir_persistence=0.61)
    assert trending.dir_best_baseline == pytest.approx(0.61)  # persistence wins


def test_meta_model_learns_and_gates() -> None:
    """Meta must learn y=2a-b like the primary, with the gate scaling output."""
    rng = np.random.default_rng(1)
    model = OnlineModel(kind="meta")
    for _ in range(3000):
        a, b = rng.normal(), rng.normal()
        model.learn_one({"a": a, "b": b}, (2 * a - b) * 1e-4)
    preds = [model.predict_one({"a": rng.normal(), "b": rng.normal()}) for _ in range(200)]
    assert any(p != 0.0 for p in preds), "gate killed everything"
    m = model.metrics()
    assert m["directional_acc"] > 0.7


def test_fade_rule_sim_trades_against_last_move() -> None:
    score = SymbolScore("X")
    # persistence = last window's move; realized reverses it (mean reversion)
    score.rows = [
        Row(ts_ns=0 * S, prediction=0.0, realized=-0.0020, persistence=0.0030,
            spread_bps=1.0),   # fade shorts, move reverses: +20bps - 1.4 = +18.6
        Row(ts_ns=20 * S, prediction=0.0, realized=0.0010, persistence=-0.0020,
            spread_bps=1.0),   # fade longs, reverses: +10 - 1.4 = +8.6
        Row(ts_ns=40 * S, prediction=0.0, realized=0.0010, persistence=0.00005,
            spread_bps=1.0),   # |last move| 0.5bps <= min_signal 1.0 -> skip
    ]
    sim = score.simulate_fade_rule(horizon_ns=10 * S, fee_bps=0.2, min_signal_bps=1.0)
    assert sim.trades == 2
    assert sim.wins == 2
    assert sim.net_bps_sum == pytest.approx(18.6 + 8.6)


def test_fade_rule_long_only_skips_shorts() -> None:
    score = SymbolScore("X")
    score.rows = [
        Row(ts_ns=0, prediction=0.0, realized=-0.002, persistence=0.003, spread_bps=1.0),
    ]
    assert score.simulate_fade_rule(10 * S, allow_short=False).trades == 0

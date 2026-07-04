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
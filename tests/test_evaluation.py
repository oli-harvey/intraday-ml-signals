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


def test_ev_model_abstains_on_noise_and_commits_on_signal() -> None:
    """Quantile-interval output: 0.0 when the distribution straddles zero,
    a pessimistic nonzero estimate when it has clearly shifted."""
    rng = np.random.default_rng(2)
    model = OnlineModel(kind="ev")
    for _ in range(6000):
        a = rng.choice([-1.0, 0.0, 1.0])
        target = (a * 20e-4) + rng.normal(0, 3e-4)  # a=+-1 shifts mean to +-20bps
        model.learn_one({"a": a}, target)
    on_signal = model.predict_one({"a": 1.0})
    on_noise = model.predict_one({"a": 0.0})
    on_short = model.predict_one({"a": -1.0})
    assert on_signal > 5e-4, f"should commit long, got {on_signal}"
    assert on_short < -5e-4, f"should commit short, got {on_short}"
    assert on_noise == 0.0, f"should abstain on noise, got {on_noise}"
    # pessimism: the committed estimate is below the true mean shift (20bps)
    assert on_signal < 20e-4


def test_adaptive_and_forest_kinds_learn() -> None:
    rng = np.random.default_rng(3)
    for kind in ("adaptive", "forest"):
        model = OnlineModel(kind=kind)
        for _ in range(1500):
            a, b = rng.normal(), rng.normal()
            model.learn_one({"a": a, "b": b}, 2 * a - b)
        m = model.metrics()
        assert m["mae"] < 0.9 * m["zero_mae"], f"{kind} failed to learn: {m}"


def test_diagnostics_report_calibration_and_residuals() -> None:
    """On a clean linear signal, diagnostics must show ~0 bias, positive R²,
    a sane residual σ, and EV quantile coverage near 0.5."""
    rng = np.random.default_rng(11)
    model = OnlineModel(kind="ev")
    for _ in range(4000):
        a = rng.choice([-1.0, 0.0, 1.0])
        model.learn_one({"a": a}, a * 15e-4 + rng.normal(0, 4e-4))
    d = model.diagnostics()
    assert d["rolling_n"] > 1000
    assert abs(d["bias_bps"]) < 2.0          # roughly unbiased
    assert d["r2"] > 0.3                      # explains variance vs predict-zero
    assert 2.0 < d["resid_std_bps"] < 10.0
    assert 0.30 < d["coverage"] < 0.70        # q25-q75 brackets ~half the outcomes
    assert 0.0 <= d["commit_rate"] <= 1.0
    pairs = model.recent_pairs(50)
    assert len(pairs) == 50 and all(p != 0.0 for p, _ in pairs)  # committed only


def test_diagnostics_empty_model_is_all_nan_not_crash() -> None:
    d = OnlineModel(kind="ev").diagnostics()
    assert d["rolling_n"] == 0.0 and d["bias_bps"] != d["bias_bps"]  # NaN

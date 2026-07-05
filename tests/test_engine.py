"""FeatureEngine behavior on a scripted quote/trade sequence."""

import numpy as np
import pytest

from signals.data.schema import Quote, Side, Tick
from signals.features.engine import FeatureConfig, FeatureEngine

CFG = FeatureConfig(lag_returns=(1, 2, 4), warmup_quotes=8, zscore_warmup=4, vol_window=8)


def _quote(i: int, mid: float, spread: float = 2.0, bs: float = 1.0, asz: float = 1.0) -> Quote:
    return Quote(
        symbol="BTC/USD",
        ts_ns=i * 1_000_000_000,
        bid=mid - spread / 2,
        ask=mid + spread / 2,
        bid_size=bs,
        ask_size=asz,
    )


def _feed(engine: FeatureEngine, mids: list[float]) -> list[dict[str, float] | None]:
    return [engine.update(_quote(i, m)) for i, m in enumerate(mids)]


def test_warmup_gating_then_emits() -> None:
    engine = FeatureEngine(CFG)
    rng = np.random.default_rng(0)
    mids = (60_000 + np.cumsum(rng.normal(0, 5, size=20))).tolist()
    out = _feed(engine, mids)
    assert all(v is None for v in out[: CFG.warmup_quotes - 1])
    emitted = [v for v in out if v is not None]
    assert emitted, "engine never warmed up"
    expected_keys = {
        "ret_1", "ret_2", "ret_4", "ema_spread", "vol", "spread_bps", "imbalance", "flow",
        "micro_bps", "uptick", "dt_s",
        # interactions (on by default)
        "ret1_over_vol", "micro_over_spread",
        "micro_x_uptick", "micro_x_ret1", "flow_x_imbalance",
    }
    assert set(emitted[0].keys()) == expected_keys
    assert all(np.isfinite(list(v.values())).all() for v in emitted)


def test_raw_features_match_hand_calc() -> None:
    engine = FeatureEngine(CFG)
    mids = [100.0, 101.0, 102.0, 101.5, 103.0, 102.0, 104.0, 105.0, 106.0]
    for i, m in enumerate(mids[:-1]):
        engine.update(_quote(i, m))
    engine.update(_quote(len(mids) - 1, mids[-1], spread=4.0, bs=3.0, asz=1.0))
    raw = engine.last_raw
    assert raw["ret_1"] == pytest.approx(106.0 / 105.0 - 1.0)
    assert raw["ret_2"] == pytest.approx(106.0 / 104.0 - 1.0)
    assert raw["ret_4"] == pytest.approx(106.0 / 103.0 - 1.0)
    assert raw["spread_bps"] == pytest.approx(4.0 / 106.0 * 1e4)
    assert raw["imbalance"] == pytest.approx((3.0 - 1.0) / 4.0)
    # microprice: bid=104, ask=108, bid_size=3, ask_size=1
    # -> (3*108 + 1*104)/4 = 107 -> offset (107-106)/106 in bps
    assert raw["micro_bps"] == pytest.approx((107.0 - 106.0) / 106.0 * 1e4)
    # rolling vol of 1-lag returns vs numpy
    rets = np.diff(mids) / np.array(mids[:-1])
    assert raw["vol"] == pytest.approx(rets[-CFG.vol_window :].std(), abs=1e-12)


def test_trades_feed_flow_ema_and_emit_nothing() -> None:
    engine = FeatureEngine(CFG)
    assert engine.update(Tick("BTC/USD", 1, 100.0, 2.0, side=Side.BUY)) is None
    assert engine.update(Tick("BTC/USD", 2, 100.0, 1.0, side=Side.SELL)) is None
    # EMA(0.1): 2.0 then 0.1*(-1) + 0.9*2.0
    assert engine._flow.value == pytest.approx(0.1 * -1.0 + 0.9 * 2.0)


def test_degenerate_quotes_are_skipped() -> None:
    engine = FeatureEngine(CFG)
    crossed = Quote("BTC/USD", 1, bid=101.0, ask=100.0, bid_size=1, ask_size=1)
    zero = Quote("BTC/USD", 2, bid=0.0, ask=0.0, bid_size=0, ask_size=0)
    assert engine.update(crossed) is None
    assert engine.update(zero) is None
    assert engine._quotes_seen == 0


def test_deterministic_across_reruns() -> None:
    rng = np.random.default_rng(7)
    mids = (100 + np.cumsum(rng.normal(0, 0.1, size=40))).tolist()
    a = [v for v in _feed(FeatureEngine(CFG), mids) if v is not None]
    b = [v for v in _feed(FeatureEngine(CFG), mids) if v is not None]
    assert a == b


def test_interaction_ratios_match_hand_calc() -> None:
    engine = FeatureEngine(CFG)
    rng = np.random.default_rng(3)
    mids = (60_000 + np.cumsum(rng.normal(0, 5, size=20))).tolist()
    _feed(engine, mids)
    raw = engine.last_raw
    assert raw["ret1_over_vol"] == pytest.approx(raw["ret_1"] / (raw["vol"] + 1e-12))
    assert raw["micro_over_spread"] == pytest.approx(
        raw["micro_bps"] / (raw["spread_bps"] + 1e-6)
    )


def test_interactions_toggle_off() -> None:
    cfg_off = FeatureConfig(
        lag_returns=(1, 2, 4), warmup_quotes=8, zscore_warmup=4, vol_window=8,
        interactions=False,
    )
    engine = FeatureEngine(cfg_off)
    rng = np.random.default_rng(4)
    mids = (60_000 + np.cumsum(rng.normal(0, 5, size=20))).tolist()
    emitted = [v for v in _feed(engine, mids) if v is not None]
    assert emitted
    assert not any("_x_" in k or "_over_" in k for k in emitted[0])


def test_cross_asset_leader_features() -> None:
    from signals.features.cross import CrossFeed

    feed = CrossFeed(staleness_ns=5_000_000_000)
    leader = FeatureEngine(CFG, crossfeed=feed)          # publishes only
    follower = FeatureEngine(CFG, crossfeed=feed, leader="BTC/USD")

    rng = np.random.default_rng(5)
    for i in range(20):
        mid_btc = 60_000 * (1 + rng.normal(0, 1e-4))
        leader.update(_quote(i, mid_btc))
        out = follower.update(
            Quote("ETH/USD", i * 1_000_000_000 + 500_000_000, 2000.0 - 1, 2000.0 + 1, 1, 1)
        )
    assert out is not None
    assert "leader_r1" in out and "leader_uptick" in out
    # staleness: follower quote 10s after last BTC update -> zeros
    follower.update(Quote("ETH/USD", 30 * 1_000_000_000, 1999.0, 2001.0, 1, 1))
    assert follower.last_raw["leader_r1"] == 0.0


def test_exclude_drops_features() -> None:
    cfg = FeatureConfig(
        lag_returns=(1, 2, 4), warmup_quotes=8, zscore_warmup=4, vol_window=8,
        exclude=("micro_bps", "micro_x_uptick", "micro_x_ret1", "micro_over_spread"),
    )
    engine = FeatureEngine(cfg)
    rng = np.random.default_rng(6)
    mids = (60_000 + np.cumsum(rng.normal(0, 5, size=20))).tolist()
    emitted = [v for v in _feed(engine, mids) if v is not None]
    assert emitted and not any(k.startswith("micro") for k in emitted[0])

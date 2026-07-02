"""Incremental online stats vs naive numpy full-recompute references.

These are the Phase 2 correctness gates: every O(1) update must match what a
batch recompute over the window would produce (the naive version exists only
here — never in the hot path).
"""

import time

import numpy as np
import pytest

from signals.features.online_stats import EMA, RunningSMA, RunningZScore, Welford


def test_welford_expanding_matches_numpy() -> None:
    xs = np.random.default_rng(0).normal(loc=100.0, scale=5.0, size=500)
    w = Welford()
    for i, x in enumerate(xs, start=1):
        w.update(float(x))
        assert w.mean == pytest.approx(xs[:i].mean(), abs=1e-9)
        assert w.std == pytest.approx(xs[:i].std(), abs=1e-9)


def test_welford_rolling_matches_numpy_sliding_window() -> None:
    xs = np.random.default_rng(1).normal(loc=0.0, scale=1e-3, size=800)  # return-like
    window = 64
    w = Welford(window=window)
    for i, x in enumerate(xs, start=1):
        w.update(float(x))
        ref = xs[max(0, i - window) : i]
        assert w.n == len(ref)
        assert w.mean == pytest.approx(ref.mean(), abs=1e-12)
        assert w.std == pytest.approx(ref.std(), abs=1e-12)


def test_welford_rolling_survives_constant_then_varied() -> None:
    w = Welford(window=4)
    for _ in range(10):
        w.update(5.0)
    assert w.variance == pytest.approx(0.0, abs=1e-15)
    w.update(6.0)
    assert w.variance > 0


def test_rolling_sma_matches_naive() -> None:
    xs = np.random.default_rng(2).normal(loc=60_000, scale=100, size=300)
    window = 7
    sma = RunningSMA(window)
    for i, x in enumerate(xs, start=1):
        got = sma.update(float(x))
        assert got == pytest.approx(xs[max(0, i - window) : i].mean(), rel=1e-12)


def test_ema_matches_recurrence() -> None:
    xs = np.arange(1, 51, dtype=float)
    alpha = 0.3
    ema = EMA(alpha)
    expected: float | None = None
    for x in xs:
        got = ema.update(float(x))
        expected = x if expected is None else alpha * x + (1 - alpha) * expected
        assert got == pytest.approx(expected, abs=1e-12)


def test_zscore_matches_naive_and_warmup_gates() -> None:
    xs = np.random.default_rng(3).normal(size=200)
    warmup = 30
    z = RunningZScore(window=None, warmup=warmup)  # expanding
    for i, x in enumerate(xs, start=1):
        got = z.normalize(float(x))
        if i < warmup:
            assert got == 0.0
        else:
            hist = xs[:i]  # stats updated with x first, then normalized
            assert got == pytest.approx((x - hist.mean()) / hist.std(), abs=1e-9)


def test_zscore_degenerate_std_returns_zero() -> None:
    z = RunningZScore(warmup=2)
    for _ in range(10):
        assert z.normalize(7.0) == 0.0  # zero variance must not divide


def test_update_cost_is_o1_not_o_window() -> None:
    """Per-update cost must not scale with window size (generous factor for noise)."""

    def cost(window: int) -> float:
        w = Welford(window=window)
        for x in range(window):  # pre-fill so removals happen
            w.update(float(x))
        start = time.perf_counter()
        for x in range(20_000):
            w.update(float(x))
        return time.perf_counter() - start

    small, large = cost(64), cost(64 * 128)
    assert large < small * 5, f"update cost grew with window: {small:.4f}s -> {large:.4f}s"

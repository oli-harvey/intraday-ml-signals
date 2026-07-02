"""Phase 2 — incremental online stats must match a naive full-recompute reference.

These are the correctness gates for the feature engine. They are expected to fail
until Phase 2 is implemented, hence xfail; flip to real assertions as each lands.
"""

import numpy as np
import pytest

from signals.features.online_stats import EMA, RunningSMA, Welford


@pytest.mark.xfail(reason="Phase 2 not implemented", strict=False)
def test_welford_matches_numpy() -> None:
    xs = np.random.default_rng(0).normal(size=500)
    w = Welford()  # expanding
    for x in xs:
        w.update(float(x))
    assert w.mean == pytest.approx(xs.mean(), abs=1e-9)
    assert w.std == pytest.approx(xs.std(), abs=1e-9)


@pytest.mark.xfail(reason="Phase 2 not implemented", strict=False)
def test_rolling_sma_matches_naive() -> None:
    xs = np.arange(1, 21, dtype=float)
    window = 5
    sma = RunningSMA(window)
    out = [sma.update(float(x)) for x in xs]
    naive = [xs[max(0, i - window + 1) : i + 1].mean() for i in range(len(xs))]
    assert out == pytest.approx(naive, abs=1e-9)


@pytest.mark.xfail(reason="Phase 2 not implemented", strict=False)
def test_ema_matches_recurrence() -> None:
    xs = np.arange(1, 11, dtype=float)
    alpha = 0.3
    ema = EMA(alpha)
    e = None
    for x in xs:
        got = ema.update(float(x))
        e = x if e is None else alpha * x + (1 - alpha) * e
        assert got == pytest.approx(e, abs=1e-12)

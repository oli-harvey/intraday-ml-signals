"""LabelQueue: strict no-lookahead label matching."""

import pytest

from signals.model.labels import LabelQueue, Pending

S = 1_000_000_000  # 1s in ns


def _pending(ts_s: float, price: float, pred: float = 0.0) -> Pending:
    return Pending(ts_ns=int(ts_s * S), features={"x": 1.0}, ref_price=price, prediction=pred)


def test_nothing_resolves_before_horizon() -> None:
    q = LabelQueue(horizon_ns=10 * S)
    q.add(_pending(0, 100.0))
    assert q.pop_ready(int(9.999 * S), 105.0) == []
    assert len(q) == 1


def test_resolves_at_first_event_after_horizon_with_that_price() -> None:
    q = LabelQueue(horizon_ns=10 * S)
    q.add(_pending(0, 100.0, pred=0.001))
    resolved = q.pop_ready(int(12.5 * S), 102.0)  # first price seen after t+10s
    assert len(resolved) == 1
    r = resolved[0]
    assert r.realized == pytest.approx(0.02)
    assert r.prediction == 0.001
    assert r.ts_ns == 0
    assert r.resolved_ts_ns == int(12.5 * S)
    assert len(q) == 0


def test_multiple_pending_resolve_in_order_when_due() -> None:
    q = LabelQueue(horizon_ns=10 * S)
    q.add(_pending(0, 100.0))
    q.add(_pending(2, 101.0))
    q.add(_pending(9, 102.0))
    resolved = q.pop_ready(int(12 * S), 110.0)  # due: t=0 and t=2; not t=9
    assert [r.ts_ns for r in resolved] == [0, 2 * S]
    assert len(q) == 1
    assert q.pop_ready(int(19 * S), 110.0)[0].ts_ns == 9 * S


def test_out_of_order_additions_clamped_monotonic() -> None:
    """Equities bursts regress exchange ts by microseconds; clamp, don't reject."""
    q = LabelQueue(horizon_ns=10 * S)
    q.add(_pending(5, 100.0))
    q.add(_pending(4.999999, 100.0))  # regressed within a burst -> clamped to 5s
    resolved = q.pop_ready(int(15 * S), 101.0)
    assert len(resolved) == 2
    assert resolved[1].ts_ns == 5 * S  # clamped timestamp


def test_bad_horizon_rejected() -> None:
    with pytest.raises(ValueError):
        LabelQueue(horizon_ns=0)

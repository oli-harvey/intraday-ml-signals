"""RingBuffer / SymbolBuffers correctness, incl. wraparound vs a deque reference."""

import random
from collections import deque

import numpy as np
import pytest

from signals.data.ringbuffer import RingBuffer, SymbolBuffers
from signals.data.schema import Tick


def test_partial_fill_and_order() -> None:
    rb = RingBuffer(5)
    for v in [1.0, 2.0, 3.0]:
        rb.push(v)
    assert len(rb) == 3
    assert not rb.is_full
    assert rb.latest() == 3.0
    assert rb.last(3).tolist() == [1.0, 2.0, 3.0]
    assert rb.last(2).tolist() == [2.0, 3.0]


def test_wraparound_matches_deque_reference() -> None:
    rng = random.Random(0)
    for capacity in (1, 5, 512):
        rb = RingBuffer(capacity)
        ref: deque[float] = deque(maxlen=capacity)
        for _ in range(1000):
            v = rng.random()
            rb.push(v)
            ref.append(v)
            n = rng.randint(0, len(ref))
            assert rb.last(n).tolist() == pytest.approx(list(ref)[len(ref) - n :])
        assert rb.is_full
        assert len(rb) == capacity


def test_last_bounds() -> None:
    rb = RingBuffer(3)
    rb.push(1.0)
    assert rb.last(0).size == 0
    with pytest.raises(ValueError):
        rb.last(2)  # only 1 available
    with pytest.raises(ValueError):
        rb.last(-1)


def test_empty_latest_raises() -> None:
    with pytest.raises(ValueError):
        RingBuffer(3).latest()


def test_int64_preserves_ns_precision() -> None:
    # float64 would corrupt epoch-ns values; int64 ring must be exact.
    ts = 1_751_450_000_123_456_789
    rb = RingBuffer(4, dtype=np.int64)
    rb.push(ts)
    assert rb.latest() == ts
    assert rb.last(1)[0] == ts


def test_symbol_buffers_update() -> None:
    sb = SymbolBuffers(depth=4)
    for i in range(6):  # overflow the depth to exercise wraparound
        sb.update(Tick(symbol="BTC/USD", ts_ns=1_000 + i, price=100.0 + i, size=0.1 * i))
    assert len(sb) == 4
    assert sb.price.last(4).tolist() == [102.0, 103.0, 104.0, 105.0]
    assert sb.ts_ns.last(4).tolist() == [1002, 1003, 1004, 1005]

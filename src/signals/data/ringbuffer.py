"""Fixed-size in-memory ring buffers for the hot path.

Preallocated numpy circular arrays for numeric series (O(1) push, reads never
reallocate history). No pandas. Never read historical data from disk here.
"""

from __future__ import annotations

import numpy as np

from .schema import Tick


class RingBuffer:
    """Preallocated circular buffer of numeric values.

    push() is O(1). last(n) copies out the most recent n values (n is small and
    fixed for feature lags — never the whole history).

    dtype matters: timestamps must use int64 — float64 loses precision above
    2**53, and epoch-ns values (~1.7e18) exceed that.
    """

    def __init__(self, capacity: int, dtype: type = np.float64) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._buf = np.zeros(capacity, dtype=dtype)
        self._head = 0  # next write index
        self._count = 0

    def push(self, value: float | int) -> None:
        self._buf[self._head] = value
        self._head = (self._head + 1) % self.capacity
        if self._count < self.capacity:
            self._count += 1

    def latest(self) -> float | int:
        """Most recently pushed value."""
        if self._count == 0:
            raise ValueError("buffer is empty")
        return self._buf[(self._head - 1) % self.capacity].item()

    def last(self, n: int) -> np.ndarray:
        """Return the most recent n values, oldest-first (copy)."""
        if n < 0 or n > self._count:
            raise ValueError(f"requested {n} values but buffer holds {self._count}")
        if n == 0:
            return self._buf[:0].copy()
        end = self._head
        start = (end - n) % self.capacity
        if start < end:
            return self._buf[start:end].copy()
        return np.concatenate((self._buf[start:], self._buf[:end]))

    @property
    def is_full(self) -> bool:
        return self._count >= self.capacity

    def __len__(self) -> int:
        return self._count


class SymbolBuffers:
    """Per-symbol hot-path state: parallel rings of trade timestamp/price/size.

    Updated from trades only for now; quote-derived state (spread, imbalance)
    is added in Phase 2 alongside the microstructure features.
    """

    def __init__(self, depth: int = 512) -> None:
        self.depth = depth
        self.ts_ns = RingBuffer(depth, dtype=np.int64)
        self.price = RingBuffer(depth)
        self.size = RingBuffer(depth)

    def update(self, tick: Tick) -> None:
        self.ts_ns.push(tick.ts_ns)
        self.price.push(tick.price)
        self.size.push(tick.size)

    def __len__(self) -> int:
        return len(self.price)

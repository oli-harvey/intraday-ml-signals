"""No-lookahead label matching.

A prediction made at tick t targets the forward return over t -> t+k, which is not
known until t+k. We queue (t, features, prediction) and only release a training pair
once the realized forward return is observed. This is the core guard against lookahead
and train/serve skew. Phase 3 — see docs/PLAN.md.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class Pending:
    ts_ns: int
    features: dict[str, float]
    ref_price: float
    prediction: float


class LabelQueue:
    """Match predictions to their realized forward return k steps later."""

    def __init__(self, horizon: int) -> None:
        self.horizon = horizon
        self._pending: deque[Pending] = deque()

    def add(self, item: Pending) -> None:
        raise NotImplementedError("Phase 3")

    def pop_ready(self, now_ts_ns: int, price: float) -> list[tuple[dict[str, float], float]]:
        """Return (features, realized_return) pairs whose horizon has elapsed."""
        raise NotImplementedError("Phase 3")

"""No-lookahead label matching.

A prediction made at time t targets the forward return over t -> t+horizon, which
is not known until t+horizon. We queue (t, features, ref_price, prediction) and
release a training triple only when an event at ts >= t+horizon supplies the
realized price. The horizon is time-based (nanoseconds), not event-count-based,
because quotes arrive irregularly. This is the core guard against lookahead and
train/serve skew: replay and live resolve labels through the exact same path.
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
    spread_bps: float = 0.0  # spread at prediction time (cost simulation)


@dataclass(frozen=True, slots=True)
class Resolved:
    ts_ns: int  # when the prediction was made
    resolved_ts_ns: int
    features: dict[str, float]
    prediction: float
    realized: float  # simple return over >= horizon
    spread_bps: float = 0.0


class LabelQueue:
    """Match predictions to their realized forward return >= horizon later.

    The realized price is the first observed price after the horizon elapses —
    a slight overshoot of the exact horizon, which is precisely what a live
    system would experience (no interpolation, no peeking)."""

    def __init__(self, horizon_ns: int) -> None:
        if horizon_ns <= 0:
            raise ValueError("horizon must be positive")
        self.horizon_ns = horizon_ns
        self._pending: deque[Pending] = deque()

    def add(self, item: Pending) -> None:
        if self._pending and item.ts_ns < self._pending[-1].ts_ns:
            raise ValueError("predictions must be added in time order")
        self._pending.append(item)

    def pop_ready(self, now_ts_ns: int, price: float) -> list[Resolved]:
        """Resolve every pending prediction whose horizon has elapsed at now."""
        out: list[Resolved] = []
        while self._pending and now_ts_ns >= self._pending[0].ts_ns + self.horizon_ns:
            p = self._pending.popleft()
            out.append(
                Resolved(
                    ts_ns=p.ts_ns,
                    resolved_ts_ns=now_ts_ns,
                    features=p.features,
                    prediction=p.prediction,
                    realized=price / p.ref_price - 1.0,
                    spread_bps=p.spread_bps,
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._pending)

"""Incremental feature engine.

Consumes normalized ticks and emits a fixed-length, online-normalized feature vector
per symbol. All state lives in ring buffers + online-stat accumulators; every update
is O(1). Output is a plain dict[str, float] ready for River `predict_one`/`learn_one`.

Phase 2 — see docs/PLAN.md.
"""

from __future__ import annotations

from ..data.schema import Tick


class FeatureEngine:
    """Per-symbol incremental feature computation.

    Features (all O(1) update): price/return lags, momentum over k lags, EMA/SMA,
    Welford volatility, and (if quotes available) spread / trade size / order imbalance.
    Every output is online z-normalized.
    """

    def __init__(self, lags: int = 8, vol_window: int = 64) -> None:
        self.lags = lags
        self.vol_window = vol_window

    def update(self, tick: Tick) -> dict[str, float]:
        """Ingest one tick, return the current feature vector."""
        raise NotImplementedError("Phase 2")

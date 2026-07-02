"""Online statistics — all O(1) per update, no recompute over the window.

Each class is validated in tests against a naive numpy full-recompute reference.
Phase 2 — see docs/PLAN.md.
"""

from __future__ import annotations


class Welford:
    """Running mean/variance (Welford's algorithm). Rolling or expanding."""

    def __init__(self, window: int | None = None) -> None:
        self.window = window  # None -> expanding

    def update(self, x: float) -> None:
        raise NotImplementedError("Phase 2")

    @property
    def mean(self) -> float:
        raise NotImplementedError("Phase 2")

    @property
    def variance(self) -> float:
        raise NotImplementedError("Phase 2")

    @property
    def std(self) -> float:
        raise NotImplementedError("Phase 2")


class EMA:
    """Exponential moving average via the O(1) update: e = a*x + (1-a)*e."""

    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.value: float | None = None

    def update(self, x: float) -> float:
        raise NotImplementedError("Phase 2")


class RunningSMA:
    """Simple moving average via running sum + deque (add new, subtract expiring)."""

    def __init__(self, window: int) -> None:
        self.window = window

    def update(self, x: float) -> float:
        raise NotImplementedError("Phase 2")


class RunningZScore:
    """Online z-normalization using a running mean/std (not fit-once)."""

    def __init__(self, window: int | None = None, warmup: int = 30) -> None:
        self.warmup = warmup

    def normalize(self, x: float) -> float:
        """Update stats with x and return its z-score (0.0 until warmed up)."""
        raise NotImplementedError("Phase 2")

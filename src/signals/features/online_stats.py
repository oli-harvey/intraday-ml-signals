"""Online statistics — all O(1) per update, no recompute over the window.

Each class is validated in tests against a naive numpy full-recompute reference.
"""

from __future__ import annotations

import math
from collections import deque


class Welford:
    """Running mean/variance. Expanding (window=None) or rolling (fixed window).

    Rolling uses Welford's add step plus West's O(1) removal — the window deque
    exists only to know *which* value expires, never to recompute over.
    Variance is population (ddof=0), matching numpy's default.
    """

    def __init__(self, window: int | None = None) -> None:
        if window is not None and window < 2:
            raise ValueError("rolling window must be >= 2")
        self.window = window
        self._q: deque[float] | None = deque() if window else None
        self.n = 0
        self._mean = 0.0
        self._m2 = 0.0

    def update(self, x: float) -> None:
        if self._q is not None:
            if self.n == self.window:
                self._remove(self._q.popleft())
            self._q.append(x)
        self._add(x)

    def _add(self, x: float) -> None:
        self.n += 1
        delta = x - self._mean
        self._mean += delta / self.n
        self._m2 += delta * (x - self._mean)

    def _remove(self, x: float) -> None:
        if self.n == 1:
            self.n, self._mean, self._m2 = 0, 0.0, 0.0
            return
        mean_new = (self.n * self._mean - x) / (self.n - 1)
        self._m2 -= (x - self._mean) * (x - mean_new)
        self._mean = mean_new
        self.n -= 1

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def variance(self) -> float:
        if self.n == 0:
            return 0.0
        return max(0.0, self._m2 / self.n)  # clamp float noise

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)


class EMA:
    """Exponential moving average via the O(1) update: e = a*x + (1-a)*e.

    Seeded with the first observation (no zero-bias warmup)."""

    def __init__(self, alpha: float) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RunningSMA:
    """Simple moving average via running sum + deque (add new, subtract expiring).

    Returns the mean of the partial window during warmup. Running-sum float drift
    is negligible at these window sizes/price magnitudes (verified in tests)."""

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self._q: deque[float] = deque()
        self._sum = 0.0

    def update(self, x: float) -> float:
        if len(self._q) == self.window:
            self._sum -= self._q.popleft()
        self._q.append(x)
        self._sum += x
        return self._sum / len(self._q)


class RunningZScore:
    """Online z-normalization using a running mean/std (not fit-once).

    Stats are updated with x first, then the z-score is returned; emits 0.0 until
    `warmup` samples have been seen (or while std is degenerate). Output is clipped
    to +/-clip: when a feature's running std is momentarily tiny, the raw z-score
    can spike by orders of magnitude, which sends SGD-style learners into a
    positive-feedback divergence (observed on replay with the linear model)."""

    def __init__(self, window: int | None = None, warmup: int = 30, clip: float = 8.0) -> None:
        self.warmup = max(2, warmup)
        self.clip = clip
        self._w = Welford(window)

    def normalize(self, x: float) -> float:
        self._w.update(x)
        if self._w.n < self.warmup:
            return 0.0
        std = self._w.std
        if std <= 0.0:
            return 0.0
        z = (x - self._w.mean) / std
        return max(-self.clip, min(self.clip, z))

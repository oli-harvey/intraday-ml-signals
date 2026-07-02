"""Exponential backoff for WS reconnects."""

from __future__ import annotations

import random


class ExponentialBackoff:
    """delay = min(cap, initial * factor**attempt), optionally jittered.

    Deterministic when jitter=0 (unit tests). reset() after a successful
    connection so a later disconnect starts from `initial` again.
    """

    def __init__(
        self,
        initial: float = 1.0,
        factor: float = 2.0,
        cap: float = 30.0,
        jitter: float = 0.0,
    ) -> None:
        self.initial = initial
        self.factor = factor
        self.cap = cap
        self.jitter = jitter
        self._attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.initial * self.factor**self._attempt)
        self._attempt += 1
        if self.jitter:
            delay *= 1.0 + self.jitter * random.random()
        return min(delay, self.cap)

    def reset(self) -> None:
        self._attempt = 0

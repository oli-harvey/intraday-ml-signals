"""Prediction -> signal policy.

Only act when the predicted return clears a threshold that covers estimated
transaction cost + spread, so we don't overtrade on noise. Phase 4 — see docs/PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    LONG = 1
    FLAT = 0
    SHORT = -1


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    action: Action
    predicted_return: float
    confidence: float


class SignalPolicy:
    def __init__(self, cost_bps: float = 5.0, dead_zone_bps: float = 2.0) -> None:
        self.cost_bps = cost_bps
        self.dead_zone_bps = dead_zone_bps

    def decide(self, symbol: str, predicted_return: float, spread_bps: float) -> Signal:
        raise NotImplementedError("Phase 4")

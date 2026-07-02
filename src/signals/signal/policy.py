"""Prediction -> signal policy.

Act only when the predicted return clears estimated round-trip cost: fee +
half-spread (we cross it once per side) + a dead-zone margin against noise.
Everything in bps of mid for legibility.
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
    threshold: float  # what the prediction had to clear (fraction)
    confidence: float  # |predicted| / threshold; 1.0 == exactly at threshold


class SignalPolicy:
    def __init__(self, cost_bps: float = 5.0, dead_zone_bps: float = 2.0) -> None:
        self.cost_bps = cost_bps
        self.dead_zone_bps = dead_zone_bps

    def decide(self, symbol: str, predicted_return: float, spread_bps: float) -> Signal:
        threshold = (self.cost_bps + 0.5 * spread_bps + self.dead_zone_bps) / 1e4
        if predicted_return > threshold:
            action = Action.LONG
        elif predicted_return < -threshold:
            action = Action.SHORT
        else:
            action = Action.FLAT
        return Signal(
            symbol=symbol,
            action=action,
            predicted_return=predicted_return,
            threshold=threshold,
            confidence=abs(predicted_return) / threshold if threshold > 0 else 0.0,
        )

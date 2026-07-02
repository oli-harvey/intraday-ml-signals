"""Hard risk controls + position sizing.

Sizing: fixed-fractional or volatility-scaled, capped. Hard limits: max position size,
daily-loss circuit breaker, max open positions. A tripped limit forces FLAT / blocks
new entries. Phase 4 — see docs/PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_usd: float
    max_open_positions: int
    daily_loss_limit_usd: float
    risk_fraction: float = 0.01  # fraction of equity per trade (fixed-fractional)


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._realized_pnl_today: float = 0.0

    def size_order(self, equity: float, price: float, volatility: float) -> float:
        """Return target quantity, capped by limits. 0 if a limit blocks the trade."""
        raise NotImplementedError("Phase 4")

    def circuit_breaker_tripped(self) -> bool:
        raise NotImplementedError("Phase 4")

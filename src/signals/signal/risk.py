"""Hard risk controls + position sizing.

Sizing: fixed-fractional notional (fraction of equity), optionally scaled down
when realized vol exceeds target, capped by max_position_usd. Hard limits: max
open positions, daily-loss circuit breaker. A tripped breaker blocks all new
entries until reset_day(); exits are always allowed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_usd: float = 1_000.0
    max_open_positions: int = 1
    daily_loss_limit_usd: float = 200.0
    risk_fraction: float = 0.01  # fraction of equity per entry
    vol_target: float | None = None  # e.g. per-quote return std; None = no vol scaling
    min_notional_usd: float = 10.0  # below this, don't bother trading


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.realized_pnl_today = 0.0

    def record_realized_pnl(self, delta_usd: float) -> None:
        self.realized_pnl_today += delta_usd

    def reset_day(self) -> None:
        self.realized_pnl_today = 0.0

    @property
    def circuit_breaker_tripped(self) -> bool:
        return self.realized_pnl_today <= -self.limits.daily_loss_limit_usd

    def entry_allowed(self, open_positions: int) -> bool:
        return not self.circuit_breaker_tripped and open_positions < self.limits.max_open_positions

    def size_order(self, equity: float, price: float, volatility: float = 0.0) -> float:
        """Return entry quantity (asset units). 0.0 if limits block the trade."""
        if self.circuit_breaker_tripped or equity <= 0 or price <= 0:
            return 0.0
        notional = equity * self.limits.risk_fraction
        if self.limits.vol_target is not None and volatility > self.limits.vol_target:
            notional *= self.limits.vol_target / volatility
        notional = min(notional, self.limits.max_position_usd)
        if notional < self.limits.min_notional_usd:
            return 0.0
        return round(notional / price, 6)  # crypto supports fractional qty

"""Local position book: maps signals to concrete order intents.

Alpaca crypto accounts are non-marginable => LONG-ONLY:
- LONG signal: enter only if flat (and risk allows).
- SHORT signal: exit an open long; with no position it is a no-op.
Tracks entry price so realized PnL can feed the risk manager's circuit breaker.
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import Action, Signal
from .risk import RiskManager


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    reason: str


@dataclass(slots=True)
class Position:
    qty: float
    entry_price: float


class PositionBook:
    def __init__(self, risk: RiskManager) -> None:
        self.risk = risk
        self.positions: dict[str, Position] = {}

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def on_signal(
        self, signal: Signal, mid: float, equity: float, volatility: float = 0.0
    ) -> OrderIntent | None:
        """Turn a signal into an order intent given current holdings. None = do nothing."""
        held = self.positions.get(signal.symbol)
        if signal.action is Action.LONG and held is None:
            if not self.risk.entry_allowed(self.open_count):
                return None
            qty = self.risk.size_order(equity, mid, volatility)
            if qty <= 0:
                return None
            return OrderIntent(signal.symbol, "buy", qty, "enter long")
        if signal.action is Action.SHORT and held is not None:
            return OrderIntent(signal.symbol, "sell", held.qty, "exit long")
        return None  # FLAT, already long on LONG, or SHORT while flat (long-only)

    def on_fill(self, symbol: str, side: str, qty: float, price: float) -> float:
        """Update book with a fill; returns realized PnL (0.0 for entries)."""
        if side == "buy":
            self.positions[symbol] = Position(qty=qty, entry_price=price)
            return 0.0
        held = self.positions.pop(symbol, None)
        if held is None:
            return 0.0
        realized = (price - held.entry_price) * min(qty, held.qty)
        self.risk.record_realized_pnl(realized)
        return realized

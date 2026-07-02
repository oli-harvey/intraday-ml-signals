"""Normalized market-data events.

Frozen, slotted dataclasses keep per-event allocation cheap and immutable as events
flow across asyncio queues. Timestamps are epoch nanoseconds (monotonic ordering).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single trade print."""

    symbol: str
    ts_ns: int
    price: float
    size: float
    side: Side | None = None  # if the feed exposes aggressor side
    recv_ns: int = 0  # local receive time (epoch ns), for latency instrumentation


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book quote (for spread / microstructure features)."""

    symbol: str
    ts_ns: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    recv_ns: int = 0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return 0.5 * (self.ask + self.bid)


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar (e.g. 1s), used for backfill and coarser features."""

    symbol: str
    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    recv_ns: int = 0


# Union of everything a DataSource can emit.
MarketEvent = Tick | Quote | Bar

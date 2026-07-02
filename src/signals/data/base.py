"""DataSource interface — the swap point for Alpaca / Finnhub / Polygon.

Everything downstream (features, model, signal) depends only on this abstraction and
on `schema.py`, so the feed can be swapped without touching the pipeline.

Lifecycle: `subscribe()` records symbols, then `stream()` owns the connection —
including auth, reconnect, and backoff — so a dropped socket never leaks state to
downstream stages. `connect()` is optional eager validation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from .schema import Bar, MarketEvent


class DataSource(ABC):
    """Abstract async market-data feed."""

    @abstractmethod
    async def connect(self) -> None:
        """Optional eager validation (config/credentials). May be a no-op."""

    @abstractmethod
    async def subscribe(self, symbols: Sequence[str]) -> None:
        """Record symbols to stream trades/quotes/bars for."""

    @abstractmethod
    def stream(self) -> AsyncIterator[MarketEvent]:
        """Yield normalized events as they arrive.

        Owns the connection: handles auth, heartbeats, and reconnect-with-backoff
        internally. Ends only when close() is called (or on fatal auth errors).
        """
        raise NotImplementedError

    @abstractmethod
    async def backfill_bars(
        self, symbol: str, lookback_s: int, resolution_s: int = 60
    ) -> list[Bar]:
        """REST historical bars to warm ring buffers on startup (cold path)."""

    @abstractmethod
    async def close(self) -> None:
        """Signal stream() to exit its reconnect loop and tear down cleanly."""

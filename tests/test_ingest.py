"""IngestStage: fake source -> ring buffers updated, events forwarded in order."""

import asyncio
from collections.abc import AsyncIterator, Sequence

from signals.data.base import DataSource
from signals.data.ingest import IngestStage
from signals.data.schema import Bar, MarketEvent, Tick


class FakeSource(DataSource):
    """Replays a scripted list of events, then ends the stream."""

    def __init__(self, events: list[MarketEvent]) -> None:
        self._events = events

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbols: Sequence[str]) -> None:
        return None

    async def stream(self) -> AsyncIterator[MarketEvent]:  # type: ignore[override]
        for event in self._events:
            yield event

    async def backfill_bars(
        self, symbol: str, lookback_s: int, resolution_s: int = 60
    ) -> list[Bar]:
        return []

    async def close(self) -> None:
        return None


async def test_ingest_updates_buffers_and_forwards_in_order() -> None:
    events: list[MarketEvent] = [
        Tick(symbol="BTC/USD", ts_ns=1, price=100.0, size=1.0),
        Tick(symbol="ETH/USD", ts_ns=2, price=10.0, size=2.0),
        Bar(symbol="BTC/USD", ts_ns=3, open=1, high=2, low=0, close=1, volume=5),
        Tick(symbol="BTC/USD", ts_ns=4, price=101.0, size=0.5),
    ]
    out: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=16)
    stage = IngestStage(FakeSource(events), out, buffer_depth=8)

    await stage.run()  # fake stream is finite; run() returns when it ends

    assert stage.events == 4
    # Trades landed in per-symbol buffers; the Bar did not touch them.
    assert stage.buffers["BTC/USD"].price.last(2).tolist() == [100.0, 101.0]
    assert len(stage.buffers["ETH/USD"]) == 1
    # Every event forwarded, order preserved.
    drained = [out.get_nowait() for _ in range(out.qsize())]
    assert drained == events
    assert stage.queue_hwm == 4

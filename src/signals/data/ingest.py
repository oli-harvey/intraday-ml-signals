"""Ingest stage: DataSource stream -> per-symbol ring buffers -> output queue.

First stage of the pipeline. Trades update SymbolBuffers (the hot-path state the
feature engine reads); every event is forwarded to the bounded output queue, whose
`await put()` provides backpressure if downstream stalls. queue_hwm records the
worst backlog seen so slow consumers are visible in monitoring.
"""

from __future__ import annotations

import asyncio

from .base import DataSource
from .ringbuffer import SymbolBuffers
from .schema import MarketEvent, Tick


class IngestStage:
    def __init__(
        self,
        source: DataSource,
        out: asyncio.Queue[MarketEvent],
        buffer_depth: int = 512,
    ) -> None:
        self.source = source
        self.out = out
        self.buffer_depth = buffer_depth
        self.buffers: dict[str, SymbolBuffers] = {}
        self.events = 0
        self.queue_hwm = 0

    def buffers_for(self, symbol: str) -> SymbolBuffers:
        buf = self.buffers.get(symbol)
        if buf is None:
            buf = self.buffers[symbol] = SymbolBuffers(self.buffer_depth)
        return buf

    async def run(self) -> None:
        """Consume the stream until the source closes or the task is cancelled."""
        async for event in self.source.stream():
            if isinstance(event, Tick):
                self.buffers_for(event.symbol).update(event)
            self.events += 1
            await self.out.put(event)
            backlog = self.out.qsize()
            if backlog > self.queue_hwm:
                self.queue_hwm = backlog

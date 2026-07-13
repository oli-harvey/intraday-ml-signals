"""ReplaySource: stream recorded events from the DuckDB cold store.

Implements DataSource, so the replay path exercises the exact same downstream
code as live ingestion (PLAN §10). Events are merged across tables and yielded
in recv_ns order — the order the live process actually saw them, which is what
matters for train/serve parity. As-fast-as-possible by default (no sleeping).
"""

from __future__ import annotations

import heapq
from collections.abc import AsyncIterator, Iterator, Sequence

import duckdb

from .base import DataSource
from .schema import Bar, MarketEvent, Quote, Side, Tick


def _load_events(db_path: str, symbols: Sequence[str] | None) -> Iterator[MarketEvent]:
    conn = duckdb.connect(db_path, read_only=True)
    where = ""
    params: list = []
    if symbols:
        where = f" WHERE symbol IN ({','.join('?' for _ in symbols)})"
        params = list(symbols)
    # ORDER BY recv_ns ALONE IS NOT A TOTAL ORDER: one websocket frame delivers many
    # quotes that all share a recv_ns (63.8% of NVDA rows are tied, up to 75 per
    # frame), and DuckDB's parallel sort breaks those ties differently on each run —
    # so replay yielded a different event sequence every time and the same DB scored
    # differently (stdev ~0.14bps, spread ~0.31bps on a ~3bps signal). Tie-break on
    # (ts_ns, symbol) for a deterministic total order that is also the semantically
    # correct one: arrival batch first, exchange time within the batch.
    order = "ORDER BY recv_ns, ts_ns, symbol"
    try:
        trades = conn.execute(f"SELECT * FROM trades{where} {order}", params).fetchall()
        quotes = conn.execute(f"SELECT * FROM quotes{where} {order}", params).fetchall()
        bars = conn.execute(f"SELECT * FROM bars{where} {order}", params).fetchall()
    finally:
        conn.close()

    def tick_iter() -> Iterator[tuple[int, MarketEvent]]:
        for symbol, ts_ns, price, size, side, recv_ns in trades:
            yield (
                recv_ns,
                Tick(symbol, ts_ns, price, size, Side(side) if side else None, recv_ns),
            )

    def quote_iter() -> Iterator[tuple[int, MarketEvent]]:
        for symbol, ts_ns, bid, ask, bid_size, ask_size, recv_ns in quotes:
            yield recv_ns, Quote(symbol, ts_ns, bid, ask, bid_size, ask_size, recv_ns)

    def bar_iter() -> Iterator[tuple[int, MarketEvent]]:
        for symbol, ts_ns, o, h, lo, c, v, recv_ns in bars:
            yield recv_ns, Bar(symbol, ts_ns, o, h, lo, c, v, recv_ns)

    for _, event in heapq.merge(tick_iter(), quote_iter(), bar_iter(), key=lambda x: x[0]):
        yield event


class ReplaySource(DataSource):
    def __init__(self, db_path: str, symbols: Sequence[str] | None = None) -> None:
        self.db_path = db_path
        self._symbols = list(symbols) if symbols else None

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self._symbols = list(symbols)

    async def stream(self) -> AsyncIterator[MarketEvent]:  # type: ignore[override]
        for event in _load_events(self.db_path, self._symbols):
            yield event

    async def backfill_bars(
        self, symbol: str, lookback_s: int, resolution_s: int = 60
    ) -> list[Bar]:
        return []

    async def close(self) -> None:
        return None

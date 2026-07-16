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

# Rows per fetchmany batch. fetchall() materialized EVERY row of a session as
# Python tuples (~400B each; a 3-symbol day is ~1GB) before yielding anything —
# that spike, on top of the evaluation's own rows, is what OOM-killed the nightly
# digest on the 3.7GB server. Streaming bounds replay memory at ~3 batches.
_FETCH_ROWS = 50_000


def _load_events(db_path: str, symbols: Sequence[str] | None) -> Iterator[MarketEvent]:
    conn = duckdb.connect(db_path, read_only=True)
    # Replay runs beside the live capture on a small box: cap DuckDB's own memory
    # (it spills sorts to disk rather than getting the process OOM-killed).
    conn.execute("SET memory_limit='512MB'")
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

    def stream(table: str, build) -> Iterator[tuple[int, MarketEvent]]:
        # one cursor per table (independent result sets on a shared connection);
        # each stream preserves the total order above, so heapq.merge on recv_ns
        # yields the exact sequence the old fetchall version did — determinism
        # is pinned by the existing replay tests.
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {table}{where} {order}", params)
            while batch := cur.fetchmany(_FETCH_ROWS):
                for row in batch:
                    yield build(row)
        finally:
            cur.close()

    def tick(row) -> tuple[int, MarketEvent]:
        symbol, ts_ns, price, size, side, recv_ns = row
        return recv_ns, Tick(symbol, ts_ns, price, size, Side(side) if side else None, recv_ns)

    def quote(row) -> tuple[int, MarketEvent]:
        symbol, ts_ns, bid, ask, bid_size, ask_size, recv_ns = row
        return recv_ns, Quote(symbol, ts_ns, bid, ask, bid_size, ask_size, recv_ns)

    def bar(row) -> tuple[int, MarketEvent]:
        symbol, ts_ns, o, h, lo, c, v, recv_ns = row
        return recv_ns, Bar(symbol, ts_ns, o, h, lo, c, v, recv_ns)

    try:
        for _, event in heapq.merge(
            stream("trades", tick), stream("quotes", quote), stream("bars", bar),
            key=lambda x: x[0],
        ):
            yield event
    finally:
        conn.close()


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

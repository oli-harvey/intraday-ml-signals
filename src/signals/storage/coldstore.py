"""Cold-path storage: append market events / predictions / orders to DuckDB.

Fed by a non-blocking tap off the live queues; rows are buffered in memory and
flushed in batches from a background task (the actual DB write runs in a thread),
so the decision loop never waits on disk. Reads happen only offline (reports,
replay) — never in the live loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np

from ..data.schema import Bar, MarketEvent, Quote, Tick

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    symbol TEXT, ts_ns BIGINT, price DOUBLE, size DOUBLE, side TEXT, recv_ns BIGINT);
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT, ts_ns BIGINT, bid DOUBLE, ask DOUBLE,
    bid_size DOUBLE, ask_size DOUBLE, recv_ns BIGINT);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT, ts_ns BIGINT, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, volume DOUBLE, recv_ns BIGINT);
CREATE TABLE IF NOT EXISTS predictions (
    symbol TEXT, ts_ns BIGINT, predicted DOUBLE, mid DOUBLE,
    spread_bps DOUBLE, proc_us DOUBLE);
CREATE TABLE IF NOT EXISTS resolutions (
    symbol TEXT, pred_ts_ns BIGINT, resolved_ts_ns BIGINT,
    predicted DOUBLE, realized DOUBLE);
CREATE TABLE IF NOT EXISTS orders (
    symbol TEXT, ts_ns BIGINT, action TEXT, qty DOUBLE,
    status TEXT, fill_price DOUBLE, note TEXT);
"""


@dataclass(frozen=True, slots=True)
class LogPrediction:
    symbol: str
    ts_ns: int
    predicted: float
    mid: float
    spread_bps: float
    proc_us: float


@dataclass(frozen=True, slots=True)
class LogResolution:
    symbol: str
    pred_ts_ns: int
    resolved_ts_ns: int
    predicted: float
    realized: float


@dataclass(frozen=True, slots=True)
class LogOrder:
    symbol: str
    ts_ns: int
    action: str
    qty: float
    status: str
    fill_price: float
    note: str = ""


LogRecord = MarketEvent | LogPrediction | LogResolution | LogOrder

_INSERTS = {
    "trades": "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?)",
    "quotes": "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
    "bars": "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "predictions": "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)",
    "resolutions": "INSERT INTO resolutions VALUES (?, ?, ?, ?, ?)",
    "orders": "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
}

# Column order per table, matching _SCHEMA and the tuples built by _row(). Used to
# build numpy columns for the bulk insert (see _flush_sync).
_COLUMNS = {
    "trades": ("symbol", "ts_ns", "price", "size", "side", "recv_ns"),
    "quotes": ("symbol", "ts_ns", "bid", "ask", "bid_size", "ask_size", "recv_ns"),
    "bars": ("symbol", "ts_ns", "open", "high", "low", "close", "volume", "recv_ns"),
    "predictions": ("symbol", "ts_ns", "predicted", "mid", "spread_bps", "proc_us"),
    "resolutions": ("symbol", "pred_ts_ns", "resolved_ts_ns", "predicted", "realized"),
    "orders": ("symbol", "ts_ns", "action", "qty", "status", "fill_price", "note"),
}
# Text columns need dtype=object (side can be None; numpy would otherwise pick a
# fixed-width unicode dtype and choke on the null).
_TEXT_COLUMNS = frozenset({"symbol", "side", "action", "status", "note"})


def _row(record: LogRecord) -> tuple[str, tuple]:
    if isinstance(record, Tick):
        side = record.side.value if record.side else None
        return "trades", (
            record.symbol, record.ts_ns, record.price, record.size, side, record.recv_ns,
        )
    if isinstance(record, Quote):
        return "quotes", (
            record.symbol, record.ts_ns, record.bid, record.ask,
            record.bid_size, record.ask_size, record.recv_ns,
        )
    if isinstance(record, Bar):
        return "bars", (
            record.symbol, record.ts_ns, record.open, record.high, record.low,
            record.close, record.volume, record.recv_ns,
        )
    if isinstance(record, LogPrediction):
        return "predictions", (
            record.symbol, record.ts_ns, record.predicted, record.mid,
            record.spread_bps, record.proc_us,
        )
    if isinstance(record, LogResolution):
        return "resolutions", (
            record.symbol, record.pred_ts_ns, record.resolved_ts_ns,
            record.predicted, record.realized,
        )
    if isinstance(record, LogOrder):
        return "orders", (
            record.symbol, record.ts_ns, record.action, record.qty,
            record.status, record.fill_price, record.note,
        )
    raise TypeError(f"unloggable record: {type(record)}")


class ColdStore:
    def __init__(self, path: str = "data/ticks.duckdb") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(path)
        self._conn.execute(_SCHEMA)
        self._buffers: dict[str, list[tuple]] = {name: [] for name in _INSERTS}
        self.rows_written = 0

    def append(self, record: LogRecord) -> None:
        """Buffer one record in memory (no I/O here)."""
        table, row = _row(record)
        self._buffers[table].append(row)

    def _flush_sync(self, batches: dict[str, list[tuple]]) -> int:
        """Bulk-insert each table's rows as numpy columns.

        `executemany` binds row-by-row in Python and manages ~1,650 rows/s on the
        VPS (2,600 even inside one transaction) — so slow that the 30-symbol open
        burst on 2026-07-13 built a multi-minute write, stalled the drain, and got
        the websocket dropped as a slow consumer. DuckDB replacement-scans a dict of
        numpy arrays and ingests it columnar: measured **1.8M rows/s on the same
        box**, a ~1,100x speedup, with no new dependency (numpy is already required).
        """
        n = 0
        for table, rows in batches.items():
            if not rows:
                continue
            columns = list(zip(*rows, strict=True))  # all rows have the table's arity
            batch = {  # noqa: F841 — DuckDB resolves `batch` via replacement scan
                name: (
                    np.array(col, dtype=object)
                    if name in _TEXT_COLUMNS
                    else np.asarray(col)
                )
                for name, col in zip(_COLUMNS[table], columns, strict=True)
            }
            self._conn.execute(f"INSERT INTO {table} SELECT * FROM batch")
            n += len(rows)
        return n

    async def flush(self) -> int:
        """Write buffered rows in a thread; returns rows written."""
        batches = {t: rows for t, rows in self._buffers.items() if rows}
        if not batches:
            return 0
        self._buffers = {name: [] for name in _INSERTS}
        n = await asyncio.to_thread(self._flush_sync, batches)
        self.rows_written += n
        return n

    async def run(
        self,
        queue: asyncio.Queue[LogRecord],
        flush_interval_s: float = 2.0,
        max_batch: int = 20_000,
    ) -> None:
        """Drain the queue continuously; flush every flush_interval_s OR whenever
        the buffer reaches max_batch rows, whichever comes first.

        Two lessons are baked in here, both learned the hard way:

        1. Drain GREEDILY. The original loop did one `asyncio.wait_for(queue.get())`
           per record — a timer handle per event — capping the writer at ~4k
           events/s. Fine for 3 crypto symbols (~200/s), hopeless for a 30-symbol
           equities open.

        2. But BOUND THE FLUSH. Greedy draining alone just moves the bottleneck to
           DuckDB: an unbounded buffer let the open-bell burst build one enormous
           executemany that ran for MINUTES (CPU-bound, commits only at the end).
           While it ran, nothing drained, the queue hit its cap, the producer's
           blocking put() stalled the websocket reader, and Alpaca dropped us as a
           slow consumer — 34 reconnects and a frozen writer on 2026-07-13. Capping
           the batch keeps every write short, so the loop always comes back to drain.
        """
        loop = asyncio.get_running_loop()
        next_flush = loop.time() + flush_interval_s
        try:
            while True:
                drained = 0
                while drained < max_batch:  # bounded: never build a runaway batch
                    try:
                        self.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                    drained += 1

                if drained >= max_batch or loop.time() >= next_flush:
                    await self.flush()
                    next_flush = loop.time() + flush_interval_s

                if drained:
                    await asyncio.sleep(0)  # yield so the producer can refill
                else:  # queue empty — block until the next record or the flush deadline
                    timeout = max(0.0, next_flush - loop.time())
                    with contextlib.suppress(TimeoutError):  # deadline hit: go flush
                        self.append(await asyncio.wait_for(queue.get(), timeout))
        finally:
            await self.flush()  # never lose the tail on cancellation

    def close(self) -> None:
        self._conn.close()

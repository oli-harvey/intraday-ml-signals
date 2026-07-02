"""Cold-path storage: append market events / predictions / orders to DuckDB.

Fed by a non-blocking tap off the live queues; rows are buffered in memory and
flushed in batches from a background task (the actual DB write runs in a thread),
so the decision loop never waits on disk. Reads happen only offline (reports,
replay) — never in the live loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import duckdb

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
        n = 0
        for table, rows in batches.items():
            if rows:
                self._conn.executemany(_INSERTS[table], rows)
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
        self, queue: asyncio.Queue[LogRecord], flush_interval_s: float = 2.0
    ) -> None:
        """Drain the tap queue continuously, flushing every flush_interval_s."""
        loop = asyncio.get_running_loop()
        next_flush = loop.time() + flush_interval_s
        try:
            while True:
                timeout = max(0.0, next_flush - loop.time())
                try:
                    record = await asyncio.wait_for(queue.get(), timeout)
                    self.append(record)
                except TimeoutError:
                    pass
                if loop.time() >= next_flush:
                    await self.flush()
                    next_flush = loop.time() + flush_interval_s
        finally:
            await self.flush()  # never lose the tail on cancellation

    def close(self) -> None:
        self._conn.close()

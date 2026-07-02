"""ColdStore round-trip + ReplaySource ordering."""

import asyncio
import contextlib

import duckdb

from signals.data.replay import ReplaySource
from signals.data.schema import Bar, Quote, Side, Tick
from signals.storage.coldstore import ColdStore, LogOrder, LogPrediction, LogResolution


async def test_roundtrip_and_replay_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = str(tmp_path / "test.duckdb")
    store = ColdStore(db)
    events = [
        Quote("BTC/USD", ts_ns=100, bid=99, ask=101, bid_size=1, ask_size=2, recv_ns=1000),
        Tick("BTC/USD", ts_ns=150, price=100.5, size=0.1, side=Side.BUY, recv_ns=1500),
        Quote("BTC/USD", ts_ns=200, bid=100, ask=102, bid_size=1, ask_size=1, recv_ns=2000),
        Bar("BTC/USD", ts_ns=300, open=1, high=2, low=0, close=1, volume=3, recv_ns=2500),
        Quote("ETH/USD", ts_ns=250, bid=10, ask=11, bid_size=5, ask_size=5, recv_ns=3000),
    ]
    for e in events:
        store.append(e)
    store.append(LogPrediction("BTC/USD", 200, 0.001, 101.0, 19.8, 42.0))
    store.append(LogResolution("BTC/USD", 100, 200, 0.001, 0.002))
    store.append(LogOrder("BTC/USD", 210, "buy", 0.01, "filled", 101.0))
    assert await store.flush() == 8
    store.close()

    conn = duckdb.connect(db, read_only=True)
    assert conn.execute("SELECT count(*) FROM quotes").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT predicted FROM predictions").fetchone()[0] == 0.001
    assert conn.execute("SELECT side FROM trades").fetchone()[0] == "buy"
    conn.close()

    # replay yields all market events in recv_ns order, filterable by symbol
    replayed = [e async for e in ReplaySource(db).stream()]
    assert [e.recv_ns for e in replayed] == [1000, 1500, 2000, 2500, 3000]
    assert replayed[1].side is Side.BUY
    btc_only = [e async for e in ReplaySource(db, ["BTC/USD"]).stream()]
    assert len(btc_only) == 4


async def test_run_flushes_on_cancellation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = str(tmp_path / "cancel.duckdb")
    store = ColdStore(db)
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(store.run(queue, flush_interval_s=60))  # long interval
    await queue.put(Tick("BTC/USD", ts_ns=1, price=1.0, size=1.0))
    await asyncio.sleep(0.05)  # let the task consume it
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert store.rows_written == 1  # tail flushed despite cancellation
    store.close()

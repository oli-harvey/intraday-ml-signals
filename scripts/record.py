"""Record live market events to the DuckDB cold store (for replay/research).

Usage:
    uv run python scripts/record.py --symbols BTC/USD ETH/USD --duration 1800 \
        --db data/session.duckdb
"""

from __future__ import annotations

import argparse
import asyncio
import time

import uvloop

from signals.config import load_alpaca_config
from signals.data.alpaca import AlpacaSource
from signals.data.ingest import IngestStage
from signals.data.schema import MarketEvent
from signals.storage.coldstore import ColdStore


async def main_async(args: argparse.Namespace) -> None:
    source = AlpacaSource(load_alpaca_config(), market="crypto", subscribe_quotes=True)
    await source.subscribe(args.symbols)
    queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=10_000)
    stage = IngestStage(source, queue)
    store = ColdStore(args.db)

    tasks = [
        asyncio.create_task(stage.run()),
        asyncio.create_task(store.run(queue)),
    ]
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.duration:
            await asyncio.sleep(30)
            print(
                f"[+{time.monotonic() - start:5.0f}s] events={stage.events}"
                f" rows_written={store.rows_written} q_hwm={stage.queue_hwm}"
                f" reconnects={source.reconnects}",
                flush=True,
            )
    finally:
        await source.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.flush()
        store.close()
    print(f"done: {store.rows_written} rows -> {args.db}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--db", default="data/session.duckdb")
    args = parser.parse_args()
    uvloop.install()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

"""Record Alpaca + Coinbase BTC feeds simultaneously into one cold store.

Both sources share one process (one recv_ns clock), one queue, one DuckDB —
so replay ordering across venues is honest. This is the data-collection side
of the cross-venue lead-lag experiment (docs/RESEARCH.md).

NB: requires the Alpaca crypto WS connection to be free (one per feed — pause
the server's intraday-pipeline service first).

Usage:
    uv run python scripts/record_dual.py --duration 10800 --db data/dualvenue.duckdb
"""

from __future__ import annotations

import argparse
import asyncio
import time

import uvloop

from signals.config import load_alpaca_config
from signals.data.alpaca import AlpacaSource
from signals.data.coinbase import CoinbaseSource
from signals.data.ingest import IngestStage
from signals.data.schema import MarketEvent
from signals.storage.coldstore import ColdStore


async def main_async(args: argparse.Namespace) -> None:
    alpaca = AlpacaSource(load_alpaca_config(), market="crypto", subscribe_quotes=True)
    await alpaca.subscribe(args.alpaca_symbols)
    coinbase = CoinbaseSource()
    await coinbase.subscribe(args.coinbase_products)

    queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=50_000)
    stages = [IngestStage(alpaca, queue), IngestStage(coinbase, queue)]
    store = ColdStore(args.db)

    tasks = [asyncio.create_task(stage.run()) for stage in stages]
    tasks.append(asyncio.create_task(store.run(queue)))
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.duration:
            await asyncio.sleep(60)
            print(
                f"[+{time.monotonic() - start:6.0f}s]"
                f" alpaca={stages[0].events} coinbase={stages[1].events}"
                f" rows={store.rows_written} q_hwm={max(s.queue_hwm for s in stages)}"
                f" reconnects={alpaca.reconnects}+{coinbase.reconnects}",
                flush=True,
            )
    finally:
        await alpaca.close()
        await coinbase.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.flush()
        store.close()
    print(f"done: {store.rows_written} rows -> {args.db}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpaca-symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--coinbase-products", nargs="+", default=["BTC-USD"])
    parser.add_argument("--duration", type=float, default=10_800)
    parser.add_argument("--db", default="data/dualvenue.duckdb")
    args = parser.parse_args()
    uvloop.install()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

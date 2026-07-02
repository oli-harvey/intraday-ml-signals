"""Phase 1 live validation: run the real ingest against Alpaca's crypto stream.

Reports tick rate per symbol, inter-trade gaps, feed->local latency, queue
high-water mark, RSS over time (watching for unbounded growth), reconnects, and
verifies ring-buffer integrity at the end. Cold-path script — prints and
subprocess calls here are fine; the hot loop under test stays clean.

Usage:
    uv run python scripts/validate_ingest.py --symbols BTC/USD ETH/USD --duration 600
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import uvloop

from signals.config import load_alpaca_config
from signals.data.alpaca import AlpacaSource
from signals.data.ingest import IngestStage
from signals.data.schema import Bar, MarketEvent, Quote, Tick


@dataclass
class SymbolStats:
    trades: int = 0
    quotes: int = 0
    bars: int = 0
    last_trade_ns: int = 0
    max_gap_ns: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def on_trade(self, tick: Tick) -> None:
        self.trades += 1
        if self.last_trade_ns:
            self.max_gap_ns = max(self.max_gap_ns, tick.ts_ns - self.last_trade_ns)
        self.last_trade_ns = tick.ts_ns
        self.latencies_ms.append((tick.recv_ns - tick.ts_ns) / 1e6)


def rss_mb() -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    return int(out.stdout.strip()) / 1024  # macOS reports KB


async def consume(queue: asyncio.Queue[MarketEvent], stats: dict[str, SymbolStats]) -> None:
    while True:
        event = await queue.get()
        s = stats.setdefault(event.symbol, SymbolStats())
        if isinstance(event, Tick):
            s.on_trade(event)
        elif isinstance(event, Quote):
            s.quotes += 1
        elif isinstance(event, Bar):
            s.bars += 1


async def report_loop(
    stage: IngestStage,
    stats: dict[str, SymbolStats],
    rss_samples: list[float],
    interval: float,
) -> None:
    start = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - start
        rss = rss_mb()
        rss_samples.append(rss)
        parts = [
            f"{sym}: {st.trades}t/{st.quotes}q/{st.bars}b ({st.trades / elapsed:.1f} t/s)"
            for sym, st in sorted(stats.items())
        ]
        print(
            f"[+{elapsed:5.0f}s] {' | '.join(parts) or 'no events yet'}"
            f" | q_hwm {stage.queue_hwm} | reconnects {stage.source.reconnects}"
            f" | rss {rss:.0f}MB",
            flush=True,
        )


def final_report(
    stage: IngestStage,
    stats: dict[str, SymbolStats],
    rss_samples: list[float],
    duration: float,
) -> bool:
    ok = True
    print("\n=== final report ===")
    for sym, st in sorted(stats.items()):
        lat = np.array(st.latencies_ms) if st.latencies_ms else np.array([0.0])
        print(
            f"{sym}: trades={st.trades} ({st.trades / duration:.2f}/s) quotes={st.quotes}"
            f" bars={st.bars} | max trade gap {st.max_gap_ns / 1e9:.1f}s"
            f" | feed->local latency ms p50={np.percentile(lat, 50):.0f}"
            f" p99={np.percentile(lat, 99):.0f} max={lat.max():.0f}"
        )
    print(f"queue high-water mark: {stage.queue_hwm}, reconnects: {stage.source.reconnects}")
    if rss_samples:
        print(
            f"rss MB: first={rss_samples[0]:.0f} last={rss_samples[-1]:.0f}"
            f" max={max(rss_samples):.0f}"
        )

    print("\n=== ring buffer integrity ===")
    for sym, buf in sorted(stage.buffers.items()):
        n = len(buf)
        ts = buf.ts_ns.last(n)
        monotonic = bool(np.all(np.diff(ts) >= 0)) if n > 1 else True
        expected = min(stats[sym].trades, buf.depth)
        counts_ok = n == expected
        print(
            f"{sym}: len={n} (expect {expected}: {'ok' if counts_ok else 'MISMATCH'})"
            f" ts monotonic: {'ok' if monotonic else 'VIOLATION'}"
            f" last price={buf.price.latest():.2f}"
        )
        ok = ok and counts_ok and monotonic

    total_trades = sum(st.trades for st in stats.values())
    if total_trades == 0:
        print("FAIL: no trades received")
        ok = False
    print(f"\nresult: {'PASS' if ok else 'FAIL'}")
    return ok


async def main_async(args: argparse.Namespace) -> bool:
    source = AlpacaSource(load_alpaca_config(), market="crypto", subscribe_quotes=args.quotes)
    await source.subscribe(args.symbols)
    queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=10_000)
    stage = IngestStage(source, queue, buffer_depth=args.depth)
    stats: dict[str, SymbolStats] = {}
    rss_samples: list[float] = [rss_mb()]

    tasks = [
        asyncio.create_task(stage.run()),
        asyncio.create_task(consume(queue, stats)),
        asyncio.create_task(report_loop(stage, stats, rss_samples, args.report_every)),
    ]
    try:
        await asyncio.sleep(args.duration)
    finally:
        await source.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return final_report(stage, stats, rss_samples, args.duration)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--duration", type=float, default=600, help="seconds to run")
    parser.add_argument("--depth", type=int, default=512, help="ring buffer depth")
    parser.add_argument("--quotes", action="store_true", help="also subscribe to quotes")
    parser.add_argument("--report-every", type=float, default=10.0)
    args = parser.parse_args()
    uvloop.install()
    sys.exit(0 if asyncio.run(main_async(args)) else 1)


if __name__ == "__main__":
    main()

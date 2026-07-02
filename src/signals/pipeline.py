"""asyncio orchestration — wires the stages together via bounded queues.

    ingest -> features -> model -> signal/risk -> execution
                  \\------------ tap -------------> cold store

Each stage is an async task consuming one queue and producing to the next. Bounded
queues provide backpressure. The SAME wiring is used by scripts/replay.py so the
replay path and the live path share code. Phase 4 wires it end-to-end — see docs/PLAN.md.
"""

from __future__ import annotations

import argparse
import asyncio


async def run(symbols: list[str], paper: bool = True) -> None:
    raise NotImplementedError("Wired end-to-end in Phase 4; stages built Phases 1-3.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live signal pipeline.")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--paper", action="store_true", default=True)
    args = parser.parse_args()
    asyncio.run(run(args.symbols, args.paper))


if __name__ == "__main__":
    main()

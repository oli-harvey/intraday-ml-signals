"""Walk-forward replay harness.

Streams stored ticks (from the DuckDB/SQLite cold store) through the EXACT SAME async
pipeline used live, so we catch train/serve skew and evaluate with no lookahead and no
time-shuffling. Offline analysis (pandas/matplotlib) is fine to summarize results.

Phase 3+ — see docs/PLAN.md.
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay stored ticks through the pipeline.")
    parser.add_argument("--db", default="data/ticks.duckdb")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.parse_args()
    raise NotImplementedError("Phase 3+: replay uses the same pipeline wiring as live.")


if __name__ == "__main__":
    main()

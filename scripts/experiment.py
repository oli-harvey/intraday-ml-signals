"""Experiment grid: models x horizons x recorded sessions, one compact table.

Every cell is a fresh walk-forward run over the recorded session (no state
shared between cells, no shuffling). Columns to care about:
- dir vs pers: model directional accuracy vs the sign-persistence baseline.
  The model only has *directional* skill if dir > pers.
- edge%: MAE improvement over always-predicting-zero (magnitude skill).

Usage:
    uv run python scripts/experiment.py --dbs data/session.duckdb data/live.duckdb \
        --symbols BTC/USD --horizons 10 30 60 --models linear hoeffding
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from signals.evaluation import evaluate

HEADER = (
    f"{'db':<18} {'model':<10} {'hz':>4} {'symbol':<8} {'n':>6}"
    f" {'dir':>6} {'pers':>6} {'d-p':>6} {'edge%':>7} {'p50us':>6}"
)


async def main_async(args: argparse.Namespace) -> None:
    print(HEADER)
    print("-" * len(HEADER))
    for db in args.dbs:
        for model in args.models:
            for horizon in args.horizons:
                result = await evaluate(
                    db, args.symbols, model, horizon, non_overlapping=args.non_overlapping
                )
                for symbol, score in sorted(result.symbols.items()):
                    seg = score.overall()
                    if seg.n == 0:
                        continue
                    delta = seg.dir_acc - seg.dir_persistence
                    print(
                        f"{Path(db).stem:<18} {model:<10} {horizon:>4.0f} {symbol:<8}"
                        f" {seg.n:>6d} {seg.dir_acc:>6.3f} {seg.dir_persistence:>6.3f}"
                        f" {delta:>+6.3f} {seg.edge_pct:>+7.1f} {result.proc_us_p50:>6.0f}",
                        flush=True,
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbs", nargs="+", required=True)
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--horizons", nargs="+", type=float, default=[10.0, 30.0, 60.0])
    parser.add_argument("--models", nargs="+", default=["linear", "hoeffding"])
    parser.add_argument(
        "--non-overlapping",
        action="store_true",
        help="score only predictions spaced >= horizon apart (independent outcomes)",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

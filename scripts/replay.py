"""Walk-forward replay evaluation (detailed, single configuration).

Streams recorded events through the SAME SymbolPipeline used live (train/serve
parity), learning online with no lookahead and no time-shuffling. Reports MAE
vs the always-zero baseline, and directional accuracy vs the sign-persistence
baseline, per walk-forward quartile.

Usage:
    uv run python scripts/replay.py --db data/session.duckdb --symbols BTC/USD \
        --horizon-s 10 --model hoeffding
"""

from __future__ import annotations

import argparse
import asyncio

from signals.evaluation import evaluate


async def main_async(args: argparse.Namespace) -> None:
    result = await evaluate(args.db, args.symbols, args.model, args.horizon_s)
    print(f"replayed {result.events} events | model={args.model} horizon={args.horizon_s}s")
    print(
        f"feature+inference latency per quote: p50={result.proc_us_p50:.0f}us"
        f" p99={result.proc_us_p99:.0f}us (budget: <15ms)"
    )
    for symbol, score in sorted(result.symbols.items()):
        print(f"\n{symbol}: {len(score.rows)} resolved predictions")
        if len(score.rows) < 8:
            print("  (too few to segment)")
            continue
        for i, seg in enumerate(score.quartiles(), start=1):
            print(
                f"  Q{i}: n={seg.n:5d} mae={seg.mae:.3e} zero_mae={seg.zero_mae:.3e}"
                f" (edge {seg.edge_pct:+.1f}%) dir={seg.dir_acc:.3f}"
                f" pers={seg.dir_persistence:.3f} fade={seg.dir_fade:.3f}"
            )
        seg = score.overall()
        print(
            f"  ALL: mae={seg.mae:.3e} vs zero={seg.zero_mae:.3e} (edge {seg.edge_pct:+.1f}%)"
            f" dir={seg.dir_acc:.3f} pers={seg.dir_persistence:.3f} fade={seg.dir_fade:.3f}"
            f" | model - best_baseline = {seg.dir_acc - seg.dir_best_baseline:+.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/session.duckdb")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--horizon-s", type=float, default=10.0)
    parser.add_argument(
        "--model", choices=["linear", "hoeffding", "classifier"], default="hoeffding"
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

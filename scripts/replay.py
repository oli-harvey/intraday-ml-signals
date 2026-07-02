"""Walk-forward replay evaluation.

Streams recorded events through the SAME SymbolPipeline used live (train/serve
parity), learning online with no lookahead and no time-shuffling. Reports MAE vs
the always-zero baseline and directional accuracy, per walk-forward segment.

Usage:
    uv run python scripts/replay.py --db data/session.duckdb --symbols BTC/USD \
        --horizon-s 10 --model linear
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

import numpy as np

from signals.core import SymbolPipeline
from signals.data.replay import ReplaySource
from signals.features.engine import FeatureEngine
from signals.model.online import OnlineModel


def segment_metrics(rows: list[tuple[float, float]]) -> dict[str, float]:
    """rows: (prediction, realized) in time order."""
    preds = np.array([r[0] for r in rows])
    reals = np.array([r[1] for r in rows])
    nonzero = (preds != 0) & (reals != 0)
    return {
        "n": len(rows),
        "mae": float(np.abs(reals - preds).mean()),
        "zero_mae": float(np.abs(reals).mean()),
        "dir_acc": (
            float(((preds[nonzero] > 0) == (reals[nonzero] > 0)).mean())
            if nonzero.any()
            else float("nan")
        ),
    }


async def main_async(args: argparse.Namespace) -> None:
    source = ReplaySource(args.db, args.symbols)
    pipelines = {
        s: SymbolPipeline(
            s,
            FeatureEngine(),
            OnlineModel(kind=args.model),
            horizon_ns=int(args.horizon_s * 1e9),
        )
        for s in args.symbols
    }
    resolved_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    proc_us: list[float] = []
    events = 0

    async for event in source.stream():
        pipe = pipelines.get(event.symbol)
        if pipe is None:
            continue
        events += 1
        step = pipe.on_event(event)
        if step.prediction is not None:
            proc_us.append(step.prediction.proc_us)
        for r in step.resolved:
            resolved_rows[event.symbol].append((r.prediction, r.realized))

    print(f"replayed {events} events | model={args.model} horizon={args.horizon_s}s")
    lat = np.array(proc_us) if proc_us else np.array([0.0])
    print(
        f"feature+inference latency per quote: p50={np.percentile(lat, 50):.0f}us"
        f" p99={np.percentile(lat, 99):.0f}us max={lat.max():.0f}us"
        f" (budget: <15ms for features+model)"
    )
    for symbol, rows in sorted(resolved_rows.items()):
        print(f"\n{symbol}: {len(rows)} resolved predictions")
        if len(rows) < 8:
            print("  (too few to segment)")
            continue
        quarters = np.array_split(np.arange(len(rows)), 4)
        for i, idx in enumerate(quarters, start=1):
            m = segment_metrics([rows[j] for j in idx])
            edge = (1 - m["mae"] / m["zero_mae"]) * 100 if m["zero_mae"] else float("nan")
            print(
                f"  Q{i}: n={m['n']:5.0f} mae={m['mae']:.3e} zero_mae={m['zero_mae']:.3e}"
                f" (edge {edge:+.1f}%) dir_acc={m['dir_acc']:.3f}"
            )
        overall = segment_metrics(rows)
        edge = (1 - overall["mae"] / overall["zero_mae"]) * 100
        print(
            f"  ALL: mae={overall['mae']:.3e} vs zero={overall['zero_mae']:.3e}"
            f" (edge {edge:+.1f}%) dir_acc={overall['dir_acc']:.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/session.duckdb")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--horizon-s", type=float, default=10.0)
    parser.add_argument("--model", choices=["linear", "hoeffding"], default="linear")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

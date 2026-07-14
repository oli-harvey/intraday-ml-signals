"""Is the windowed +3.3bps a real rule, or a lucky sampling grid?

The 07-14 finding: scoring with non_overlapping=True also subsampled the TRADE sim to
one entry per 5s window, and that is worth ~3x the edge (NVDA +3.32 windowed vs +1.09
per-quote). Windowed is a legitimate rule — "sample the signal every 5s and act on that
reading" — but only if it doesn't depend on WHICH 5s grid you happen to land on.

So: replace the data-driven grid with an ABSOLUTE clock grid (what you'd actually
implement live), and sweep the phase offset across the whole window.

  stable across phases  -> a real rule; the +3.3 stands and clears the 1bp haircut
  swings across phases  -> a lucky grid; the honest number is the per-quote +1.09

One evaluate per session yields every resolved row; the phases are then swept cheaply
over those rows, so this is ~4 evaluates, not 40.

Run:  .venv/bin/python scripts/cadence_sweep.py
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats

from signals.evaluation import SymbolScore, evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig


def on_grid(rows, horizon_ns: int, phase_ns: int):
    """Keep the first row in each absolute time bucket: a live 'sample every 5s' rule
    with a fixed phase, rather than a grid that drifts with the data."""
    kept, last_bucket = [], None
    for r in rows:
        bucket = (r.ts_ns + phase_ns) // horizon_ns
        if bucket != last_bucket:
            kept.append(r)
            last_bucket = bucket
    return kept


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+",
                    default=sorted(glob.glob("data/equities_2026-07-0[789].duckdb"))
                    + sorted(glob.glob("data/equities_2026-07-10.duckdb")))
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL"])
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--phases", type=int, default=10)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    args = ap.parse_args()

    hn = int(args.horizon_s * 1e9)
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    phases = [int(i * hn / args.phases) for i in range(args.phases)]

    def sim(rows):
        return SymbolScore("x", rows).simulate_trading(
            hn, fee_bps=0.0, dead_zone_bps=args.dead_zone_bps,
            max_spread_bps=args.max_spread_bps,
        )

    per_symbol_phase_means: dict[str, list[float]] = {s: [] for s in args.symbols}

    for db in args.dbs:
        day = db.split("equities_")[-1][:10]
        res = await evaluate(db, args.symbols, model_kind="ev", horizon_s=args.horizon_s,
                             non_overlapping=False, feature_config=cfg)  # ALL rows
        print(f"\n=== {day} ===")
        for symbol in args.symbols:
            rows = res.symbols[symbol].rows
            pq = sim(rows)  # per-quote: act on every signal
            nets, trades = [], []
            for ph in phases:
                s = sim(on_grid(rows, hn, ph))
                nets.append(s.avg_net_bps)
                trades.append(s.trades)
            per_symbol_phase_means[symbol].append(stats.mean(nets))
            spread = max(nets) - min(nets)
            print(
                f"  {symbol:5s} per-quote {pq.trades:5d}tr {pq.avg_net_bps:+6.2f}  |  "
                f"windowed×{args.phases} phases: mean {stats.mean(nets):+6.2f} "
                f"sd {stats.pstdev(nets):5.2f} range [{min(nets):+6.2f} … {max(nets):+6.2f}] "
                f"spread {spread:5.2f} · trades ~{int(stats.mean(trades))}"
            )
            neg = sum(1 for n in nets if n <= 0)
            haircut = sum(1 for n in nets if n <= 1.0)  # must clear a 1bp slippage haircut
            print(f"        phases ≤0: {neg}/{len(nets)} · phases failing the 1bp haircut: "
                  f"{haircut}/{len(nets)}")

    print("\n=== VERDICT ===")
    for symbol, means in per_symbol_phase_means.items():
        print(f"  {symbol:5s} per-session phase-mean net: "
              + "  ".join(f"{m:+.2f}" for m in means))
    print("\n  Stable & >1bp on every phase and session -> the windowed rule is real.")
    print("  Swings, or fails the 1bp haircut on many phases -> lucky grid; the honest")
    print("  number is the per-quote one and the NVDA edge does not survive slippage.")


if __name__ == "__main__":
    asyncio.run(main())

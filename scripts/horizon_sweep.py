"""Horizon sweep, done properly: does a longer hold beat the fixed spread toll?

The one lever left. Every equities result dies the same way: the direction edge is
real (d-best positive on both single names, all four sessions) but the 5s move is too
small against the spread. The toll is FIXED (~1-2bp round trip) while the size of a
move grows ~sqrt(t) — so a 30s hold faces the same toll against a ~2.4x bigger move.
Against that, reversion decays with horizon. Which wins is an empirical question that
has never been asked with the methodology corrected.

Corrected throughout (RESEARCH.md 2026-07-13/14):
  - true no-micro ablation (MICRO_FEATURES, incl. the product interactions)
  - deterministic replay
  - net is PHASE-SWEPT: mean and spread over absolute-clock sampling offsets, so a
    lucky grid can't masquerade as an edge again
  - d-best scored on non-overlapping rows (with the baseline recomputed there)

A horizon is only interesting if the phase-mean net is positive AND the phase spread
is small relative to it AND it holds across sessions.

Run:  .venv/bin/python scripts/horizon_sweep.py
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats
from dataclasses import replace

from signals.evaluation import SymbolScore, evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig


def non_overlapping(rows, horizon_ns: int):
    kept, last, prev = [], -(10**18), 0.0
    for r in rows:
        if r.ts_ns >= last + horizon_ns:
            kept.append(replace(r, persistence=prev))
            last, prev = r.ts_ns, r.realized
    return kept


def on_grid(rows, horizon_ns: int, phase_ns: int):
    kept, last_bucket = [], None
    for r in rows:
        b = (r.ts_ns + phase_ns) // horizon_ns
        if b != last_bucket:
            kept.append(r)
            last_bucket = b
    return kept


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", default=sorted(
        glob.glob("data/equities_2026-07-0[789].duckdb")
        + glob.glob("data/equities_2026-07-10.duckdb")))
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL"])
    ap.add_argument("--horizons", nargs="+", type=float, default=[5, 15, 30, 60])
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    args = ap.parse_args()
    cfg = FeatureConfig(exclude=MICRO_FEATURES)

    # results[(sym, hz)] = list over sessions of (phase_mean_net, phase_spread, dbest, trades)
    results: dict[tuple, list] = {}

    for hz in args.horizons:
        hn = int(hz * 1e9)
        phases = [int(i * hn / args.phases) for i in range(args.phases)]
        for db in args.dbs:
            res = await evaluate(db, args.symbols, model_kind="ev", horizon_s=hz,
                                 non_overlapping=False, feature_config=cfg)
            for sym in args.symbols:
                rows = res.symbols[sym].rows
                seg = SymbolScore(sym, non_overlapping(rows, hn)).overall()
                nets, trades = [], []
                for ph in phases:
                    s = SymbolScore(sym, on_grid(rows, hn, ph)).simulate_trading(
                        hn, fee_bps=0.0, dead_zone_bps=args.dead_zone_bps,
                        max_spread_bps=args.max_spread_bps)
                    if s.trades:
                        nets.append(s.avg_net_bps)
                        trades.append(s.trades)
                if not nets:
                    continue
                results.setdefault((sym, hz), []).append((
                    stats.mean(nets), max(nets) - min(nets),
                    seg.dir_acc - seg.dir_best_baseline, int(stats.mean(trades)),
                ))

    print(f"\nHORIZON SWEEP — phase-swept ({args.phases} phases), "
          f"dz{args.dead_zone_bps:g} spread<{args.max_spread_bps:g}bp, "
          f"{len(args.dbs)} sessions\n")
    hdr = (f"{'sym':5s}{'horiz':>7s}{'net(mean)':>11s}{'±phase':>8s}{'net/±ph':>9s}"
           f"{'d-best':>8s}{'trades':>8s}{'sessions>0':>12s}")
    print(hdr)
    print("-" * len(hdr))
    for sym in args.symbols:
        for hz in args.horizons:
            vals = results.get((sym, hz))
            if not vals:
                continue
            nets = [v[0] for v in vals]
            spreads = [v[1] for v in vals]
            dbests = [v[2] for v in vals]
            trades = [v[3] for v in vals]
            m, sp = stats.mean(nets), stats.mean(spreads)
            ratio = m / sp if sp else float("nan")  # signal vs phase fragility
            pos = sum(1 for n in nets if n > 0)
            flag = "  <== robust" if ratio > 1 and pos == len(nets) and m > 1.0 else ""
            print(f"{sym:5s}{hz:>6g}s{m:>11.2f}{sp:>8.2f}{ratio:>9.2f}"
                  f"{stats.mean(dbests):>8.3f}{int(stats.mean(trades)):>8d}"
                  f"{pos:>7d}/{len(nets)}{flag}")
        print()

    print("Read: net/±ph > 1 means the edge is bigger than its phase fragility — the")
    print("test 5s FAILED (net 1.9 vs spread 4.9 -> 0.4). A horizon that is positive on")
    print("every session, clears ~1bp after slippage, AND has net/±ph > 1 is the first")
    print("thing here worth believing.")


if __name__ == "__main__":
    asyncio.run(main())

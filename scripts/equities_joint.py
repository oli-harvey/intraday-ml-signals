"""Joint multi-session screen: does ANY equities config survive across all days?

For every (symbol, dead-zone) at 5s no-micro EV, compute net bps/trade on each
captured session and count how many are green. A real edge is green on most/all
independent sessions; the retracted NVDA config (green, green, red, flat) is what
day-hopping noise looks like. This is the honest verdict tool now that ≥4 sessions
have accumulated.

Run:  .venv/bin/python scripts/equities_joint.py
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig, MICRO_FEATURES

SYMBOLS = ["SPY", "AAPL", "NVDA"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", default=sorted(glob.glob("data/equities_2026-*.duckdb")))
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--dzs", nargs="+", type=float, default=[2, 4, 8])
    args = ap.parse_args()
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(args.horizon_s * 1e9)
    days = [db.split("equities_")[-1].replace(".duckdb", "")[5:] for db in args.dbs]  # MM-DD

    # net[(sym,dz)] = [per-session net bps]; dbest[sym] = [per-session d-best]
    net: dict[tuple, list[float]] = {}
    dbest: dict[str, list[float]] = {s: [] for s in SYMBOLS}
    for db in args.dbs:
        res = await evaluate(db, SYMBOLS, model_kind="ev", horizon_s=args.horizon_s,
                             non_overlapping=True, feature_config=cfg)
        for sym in SYMBOLS:
            sc = res.symbols[sym]
            dbest[sym].append(sc.overall().dir_acc - sc.overall().dir_best_baseline)
            for dz in args.dzs:
                sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=dz)
                net.setdefault((sym, dz), []).append(sim.avg_net_bps)

    print(f"\nJOINT SCREEN — {args.horizon_s:g}s no-micro EV — {len(args.dbs)} sessions "
          f"({', '.join(days)})\n")
    print(f"{'sym':5s}{'dz':>4s}  " + "  ".join(f"{d:>6s}" for d in days)
          + f"  {'green':>6s} {'mean':>7s}")
    print("-" * (11 + 8 * len(days) + 16))
    for sym in SYMBOLS:
        for dz in args.dzs:
            vals = net[(sym, dz)]
            g = sum(1 for v in vals if v > 0)
            cells = "  ".join(f"{v:>+6.1f}" for v in vals)
            flag = "  <== all green" if g == len(vals) else (" <= 3+" if g >= 3 else "")
            print(f"{sym:5s}{dz:>4g}  {cells}  {g:>3d}/{len(vals)} {stats.mean(vals):>+7.2f}{flag}")
        print()

    print("direction edge over baseline (d-best) per session — stable?:")
    for sym in SYMBOLS:
        cells = "  ".join(f"{v:>+6.3f}" for v in dbest[sym])
        print(f"  {sym:5s} {cells}  mean {stats.mean(dbest[sym]):>+.3f}")
    print("\nVerdict: a cell green on all (or 3+/4) sessions = a candidate worth a "
          "paper-live test. Otherwise the edge is day-specific noise.")


if __name__ == "__main__":
    asyncio.run(main())

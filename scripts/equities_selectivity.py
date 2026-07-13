"""Two probes on the ~1bp-short equities EV result:

A) SELECTIVITY sweep — the model's rows are fixed; re-run the trade sim at rising
   dead-zones (trade only when |pred| clears a bigger bar). If magnitude
   selectivity is real, net_bps rises toward/through 0 as trades fall. If net just
   gets noisier around the same negative value, the signal has no exploitable tail.

B) FEATURE ABLATION — crypto found dropping microstructure IMPROVED direction
   (momentum/lag carries it, micro distracts the model). Memory flagged: revisit
   on equities. Compare full features vs momentum-only (exclude the micro family)
   at the chosen horizon.

Run:  .venv/bin/python scripts/equities_selectivity.py \
        --db data/equities_2026-07-07.duckdb --horizon-s 10
"""

from __future__ import annotations

import argparse
import asyncio

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig, MICRO_FEATURES

DEFAULT = ["SPY", "AAPL", "NVDA"]
# microstructure family (order-book/flow) vs the momentum/lag/vol family kept.
# micro_over_spread is the micro-derived interaction (computed before exclude runs).


async def run(db, symbols, horizon_s, cfg=None):
    return await evaluate(db, symbols, model_kind="ev", horizon_s=horizon_s,
                          non_overlapping=True, feature_config=cfg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/equities_2026-07-07.duckdb")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT)
    ap.add_argument("--horizon-s", type=float, default=10.0)
    ap.add_argument("--dead-zones", nargs="+", type=float, default=[0.5, 1, 2, 4, 8])
    args = ap.parse_args()
    hn = int(args.horizon_s * 1e9)

    print(f"\n=== A) SELECTIVITY sweep (ev, {args.horizon_s:g}s, fee=0) ===")
    res = asyncio.run(run(args.db, args.symbols, args.horizon_s))
    print(f"{'sym':6s} {'dz_bps':>6s} {'trades':>7s} {'net_bps':>8s} {'hit%':>5s} {'total_bps':>10s}")
    print("-" * 50)
    for sym in args.symbols:
        sc = res.symbols[sym]
        for dz in args.dead_zones:
            sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=dz)
            total = sim.net_bps_sum
            print(f"{sym:6s} {dz:6g} {sim.trades:7d} {sim.avg_net_bps:8.2f} "
                  f"{sim.hit_rate*100:4.0f}% {total:10.0f}")
        print()

    print(f"=== B) FEATURE ABLATION (ev, {args.horizon_s:g}s): full vs no-micro ===")
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    res_abl = asyncio.run(run(args.db, args.symbols, args.horizon_s, cfg))
    print(f"{'sym':6s} {'variant':>9s} {'mdir':>6s} {'base':>6s} {'d-best':>7s} "
          f"{'trades':>7s} {'net_bps':>8s}")
    print("-" * 56)
    for sym in args.symbols:
        for tag, r in (("full", res), ("no-micro", res_abl)):
            sc = r.symbols[sym]
            seg = sc.overall()
            sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=0.5)
            print(f"{sym:6s} {tag:>9s} {seg.dir_acc:6.3f} {seg.dir_best_baseline:6.3f} "
                  f"{seg.dir_acc - seg.dir_best_baseline:+7.3f} {sim.trades:7d} "
                  f"{sim.avg_net_bps:8.2f}")
        print()


if __name__ == "__main__":
    main()

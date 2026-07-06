"""Research batch: horizon sweep, ablation, meta-labeling, lead-lag.

Non-overlapping scoring throughout; results printed as tables. Findings land
in docs/RESEARCH.md.

Usage:
    python scripts/research_batch.py --db data/paper_2026-07-04.duckdb \
        --symbols BTC/USD ETH/USD --fee-bps 0.2
The first symbol drives sections 1-3 and acts as leader for the rest in 4.
"""

from __future__ import annotations

import argparse
import asyncio

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig


def line(tag: str, seg, sim) -> str:  # type: ignore[no-untyped-def]
    return (
        f"{tag:<34} n={seg.n:>5d} dir={seg.dir_acc:.3f} pers={seg.dir_persistence:.3f}"
        f" fade={seg.dir_fade:.3f} d-best={seg.dir_acc - seg.dir_best_baseline:+.3f}"
        f" edge={seg.edge_pct:+6.1f}% trades={sim.trades:>4d} net={sim.net_bps_sum:+8.1f}bps"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/paper_2026-07-04.duckdb")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--fee-bps", type=float, default=0.2)
    parser.add_argument("--horizons", nargs="+", type=float, default=[5, 10, 30, 60, 120])
    args = parser.parse_args()
    global DB, FEE
    DB, FEE = args.db, args.fee_bps
    primary, followers = args.symbols[0], args.symbols[1:]

    print("=" * 70)
    print(f"1) HORIZON SWEEP ({primary}, non-overlapping)")
    print("=" * 70)
    for kind in ("hoeffding", "classifier"):
        for hz in args.horizons:
            r = await evaluate(DB, [primary], kind, hz, non_overlapping=True)
            s = r.symbols[primary]
            sim = s.simulate_trading(r.horizon_ns, fee_bps=FEE)
            print(line(f"{kind} hz={hz:.0f}s", s.overall(), sim), flush=True)

    print()
    print("=" * 70)
    print(f"2) FEATURE-GROUP ABLATION ({primary}, hoeffding, 10s, non-overlapping)")
    print("=" * 70)
    micro = ("micro_bps", "micro_over_spread", "micro_x_uptick", "micro_x_ret1",
             "imbalance", "flow", "flow_x_imbalance", "spread_bps")
    momentum = ("ret_1", "ret_2", "ret_4", "ret_8", "uptick", "ema_spread",
                "ret1_over_vol", "micro_x_ret1", "micro_x_uptick")
    groups = {
        "full": None,
        "no interactions": FeatureConfig(interactions=False),
        "no microstructure": FeatureConfig(exclude=micro),
        "no momentum": FeatureConfig(exclude=momentum),
    }
    for tag, cfg in groups.items():
        r = await evaluate(DB, [primary], "hoeffding", 10.0, feature_config=cfg,
                           non_overlapping=True)
        s = r.symbols[primary]
        print(line(tag, s.overall(), s.simulate_trading(r.horizon_ns, fee_bps=FEE)), flush=True)

    print()
    print("=" * 70)
    print(f"3) META-LABELING vs PRIMARY ({primary}, 10s, non-overlapping)")
    print("=" * 70)
    for kind in ("hoeffding", "meta"):
        r = await evaluate(DB, [primary], kind, 10.0, non_overlapping=True)
        s = r.symbols[primary]
        print(line(kind, s.overall(), s.simulate_trading(r.horizon_ns, fee_bps=FEE)), flush=True)

    if followers:
        print()
        print("=" * 70)
        print(f"4) CROSS-ASSET LEAD-LAG: followers +/- {primary} leader (10s, non-overlapping)")
        print("=" * 70)
        for follower in followers:
            for tag, leaders in (
                (f"{follower} alone", None),
                (f"{follower} + leader", {follower: primary}),
            ):
                r = await evaluate(DB, [primary, follower], "hoeffding", 10.0,
                                   non_overlapping=True, leaders=leaders)
                s = r.symbols[follower]
                sim = s.simulate_trading(r.horizon_ns, fee_bps=FEE)
                print(line(tag, s.overall(), sim), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

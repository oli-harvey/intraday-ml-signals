"""Research batch 2026-07-05: horizon sweep, ablation, meta-labeling, lead-lag.

All on the 22.3h soak DB, non-overlapping scoring, results printed as tables.
One-off research driver — findings land in docs/RESEARCH.md.
"""

from __future__ import annotations

import asyncio

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig

DB = "data/paper_2026-07-04.duckdb"
FEE = 0.2  # equity-ish; the optimistic case


def line(tag: str, seg, sim) -> str:  # type: ignore[no-untyped-def]
    return (
        f"{tag:<34} n={seg.n:>5d} dir={seg.dir_acc:.3f} pers={seg.dir_persistence:.3f}"
        f" fade={seg.dir_fade:.3f} d-best={seg.dir_acc - seg.dir_best_baseline:+.3f}"
        f" edge={seg.edge_pct:+6.1f}% trades={sim.trades:>4d} net={sim.net_bps_sum:+8.1f}bps"
    )


async def main() -> None:
    print("=" * 70)
    print("1) HORIZON SWEEP (BTC/USD, non-overlapping)")
    print("=" * 70)
    for kind in ("hoeffding", "classifier"):
        for hz in (5.0, 10.0, 30.0, 60.0, 120.0):
            r = await evaluate(DB, ["BTC/USD"], kind, hz, non_overlapping=True)
            s = r.symbols["BTC/USD"]
            sim = s.simulate_trading(r.horizon_ns, fee_bps=FEE)
            print(line(f"{kind} hz={hz:.0f}s", s.overall(), sim), flush=True)

    print()
    print("=" * 70)
    print("2) FEATURE-GROUP ABLATION (BTC/USD, hoeffding, 10s, non-overlapping)")
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
        r = await evaluate(DB, ["BTC/USD"], "hoeffding", 10.0, feature_config=cfg,
                           non_overlapping=True)
        s = r.symbols["BTC/USD"]
        print(line(tag, s.overall(), s.simulate_trading(r.horizon_ns, fee_bps=FEE)), flush=True)

    print()
    print("=" * 70)
    print("3) META-LABELING vs PRIMARY (BTC/USD, 10s, non-overlapping)")
    print("=" * 70)
    for kind in ("hoeffding", "meta"):
        r = await evaluate(DB, ["BTC/USD"], kind, 10.0, non_overlapping=True)
        s = r.symbols["BTC/USD"]
        print(line(kind, s.overall(), s.simulate_trading(r.horizon_ns, fee_bps=FEE)), flush=True)

    print()
    print("=" * 70)
    print("4) CROSS-ASSET LEAD-LAG: ETH with/without BTC leader (10s, non-overlapping)")
    print("=" * 70)
    for tag, leaders in (("ETH alone", None), ("ETH + BTC leader", {"ETH/USD": "BTC/USD"})):
        r = await evaluate(DB, ["BTC/USD", "ETH/USD"], "hoeffding", 10.0,
                           non_overlapping=True, leaders=leaders)
        s = r.symbols["ETH/USD"]
        print(line(tag, s.overall(), s.simulate_trading(r.horizon_ns, fee_bps=FEE)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

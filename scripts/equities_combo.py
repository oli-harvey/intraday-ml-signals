"""Decisive equities test: combine the two knobs that each helped a name.

no-micro feature ablation (helped AAPL direction) + selectivity dead-zone sweep
(got NVDA to ~breakeven), replicated across BOTH sessions. If any (session,
horizon, symbol, dead-zone) cell goes net_bps > 0 on both days, that's the first
survivable config. If not, liquid-equity intraday reversion is structurally short
of the spread and this book closes too.

Run:  .venv/bin/python scripts/equities_combo.py
"""

from __future__ import annotations

import argparse
import asyncio
import glob

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig

SYMBOLS = ["SPY", "AAPL", "NVDA"]
HORIZONS = [5.0, 10.0]
DEAD_ZONES = [0.5, 2, 4, 8]
MICRO = ["spread_bps", "imbalance", "flow", "micro_bps", "uptick", "dt_s", "micro_over_spread"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+",
                    default=sorted(glob.glob("data/equities_2026-*.duckdb")))
    args = ap.parse_args()
    DBS = args.dbs
    cfg = FeatureConfig(exclude=tuple(MICRO))  # no-micro (the ablation winner)
    for db in DBS:
        print(f"\n########## {db}  (no-micro, ev) ##########")
        for hz in HORIZONS:
            res = await evaluate(db, SYMBOLS, model_kind="ev", horizon_s=hz,
                                 non_overlapping=True, feature_config=cfg)
            hn = int(hz * 1e9)
            print(f"\n-- horizon {hz:g}s -- events={res.events}")
            print(f"{'sym':6s} {'mdir':>6s} {'base':>6s} {'d-best':>7s} | "
                  + "  ".join(f"dz{dz:g}:net/tr" for dz in DEAD_ZONES))
            for sym in SYMBOLS:
                sc = res.symbols[sym]
                seg = sc.overall()
                cells = []
                for dz in DEAD_ZONES:
                    sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=dz)
                    star = "*" if sim.avg_net_bps > 0 else " "
                    cells.append(f"{sim.avg_net_bps:+5.2f}/{sim.trades:<4d}{star}")
                print(f"{sym:6s} {seg.dir_acc:6.3f} {seg.dir_best_baseline:6.3f} "
                      f"{seg.dir_acc - seg.dir_best_baseline:+7.3f} | " + "  ".join(cells))
    print("\n* = net_bps > 0. A config that stars on BOTH sessions at the same "
          "(sym,horizon,dz) is the first survivable equities signal.")


if __name__ == "__main__":
    asyncio.run(main())

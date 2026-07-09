"""Equities eval: does the EV model clear the 1-3bp equities toll?

Crypto cross-venue is closed (move << 20-70bp spread). Equities are zero-
commission with 1-3bp spreads, and the fade rule already got AAPL to -0.9bp/trade
- a hair off the line. The EV model (magnitude-selective, abstains when the
outcome distribution straddles zero) was never run here; its whole purpose is to
trade only when the edge clears costs. This is its natural home.

Per symbol / horizon: model direction vs the strongest naive baseline (fade/
persistence/coin-flip), and the cost-charged trade sim net bps with fee=0 (equities
are commission-free; the toll is the spread, charged once per round trip inside
simulate_trading). Non-overlapping scoring (the honest view).

Run:  .venv/bin/python scripts/equities_eval.py \
        --db data/equities_2026-07-07.duckdb --model ev
"""

from __future__ import annotations

import argparse
import asyncio

from signals.evaluation import evaluate

DEFAULT = ["SPY", "AAPL", "NVDA"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/equities_2026-07-07.duckdb")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT)
    ap.add_argument("--model", default="ev")
    ap.add_argument("--horizons", nargs="+", type=float, default=[2, 5, 10, 30])
    ap.add_argument("--fee-bps", type=float, default=0.0, help="equities: commission-free")
    ap.add_argument("--dead-zone-bps", type=float, default=0.5)
    args = ap.parse_args()

    print(f"\ndb={args.db}  model={args.model}  fee={args.fee_bps}bps  "
          f"dead_zone={args.dead_zone_bps}bps  (non-overlapping)")
    hdr = (f"{'sym':6s} {'H':>4s} {'n':>6s} {'mdir':>6s} {'base':>6s} {'d-best':>7s} "
           f"{'edge%':>6s} | {'trades':>6s} {'net_bps':>8s} {'hit%':>5s}")
    for hz in args.horizons:
        res = asyncio.run(
            evaluate(args.db, args.symbols, model_kind=args.model,
                     horizon_s=hz, non_overlapping=True)
        )
        print(f"\n-- horizon {hz:g}s -- events={res.events} proc_us p50={res.proc_us_p50:.0f}")
        print(hdr)
        print("-" * len(hdr))
        for sym in args.symbols:
            sc = res.symbols[sym]
            seg = sc.overall()
            sim = sc.simulate_trading(
                res.horizon_ns, fee_bps=args.fee_bps, dead_zone_bps=args.dead_zone_bps
            )
            d_best = seg.dir_acc - seg.dir_best_baseline
            print(f"{sym:6s} {hz:4g} {seg.n:6d} {seg.dir_acc:6.3f} "
                  f"{seg.dir_best_baseline:6.3f} {d_best:+7.3f} {seg.edge_pct:6.1f} | "
                  f"{sim.trades:6d} {sim.avg_net_bps:8.2f} {sim.hit_rate*100:4.0f}%")

    print("\nRead: net_bps > 0 on any (sym,H) = the first thing that survives costs. "
          "d-best > 0 with positive net = magnitude selectivity finally paying. "
          "Watch trades count: abstention should cut trades AND lift net_bps.")


if __name__ == "__main__":
    main()

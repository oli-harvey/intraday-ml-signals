"""Spread-conditional entry: can trading only tight-spread moments net the edge?

The single-name direction edge (d-best) is stable across sessions (RESEARCH.md
2026-07-10) but sits under the ~1-3bp spread charged on every trade. This tests
the one untested lever: cap the quoted spread at entry. Since the round-trip toll
IS the spread, a tight cap cuts the toll — and because the cap itself protects
against costs, the dead-zone can be relaxed (trade more). The decisive question is
whether the direction edge SURVIVES the tight-spread subset or lives only in the
wide-spread moments you can't trade.

Green-count per (symbol, dead-zone, spread-cap) across all captured sessions.
A cell green on 4/4 (or 3+/4) with a non-trivial trade count = the first
monetisable equities config. 4 evaluates total (one per session), configs swept
cheaply on the cached rows.

Run:  .venv/bin/python scripts/equities_spread.py
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig, MICRO_FEATURES

SYMBOLS = ["SPY", "AAPL", "NVDA"]
DZS = [2.0, 4.0]
CAPS: list[float | None] = [None, 2.0, 1.5, 1.0]


def cap_label(c: float | None) -> str:
    return "none" if c is None else f"<{c:g}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", default=sorted(glob.glob("data/equities_2026-*.duckdb")))
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--long-only", action="store_true", help="no shorts (margin-acct reality)")
    args = ap.parse_args()
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(args.horizon_s * 1e9)
    days = [db.split("equities_")[-1][5:10] for db in args.dbs]
    allow_short = not args.long_only

    # acc[(sym,dz,cap)] = list of (net_bps, trades) per session
    acc: dict[tuple, list[tuple[float, int]]] = {}
    for db in args.dbs:
        res = await evaluate(db, SYMBOLS, model_kind="ev", horizon_s=args.horizon_s,
                             non_overlapping=True, feature_config=cfg)
        for sym in SYMBOLS:
            sc = res.symbols[sym]
            for dz in DZS:
                for cap in CAPS:
                    sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=dz,
                                              max_spread_bps=cap, allow_short=allow_short)
                    acc.setdefault((sym, dz, cap), []).append((sim.avg_net_bps, sim.trades))

    print(f"\nSPREAD-CONDITIONAL SCREEN — {args.horizon_s:g}s no-micro EV — "
          f"{'LONG-ONLY' if args.long_only else 'long+short'} — "
          f"{len(args.dbs)} sessions ({', '.join(days)})")
    print("cells: green/N sessions, mean net bps, median trades/session\n")
    for sym in SYMBOLS:
        print(f"{sym}")
        header = "dz/cap"
        print(f"  {header:>7s} " + "  ".join(f"{cap_label(c):>16s}" for c in CAPS))
        for dz in DZS:
            cells = []
            for cap in CAPS:
                vals = acc[(sym, dz, cap)]
                nets = [v for v, _ in vals]
                trs = [t for _, t in vals]
                # count green only among sessions that actually traded (n>0)
                traded = [v for v, t in vals if t > 0]
                green = sum(1 for v in traded if v > 0)
                mean_net = stats.mean(traded) if traded else float("nan")
                cells.append(f"{green}/{len(traded)} {mean_net:>+5.1f} t{int(stats.median(trs))}")
            print(f"  {'dz' + str(int(dz)):>7s} " + "  ".join(f"{c:>16s}" for c in cells))
        print()

    # Slippage-haircut robustness: a session is green under an h-bp haircut iff its
    # avg net exceeds h (haircut just shifts per-trade net by -h). Shortlist configs
    # that survive at each haircut across all traded sessions -- the thin-margin test.
    print("SLIPPAGE ROBUSTNESS — configs still green on ALL sessions after an h-bp/trade haircut:")
    for h in (0.0, 0.5, 1.0):
        survivors = []
        for (sym, dz, cap), vals in acc.items():
            traded = [(v, t) for v, t in vals if t > 0]
            if len(traded) == len(vals) and traded and all(v > h for v, _ in traded):
                med_t = int(stats.median([t for _, t in vals]))
                survivors.append(f"{sym} dz{int(dz)} {cap_label(cap)} (t{med_t})")
        tag = "no haircut" if h == 0 else f"-{h:g}bps"
        print(f"  {tag:>10s}: " + ("  ·  ".join(survivors) if survivors else "(none)"))

    print("\nRead: 4/4 survivors under a ~1bp haircut with real trade counts are the "
          "genuinely robust configs; ones that die at 0.5-1bp are too thin to trust "
          "against real fills.")


if __name__ == "__main__":
    asyncio.run(main())

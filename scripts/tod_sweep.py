"""Time-of-day conditioning: is the 5s reversion edge concentrated near the open?

The flat phase-mean net (~+2bp, net/±ph < 1) is a whole-session average. The
microstructure literature says short-horizon reversion is strongest right after
the open (wide spreads, inventory imbalances, price discovery) and fades into the
quiet midday. If that holds here, a session average would HIDE a real open-only
edge by diluting it with dead midday hours — and an open-only rule might clear the
bar the whole-session config never does.

Method — identical machinery to horizon_sweep, just partitioned by ET clock:
  - true no-micro ablation (MICRO_FEATURES), deterministic replay
  - net is PHASE-SWEPT within each bucket (mean + spread over sampling offsets)
  - d-best on non-overlapping rows with the baseline recomputed in-bucket

COVERAGE CAVEAT (do not ignore): only DST-fixed sessions captured the 09:30 open.
07-06..07-09 started at 10:30 ET, so their 'open' bucket is EMPTY. Each bucket
reports how many sessions actually contributed (n=) — an 'open' number resting on
n=2 is a hypothesis, not a result. This is the exact small-sample trap this project
keeps falling into, surfaced rather than hidden.

Run:  .venv/bin/python scripts/tod_sweep.py --dbs data/equities_2026-07-*.duckdb
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import glob
import statistics as stats
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

from signals.evaluation import SymbolScore, evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig

ET = ZoneInfo("America/New_York")

# (name, start, end) in ET local time — the standard intraday partition.
BUCKETS = [
    ("open", dt.time(9, 30), dt.time(10, 30)),
    ("morn", dt.time(10, 30), dt.time(12, 0)),
    ("mid", dt.time(12, 0), dt.time(14, 0)),
    ("aft", dt.time(14, 0), dt.time(15, 30)),
    ("close", dt.time(15, 30), dt.time(16, 0)),
]


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


def bucket_ns(day: str, start: dt.time, end: dt.time) -> tuple[int, int]:
    """ET wall-clock window -> [lo_ns, hi_ns) in epoch ns for this session's date.
    Computed once per (session, bucket) so we never do a per-row tz conversion."""
    d = dt.date.fromisoformat(day)
    lo = dt.datetime.combine(d, start, ET).timestamp()
    hi = dt.datetime.combine(d, end, ET).timestamp()
    return int(lo * 1e9), int(hi * 1e9)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", default=sorted(
        glob.glob("data/equities_2026-07-0[6789].duckdb")
        + glob.glob("data/equities_2026-07-1[04].duckdb")))
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL"])
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    args = ap.parse_args()
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(args.horizon_s * 1e9)
    phases = [int(i * hn / args.phases) for i in range(args.phases)]

    # results[(sym, bucket)] = list over sessions of (phase_mean_net, phase_spread, dbest, trades)
    results: dict[tuple, list] = {}

    for db in args.dbs:
        day = Path(db).stem.replace("equities_", "")
        res = await evaluate(db, args.symbols, model_kind="ev", horizon_s=args.horizon_s,
                             non_overlapping=False, feature_config=cfg)
        for sym in args.symbols:
            rows = res.symbols[sym].rows
            for name, start, end in BUCKETS:
                lo, hi = bucket_ns(day, start, end)
                brows = [r for r in rows if lo <= r.ts_ns < hi]
                if len(brows) < 200:  # bucket absent (pre-open capture) or too thin
                    continue
                seg = SymbolScore(sym, non_overlapping(brows, hn)).overall()
                nets, trades = [], []
                for ph in phases:
                    s = SymbolScore(sym, on_grid(brows, hn, ph)).simulate_trading(
                        hn, fee_bps=0.0, dead_zone_bps=args.dead_zone_bps,
                        max_spread_bps=args.max_spread_bps)
                    if s.trades:
                        nets.append(s.avg_net_bps)
                        trades.append(s.trades)
                if not nets:
                    continue
                results.setdefault((sym, name), []).append((
                    stats.mean(nets), max(nets) - min(nets),
                    seg.dir_acc - seg.dir_best_baseline, int(stats.mean(trades)),
                ))

    print(f"\nTIME-OF-DAY SWEEP — {args.horizon_s:g}s, phase-swept ({args.phases} phases), "
          f"dz{args.dead_zone_bps:g} spread<{args.max_spread_bps:g}bp, "
          f"{len(args.dbs)} sessions (open only in DST-fixed ones)\n")
    hdr = (f"{'sym':5s}{'bucket':>7s}{'net(mean)':>11s}{'±phase':>8s}{'net/±ph':>9s}"
           f"{'d-best':>8s}{'trades':>8s}{'sess>0':>9s}")
    print(hdr)
    print("-" * len(hdr))
    for sym in args.symbols:
        for name, _, _ in BUCKETS:
            vals = results.get((sym, name))
            if not vals:
                continue
            nets = [v[0] for v in vals]
            spreads = [v[1] for v in vals]
            dbests = [v[2] for v in vals]
            trades = [v[3] for v in vals]
            m, sp = stats.mean(nets), stats.mean(spreads)
            ratio = m / sp if sp else float("nan")
            pos = sum(1 for n in nets if n > 0)
            flag = "  <== net/±ph>1" if ratio > 1 and pos == len(nets) and m > 1.0 else ""
            print(f"{sym:5s}{name:>7s}{m:>11.2f}{sp:>8.2f}{ratio:>9.2f}"
                  f"{stats.mean(dbests):>8.3f}{int(stats.mean(trades)):>8d}"
                  f"{pos:>6d}/{len(nets)}{flag}")
        print()

    print("Read: a bucket with net/±ph > 1, positive on every session that has it, and")
    print("~+1bp+ after slippage would be the first deployable slice. If 'open' beats the")
    print("whole-session average, the flat headline was hiding an open-only edge. But an")
    print("'open' number on n=2 sessions is a lead to confirm, not a result to ship.")


if __name__ == "__main__":
    asyncio.run(main())

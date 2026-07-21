"""Model comparison sweep: is there a better online learner than the tracked
'ev' config? Same discipline as every other claim in this project — phase-
swept (a lucky grid retracted one headline already, RESEARCH 2026-07-14),
non-overlapping direction scoring, deterministic replay, no-micro ablation.

Candidates (all in signals/model/online.py):
  ev       — TRACKED. Three online quantile LINEAR regressions (q25/q50/q75),
             pessimistic-quantile decision (trade only if the whole interval
             clears zero).
  ev_tree  — 2026-07-21 candidate. Same decision rule, but each quantile head
             is a Hoeffding tree with a quantile-loss linear model AT THE LEAF
             (leaf_prediction="model") — captures nonlinear feature
             interactions a single global linear fit cannot.
  hoeffding, adaptive, forest, meta, linear — the other kinds already in the
             codebase, tested here on equities for the first time under the
             corrected (07-13+) methodology.

Two-phase protocol (the project's own lesson: a 2-session read on AAPL looked
like the best config in the project and didn't survive 4 more sessions):
  --lean    fewer phases / fewer sessions — a fast first screen, PRELIMINARY
            by construction. Never cite a --lean result as a finding.
  (default) full phase count over all --dbs given — the only mode whose
            output belongs in RESEARCH.md.

Run:  .venv/bin/python scripts/model_sweep.py --lean       # fast screen
      .venv/bin/python scripts/model_sweep.py               # full confirm
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats
from dataclasses import replace

from signals.evaluation import SymbolScore, evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig

MODELS = ["ev", "ev_tree", "hoeffding", "adaptive", "forest", "meta", "linear"]


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbs", nargs="+", default=sorted(
        glob.glob("data/equities_2026-07-0[6789].duckdb")
        + glob.glob("data/equities_2026-07-1[04].duckdb")))
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL"])
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    ap.add_argument("--lean", action="store_true",
                    help="fast preliminary screen: 3 phases, first 2 dbs only")
    args = ap.parse_args()
    dbs = args.dbs[:2] if args.lean else args.dbs
    phases_n = 3 if args.lean else args.phases
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(args.horizon_s * 1e9)
    offsets = [int(i * hn / phases_n) for i in range(phases_n)]

    # results[(sym, model)] = list over sessions of (phase_mean_net, phase_spread, dbest, trades)
    results: dict[tuple, list] = {}
    for model_kind in args.models:
        for db in dbs:
            res = await evaluate(db, args.symbols, model_kind=model_kind, horizon_s=args.horizon_s,
                                 non_overlapping=False, feature_config=cfg)
            for sym in args.symbols:
                rows = res.symbols[sym].rows
                seg = SymbolScore(sym, non_overlapping(rows, hn)).overall()
                nets, trades = [], []
                for ph in offsets:
                    s = SymbolScore(sym, on_grid(rows, hn, ph)).simulate_trading(
                        hn, fee_bps=0.0, dead_zone_bps=args.dead_zone_bps,
                        max_spread_bps=args.max_spread_bps)
                    if s.trades:
                        nets.append(s.avg_net_bps)
                        trades.append(s.trades)
                if not nets:
                    continue
                results.setdefault((sym, model_kind), []).append((
                    stats.mean(nets), max(nets) - min(nets),
                    seg.dir_acc - seg.dir_best_baseline, int(stats.mean(trades)),
                ))

    tag = "LEAN PRELIMINARY SCREEN — do not cite as a finding" if args.lean else "FULL CONFIRM"
    print(f"\nMODEL SWEEP ({tag}) — {args.horizon_s:g}s, phase-swept ({phases_n} phases), "
          f"dz{args.dead_zone_bps:g} spread<{args.max_spread_bps:g}bp, {len(dbs)} sessions\n")
    hdr = (f"{'sym':5s}{'model':>11s}{'net(mean)':>11s}{'±phase':>8s}{'net/±ph':>9s}"
           f"{'d-best':>8s}{'trades':>8s}{'sessions>0':>12s}")
    print(hdr)
    print("-" * len(hdr))
    for sym in args.symbols:
        for model_kind in args.models:
            vals = results.get((sym, model_kind))
            if not vals:
                print(f"{sym:5s}{model_kind:>11s}   (no trades)")
                continue
            nets = [v[0] for v in vals]
            spreads = [v[1] for v in vals]
            dbests = [v[2] for v in vals]
            trades = [v[3] for v in vals]
            m, sp = stats.mean(nets), stats.mean(spreads)
            ratio = m / sp if sp else float("nan")
            pos = sum(1 for n in nets if n > 0)
            flag = "  <== beats net/±ph>1" if ratio > 1 and pos == len(nets) and m > 1.0 else ""
            print(f"{sym:5s}{model_kind:>11s}{m:>11.2f}{sp:>8.2f}{ratio:>9.2f}"
                  f"{stats.mean(dbests):>8.3f}{int(stats.mean(trades)):>8d}"
                  f"{pos:>7d}/{len(nets)}{flag}")
        print()

    print("Read: net/±ph > 1 means the edge is bigger than its phase fragility. Tracked 'ev'")
    print("currently tops out at NVDA 0.65 / AAPL 0.80 (RESEARCH.md 07-16) on 6 sessions.")
    print("A candidate only matters if it clears net/±ph>1 AND beats 'ev' by a real margin,")
    print("replicated on the FULL (non --lean) run.")


if __name__ == "__main__":
    asyncio.run(main())

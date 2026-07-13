"""sign(gap) baseline: is the ML adding anything over the raw cross-venue gap?

The cross-venue result (dir up to 0.85-0.93 on alts) is suspected to be nothing
but stale-quote convergence: when the thin Alpaca alt venue lags Coinbase, the
gap `leader_gap_bps = CB_mid - Alpaca_mid` points at where Alpaca is about to
move, and the model just learns to output sign(gap). This script tests that
directly by scoring the RAW gap sign against the same resolved outcomes the
model is graded on, and — the decisive cut — measuring the model's direction
accuracy on rows where it DISAGREES with sign(gap). If the model only looks
good by copying the gap, its disagreement rows collapse to a coin flip (or
worse), and the ML is a dressed-up gap indicator with no independent skill.

Run:  .venv/bin/python scripts/gap_baseline.py \
        --db data/paper_live.duckdb --model ev --horizon-s 5
"""

from __future__ import annotations

import argparse
import asyncio
import math

from signals.evaluation import evaluate

DEFAULT_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD", "LINK/USD"]


def _acc(rows, side_of) -> tuple[float, int]:
    """Directional accuracy of a per-row side function vs realized sign.
    Counts only rows where both the side and the realized move are nonzero."""
    hits = n = 0
    for r in rows:
        s = side_of(r)
        if s == 0.0 or r.realized == 0.0:
            continue
        n += 1
        hits += 1 if (s > 0) == (r.realized > 0) else 0
    return (hits / n if n else math.nan, n)


def _net_bps(rows, side_of, horizon_ns, fee_bps=5.0) -> tuple[float, int]:
    """sign-rule as a strategy: full spread + 2 fees per round trip, one
    position at a time held for the horizon. Same cost model as SymbolScore."""
    total = trades = 0.0
    busy_until = -(10**18)
    for r in rows:
        if r.ts_ns < busy_until:
            continue
        d = side_of(r)
        if d == 0.0:
            continue
        total += (1.0 if d > 0 else -1.0) * r.realized * 1e4 - (r.spread_bps + 2 * fee_bps)
        trades += 1
        busy_until = r.ts_ns + horizon_ns
    return (total / trades if trades else math.nan, int(trades))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/paper_live.duckdb")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_PAIRS)
    ap.add_argument("--model", default="ev")
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--overlapping", action="store_true",
                    help="score all predictions (default: non-overlapping only)")
    args = ap.parse_args()

    leaders = {s: f"CB:{s}" for s in args.symbols}
    res = asyncio.run(
        evaluate(
            args.db, args.symbols, model_kind=args.model, horizon_s=args.horizon_s,
            non_overlapping=not args.overlapping, leaders=leaders,
        )
    )
    hn = res.horizon_ns

    def _sign(x: float) -> float:
        return 1.0 if x > 0 else -1.0 if x < 0 else 0.0

    def pred_side(r) -> float:
        return _sign(r.prediction)

    def gap_side(r) -> float:
        return _sign(r.gap_bps)

    # persistence = "last independent window's move repeats"; fade = its mirror.
    # If either matches model_dir on the alts, the direction is autocorrelation,
    # not the gap and not model skill.
    def pers_side(r) -> float:
        return _sign(r.persistence)

    def fade_side(r) -> float:
        return -pers_side(r)

    print(f"\ndb={args.db}  model={args.model}  horizon={args.horizon_s}s  "
          f"{'overlapping' if args.overlapping else 'NON-overlapping'}")
    print(f"events={res.events}  proc_us p50={res.proc_us_p50:.0f}\n")
    hdr = (f"{'sym':9s} {'n':>5s} {'model_dir':>9s} {'gap_dir':>8s} {'fade_dir':>8s} "
           f"{'agree%':>7s} {'dir|agree':>9s} {'dir|disagr':>10s} {'n_disagr':>8s} "
           f"{'model_bps':>9s} {'gap_bps':>8s}")
    print(hdr)
    print("-" * len(hdr))
    for sym in args.symbols:
        rows = [r for r in res.symbols[sym].rows if r.gap_bps != 0.0]
        if not rows:
            print(f"{sym:9s}  (no nonzero-gap rows)")
            continue
        model_dir, _ = _acc(rows, pred_side)
        gap_dir, _ = _acc(rows, gap_side)
        fade_dir, _ = _acc(rows, fade_side)
        # committed rows only (model actually took a side) for agreement/conditional
        committed = [r for r in rows if pred_side(r) != 0.0 and r.realized != 0.0]
        agree = [r for r in committed if pred_side(r) == gap_side(r)]
        disagree = [r for r in committed if pred_side(r) != gap_side(r) and gap_side(r) != 0.0]
        agree_pct = len(agree) / len(committed) if committed else math.nan
        dir_agree, _ = _acc(agree, pred_side)
        dir_disagree, n_dis = _acc(disagree, pred_side)
        model_bps, _ = _net_bps(rows, pred_side, hn)
        gap_bps, _ = _net_bps(rows, gap_side, hn)
        print(f"{sym:9s} {len(rows):5d} {model_dir:9.3f} {gap_dir:8.3f} {fade_dir:8.3f} "
              f"{agree_pct*100:6.1f}% {dir_agree:9.3f} {dir_disagree:10.3f} "
              f"{n_dis:8d} {model_bps:9.2f} {gap_bps:8.2f}")

    print("\nRead: if gap_dir ~= model_dir AND dir|disagree <= 0.5, the ML adds "
          "nothing over sign(gap) — it's a gap indicator. Both bps<0 = the gap "
          "is a data artifact you can't monetise after the toll.")


if __name__ == "__main__":
    main()

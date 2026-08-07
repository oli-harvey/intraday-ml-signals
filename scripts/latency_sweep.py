"""Does the backtest edge survive REAL fill latency? The one experiment that can
explain the project's central contradiction.

The contradiction (2026-08-07): the phase-swept backtest tally says NVDA is
+2.6bps/session, net/σ′ ≈ 1.1, positive on 22 of 23 sessions and holding as n
grows. The one live real-money test (2026-07-21, 356 fills) lost money at 47.5%
gross direction. Both were measured carefully. They cannot both describe the
same strategy.

The prime suspect is measured, not hypothesised: entry orders on the paper
account take **1-2.4 seconds to confirm** (Alpaca submitted_at/filled_at,
RESEARCH 2026-07-21) — 20-48% of a 5s horizon. The backtest enters at the mid
AT THE SIGNAL; the live trader enters at whatever the price is 1-2.4s later,
by which time a mean-reverting move has partly reverted. That is a real cost
the sim never charged.

Method (one replay per session, every latency applied to the same rows, so the
comparison is exact and cheap):
  - signal at t, direction from the SAME simrule.decide as everywhere else
  - entry price = mid at the first quote at or after t + L   (the fill)
  - exit price  = mid at t + horizon                          (unchanged: the
    live trader shortens the hold so the exit still lands at signal+horizon,
    RESEARCH 2026-07-21 fix 3)
  - charged the same quoted spread, so the ONLY thing varying is the drift
    between signal and fill. L=0 reproduces the standard backtest exactly.
Phase-swept (8 offsets) and non-overlapping-scored like every other claim here.

Run: .venv/bin/python scripts/latency_sweep.py --dbs data/equities_2026-0*.duckdb
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import statistics as stats

import duckdb
import numpy as np

from signals import simrule
from signals.evaluation import evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig

LATENCIES = [0.0, 0.5, 1.0, 1.5, 2.0, 2.4]


def mid_series(db: str, sym: str) -> tuple[np.ndarray, np.ndarray]:
    """(ts_ns, mid) for a symbol, ascending — the price path the fill lands on."""
    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            "SELECT ts_ns, (bid + ask) / 2.0 FROM quotes "
            "WHERE symbol = ? AND bid > 0 AND ask > bid ORDER BY ts_ns",
            [sym],
        ).fetchall()
    finally:
        con.close()
    ts = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    mid = np.fromiter((r[1] for r in rows), dtype=np.float64, count=len(rows))
    return ts, mid


def on_grid(rows, horizon_ns: int, phase_ns: int):
    kept, last_bucket = [], None
    for r in rows:
        b = (r.ts_ns + phase_ns) // horizon_ns
        if b != last_bucket:
            kept.append(r)
            last_bucket = b
    return kept


def sim_with_latency(rows, ts: np.ndarray, mid: np.ndarray, horizon_ns: int,
                     latency_s: float, dead_zone_bps: float,
                     max_spread_bps: float) -> tuple[int, float]:
    """Trades and mean net bps when the fill lands `latency_s` after the signal."""
    lat_ns = int(latency_s * 1e9)
    busy_until = -(10**18)
    trades, net_sum = 0, 0.0
    for r in rows:
        if r.ts_ns < busy_until:
            continue
        direction = simrule.decide(
            r.prediction, r.spread_bps, fee_bps=0.0,
            dead_zone_bps=dead_zone_bps, max_spread_bps=max_spread_bps,
        )
        if direction == 0.0:
            continue
        # price at signal, and the price we ACTUALLY get `latency_s` later
        i_sig = int(np.searchsorted(ts, r.ts_ns, side="left"))
        i_fill = int(np.searchsorted(ts, r.ts_ns + lat_ns, side="left"))
        if i_sig >= len(mid) or i_fill >= len(mid):
            continue
        m_sig, m_fill = mid[i_sig], mid[i_fill]
        if m_sig <= 0 or m_fill <= 0:
            continue
        # exit price is fixed by the horizon: mid(t+h) = mid(t) * (1 + realized)
        m_exit = m_sig * (1.0 + r.realized)
        realized_from_fill = m_exit / m_fill - 1.0
        net_sum += simrule.net_bps(direction, realized_from_fill, r.spread_bps, 0.0)
        trades += 1
        busy_until = r.ts_ns + horizon_ns
    return trades, (net_sum / trades if trades else float("nan"))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbs", nargs="+", default=sorted(
        glob.glob("data/equities_2026-0[78]-*.duckdb")))
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL"])
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--phases", type=int, default=8)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    args = ap.parse_args()

    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(args.horizon_s * 1e9)
    offsets = [int(i * hn / args.phases) for i in range(args.phases)]
    # results[(sym, latency)] = list over sessions of phase-mean net
    results: dict[tuple, list[float]] = {}
    trade_counts: dict[tuple, list[int]] = {}

    for db in args.dbs:
        res = await evaluate(db, args.symbols, model_kind="ev",
                             horizon_s=args.horizon_s, non_overlapping=False,
                             feature_config=cfg)
        for sym in args.symbols:
            rows = res.symbols[sym].rows
            if not rows:
                continue
            ts, mid = mid_series(db, sym)
            if len(ts) == 0:
                continue
            for lat in LATENCIES:
                nets, trs = [], []
                for ph in offsets:
                    t, n = sim_with_latency(on_grid(rows, hn, ph), ts, mid, hn, lat,
                                            args.dead_zone_bps, args.max_spread_bps)
                    if t:
                        nets.append(n)
                        trs.append(t)
                if nets:
                    results.setdefault((sym, lat), []).append(stats.mean(nets))
                    trade_counts.setdefault((sym, lat), []).append(int(stats.mean(trs)))
        print(f"[done] {db}", flush=True)

    print(f"\nLATENCY SWEEP — {args.horizon_s:g}s horizon, phase-swept "
          f"({args.phases} phases), dz{args.dead_zone_bps:g} "
          f"spread<{args.max_spread_bps:g}bp, {len(args.dbs)} sessions")
    print("entry = mid at first quote >= signal + latency; exit = mid at signal + horizon\n")
    hdr = (f"{'sym':6s}{'latency':>9s}{'net(mean)':>11s}{'±sd':>7s}"
           f"{'net/sd':>8s}{'trades':>8s}{'sessions>0':>12s}")
    print(hdr)
    print("-" * len(hdr))
    for sym in args.symbols:
        base = None
        for lat in LATENCIES:
            vals = results.get((sym, lat))
            if not vals:
                continue
            m = stats.mean(vals)
            sd = stats.stdev(vals) if len(vals) > 1 else float("nan")
            ratio = m / sd if sd and sd == sd else float("nan")
            pos = sum(1 for v in vals if v > 0)
            tr = int(stats.mean(trade_counts[(sym, lat)]))
            if base is None:
                base = m
            drop = "" if lat == 0 else f"  ({(m - base):+.2f} vs L=0)"
            print(f"{sym:6s}{lat:>8.1f}s{m:>11.2f}{sd:>7.2f}{ratio:>8.2f}"
                  f"{tr:>8d}{pos:>7d}/{len(vals)}{drop}")
        print()
    print("Measured live entry latency on the paper account: 1.0-2.4s (RESEARCH 07-21).")
    print("If net at L=1-2.4s is <= 0, fill latency ALONE explains the live loss and the")
    print("backtest edge is unreachable through this broker at a 5s horizon.")


if __name__ == "__main__":
    asyncio.run(main())

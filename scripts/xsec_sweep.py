"""Cross-sectional residual reversion — the one lever never tested.

Every equities test so far has been UNIVARIATE: predict/fade a symbol's own 5s move.
But a 5s single-name move is (market move) + (idiosyncratic move), and the univariate
fade fights both. We capture 30 symbols simultaneously, so we can hedge the market
leg out: fade the RESIDUAL r_sym - r_hedge (hedge = QQQ/SPY, beta=1 so there is
nothing to estimate and nothing to look ahead with). If single-name reversion is
mostly idiosyncratic, the residual should revert more cleanly than the raw move.

The toll doubles, though: a dollar-neutral pair pays BOTH legs' spreads round trip.
The hedge legs are the cheapest instruments on the tape (~0.4-0.7bp), so the test is
whether the cleaner signal buys more than ~+0.5bp of extra toll.

Honest by construction, same discipline as everything else:
  - rule-based (fade), zero fitted parameters -> nothing to overfit
  - phase-swept: every number is a mean over absolute-clock grid offsets, with the
    spread across phases (fragility) reported next to it; net/±ph > 1 or it isn't real
  - one position per bucket, mid-to-mid PnL, both spreads charged at entry
  - per-session results; a config counts only if it is positive on EVERY session

Run:  .venv/bin/python scripts/xsec_sweep.py --dbs data/equities_2026-07-*.duckdb
"""

from __future__ import annotations

import argparse
import glob
import statistics as stats

import duckdb
import numpy as np

HORIZON_NS = 5_000_000_000

SQL = """
SELECT (ts_ns + ?) // ? AS b,
       arg_max((bid + ask) / 2, ts_ns) AS mid,
       arg_max((ask - bid) / ((bid + ask) / 2), ts_ns) * 1e4 AS spread_bps
FROM quotes
WHERE symbol = ? AND bid > 0 AND ask >= bid
GROUP BY b ORDER BY b
"""


def bucket_series(conn, symbol: str, phase_ns: int) -> dict[int, tuple[float, float]]:
    """bucket -> (last mid, last spread_bps) on the phase-shifted 5s grid."""
    rows = conn.execute(SQL, [phase_ns, HORIZON_NS, symbol]).fetchall()
    return {int(b): (mid, sp) for b, mid, sp in rows}


def fade_session(
    sym_s: dict, hedge_s: dict, dead_zone_bps: float, cap_bps: float,
) -> tuple[float, float, int, float] | None:
    """Fade the previous bucket's residual over the next bucket.

    Signal at bucket b needs (b-1, b) on both legs; outcome needs b+1. Entry only
    when |residual| >= dead zone and sym_spread + hedge_spread <= cap. Returns
    (net_bps/trade, gross_bps/trade, trades, dir_acc) or None if no trades.
    """
    common = sorted(set(sym_s) & set(hedge_s))
    nets, grosses, hits = [], [], []
    for i in range(1, len(common) - 1):
        b0, b1, b2 = common[i - 1], common[i], common[i + 1]
        if b1 != b0 + 1 or b2 != b1 + 1:
            continue  # gap in either leg's quotes — no clean signal/outcome window
        sm0, _ = sym_s[b0]
        sm1, ssp = sym_s[b1]
        sm2, _ = sym_s[b2]
        hm0, _ = hedge_s[b0]
        hm1, hsp = hedge_s[b1]
        hm2, _ = hedge_s[b2]
        residual = (sm1 / sm0 - 1) - (hm1 / hm0 - 1)
        toll = ssp + hsp
        if abs(residual) * 1e4 < dead_zone_bps or toll > cap_bps:
            continue
        direction = -1.0 if residual > 0 else 1.0
        outcome = (sm2 / sm1 - 1) - (hm2 / hm1 - 1)
        gross = direction * outcome * 1e4
        nets.append(gross - toll)
        grosses.append(gross)
        hits.append(1.0 if gross > 0 else 0.0)
    if not nets:
        return None
    return (
        float(np.mean(nets)), float(np.mean(grosses)), len(nets), float(np.mean(hits)),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbs", nargs="+", default=sorted(
        glob.glob("data/equities_2026-07-0[6789].duckdb")
        + glob.glob("data/equities_2026-07-1[04].duckdb")))
    ap.add_argument("--symbols", nargs="+",
                    default=["NVDA", "AAPL", "MSFT", "AMD", "TSLA"])
    ap.add_argument("--hedges", nargs="+", default=["QQQ", "SPY"])
    ap.add_argument("--dead-zones", nargs="+", type=float, default=[2.0, 4.0, 8.0])
    ap.add_argument("--cap-bps", type=float, default=3.0,
                    help="max combined (sym+hedge) spread at entry")
    ap.add_argument("--phases", type=int, default=8)
    args = ap.parse_args()
    phases = [int(i * HORIZON_NS / args.phases) for i in range(args.phases)]

    # results[(sym, hedge, dz)] = per-session list of
    #   (phase-mean net, phase spread, phase-mean gross, mean trades, mean dir)
    results: dict[tuple, list] = {}
    for db in args.dbs:
        conn = duckdb.connect(db, read_only=True)
        for ph in phases:
            series = {s: bucket_series(conn, s, ph)
                      for s in {*args.symbols, *args.hedges}}
            for sym in args.symbols:
                for hedge in args.hedges:
                    for dz in args.dead_zones:
                        r = fade_session(series[sym], series[hedge], dz, args.cap_bps)
                        if r is None:
                            continue
                        results.setdefault((sym, hedge, dz), {}).setdefault(
                            db, []).append(r)
        conn.close()

    print(f"\nCROSS-SECTIONAL RESIDUAL FADE — 5s, phase-swept ({args.phases} phases), "
          f"combined spread<{args.cap_bps:g}bp, {len(args.dbs)} sessions\n")
    hdr = (f"{'sym':5s}{'hedge':>6s}{'dz':>4s}{'gross':>8s}{'net':>8s}{'±phase':>8s}"
           f"{'net/±ph':>9s}{'dir':>6s}{'tr/day':>8s}{'sess>0':>8s}")
    print(hdr)
    print("-" * len(hdr))
    for (sym, hedge, dz), by_db in sorted(results.items()):
        sess_net, sess_spread, sess_gross, sess_tr, sess_dir = [], [], [], [], []
        for _db, phase_runs in by_db.items():
            nets = [p[0] for p in phase_runs]
            sess_net.append(stats.mean(nets))
            sess_spread.append(max(nets) - min(nets) if len(nets) > 1 else 0.0)
            sess_gross.append(stats.mean(p[1] for p in phase_runs))
            sess_tr.append(stats.mean(p[2] for p in phase_runs))
            sess_dir.append(stats.mean(p[3] for p in phase_runs))
        m, sp = stats.mean(sess_net), stats.mean(sess_spread)
        ratio = m / sp if sp else float("nan")
        pos = sum(1 for n in sess_net if n > 0)
        flag = "  <== clears" if ratio > 1 and pos == len(sess_net) and m > 1.0 else ""
        print(f"{sym:5s}{hedge:>6s}{dz:>4g}{stats.mean(sess_gross):>8.2f}{m:>8.2f}"
              f"{sp:>8.2f}{ratio:>9.2f}{stats.mean(sess_dir):>6.2f}"
              f"{int(stats.mean(sess_tr)):>8d}{pos:>5d}/{len(sess_net)}{flag}")

    print("\nRead: gross = before costs (does the residual even revert?); net charges")
    print("BOTH legs' spreads. dir > 0.5 = residual reversion is real directionally.")
    print("The bar is unchanged: net/±ph > 1, positive every session, > +1bp mean.")


if __name__ == "__main__":
    main()

"""Cross-check: does the LIVE shadow book reproduce the OFFLINE backtest, on a real
session, end to end?

tests/test_livesim.py already pins live-book == backtest on a slice. This is the
same assertion at full scale on a real captured session — the out-of-sample /
train-serve-skew check. Two outcomes, both useful:

  - they MATCH  -> the nightly digest and the live Telegram numbers mean the same
    thing; the negative result is trustworthy because both paths agree.
  - they DIFFER -> the live book and the backtest disagree on real data, which is a
    BUG (a finding), not a discrepancy to rationalise.

One replay pass feeds BOTH consumers off the SAME SymbolPipeline, so predictions
are identical by construction and any difference is purely in the trade-accounting
(livesim vs evaluation.simulate_trading), which is exactly what we want to test.

  live windowed   == backtest non-overlapping sim   (one look per 5s window)
  live per-quote  == backtest all-rows sim           (act on every signal)

Run:  .venv/bin/python scripts/live_vs_backtest.py --db data/equities_2026-07-14.duckdb
"""

from __future__ import annotations

import argparse
import asyncio

from signals.core import SymbolPipeline
from signals.data.replay import ReplaySource
from signals.evaluation import Row, SymbolScore
from signals.features.engine import MICRO_FEATURES, FeatureConfig, FeatureEngine
from signals.livesim import LiveSim
from signals.model.online import OnlineModel


async def run(db: str, symbols: list[str], horizon_s: float,
              dead_zone_bps: float, max_spread_bps: float) -> None:
    hn = int(horizon_s * 1e9)
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    pipes = {s: SymbolPipeline(s, FeatureEngine(cfg), OnlineModel(kind="ev"), hn)
             for s in symbols}

    # LIVE side: exactly the two books stocks_live.py runs.
    live_w = LiveSim(horizon_ns=hn, dead_zone_bps=dead_zone_bps,
                     max_spread_bps=max_spread_bps, windowed=True)
    live_pq = LiveSim(horizon_ns=hn, dead_zone_bps=dead_zone_bps,
                      max_spread_bps=max_spread_bps, windowed=False)

    # BACKTEST side: reconstruct evaluation.evaluate's row streams from the SAME
    # resolved events, so the offline sim sees identical predictions.
    all_rows: dict[str, list[Row]] = {s: [] for s in symbols}
    nov_rows: dict[str, list[Row]] = {s: [] for s in symbols}
    last_real = dict.fromkeys(symbols, 0.0)
    last_scored = dict.fromkeys(symbols, -(10**18))

    source = ReplaySource(db, symbols)
    async for event in source.stream():
        pipe = pipes.get(event.symbol)
        if pipe is None:
            continue
        step = pipe.on_event(event)
        for r in step.resolved:
            sym = event.symbol
            live_w.on_resolved(sym, r.ts_ns, r.prediction, r.realized, r.spread_bps)
            live_pq.on_resolved(sym, r.ts_ns, r.prediction, r.realized, r.spread_bps)
            row = Row(ts_ns=r.ts_ns, prediction=r.prediction, realized=r.realized,
                      persistence=last_real[sym], spread_bps=r.spread_bps)
            all_rows[sym].append(row)
            if r.ts_ns >= last_scored[sym] + hn:  # evaluate's non_overlapping gate
                nov_rows[sym].append(row)
                last_scored[sym] = r.ts_ns
            last_real[sym] = r.realized

    def bt(rows, sym):
        return SymbolScore(sym, rows).simulate_trading(
            hn, fee_bps=0.0, dead_zone_bps=dead_zone_bps, max_spread_bps=max_spread_bps)

    print(f"\nLIVE vs BACKTEST — {db.split('/')[-1]}, {horizon_s:g}s "
          f"dz{dead_zone_bps:g} spread<{max_spread_bps:g}bp\n")
    hdr = (f"{'sym':5s}{'cadence':>9s}{'live_tr':>9s}{'bt_tr':>7s}"
           f"{'live_net':>10s}{'bt_net':>9s}{'Δnet':>8s}  match")
    print(hdr)
    print("-" * len(hdr))
    all_ok = True
    for sym in symbols:
        wb = live_w.book_for(sym)
        pb = live_pq.book_for(sym)
        bt_w = bt(nov_rows[sym], sym)
        bt_pq = bt(all_rows[sym], sym)
        for cad, book, b in (("windowed", wb, bt_w), ("per-quote", pb, bt_pq)):
            live_net = book.avg_net_bps
            d = (live_net - b.avg_net_bps) if (live_net == live_net and
                                               b.avg_net_bps == b.avg_net_bps) else float("nan")
            # match = identical trade count AND net within a rounding epsilon
            ok = (book.trades == b.trades) and (abs(d) < 1e-6 if d == d else True)
            all_ok = all_ok and ok
            mark = "OK" if ok else "**DIFF**"
            print(f"{sym:5s}{cad:>9s}{book.trades:>9d}{b.trades:>7d}"
                  f"{live_net:>10.4f}{b.avg_net_bps:>9.4f}{d:>8.4f}  {mark}")
    print()
    print("VERDICT:", "live book reproduces the backtest exactly — numbers are "
          "trustworthy." if all_ok else "MISMATCH — the live book and backtest "
          "disagree. This is a bug; investigate before trusting either number.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/equities_2026-07-14.duckdb")
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "AAPL", "SPY"])
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--dead-zone-bps", type=float, default=4.0)
    ap.add_argument("--max-spread-bps", type=float, default=2.0)
    args = ap.parse_args()
    asyncio.run(run(args.db, args.symbols, args.horizon_s,
                    args.dead_zone_bps, args.max_spread_bps))


if __name__ == "__main__":
    main()

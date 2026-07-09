"""Maker/limit-order execution sim: can passive fills beat the toll?

The signal is real (sign(gap) baseline, 2026-07-09) but every pair loses 18-96
bps/trade as a TAKER because the round-trip spread dwarfs the move. This asks the
only question left: if we stop crossing the spread and instead post a passive
limit at the touch — filled only when the market comes to us — does any pair turn
positive net?

Honesty crux = adverse selection. A passive BUY at the bid fills only when price
falls to your bid; for a LONG signal you therefore fill precisely on the dips,
some of which keep falling. We do NOT assume a fill: for each committed signal we
walk the ACTUAL future quote path from the DB and fill only if the mid touches the
limit within the wait window. Unfilled orders are cancelled (no trade, slot freed).

Three executions on the SAME model-committed signals (execution is the only diff):
  taker/taker      buy ask, sell bid at horizon        (the current real cost)
  maker-in/taker   post at bid, touch-fill, sell bid    (realistic improvement)
  maker-in/maker   post at bid, touch-fill, post at ask exit (optimistic ceiling)

Note on fees: --maker-fee-bps defaults to 0 (ceiling). Alpaca crypto's real maker
fee is ~15bps, which would negate most of this — so a positive result here is an
argument for a CHEAPER VENUE, not necessarily Alpaca maker orders.

Run:  .venv/bin/python scripts/maker_sim.py --db data/paper_live.duckdb \
        --model ev --horizon-s 5 --wait-s 2
"""

from __future__ import annotations

import argparse
import asyncio
import math

import duckdb
import numpy as np

from signals.evaluation import evaluate

DEFAULT_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD", "LINK/USD"]


class Quotes:
    """Sorted quote path for one symbol; O(log n) touch/point queries (cold path)."""

    def __init__(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        self.ts, self.bid, self.ask = ts, bid, ask
        self.mid = (bid + ask) / 2.0

    def at(self, t_ns: int) -> tuple[float, float] | None:
        """First quote at/after t (the price you'd actually transact against)."""
        i = int(np.searchsorted(self.ts, t_ns, side="left"))
        if i >= len(self.ts):
            return None
        return float(self.bid[i]), float(self.ask[i])

    def touch_long(self, t_ns: int, wait_ns: int, limit: float) -> int | None:
        """First ts in (t, t+wait] where mid <= limit (a passive BUY at `limit`
        fills as price falls to it), else None. Excludes t itself."""
        lo = int(np.searchsorted(self.ts, t_ns, side="right"))
        hi = int(np.searchsorted(self.ts, t_ns + wait_ns, side="right"))
        seg = self.mid[lo:hi]
        hits = np.nonzero(seg <= limit)[0]
        return int(self.ts[lo + hits[0]]) if len(hits) else None

    def touch_short(self, t_ns: int, wait_ns: int, limit: float) -> int | None:
        lo = int(np.searchsorted(self.ts, t_ns, side="right"))
        hi = int(np.searchsorted(self.ts, t_ns + wait_ns, side="right"))
        seg = self.mid[lo:hi]
        hits = np.nonzero(seg >= limit)[0]
        return int(self.ts[lo + hits[0]]) if len(hits) else None


def load_quotes(db: str, symbol: str) -> Quotes:
    con = duckdb.connect(db, read_only=True)
    df = con.execute(
        "SELECT ts_ns, bid, ask FROM quotes WHERE symbol=? AND bid>0 AND ask>bid "
        "ORDER BY ts_ns",
        [symbol],
    ).fetchnumpy()
    con.close()
    return Quotes(df["ts_ns"].astype(np.int64), df["bid"].astype(float), df["ask"].astype(float))


class Book:
    def __init__(self) -> None:
        self.trades = self.wins = self.fills = self.signals = 0
        self.net = 0.0

    def avg(self) -> float:
        return self.net / self.trades if self.trades else math.nan

    def fill_rate(self) -> float:
        return self.fills / self.signals if self.signals else math.nan

    def hit(self) -> float:
        return self.wins / self.trades if self.trades else math.nan

    def add(self, net_bps: float) -> None:
        self.trades += 1
        self.fills += 1
        self.wins += 1 if net_bps > 0 else 0
        self.net += net_bps


def simulate(
    rows, q: Quotes, horizon_ns: int, wait_ns: int, taker_fee: float,
    maker_fee: float, mode: str,
) -> Book:
    """mode: 'taker' | 'maker_taker' | 'maker_maker'. One position at a time."""
    b = Book()
    busy_until = -(10**18)
    for r in rows:
        side = 1 if r.prediction > 0 else -1 if r.prediction < 0 else 0
        if side == 0 or r.ts_ns < busy_until:
            continue
        b.signals += 1
        px = q.at(r.ts_ns)
        if px is None:
            continue
        bid0, ask0 = px

        if mode == "taker":
            # cross now (buy ask / sell bid), exit taker at horizon
            entry = ask0 if side == 1 else bid0
            ex = q.at(r.ts_ns + horizon_ns)
            if ex is None:
                continue
            exit_px = ex[0] if side == 1 else ex[1]  # sell bid / buy ask
            fee = 2 * taker_fee
            busy_until = r.ts_ns + horizon_ns
        else:
            # passive entry at the touch; unfilled -> no trade, slot stays free
            limit = bid0 if side == 1 else ask0
            fill_ts = (
                q.touch_long(r.ts_ns, wait_ns, limit) if side == 1
                else q.touch_short(r.ts_ns, wait_ns, limit)
            )
            if fill_ts is None:
                continue  # order expired unfilled
            entry = limit
            exit_ts = fill_ts + horizon_ns
            ex = q.at(exit_ts)
            if ex is None:
                continue
            if mode == "maker_taker":
                exit_px = ex[0] if side == 1 else ex[1]  # cross out
                fee = maker_fee + taker_fee
            else:  # maker_maker: passive exit, fall back to taker if it never fills
                exit_limit = ex[1] if side == 1 else ex[0]  # post at the far touch
                mk = (
                    q.touch_short(exit_ts, wait_ns, exit_limit) if side == 1
                    else q.touch_long(exit_ts, wait_ns, exit_limit)
                )
                if mk is not None:
                    exit_px, fee = exit_limit, 2 * maker_fee
                else:
                    exit_px = ex[0] if side == 1 else ex[1]
                    fee = maker_fee + taker_fee
            busy_until = exit_ts

        gross = side * (exit_px - entry) / entry * 1e4
        b.add(gross - fee)
    return b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/paper_live.duckdb")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_PAIRS)
    ap.add_argument("--model", default="ev")
    ap.add_argument("--horizon-s", type=float, default=5.0)
    ap.add_argument("--wait-s", type=float, default=2.0, help="limit order rest window")
    ap.add_argument("--taker-fee-bps", type=float, default=5.0)
    ap.add_argument("--maker-fee-bps", type=float, default=0.0)
    args = ap.parse_args()

    leaders = {s: f"CB:{s}" for s in args.symbols}
    res = asyncio.run(
        evaluate(args.db, args.symbols, model_kind=args.model, horizon_s=args.horizon_s,
                 non_overlapping=False, leaders=leaders)
    )
    hn = int(args.horizon_s * 1e9)
    wn = int(args.wait_s * 1e9)

    print(f"\ndb={args.db}  model={args.model}  horizon={args.horizon_s}s  "
          f"wait={args.wait_s}s  taker_fee={args.taker_fee_bps}  maker_fee={args.maker_fee_bps}")
    print("net bps/trade (fills/signals shown as fill%):\n")
    hdr = (f"{'sym':9s} {'sig':>6s} | {'taker':>8s} | {'mk_in/tk':>8s} {'fill%':>6s} "
           f"| {'mk/mk':>8s} {'fill%':>6s}")
    print(hdr); print("-" * len(hdr))
    agg = {}
    for sym in args.symbols:
        rows = res.symbols[sym].rows
        q = load_quotes(args.db, sym)
        tk = simulate(rows, q, hn, wn, args.taker_fee_bps, args.maker_fee_bps, "taker")
        mt = simulate(rows, q, hn, wn, args.taker_fee_bps, args.maker_fee_bps, "maker_taker")
        mm = simulate(rows, q, hn, wn, args.taker_fee_bps, args.maker_fee_bps, "maker_maker")
        agg[sym] = (tk, mt, mm)
        print(f"{sym:9s} {tk.signals:6d} | {tk.avg():8.2f} | {mt.avg():8.2f} "
              f"{mt.fill_rate()*100:5.0f}% | {mm.avg():8.2f} {mm.fill_rate()*100:5.0f}%")

    print("\nRead: does any pair go POSITIVE under maker execution? If mk/mk wins "
          "and fill% is real (not ~0), passive execution captures the signal — and "
          "the lever is a venue whose maker fee is below that edge. If maker stays "
          "negative, the move is genuinely smaller than any executable cost.")


if __name__ == "__main__":
    main()

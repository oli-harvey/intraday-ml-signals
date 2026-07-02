"""Offline session report from a cold-store DB (pandas/matplotlib OK here).

Summarizes a recorded or live-trading session: event counts, walk-forward
prediction quality (MAE vs zero baseline, directional accuracy), decision
latency against the budget, orders and realized PnL. Saves a PNG alongside.

Usage:
    uv run python scripts/report.py --db data/live.duckdb --out reports/
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def load(conn: duckdb.DuckDBPyConnection, table: str) -> pd.DataFrame:
    return conn.execute(f"SELECT * FROM {table}").df()


def prediction_quality(res: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward quality by time quartile per symbol."""
    rows = []
    for symbol, g in res.groupby("symbol"):
        g = g.sort_values("pred_ts_ns").reset_index(drop=True)
        g["quartile"] = pd.qcut(g.index, 4, labels=["Q1", "Q2", "Q3", "Q4"])
        for q, seg in g.groupby("quartile", observed=True):
            nz = seg[(seg.predicted != 0) & (seg.realized != 0)]
            rows.append(
                {
                    "symbol": symbol,
                    "segment": q,
                    "n": len(seg),
                    "mae": (seg.realized - seg.predicted).abs().mean(),
                    "zero_mae": seg.realized.abs().mean(),
                    "dir_acc": (
                        (np.sign(nz.predicted) == np.sign(nz.realized)).mean()
                        if len(nz)
                        else np.nan
                    ),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["edge_pct"] = (1 - df.mae / df.zero_mae) * 100
    return df


def realized_pnl(orders: pd.DataFrame) -> pd.DataFrame:
    """FIFO-pair buys/sells per symbol into round trips."""
    trips = []
    for symbol, g in orders[orders.status == "filled"].groupby("symbol"):
        entry = None
        for _, o in g.sort_values("ts_ns").iterrows():
            if o.action == "buy":
                entry = o
            elif o.action == "sell" and entry is not None:
                qty = min(o.qty, entry.qty)
                trips.append(
                    {
                        "symbol": symbol,
                        "entry_price": entry.fill_price,
                        "exit_price": o.fill_price,
                        "qty": qty,
                        "pnl_usd": (o.fill_price - entry.fill_price) * qty,
                        "ret_bps": (o.fill_price / entry.fill_price - 1) * 1e4,
                    }
                )
                entry = None
    return pd.DataFrame(trips)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default="reports")
    args = parser.parse_args()

    conn = duckdb.connect(args.db, read_only=True)
    tables = {t: load(conn, t) for t in ("quotes", "trades", "bars", "predictions",
                                          "resolutions", "orders")}
    conn.close()

    print(f"=== report: {args.db} ===")
    for name, df in tables.items():
        by_sym = df.groupby("symbol").size().to_dict() if len(df) else {}
        print(f"{name:12s} {len(df):7d}  {by_sym}")

    preds, res, orders = tables["predictions"], tables["resolutions"], tables["orders"]

    if len(preds):
        lat = preds.proc_us
        print(
            f"\ndecision latency (feature+inference per quote):"
            f" p50={lat.quantile(0.5):.0f}us p99={lat.quantile(0.99):.0f}us"
            f" max={lat.max():.0f}us  (budget: <15ms)"
        )

    quality = prediction_quality(res) if len(res) else pd.DataFrame()
    if len(quality):
        print("\nwalk-forward prediction quality (time quartiles):")
        print(quality.to_string(index=False, float_format=lambda v: f"{v:.4g}"))

    trips = realized_pnl(orders) if len(orders) else pd.DataFrame()
    if len(trips):
        print(
            f"\nround trips: {len(trips)} | total pnl ${trips.pnl_usd.sum():+.2f}"
            f" | hit rate {(trips.pnl_usd > 0).mean():.2f}"
            f" | avg {trips.ret_bps.mean():+.1f} bps/trip"
        )
        print(trips.to_string(index=False, float_format=lambda v: f"{v:.6g}"))
    elif len(orders):
        print("\norders present but no completed round trips")

    # ---- figure ----
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    fig.suptitle(f"session report: {Path(args.db).name}")

    ax = axes[0][0]
    if len(res):
        for symbol, g in res.groupby("symbol"):
            g = g.sort_values("pred_ts_ns").reset_index(drop=True)
            nz = g[(g.predicted != 0) & (g.realized != 0)]
            hits = (np.sign(nz.predicted) == np.sign(nz.realized)).astype(float)
            ax.plot(hits.rolling(200, min_periods=20).mean().values, label=symbol)
        ax.axhline(0.5, color="grey", ls="--", lw=1)
        ax.legend()
    ax.set_title("rolling directional accuracy (200 preds)")

    ax = axes[0][1]
    if len(preds):
        ax.hist(np.clip(preds.proc_us, 0, np.percentile(preds.proc_us, 99.5)), bins=60)
    ax.set_title("decision latency per quote (us)")

    ax = axes[1][0]
    if len(res):
        for symbol, g in res.groupby("symbol"):
            g = g.sort_values("pred_ts_ns")
            ax.plot(np.cumsum(np.sign(g.predicted) * g.realized).values, label=symbol)
        ax.axhline(0, color="grey", ls="--", lw=1)
        ax.legend()
    ax.set_title("cumulative sign(pred)*realized (frictionless)")

    ax = axes[1][1]
    q = tables["quotes"]
    if len(q):
        for symbol, g in q.groupby("symbol"):
            g = g.sort_values("ts_ns")
            mid = (g.bid + g.ask) / 2
            t = (g.ts_ns - g.ts_ns.min()) / 60e9
            ax.plot(t, mid / mid.iloc[0], lw=0.8, label=symbol)
        if len(orders):
            o = orders[orders.status == "filled"]
            t0 = q.ts_ns.min()
            for _, row in o.iterrows():
                color = "g" if row.action == "buy" else "r"
                ax.axvline((row.ts_ns - t0) / 60e9, color=color, alpha=0.4, lw=1)
        ax.legend()
    ax.set_title("normalized mid (order times marked)")
    ax.set_xlabel("minutes")

    png = out_dir / f"{Path(args.db).stem}_report.png"
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    print(f"\nfigure -> {png}")


if __name__ == "__main__":
    main()

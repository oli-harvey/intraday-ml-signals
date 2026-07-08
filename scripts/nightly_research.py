"""Nightly auto-research: evaluate the live dual-venue capture, feed the dashboard.

Snapshots data/paper_live.duckdb (cp + WAL; the live writer stays untouched),
replays each traded pair through the standard evaluation (ev model, 5s,
non-overlapping, CB leader when present), and writes:
- logs/research_latest.json  -> rendered as a table on the dashboard
- logs/research_history.jsonl -> the longitudinal record (d-best over days)

Cron: 04:30 UTC daily on the server (quiet hours; runs take tens of minutes).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

import duckdb

from signals.evaluation import evaluate


def snapshot(db: Path, dest: Path) -> None:
    shutil.copy2(db, dest)
    wal = db.with_suffix(db.suffix + ".wal")
    dest_wal = dest.with_suffix(dest.suffix + ".wal")
    if wal.exists():
        shutil.copy2(wal, dest_wal)
    elif dest_wal.exists():
        dest_wal.unlink()


async def main_async(args: argparse.Namespace) -> None:
    root = Path(args.root)
    snap = Path("/tmp/research_snapshot.duckdb")
    snapshot(root / args.db, snap)

    conn = duckdb.connect(str(snap), read_only=True)  # also validates the copy
    symbols = [s for (s,) in conn.execute("SELECT DISTINCT symbol FROM quotes").fetchall()]
    conn.close()
    followers = sorted(s for s in symbols if not s.startswith("CB:"))
    leaders_present = {s for s in symbols if s.startswith("CB:")}

    results = []
    for sym in followers:
        leader = f"CB:{sym}"
        use_leader = leader in leaders_present
        eval_syms = [leader, sym] if use_leader else [sym]
        leaders = {sym: leader} if use_leader else None
        try:
            r = await evaluate(str(snap), eval_syms, args.model, args.horizon_s,
                               non_overlapping=True, leaders=leaders)
        except Exception as exc:  # one bad pair must not sink the run
            results.append({"symbol": sym, "error": str(exc)[:80]})
            continue
        seg = r.symbols[sym].overall()
        sim = r.symbols[sym].simulate_trading(r.horizon_ns, fee_bps=args.fee_bps)
        results.append({
            "symbol": sym,
            "leader": use_leader,
            "n": seg.n,
            "dir": round(seg.dir_acc, 3) if seg.dir_acc == seg.dir_acc else None,
            "fade": round(seg.dir_fade, 3) if seg.dir_fade == seg.dir_fade else None,
            "d_best": (
                round(seg.dir_acc - seg.dir_best_baseline, 3)
                if seg.dir_acc == seg.dir_acc
                else None
            ),
            "edge_pct": round(seg.edge_pct, 1) if seg.edge_pct == seg.edge_pct else None,
            "trades": sim.trades,
            "net_bps": round(sim.net_bps_sum, 1),
        })
        print(results[-1], flush=True)

    payload = {
        "ts": time.time(),
        "model": args.model,
        "horizon_s": args.horizon_s,
        "results": results,
    }
    out = root / "logs" / "research_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    with (root / "logs" / "research_history.jsonl").open("a") as fh:
        fh.write(json.dumps(payload) + "\n")
    print(f"wrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--db", default="data/paper_live.duckdb")
    parser.add_argument("--model", default="ev")
    parser.add_argument("--horizon-s", type=float, default=5.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

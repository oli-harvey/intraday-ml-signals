"""Archive old session DBs to zstd parquet — the tape is irreplaceable, disk is not.

The capture writes ~350MB/day of DuckDB; the 38G server fills in ~2.5 months. Sessions
older than --keep-days are exported table-by-table to data/archive/<day>/<table>.parquet
(zstd, typically 5-10x smaller), row-counts VERIFIED against the source, and only then
is the .duckdb deleted. Nothing that could still be in use is touched: today's and
yesterday's sessions are always kept regardless of --keep-days.

Archived sessions stay directly queryable (duckdb reads parquet natively):
    SELECT * FROM read_parquet('data/archive/2026-07-06/quotes.parquet')
To replay one, rebuild a DB:
    CREATE TABLE quotes AS SELECT * FROM read_parquet('.../quotes.parquet');  -- etc.

Cron (MERGE — weekly, Sunday 03:10 UTC):
    10 3 * * 0 cd $HOME/intraday-ml-signals && .venv/bin/python \
        scripts/archive_sessions.py --root . >> logs/archive_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
from pathlib import Path

import duckdb

TABLES = ("trades", "quotes", "bars", "predictions", "resolutions", "orders")


def archive_db(db: Path, out_dir: Path, dry_run: bool) -> tuple[bool, int]:
    """Export every table to parquet, verify counts, delete the source.
    Returns (deleted, bytes_freed)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db), read_only=True)
    ok = True
    try:
        for table in TABLES:
            try:
                n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except duckdb.CatalogException:
                continue  # table absent in this session's schema — fine
            if n == 0:
                continue
            pq = out_dir / f"{table}.parquet"
            if pq.exists():
                m = conn.execute(
                    "SELECT count(*) FROM read_parquet(?)", [str(pq)]).fetchone()[0]
                if m == n:
                    continue  # already archived and verified — idempotent re-run
            conn.execute(
                f"COPY (SELECT * FROM {table}) TO '{pq}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)")
            m = conn.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(pq)]).fetchone()[0]
            if m != n:
                print(f"  {db.name}/{table}: VERIFY FAILED ({m} != {n}) — keeping DB")
                ok = False
    finally:
        conn.close()
    size = db.stat().st_size
    if ok and not dry_run:
        db.unlink()
        return True, size
    return False, 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--keep-days", type=int, default=14,
                    help="sessions younger than this many days are never archived")
    ap.add_argument("--dry-run", action="store_true",
                    help="export+verify but never delete the source DB")
    args = ap.parse_args()
    root = Path(args.root)
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=max(2, args.keep_days))  # >=2: never touch live

    freed = 0
    for dbp in sorted(glob.glob(str(root / "data" / "equities_2*.duckdb"))):
        db = Path(dbp)
        day_s = db.stem.replace("equities_", "")
        try:
            day = dt.date.fromisoformat(day_s)
        except ValueError:
            print(f"{db.name}: unparseable day — skip")
            continue
        if day >= cutoff:
            continue
        deleted, size = archive_db(db, root / "data" / "archive" / day_s, args.dry_run)
        state = "archived+deleted" if deleted else (
            "exported (dry-run, DB kept)" if args.dry_run else "KEPT (verify failed)")
        print(f"{db.name}: {state}")
        freed += size
    print(f"freed {freed / 1e6:,.0f} MB" if freed else "nothing to archive")


if __name__ == "__main__":
    main()

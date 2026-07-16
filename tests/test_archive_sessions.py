"""Archival must never lose tape: export, VERIFY, only then delete."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from archive_sessions import archive_db  # noqa: E402


def _make_db(path: Path, n: int = 500) -> None:
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE quotes (symbol TEXT, ts_ns BIGINT, bid DOUBLE, ask DOUBLE, "
        "bid_size DOUBLE, ask_size DOUBLE, recv_ns BIGINT)")
    conn.execute("CREATE TABLE trades (symbol TEXT, ts_ns BIGINT, price DOUBLE, "
                 "size DOUBLE, side TEXT, recv_ns BIGINT)")  # present but empty
    conn.executemany(
        "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("NVDA", i, 100.0, 100.01, 1.0, 1.0, i) for i in range(n)])
    conn.close()


def test_archive_verifies_then_deletes(tmp_path: Path):
    db = tmp_path / "equities_2026-01-05.duckdb"
    _make_db(db)
    out = tmp_path / "archive" / "2026-01-05"
    deleted, freed = archive_db(db, out, dry_run=False)
    assert deleted and freed > 0
    assert not db.exists()
    n = duckdb.execute(
        "SELECT count(*) FROM read_parquet(?)", [str(out / "quotes.parquet")]
    ).fetchone()[0]
    assert n == 500
    assert not (out / "trades.parquet").exists()  # empty tables are not exported


def test_dry_run_exports_but_keeps_the_db(tmp_path: Path):
    db = tmp_path / "equities_2026-01-06.duckdb"
    _make_db(db)
    out = tmp_path / "archive" / "2026-01-06"
    deleted, freed = archive_db(db, out, dry_run=True)
    assert not deleted and freed == 0
    assert db.exists()
    assert (out / "quotes.parquet").exists()  # export happened, delete didn't

    # second (real) run is idempotent over the verified parquet, then deletes
    deleted, _ = archive_db(db, out, dry_run=False)
    assert deleted and not db.exists()

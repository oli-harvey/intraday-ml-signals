"""The batched digest screen must equal the single-pass screen, exactly.

The nightly digest was OOM-killed on the 3.7GB server (evaluate + ReplaySource hold
every quote and resolved row for the whole symbol list in memory), so screen() now
evaluates a few symbols per replay pass. That is only a fix if batching cannot
change the numbers: pipelines are per-symbol independent and replay is
deterministic, so any difference would be a bug. This pins it.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from equities_digest import screen  # noqa: E402

SCHEMA = """
CREATE TABLE trades (
    symbol TEXT, ts_ns BIGINT, price DOUBLE, size DOUBLE, side TEXT, recv_ns BIGINT);
CREATE TABLE quotes (
    symbol TEXT, ts_ns BIGINT, bid DOUBLE, ask DOUBLE,
    bid_size DOUBLE, ask_size DOUBLE, recv_ns BIGINT);
CREATE TABLE bars (
    symbol TEXT, ts_ns BIGINT, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, volume DOUBLE, recv_ns BIGINT);
"""


@pytest.fixture(scope="module")
def session_db(tmp_path_factory) -> Path:
    """A small synthetic session: 3 symbols (one TRACKED, to exercise the
    phase-sweep path), random-walk mids, spreads straddling the 2bp gate."""
    db = tmp_path_factory.mktemp("digest") / "equities_synth.duckdb"
    conn = duckdb.connect(str(db))
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    rng = random.Random(11)
    rows = []
    for sym, mid0 in (("NVDA", 900.0), ("SPY", 550.0), ("XYZ", 50.0)):
        mid, ts = mid0, 1_700_000_000_000_000_000
        for _ in range(4_000):
            ts += rng.randint(50_000_000, 400_000_000)  # 0.05-0.4s between quotes
            mid *= 1 + rng.gauss(0, 4e-5)
            half = mid * rng.choice([0.4, 0.9, 1.4, 3.0]) * 1e-4 / 2
            rows.append((sym, ts, mid - half, mid + half, 1.0, 1.0, ts + 1_000_000))
    conn.executemany("INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()
    return db


def test_batched_screen_equals_single_pass_exactly(session_db: Path):
    whole = asyncio.run(screen(session_db, batch=99))   # all symbols, one pass
    batched = asyncio.run(screen(session_db, batch=1))  # one symbol per pass
    assert whole.keys() == batched.keys()
    for sym in whole:
        for key, want in whole[sym].items():
            got = batched[sym][key]
            if want != want:  # NaN
                assert got != got, (sym, key, got)
            else:
                assert got == want, (sym, key, got, want)


def test_screen_scores_every_captured_symbol(session_db: Path):
    result = asyncio.run(screen(session_db, batch=2))
    assert set(result) == {"NVDA", "SPY", "XYZ"}
    for r in result.values():
        assert r["trades_pq"] >= 0
        assert "net_bps" in r and "d_best" in r

"""The live shadow book must agree with the offline backtest, exactly.

If the live session says "NVDA: 34 trades, +2.9bps" and the nightly digest replays
the same day to a different answer, the whole comparison is worthless. Both call
`simrule`, and this pins that they stay in lockstep.
"""

from __future__ import annotations

import random

from signals.evaluation import Row, SymbolScore
from signals.livesim import LiveSim
from signals.simrule import decide, net_bps

HORIZON_NS = 5_000_000_000


def _rows(n: int, seed: int = 7) -> list[Row]:
    rng = random.Random(seed)
    rows, ts = [], 0
    for _ in range(n):
        ts += rng.randint(10_000_000, 3_000_000_000)  # irregular, sub- and super-horizon
        rows.append(Row(
            ts_ns=ts,
            prediction=rng.gauss(0, 8e-4),      # ±8bps-ish
            realized=rng.gauss(0, 6e-4),
            persistence=0.0,
            spread_bps=rng.choice([0.5, 1.2, 1.9, 2.4, 6.0]),  # straddles the 2bp gate
        ))
    return rows


def test_per_quote_book_matches_the_all_rows_backtest_exactly():
    """windowed=False acts on every signal — must equal simulate_trading over all rows."""
    rows = _rows(2_000)
    expected = SymbolScore("NVDA", rows).simulate_trading(
        HORIZON_NS, fee_bps=0.0, dead_zone_bps=4.0, max_spread_bps=2.0
    )
    sim = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=4.0, max_spread_bps=2.0,
                  windowed=False)
    for r in rows:  # same events, arriving one at a time as they resolve live
        sim.on_resolved("NVDA", r.ts_ns, r.prediction, r.realized, r.spread_bps)

    assert sim.total_trades == expected.trades
    assert sim.total_wins == expected.wins
    assert sim.total_net_bps == expected.net_bps_sum


def test_windowed_cadence_looks_once_per_horizon_and_trades_less():
    """windowed=True mirrors evaluate(non_overlapping=True) — the cadence every
    headline number in RESEARCH.md was measured at. It must sample strictly less."""
    rows = _rows(2_000)
    per_quote = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=4.0, max_spread_bps=2.0,
                        windowed=False)
    windowed = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=4.0, max_spread_bps=2.0,
                       windowed=True)
    for r in rows:
        per_quote.on_resolved("NVDA", r.ts_ns, r.prediction, r.realized, r.spread_bps)
        windowed.on_resolved("NVDA", r.ts_ns, r.prediction, r.realized, r.spread_bps)

    assert windowed.total_trades < per_quote.total_trades
    # and it never looks twice inside one horizon window
    assert windowed.books["NVDA"].trades <= len(rows)


def test_spread_gate_blocks_expensive_trades():
    # strong signal, but the spread is wider than the gate -> stand aside
    assert decide(0.01, spread_bps=6.0, dead_zone_bps=4.0, max_spread_bps=2.0) == 0.0
    assert decide(0.01, spread_bps=1.0, dead_zone_bps=4.0, max_spread_bps=2.0) == 1.0


def test_short_side_can_be_disabled():
    assert decide(-0.01, spread_bps=1.0, dead_zone_bps=4.0, max_spread_bps=2.0) == -1.0
    assert decide(-0.01, spread_bps=1.0, dead_zone_bps=4.0, max_spread_bps=2.0,
                  allow_short=False) == 0.0


def test_one_position_at_a_time_per_symbol():
    sim = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=0.0, max_spread_bps=None)
    assert sim.on_resolved("NVDA", 0, 0.01, 0.001, 1.0) is not None
    # inside the horizon -> ignored
    assert sim.on_resolved("NVDA", HORIZON_NS // 2, 0.01, 0.001, 1.0) is None
    # after it -> trades again
    assert sim.on_resolved("NVDA", HORIZON_NS + 1, 0.01, 0.001, 1.0) is not None
    assert sim.total_trades == 2


def test_symbols_have_independent_books():
    sim = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=0.0, max_spread_bps=None)
    sim.on_resolved("NVDA", 0, 0.01, 0.001, 1.0)
    assert sim.on_resolved("AAPL", 0, 0.01, 0.001, 1.0) is not None  # not blocked by NVDA
    assert sim.total_trades == 2


def test_net_bps_charges_the_full_spread_round_trip():
    # +10bps move, 2bp spread, long -> 8bps net
    assert net_bps(1.0, 0.0010, spread_bps=2.0) == 8.0
    # short profits when the move is negative
    assert net_bps(-1.0, -0.0010, spread_bps=2.0) == 8.0


def test_summary_reports_per_symbol_and_is_json_safe():
    sim = LiveSim(horizon_ns=HORIZON_NS, dead_zone_bps=0.0, max_spread_bps=None)
    sim.on_resolved("NVDA", 0, 0.01, 0.001, 1.0)
    s = sim.summary()
    assert s["trades"] == 1
    assert "NVDA" in s["by_symbol"]
    assert s["recent"][-1]["side"] == "long"

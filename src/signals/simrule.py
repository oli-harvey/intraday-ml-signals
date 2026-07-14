"""The trade rule — the SINGLE definition shared by the offline backtest and the
live shadow sim.

`evaluation.simulate_trading` (replay) and `scripts/stocks_live.py` (live session)
both call these. That is deliberate: the moment the live sim and the backtest each
own a copy of "when do we trade and what did it earn", they drift, and the nightly
green tally stops being a statement about the thing that actually ran. Same code
path, no skew — the same guard `core.SymbolPipeline` gives features/predictions.

The rule (the tracked NVDA config, RESEARCH.md 2026-07-10/13):
  - skip if the quoted spread is wider than max_spread_bps (spread-conditional entry:
    the round-trip toll IS the spread, so only trade when it's cheap)
  - take a side only when |prediction| clears fee + half-spread + dead-zone
  - hold to the horizon; charge the full spread plus two fees on the round trip
"""

from __future__ import annotations


def decide(
    prediction: float,
    spread_bps: float,
    *,
    fee_bps: float = 0.0,
    dead_zone_bps: float = 4.0,
    allow_short: bool = True,
    max_spread_bps: float | None = None,
) -> float:
    """+1 long / -1 short / 0 stand aside. `prediction` is a forward return."""
    if max_spread_bps is not None and spread_bps > max_spread_bps:
        return 0.0  # toll too high — don't pay it
    threshold = (fee_bps + 0.5 * spread_bps + dead_zone_bps) / 1e4
    if prediction > threshold:
        return 1.0
    if prediction < -threshold and allow_short:
        return -1.0
    return 0.0


def net_bps(direction: float, realized: float, spread_bps: float, fee_bps: float = 0.0) -> float:
    """Net basis points on a completed round trip (cross in, cross out)."""
    return direction * realized * 1e4 - (spread_bps + 2 * fee_bps)

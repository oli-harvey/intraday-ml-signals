"""Cross-asset / cross-venue lead-lag state.

Two distinct uses of the same mechanism:
- cross-ASSET: BTC's move as a feature for ETH (helps sparse followers only —
  see docs/RESEARCH.md 2026-07-07).
- cross-VENUE: the same asset from the discovery venue (e.g. Coinbase BTC) as
  leader for the traded venue (Alpaca BTC). Here the killer feature is the
  price GAP between venues: if the leader moved and the follower hasn't, the
  follower tends to catch up. The leader's mid is shared so the follower's
  engine can compute `leader_gap_bps` against its own mid.

Staleness-gated: leader readings older than `staleness_ns` contribute zeros
(no signal) rather than stale information. O(1) per update/read, hot-path safe.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LeaderState:
    ts_ns: int = 0
    mid: float = 0.0
    r1: float = 0.0  # last 1-lag mid return
    uptick: float = 0.0  # persistence EMA of mid-change signs


class CrossFeed:
    def __init__(self, staleness_ns: int = 5_000_000_000) -> None:
        self.staleness_ns = staleness_ns
        self._state: dict[str, LeaderState] = {}

    def update(self, symbol: str, ts_ns: int, mid: float, r1: float, uptick: float) -> None:
        state = self._state.get(symbol)
        if state is None:
            state = self._state[symbol] = LeaderState()
        state.ts_ns = ts_ns
        state.mid = mid
        state.r1 = r1
        state.uptick = uptick

    def leader_state(self, leader: str, now_ns: int) -> LeaderState | None:
        """Fresh leader state, or None if missing/stale."""
        state = self._state.get(leader)
        if state is None or now_ns - state.ts_ns > self.staleness_ns:
            return None
        return state

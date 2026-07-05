"""Cross-asset lead-lag state.

BTC classically leads ETH (and majors lead alts) at short horizons: a BTC move
is information about ETH's next move before ETH's own quotes reflect it. Each
symbol's FeatureEngine posts its latest 1-lag mid return and persistence EMA
here; engines configured with a leader read them back as extra features.

Staleness-gated: a leader reading older than `staleness_ns` contributes zeros
(no signal) rather than stale information. O(1) per update/read, hot-path safe.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LeaderState:
    ts_ns: int = 0
    r1: float = 0.0  # last 1-lag mid return
    uptick: float = 0.0  # persistence EMA of mid-change signs


class CrossFeed:
    def __init__(self, staleness_ns: int = 5_000_000_000) -> None:
        self.staleness_ns = staleness_ns
        self._state: dict[str, LeaderState] = {}

    def update(self, symbol: str, ts_ns: int, r1: float, uptick: float) -> None:
        state = self._state.get(symbol)
        if state is None:
            state = self._state[symbol] = LeaderState()
        state.ts_ns = ts_ns
        state.r1 = r1
        state.uptick = uptick

    def leader_features(self, leader: str, now_ns: int) -> dict[str, float]:
        state = self._state.get(leader)
        if state is None or now_ns - state.ts_ns > self.staleness_ns:
            return {"leader_r1": 0.0, "leader_uptick": 0.0}
        return {"leader_r1": state.r1, "leader_uptick": state.uptick}

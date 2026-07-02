"""Incremental feature engine (per symbol).

Quotes are the clock: Alpaca's crypto trade feed is thin (own-venue prints only),
so features are computed on quote-mid updates; trades feed a signed-flow EMA as a
supplementary input. All state lives in a mid-price ring buffer + O(1) online-stat
accumulators. Output is a dict[str, float] of online z-normalized features, ready
for River's `predict_one`/`learn_one` — or None until warmed up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.ringbuffer import RingBuffer
from ..data.schema import MarketEvent, Quote, Side, Tick
from .online_stats import EMA, RunningZScore, Welford


@dataclass(frozen=True)
class FeatureConfig:
    lag_returns: tuple[int, ...] = (1, 2, 4, 8)  # % change over k quote-mid lags
    ema_fast_alpha: float = 0.3
    ema_slow_alpha: float = 0.03
    vol_window: int = 64  # rolling Welford window over 1-lag returns
    zscore_window: int = 1024  # rolling normalization horizon
    zscore_warmup: int = 30
    flow_alpha: float = 0.1  # EMA of signed trade size
    warmup_quotes: int = 64  # emit nothing before this many valid quotes
    mid_depth: int = field(init=False, default=0)  # derived; see __post_init__

    def __post_init__(self) -> None:
        object.__setattr__(self, "mid_depth", max(self.lag_returns) + 1)


class FeatureEngine:
    """O(1)-per-event incremental features for one symbol."""

    def __init__(self, config: FeatureConfig | None = None) -> None:
        self.config = config or FeatureConfig()
        c = self.config
        self._mids = RingBuffer(c.mid_depth)
        self._ema_fast = EMA(c.ema_fast_alpha)
        self._ema_slow = EMA(c.ema_slow_alpha)
        self._vol = Welford(window=c.vol_window)
        self._flow = EMA(c.flow_alpha)
        self._z: dict[str, RunningZScore] = {}
        self._quotes_seen = 0
        self.last_raw: dict[str, float] = {}  # pre-normalization values (tests/debug)

    def _znorm(self, name: str, value: float) -> float:
        z = self._z.get(name)
        if z is None:
            c = self.config
            z = self._z[name] = RunningZScore(window=c.zscore_window, warmup=c.zscore_warmup)
        return z.normalize(value)

    def update(self, event: MarketEvent) -> dict[str, float] | None:
        """Ingest one event; return the feature vector on quote updates, else None."""
        if isinstance(event, Tick):
            if event.side is Side.BUY:
                self._flow.update(event.size)
            elif event.side is Side.SELL:
                self._flow.update(-event.size)
            return None
        if not isinstance(event, Quote):
            return None  # bars not used as features yet

        mid = event.mid
        if mid <= 0.0 or event.bid <= 0.0 or event.ask < event.bid:
            return None  # degenerate/crossed quote

        c = self.config
        if len(self._mids) >= 1:
            prev = float(self._mids.latest())
            self._vol.update(mid / prev - 1.0)
        ema_fast = self._ema_fast.update(mid)
        ema_slow = self._ema_slow.update(mid)
        self._mids.push(mid)
        self._quotes_seen += 1

        max_lag = max(c.lag_returns)
        if len(self._mids) <= max_lag or self._quotes_seen < c.warmup_quotes:
            return None

        lags = self._mids.last(max_lag + 1)  # oldest-first; [-1] is current mid
        raw: dict[str, float] = {}
        for k in c.lag_returns:
            raw[f"ret_{k}"] = mid / float(lags[-1 - k]) - 1.0
        raw["ema_spread"] = (ema_fast - ema_slow) / mid
        raw["vol"] = self._vol.std
        raw["spread_bps"] = (event.ask - event.bid) / mid * 1e4
        total_size = event.bid_size + event.ask_size
        raw["imbalance"] = (event.bid_size - event.ask_size) / total_size if total_size else 0.0
        raw["flow"] = self._flow.value if self._flow.value is not None else 0.0

        self.last_raw = raw
        return {name: self._znorm(name, value) for name, value in raw.items()}

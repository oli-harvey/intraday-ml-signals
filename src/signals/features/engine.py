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
from .cross import CrossFeed
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
    uptick_alpha: float = 0.1  # EMA of sign(mid change): short-term persistence
    dt_alpha: float = 0.1  # EMA of quote inter-arrival seconds: activity regime
    interactions: bool = True  # cross terms between base features (see update())
    exclude: tuple[str, ...] = ()  # feature names to drop from output (ablations)
    warmup_quotes: int = 64  # emit nothing before this many valid quotes
    mid_depth: int = field(init=False, default=0)  # derived; see __post_init__

    def __post_init__(self) -> None:
        object.__setattr__(self, "mid_depth", max(self.lag_returns) + 1)


class FeatureEngine:
    """O(1)-per-event incremental features for one symbol."""

    def __init__(
        self,
        config: FeatureConfig | None = None,
        crossfeed: CrossFeed | None = None,
        leader: str | None = None,
    ) -> None:
        self.config = config or FeatureConfig()
        self.crossfeed = crossfeed  # shared blackboard; engine posts its own state
        self.leader = leader  # symbol whose state to consume as features
        c = self.config
        self._mids = RingBuffer(c.mid_depth)
        self._ema_fast = EMA(c.ema_fast_alpha)
        self._ema_slow = EMA(c.ema_slow_alpha)
        self._vol = Welford(window=c.vol_window)
        self._flow = EMA(c.flow_alpha)
        self._uptick = EMA(c.uptick_alpha)
        self._dt = EMA(c.dt_alpha)
        self._prev_ts_ns = 0
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
            side = event.side
            if side is None and len(self._mids) >= 1:
                # Equities feeds don't tag the aggressor; infer via the quote rule
                # (Lee-Ready lite): a print above the mid was buyer-initiated,
                # below it seller-initiated. At exactly the mid, no signal.
                last_mid = float(self._mids.latest())
                if event.price > last_mid:
                    side = Side.BUY
                elif event.price < last_mid:
                    side = Side.SELL
            if side is Side.BUY:
                self._flow.update(event.size)
            elif side is Side.SELL:
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
            r1 = mid / prev - 1.0
            self._vol.update(r1)
            if r1 != 0.0:  # persistence of mid direction (unchanged mids carry no sign)
                self._uptick.update(1.0 if r1 > 0 else -1.0)
            if self.crossfeed is not None:  # publish own state for followers
                self.crossfeed.update(
                    event.symbol, event.ts_ns, mid, r1, self._uptick.value or 0.0
                )
        if self._prev_ts_ns:
            self._dt.update((event.ts_ns - self._prev_ts_ns) / 1e9)
        self._prev_ts_ns = event.ts_ns
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
        # Microprice (size-weighted quote): where the book is "leaning". A large bid
        # size pushes fair value toward the ask. Classic next-mid-move predictor.
        if total_size > 0:
            microprice = (event.bid_size * event.ask + event.ask_size * event.bid) / total_size
            raw["micro_bps"] = (microprice - mid) / mid * 1e4
        else:
            raw["micro_bps"] = 0.0
        raw["uptick"] = self._uptick.value if self._uptick.value is not None else 0.0
        raw["dt_s"] = self._dt.value if self._dt.value is not None else 0.0
        if self.crossfeed is not None and self.leader is not None:
            state = self.crossfeed.leader_state(self.leader, event.ts_ns)
            # Venue-prefixed leaders ("CB:BTC/USD" leading "BTC/USD") are the
            # same asset, so a price gap is meaningful; cross-asset leaders
            # (BTC leading ETH) get momentum/persistence only.
            same_asset = self.leader.split(":", 1)[-1] == event.symbol
            if state is not None:
                raw["leader_r1"] = state.r1
                raw["leader_uptick"] = state.uptick
                if same_asset:
                    # Cross-venue gap: the leader venue moved, we haven't (yet)
                    # — the most direct same-asset lead-lag signal.
                    raw["leader_gap_bps"] = (state.mid - mid) / mid * 1e4
            else:
                raw["leader_r1"] = 0.0
                raw["leader_uptick"] = 0.0
                if same_asset:
                    raw["leader_gap_bps"] = 0.0

        if c.interactions:
            # Ratio interactions on raw values (each has a natural denominator):
            # - vol-normalized momentum: a 2bps move in a quiet regime is signal,
            #   the same move in a storm is noise (t-statistic of the last move).
            # - lean/spread: microprice offset as a fraction of the spread — a
            #   half-spread lean is the book shouting regardless of spread width.
            raw["ret1_over_vol"] = raw["ret_1"] / (raw["vol"] + 1e-12)
            raw["micro_over_spread"] = raw["micro_bps"] / (raw["spread_bps"] + 1e-6)

        self.last_raw = raw
        z = {name: self._znorm(name, value) for name, value in raw.items()}

        if c.interactions:
            # Product interactions on the z-scored components (dimensionless,
            # centered): the product is positive when the two signals AGREE in
            # sign — confirmation — and negative on disagreement. Re-z-normalized
            # so the model sees a calibrated, clipped input like everything else.
            for name, a, b in (
                ("micro_x_uptick", "micro_bps", "uptick"),  # lean confirmed by tape
                ("micro_x_ret1", "micro_bps", "ret_1"),  # lean confirmed by last move
                ("flow_x_imbalance", "flow", "imbalance"),  # aggressors + book agree
            ):
                product = z[a] * z[b]
                self.last_raw[name] = product
                z[name] = self._znorm(name, product)
        if c.exclude:
            for name in c.exclude:
                z.pop(name, None)
        return z

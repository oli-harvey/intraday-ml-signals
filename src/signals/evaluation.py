"""Walk-forward evaluation over recorded sessions (offline; used by scripts).

Runs the SAME SymbolPipeline as live over a ReplaySource and scores it against
two naive baselines that any claimed edge must beat:

- zero baseline: always predict 0 (MAE floor — beating it means magnitude skill)
- persistence baseline: predict the last *resolved* realized return (sign-wise,
  "the recent past continues"). Quote-mid returns over overlapping horizons are
  strongly autocorrelated, so raw directional accuracy flatters the model; the
  persistence baseline measures how much of that is free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .core import SymbolPipeline
from .data.replay import ReplaySource
from .features.cross import CrossFeed
from .features.engine import FeatureConfig, FeatureEngine
from .model.online import OnlineModel


@dataclass
class SegmentScore:
    n: int
    mae: float
    zero_mae: float
    dir_acc: float
    dir_persistence: float

    @property
    def edge_pct(self) -> float:
        return (1 - self.mae / self.zero_mae) * 100 if self.zero_mae else float("nan")

    @property
    def dir_fade(self) -> float:
        """Fade baseline: bet AGAINST the last window's move. The exact mirror of
        persistence — on mean-reverting series this is the one to beat."""
        return 1.0 - self.dir_persistence

    @property
    def dir_best_baseline(self) -> float:
        """The strongest naive direction rule on this segment (persistence, fade,
        or coin flip). Model skill = dir_acc - this, not dir_acc - 0.5."""
        candidates = [self.dir_persistence, self.dir_fade, 0.5]
        return max(c for c in candidates if c == c)  # NaN-safe max


@dataclass(frozen=True, slots=True)
class Row:
    ts_ns: int
    prediction: float
    realized: float
    persistence: float  # baseline forecast known at prediction time
    spread_bps: float


@dataclass
class TradeSim:
    """Cost-charged simulation of the live threshold policy over resolved rows."""

    trades: int = 0
    wins: int = 0
    net_bps_sum: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.trades if self.trades else float("nan")

    @property
    def avg_net_bps(self) -> float:
        return self.net_bps_sum / self.trades if self.trades else float("nan")


@dataclass
class SymbolScore:
    symbol: str
    rows: list[Row] = field(default_factory=list)

    def simulate_trading(
        self,
        horizon_ns: int,
        fee_bps: float = 5.0,
        dead_zone_bps: float = 2.0,
        allow_short: bool = True,
    ) -> TradeSim:
        """Sequential trades (one position at a time, held for the horizon),
        entering only when |prediction| clears fee + half-spread + dead-zone —
        the same rule as the live SignalPolicy. Round trip is charged the full
        spread plus two fees. Exits at the realized horizon price."""
        sim = TradeSim()
        busy_until = -(10**18)
        for r in self.rows:
            if r.ts_ns < busy_until:
                continue  # position still open from a previous signal
            threshold = (fee_bps + 0.5 * r.spread_bps + dead_zone_bps) / 1e4
            if r.prediction > threshold:
                direction = 1.0
            elif r.prediction < -threshold and allow_short:
                direction = -1.0
            else:
                continue
            net_bps = direction * r.realized * 1e4 - (r.spread_bps + 2 * fee_bps)
            sim.trades += 1
            sim.wins += 1 if net_bps > 0 else 0
            sim.net_bps_sum += net_bps
            busy_until = r.ts_ns + horizon_ns
        return sim

    def simulate_fade_rule(
        self,
        horizon_ns: int,
        fee_bps: float = 0.2,
        min_signal_bps: float = 0.0,
        allow_short: bool = True,
    ) -> TradeSim:
        """The fade BASELINE as a STRATEGY: trade against the previous
        independent window's move (r.persistence) when it exceeds
        min_signal_bps. Same costs and one-position-at-a-time sequencing as
        the model sim. Only meaningful on rows produced with
        non_overlapping=True (persistence = the last independent window)."""
        sim = TradeSim()
        busy_until = -(10**18)
        for r in self.rows:
            if r.ts_ns < busy_until:
                continue
            if abs(r.persistence) * 1e4 <= min_signal_bps:
                continue  # last move too small to fade
            direction = -1.0 if r.persistence > 0 else 1.0
            if direction < 0 and not allow_short:
                continue
            net_bps = direction * r.realized * 1e4 - (r.spread_bps + 2 * fee_bps)
            sim.trades += 1
            sim.wins += 1 if net_bps > 0 else 0
            sim.net_bps_sum += net_bps
            busy_until = r.ts_ns + horizon_ns
        return sim

    def segment(self, lo: int, hi: int) -> SegmentScore:
        seg = self.rows[lo:hi]
        preds = np.array([r.prediction for r in seg])
        reals = np.array([r.realized for r in seg])
        pers = np.array([r.persistence for r in seg])
        nz = (preds != 0) & (reals != 0)
        nzp = (pers != 0) & (reals != 0)
        return SegmentScore(
            n=len(seg),
            mae=float(np.abs(reals - preds).mean()) if len(seg) else float("nan"),
            zero_mae=float(np.abs(reals).mean()) if len(seg) else float("nan"),
            dir_acc=(
                float(((preds[nz] > 0) == (reals[nz] > 0)).mean()) if nz.any() else float("nan")
            ),
            dir_persistence=(
                float(((pers[nzp] > 0) == (reals[nzp] > 0)).mean())
                if nzp.any()
                else float("nan")
            ),
        )

    def overall(self) -> SegmentScore:
        return self.segment(0, len(self.rows))

    def quartiles(self) -> list[SegmentScore]:
        n = len(self.rows)
        bounds = [round(i * n / 4) for i in range(5)]
        return [self.segment(bounds[i], bounds[i + 1]) for i in range(4)]


@dataclass
class EvalResult:
    events: int
    proc_us_p50: float
    proc_us_p99: float
    horizon_ns: int
    symbols: dict[str, SymbolScore]


async def evaluate(
    db_path: str,
    symbols: list[str],
    model_kind: str = "hoeffding",
    horizon_s: float = 10.0,
    feature_config: FeatureConfig | None = None,
    non_overlapping: bool = False,
    leaders: dict[str, str] | None = None,
) -> EvalResult:
    """non_overlapping: score only predictions spaced >= horizon apart. Successive
    quote-rate predictions share ~99% of their outcome window, so overlapping
    scores mostly measure autocorrelation; non-overlapping is the honest view
    (fewer samples, independent outcomes).

    leaders: follower -> leader symbol map for cross-asset features (e.g.
    {"ETH/USD": "BTC/USD"}); the leader must also be in `symbols`."""
    source = ReplaySource(db_path, symbols)
    horizon_ns = int(horizon_s * 1e9)
    crossfeed = CrossFeed() if leaders else None
    pipelines = {
        s: SymbolPipeline(
            s,
            FeatureEngine(feature_config, crossfeed=crossfeed, leader=(leaders or {}).get(s)),
            OnlineModel(kind=model_kind),
            horizon_ns=horizon_ns,
        )
        for s in symbols
    }
    scores = {s: SymbolScore(s) for s in symbols}
    last_realized: dict[str, float] = dict.fromkeys(symbols, 0.0)
    last_scored_ts: dict[str, int] = dict.fromkeys(symbols, -(10**18))
    proc_us: list[float] = []
    events = 0

    async for event in source.stream():
        pipe = pipelines.get(event.symbol)
        if pipe is None:
            continue
        events += 1
        step = pipe.on_event(event)
        if step.prediction is not None:
            proc_us.append(step.prediction.proc_us)
        for r in step.resolved:
            if non_overlapping and r.ts_ns < last_scored_ts[event.symbol] + horizon_ns:
                continue  # outcome window overlaps the last scored one — skip
            # persistence forecast = the last realized return known BEFORE this one
            # (in non-overlapping mode: the previous independent window's return)
            scores[event.symbol].rows.append(
                Row(
                    ts_ns=r.ts_ns,
                    prediction=r.prediction,
                    realized=r.realized,
                    persistence=last_realized[event.symbol],
                    spread_bps=r.spread_bps,
                )
            )
            last_realized[event.symbol] = r.realized
            last_scored_ts[event.symbol] = r.ts_ns

    lat = np.array(proc_us) if proc_us else np.array([0.0])
    return EvalResult(
        events=events,
        proc_us_p50=float(np.percentile(lat, 50)),
        proc_us_p99=float(np.percentile(lat, 99)),
        horizon_ns=horizon_ns,
        symbols=scores,
    )

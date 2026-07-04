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


@dataclass
class SymbolScore:
    symbol: str
    rows: list[tuple[float, float, float]] = field(default_factory=list)
    # (prediction, realized, persistence_prediction)

    def segment(self, lo: int, hi: int) -> SegmentScore:
        seg = self.rows[lo:hi]
        preds = np.array([r[0] for r in seg])
        reals = np.array([r[1] for r in seg])
        pers = np.array([r[2] for r in seg])
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
    symbols: dict[str, SymbolScore]


async def evaluate(
    db_path: str,
    symbols: list[str],
    model_kind: str = "hoeffding",
    horizon_s: float = 10.0,
    feature_config: FeatureConfig | None = None,
    non_overlapping: bool = False,
) -> EvalResult:
    """non_overlapping: score only predictions spaced >= horizon apart. Successive
    quote-rate predictions share ~99% of their outcome window, so overlapping
    scores mostly measure autocorrelation; non-overlapping is the honest view
    (fewer samples, independent outcomes)."""
    source = ReplaySource(db_path, symbols)
    horizon_ns = int(horizon_s * 1e9)
    pipelines = {
        s: SymbolPipeline(
            s,
            FeatureEngine(feature_config),
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
                (r.prediction, r.realized, last_realized[event.symbol])
            )
            last_realized[event.symbol] = r.realized
            last_scored_ts[event.symbol] = r.ts_ns

    lat = np.array(proc_us) if proc_us else np.array([0.0])
    return EvalResult(
        events=events,
        proc_us_p50=float(np.percentile(lat, 50)),
        proc_us_p99=float(np.percentile(lat, 99)),
        symbols=scores,
    )

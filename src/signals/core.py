"""Per-symbol prediction core: features -> predict -> queue label -> learn.

This class is shared VERBATIM by the historical replay driver and the live
pipeline — that is the design guard against train/serve skew (PLAN §10). It is
synchronous and O(1) per event; async orchestration lives around it.

Event handling (quote = the clock):
1. Resolve any pending labels whose horizon elapsed at this quote's timestamp,
   and learn from them. Done BEFORE predicting so the model never sees a label
   derived from the same event it is about to predict on.
2. Update features; if warmed up, predict forward return and enqueue the
   prediction for later labelling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .data.schema import MarketEvent, Quote
from .features.engine import FeatureEngine
from .model.labels import LabelQueue, Pending, Resolved
from .model.online import OnlineModel


@dataclass(frozen=True, slots=True)
class Prediction:
    symbol: str
    ts_ns: int
    predicted: float  # forward simple return over the horizon
    mid: float
    spread_bps: float
    proc_us: float  # feature+inference wall time for this event


@dataclass(frozen=True, slots=True)
class StepResult:
    prediction: Prediction | None
    resolved: list[Resolved]


class SymbolPipeline:
    def __init__(
        self,
        symbol: str,
        features: FeatureEngine,
        model: OnlineModel,
        horizon_ns: int,
    ) -> None:
        self.symbol = symbol
        self.features = features
        self.model = model
        self.labels = LabelQueue(horizon_ns)

    def on_event(self, event: MarketEvent) -> StepResult:
        if not isinstance(event, Quote):
            self.features.update(event)  # trades feed flow state; no prediction
            return StepResult(None, [])

        started = time.perf_counter_ns()
        mid = event.mid

        resolved: list[Resolved] = []
        if mid > 0:
            resolved = self.labels.pop_ready(event.ts_ns, mid)
            for r in resolved:
                self.model.learn_one(r.features, r.realized, r.prediction)

        vector = self.features.update(event)
        prediction: Prediction | None = None
        if vector is not None:
            predicted = self.model.predict_one(vector)
            self.labels.add(
                Pending(ts_ns=event.ts_ns, features=vector, ref_price=mid, prediction=predicted)
            )
            prediction = Prediction(
                symbol=self.symbol,
                ts_ns=event.ts_ns,
                predicted=predicted,
                mid=mid,
                spread_bps=(event.ask - event.bid) / mid * 1e4,
                proc_us=(time.perf_counter_ns() - started) / 1e3,
            )
        return StepResult(prediction, resolved)

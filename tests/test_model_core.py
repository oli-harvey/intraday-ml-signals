"""OnlineModel learning + SymbolPipeline end-to-end no-lookahead behavior."""

import numpy as np

from signals.core import SymbolPipeline
from signals.data.schema import Quote
from signals.features.engine import FeatureConfig, FeatureEngine
from signals.model.online import OnlineModel

S = 1_000_000_000


def test_online_model_learns_linear_relationship() -> None:
    """y = 2*a - b, online: MAE must end up well below the zero baseline."""
    rng = np.random.default_rng(0)
    model = OnlineModel(kind="linear")
    for _ in range(3000):
        a, b = rng.normal(), rng.normal()
        x = {"a": a, "b": b}
        model.learn_one(x, 2 * a - b)
    m = model.metrics()
    assert m["n"] == 3000
    assert m["mae"] < 0.5 * m["zero_mae"], f"failed to learn: {m}"
    assert m["directional_acc"] > 0.8


def _quote(ts_s: float, mid: float) -> Quote:
    return Quote(
        symbol="BTC/USD", ts_ns=int(ts_s * S), bid=mid - 1, ask=mid + 1, bid_size=1, ask_size=1
    )


def _pipeline(horizon_s: float = 5.0) -> SymbolPipeline:
    cfg = FeatureConfig(lag_returns=(1, 2), warmup_quotes=8, zscore_warmup=4, vol_window=8)
    return SymbolPipeline(
        "BTC/USD", FeatureEngine(cfg), OnlineModel("linear"), horizon_ns=int(horizon_s * S)
    )


def test_pipeline_predicts_after_warmup_and_resolves_after_horizon() -> None:
    pipe = _pipeline(horizon_s=5.0)
    rng = np.random.default_rng(1)
    mid = 100.0
    predictions = 0
    resolved = 0
    first_prediction_ts = None
    for i in range(200):
        ts = i * 1.0  # 1 quote per second
        mid *= 1 + rng.normal(0, 1e-4)
        step = pipe.on_event(_quote(ts, mid))
        if step.prediction is not None:
            predictions += 1
            if first_prediction_ts is None:
                first_prediction_ts = ts
        for r in step.resolved:
            resolved += 1
            # resolution must be at least horizon after the prediction
            assert r.resolved_ts_ns - r.ts_ns >= 5 * S
    assert predictions > 100
    assert resolved > 90
    assert pipe.model.n_learned == resolved
    # pending predictions at the end = made but not yet resolvable (no future data): correct
    assert len(pipe.labels) == predictions - resolved


def test_learning_happens_before_prediction_on_same_event() -> None:
    """The learn-then-predict order within one event must hold (no same-event leak)."""
    pipe = _pipeline(horizon_s=1.0)
    learned_at_prediction: list[int] = []
    for i in range(50):
        step = pipe.on_event(_quote(i * 1.0, 100.0 + i * 0.01))
        if step.prediction is not None:
            learned_at_prediction.append(pipe.model.n_learned)
    # n_learned grows between predictions -> resolutions were applied before predicting
    assert learned_at_prediction[-1] > learned_at_prediction[0]

"""River online model wrapper.

predict_one(features) -> forward-return estimate; learn_one(features, target) updates
incrementally once the label is available (via LabelQueue). Start with a linear model;
compare HoeffdingTreeRegressor. Regression first; classification (up/down/flat with a
dead-zone) as an alternative. Phase 3 — see docs/PLAN.md.
"""

from __future__ import annotations


class OnlineModel:
    """Thin wrapper over a River estimator + rolling walk-forward metrics."""

    def __init__(self, kind: str = "linear") -> None:
        self.kind = kind
        # self._model = river.linear_model.LinearRegression() | HoeffdingTreeRegressor()
        # self._metrics = river.metrics.MAE() + directional accuracy

    def predict_one(self, features: dict[str, float]) -> float:
        raise NotImplementedError("Phase 3")

    def learn_one(self, features: dict[str, float], target: float) -> None:
        raise NotImplementedError("Phase 3")

    def metrics(self) -> dict[str, float]:
        raise NotImplementedError("Phase 3")

"""River online model wrapper.

predict_one(features) -> forward-return estimate; learn_one(features, target)
updates incrementally once the label is available (via LabelQueue). Regression
on forward return, per the brief; classification with a dead-zone is a later
comparison. Feature scaling is handled upstream by the engine's online z-scores.
"""

from __future__ import annotations

from collections import deque


class OnlineModel:
    """Thin wrapper over a River estimator + walk-forward metrics.

    Metrics tracked online, no lookahead:
    - mae / zero_mae: model MAE vs the "always predict 0" baseline (any edge
      must beat this by construction, not by shuffling).
    - directional_acc: rolling fraction of correct signs, counted only when
      both prediction and outcome are nonzero.
    """

    def __init__(
        self, kind: str = "linear", directional_window: int = 1000, band_bps: float = 5.0
    ) -> None:
        from river import linear_model, optim, tree  # local: keep import cost off hot path

        self.kind = kind
        self.band = band_bps / 1e4  # classifier dead-zone half-width (return units)
        if kind == "linear":
            self._model = linear_model.LinearRegression(optimizer=optim.SGD(0.01))
        elif kind == "hoeffding":
            self._model = tree.HoeffdingTreeRegressor(grace_period=200)
        elif kind == "classifier":
            # 3-class: -1 (down through band) / 0 (dead-zone) / +1 (up through band).
            # Aligns the objective with the trade decision: only tail precision matters.
            self._model = tree.HoeffdingTreeClassifier(grace_period=200)
            self._tail_mean = 0.0  # online mean of |realized| in the tails
            self._tail_n = 0
        else:
            raise ValueError(f"unknown model kind: {kind}")
        self._abs_err_sum = 0.0
        self._abs_target_sum = 0.0
        self._direction: deque[float] = deque(maxlen=directional_window)
        self.n_learned = 0

    def predict_one(self, features: dict[str, float]) -> float:
        if self.kind != "classifier":
            return float(self._model.predict_one(features) or 0.0)
        proba = self._model.predict_proba_one(features)
        if not proba or self._tail_n == 0:
            return 0.0
        # expected return ~ (P(up) - P(down)) * E[|move| | tail]
        return (proba.get(1, 0.0) - proba.get(-1, 0.0)) * self._tail_mean

    def learn_one(
        self, features: dict[str, float], target: float, prediction: float | None = None
    ) -> None:
        pred = prediction if prediction is not None else self.predict_one(features)
        self._abs_err_sum += abs(target - pred)
        self._abs_target_sum += abs(target)
        if pred != 0.0 and target != 0.0:
            self._direction.append(1.0 if (pred > 0) == (target > 0) else 0.0)
        if self.kind == "classifier":
            cls = 1 if target > self.band else -1 if target < -self.band else 0
            if cls != 0:  # track typical tail magnitude for the expected-return proxy
                self._tail_n += 1
                self._tail_mean += (abs(target) - self._tail_mean) / self._tail_n
            self._model.learn_one(features, cls)
        else:
            self._model.learn_one(features, target)
        self.n_learned += 1

    def metrics(self) -> dict[str, float]:
        n = max(1, self.n_learned)
        return {
            "n": float(self.n_learned),
            "mae": self._abs_err_sum / n,
            "zero_mae": self._abs_target_sum / n,  # baseline: always predict 0
            "directional_acc": (
                sum(self._direction) / len(self._direction) if self._direction else float("nan")
            ),
        }

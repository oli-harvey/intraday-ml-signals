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
        elif kind == "meta":
            # Meta-labeling (Lopez de Prado): primary regressor proposes direction
            # and magnitude; a logistic gate learns P(primary sign is correct | features,
            # primary confidence) and scales the output by max(0, 2p-1) — signals the
            # gate distrusts are zeroed, so the threshold policy never sees them.
            self._model = tree.HoeffdingTreeRegressor(grace_period=200)
            self._gate = linear_model.LogisticRegression()
        elif kind == "adaptive":
            # Hoeffding tree with ADWIN drift detection: subtrees are replaced
            # when their error distribution shifts — regime changes handled by
            # the model instead of hoped away.
            self._model = tree.HoeffdingAdaptiveTreeRegressor(grace_period=200, seed=1)
        elif kind == "forest":
            # Adaptive Random Forest: bagged adaptive trees + per-tree drift
            # detectors. ~10x the compute of one tree — still micro-seconds,
            # far inside the budget.
            from river import forest

            self._model = forest.ARFRegressor(n_models=10, seed=1)
        elif kind == "ev":
            # Decision-aware CONTINUOUS objective. Three online quantile
            # regressions on the forward return (in bps): q25 / q50 / q75.
            # Output = the PESSIMISTIC side of the interval: q25 if even it is
            # positive (long), q75 if even it is negative (short), else 0.0
            # (abstain). The policy still charges toll on top, so a trade fires
            # only when ~75% of the outcome distribution clears costs — this is
            # magnitude selectivity expressed continuously, with no fixed band
            # and no information thrown away by binarising the label.
            self._quantiles = {
                q: linear_model.LinearRegression(
                    optimizer=optim.SGD(0.05), loss=optim.losses.Quantile(alpha=q)
                )
                for q in (0.25, 0.5, 0.75)
            }
            self._model = self._quantiles[0.5]  # median, for generic metrics paths
        else:
            raise ValueError(f"unknown model kind: {kind}")
        self._abs_err_sum = 0.0
        self._abs_target_sum = 0.0
        self._direction: deque[float] = deque(maxlen=directional_window)
        self.n_learned = 0

    def predict_one(self, features: dict[str, float]) -> float:
        if self.kind == "ev":
            # bps-scaled internally; interface stays in return units
            q25 = float(self._quantiles[0.25].predict_one(features) or 0.0)
            q75 = float(self._quantiles[0.75].predict_one(features) or 0.0)
            if q25 > 0.0:
                return q25 / 1e4  # even the pessimistic quantile is a gain: long
            if q75 < 0.0:
                return q75 / 1e4  # even the optimistic quantile is a loss: short
            return 0.0  # distribution straddles zero: abstain
        if self.kind == "classifier":
            proba = self._model.predict_proba_one(features)
            if not proba or self._tail_n == 0:
                return 0.0
            # expected return ~ (P(up) - P(down)) * E[|move| | tail]
            return (proba.get(1, 0.0) - proba.get(-1, 0.0)) * self._tail_mean
        if self.kind == "meta":
            primary = float(self._model.predict_one(features) or 0.0)
            if primary == 0.0:
                return 0.0
            p_correct = self._gate.predict_proba_one(self._gate_features(features, primary))
            return primary * max(0.0, 2.0 * p_correct.get(True, 0.5) - 1.0)
        return float(self._model.predict_one(features) or 0.0)

    @staticmethod
    def _gate_features(features: dict[str, float], primary: float) -> dict[str, float]:
        gf = dict(features)
        gf["primary_abs_bps"] = abs(primary) * 1e4
        gf["primary_sign"] = 1.0 if primary > 0 else -1.0
        return gf

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
        elif self.kind == "ev":
            for model in self._quantiles.values():
                model.learn_one(features, target * 1e4)  # learn in bps
        elif self.kind == "meta":
            # Gate label: would the CURRENT primary's sign have been right here?
            # (Standard online approximation — the primary drifts between the
            # prediction and its resolution, so we grade today's primary.)
            primary = float(self._model.predict_one(features) or 0.0)
            if primary != 0.0 and target != 0.0:
                self._gate.learn_one(
                    self._gate_features(features, primary), (primary > 0) == (target > 0)
                )
            self._model.learn_one(features, target)
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

"""Walk-forward quarterly loss guard using only released holding-window labels."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class QuarterlyLossGuardConfig:
    name: str
    loss_threshold: float = -0.03
    flag_quantile: float = 0.60
    exposure_cap: float = 0.0
    min_history_periods: int = 24
    min_loss_periods: int = 5
    max_features: int = 12
    minimum_coverage: float = 0.70


def _numeric_features(row: Mapping[str, Any]) -> dict[str, float]:
    output = {}
    for name, raw in row.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isfinite(value):
            output[str(name)] = value
    return output


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * q))),
    )
    return ordered[position]


class QuarterlyWalkForwardLossGuard:
    """Robust linear loss score fitted only to completed prior quarters."""

    def __init__(self, config: QuarterlyLossGuardConfig) -> None:
        self.config = config
        self._history: list[tuple[dict[str, float], float]] = []

    @property
    def history_count(self) -> int:
        return len(self._history)

    def observe_completed_period(
        self,
        features: Mapping[str, Any],
        risky_return: float,
    ) -> None:
        if math.isfinite(float(risky_return)):
            self._history.append((_numeric_features(features), float(risky_return)))

    def _model(self) -> dict[str, Any] | None:
        if len(self._history) < self.config.min_history_periods:
            return None
        loss = [item for item in self._history if item[1] <= self.config.loss_threshold]
        ok = [item for item in self._history if item[1] > self.config.loss_threshold]
        if len(loss) < self.config.min_loss_periods or len(ok) < self.config.min_loss_periods:
            return None
        names = sorted({name for features, _outcome in self._history for name in features})
        specs = []
        required = max(
            self.config.min_history_periods,
            int(math.ceil(len(self._history) * self.config.minimum_coverage)),
        )
        for name in names:
            values = [features[name] for features, _outcome in self._history if name in features]
            loss_values = [features[name] for features, _outcome in loss if name in features]
            ok_values = [features[name] for features, _outcome in ok if name in features]
            if len(values) < required or len(loss_values) < 3 or len(ok_values) < 3:
                continue
            center = statistics.median(values)
            scale = max(statistics.pstdev(values), 1e-9)
            weight = (statistics.median(loss_values) - statistics.median(ok_values)) / scale
            if abs(weight) < 0.05:
                continue
            specs.append(
                {
                    "feature": name,
                    "center": center,
                    "scale": scale,
                    "weight": weight,
                    "strength": abs(weight) * len(values) / len(self._history),
                }
            )
        specs.sort(key=lambda item: (-item["strength"], item["feature"]))
        specs = specs[: self.config.max_features]
        if not specs:
            return None
        scores = [self._score(features, specs) for features, _outcome in self._history]
        return {
            "features": specs,
            "threshold": _quantile(scores, self.config.flag_quantile),
            "loss_count": len(loss),
        }

    @staticmethod
    def _score(features: Mapping[str, float], specs: list[dict[str, Any]]) -> float:
        score = 0.0
        used = 0
        for spec in specs:
            value = features.get(str(spec["feature"]))
            if value is None:
                continue
            z_score = max(-5.0, min(5.0, (value - spec["center"]) / spec["scale"]))
            score += spec["weight"] * z_score
            used += 1
        return score / math.sqrt(used) if used else 0.0

    def decision(self, features: Mapping[str, Any]) -> dict[str, Any]:
        model = self._model()
        if model is None:
            return {"flagged": False, "mode": "warmup", "history_count": len(self._history)}
        clean = _numeric_features(features)
        score = self._score(clean, model["features"])
        return {
            "flagged": score >= model["threshold"],
            "mode": "online",
            "history_count": len(self._history),
            "loss_count": model["loss_count"],
            "score": score,
            "threshold": model["threshold"],
            "features": [item["feature"] for item in model["features"]],
        }

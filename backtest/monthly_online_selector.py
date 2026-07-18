"""Strict point-in-time online feature weighting for monthly CSI selection."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

import numpy as np

from backtest.csi_snapshot_selector import percentile_ranks


ONLINE_SELECTOR_FEATURES = (
    "trend_6m",
    "momentum_3m",
    "momentum_6m",
    "drawdown_12m",
    "volatility_3m",
    "pe_ttm_history_percentile_3y",
    "pb_history_percentile_3y",
    "turnover_acceleration_1m_6m",
    "risk_adjusted_trend_6m",
)


@dataclass(frozen=True)
class OnlineSelectorConfig:
    name: str = "online_ic60_top10"
    top_n: int = 10
    min_history_months: int = 36
    history_months: int = 60
    min_abs_median_ic: float = 0.03
    min_direction_consistency: float = 0.55
    features: tuple[str, ...] = ONLINE_SELECTOR_FEATURES


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = rank
        cursor = end
    return ranks


def rank_correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    ranked_x = _average_ranks(xs)
    ranked_y = _average_ranks(ys)
    mean_x = statistics.mean(ranked_x)
    mean_y = statistics.mean(ranked_y)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(ranked_x, ranked_y))
    denominator_x = sum((x - mean_x) ** 2 for x in ranked_x)
    denominator_y = sum((y - mean_y) ** 2 for y in ranked_y)
    denominator = (denominator_x * denominator_y) ** 0.5
    return numerator / denominator if denominator > 0 else None


def cross_section_ics(
    rows: Sequence[dict[str, Any]],
    outcomes: dict[str, float],
    features: Sequence[str],
) -> dict[str, float]:
    output = {}
    for feature in features:
        usable = [
            row
            for row in rows
            if row.get(feature) is not None and row["ts_code"] in outcomes
        ]
        correlation = rank_correlation(
            [float(row[feature]) for row in usable],
            [float(outcomes[row["ts_code"]]) for row in usable],
        )
        if correlation is not None:
            output[feature] = correlation
    return output


class OnlineCrossSectionSelector:
    """Release cross-sectional labels only after their execution period ends."""

    def __init__(self, config: OnlineSelectorConfig) -> None:
        self.config = config
        self._history: dict[str, list[float]] = {
            feature: [] for feature in config.features
        }
        self._pending: list[tuple[date, dict[str, float]]] = []

    def queue_observation(
        self,
        end_exec: date,
        rows: Sequence[dict[str, Any]],
        outcomes: dict[str, float],
    ) -> None:
        self._pending.append(
            (end_exec, cross_section_ics(rows, outcomes, self.config.features))
        )
        self._pending.sort(key=lambda item: item[0])

    def release_known(self, snapshot: date) -> int:
        known = [item for item in self._pending if item[0] <= snapshot]
        self._pending = [item for item in self._pending if item[0] > snapshot]
        for _end_exec, values in known:
            for feature, value in values.items():
                self._history[feature].append(value)
        return len(known)

    def learned_weights(self) -> dict[str, dict[str, float]]:
        learned = {}
        for feature, full_history in self._history.items():
            history = full_history[-self.config.history_months :]
            if len(history) < self.config.min_history_months:
                continue
            median_ic = statistics.median(history)
            if abs(median_ic) < self.config.min_abs_median_ic:
                continue
            orientation = 1.0 if median_ic > 0 else -1.0
            consistency = sum(value * orientation > 0 for value in history) / len(history)
            if consistency < self.config.min_direction_consistency:
                continue
            reliability = abs(median_ic) * max(2.0 * consistency - 1.0, 0.0)
            if reliability <= 0.0:
                continue
            learned[feature] = {
                "orientation": orientation,
                "median_ic": median_ic,
                "consistency": consistency,
                "weight": reliability,
                "history_count": float(len(history)),
            }
        return learned

    def select(
        self,
        rows: Sequence[dict[str, Any]],
        fallback: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        learned = self.learned_weights()
        if not learned:
            return [dict(row) for row in fallback], {
                "mode": "fallback",
                "learned_features": {},
            }
        rank_maps = {
            feature: percentile_ranks(
                {
                    str(row["ts_code"]): float(row[feature])
                    for row in rows
                    if row.get(feature) is not None
                }
            )
            for feature in learned
        }
        scored = []
        total_weight = sum(item["weight"] for item in learned.values())
        for source in rows:
            row = dict(source)
            score = 0.0
            for feature, item in learned.items():
                rank = rank_maps[feature].get(str(row["ts_code"]), 0.5)
                aligned_rank = rank if item["orientation"] > 0 else 1.0 - rank
                score += item["weight"] * aligned_rank
            row["online_selector_score"] = score / total_weight
            scored.append(row)
        selected = sorted(
            scored,
            key=lambda row: (-row["online_selector_score"], row["ts_code"]),
        )[: self.config.top_n]
        score_sum = sum(max(float(row["online_selector_score"]), 0.01) for row in selected)
        for row in selected:
            row["weight"] = max(float(row["online_selector_score"]), 0.01) / score_sum
        return selected, {
            "mode": "online",
            "learned_features": learned,
        }


@dataclass(frozen=True)
class OnlineRidgeSelectorConfig:
    name: str = "online_ridge20q_top5"
    top_n: int = 5
    min_history_periods: int = 8
    history_periods: int = 20
    ridge_alpha: float = 2.0
    features: tuple[str, ...] = ONLINE_SELECTOR_FEATURES


class OnlineRidgeCrossSectionSelector:
    """Walk-forward ridge model on point-in-time cross-sectional ranks."""

    def __init__(self, config: OnlineRidgeSelectorConfig) -> None:
        self.config = config
        self._history: list[tuple[np.ndarray, np.ndarray]] = []
        self._pending: list[tuple[date, np.ndarray, np.ndarray]] = []

    def _matrix(self, rows: Sequence[dict[str, Any]]) -> np.ndarray:
        rank_maps = {
            feature: percentile_ranks(
                {
                    str(row["ts_code"]): float(row[feature])
                    for row in rows
                    if row.get(feature) is not None
                }
            )
            for feature in self.config.features
        }
        matrix = np.asarray(
            [
                [
                    2.0 * rank_maps[feature].get(str(row["ts_code"]), 0.5) - 1.0
                    for feature in self.config.features
                ]
                for row in rows
            ],
            dtype=float,
        )
        return np.clip(
            np.nan_to_num(matrix, nan=0.0, posinf=1.0, neginf=-1.0),
            -1.0,
            1.0,
        )

    def queue_observation(
        self,
        end_exec: date,
        rows: Sequence[dict[str, Any]],
        outcomes: dict[str, float],
    ) -> None:
        usable = [row for row in rows if str(row["ts_code"]) in outcomes]
        if len(usable) < 5:
            return
        outcome_ranks = percentile_ranks(
            {str(row["ts_code"]): float(outcomes[str(row["ts_code"])]) for row in usable}
        )
        x = self._matrix(usable)
        y = np.asarray(
            [2.0 * outcome_ranks[str(row["ts_code"])] - 1.0 for row in usable],
            dtype=float,
        )
        y = np.clip(np.nan_to_num(y, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
        scale = max(len(usable), 1) ** -0.5
        self._pending.append((end_exec, x * scale, y * scale))
        self._pending.sort(key=lambda item: item[0])

    def release_known(self, snapshot: date) -> int:
        known = [item for item in self._pending if item[0] <= snapshot]
        self._pending = [item for item in self._pending if item[0] > snapshot]
        self._history.extend((x, y) for _end, x, y in known)
        return len(known)

    def coefficients(self) -> np.ndarray | None:
        history = self._history[-self.config.history_periods :]
        if len(history) < self.config.min_history_periods:
            return None
        x = np.clip(
            np.nan_to_num(np.vstack([item[0] for item in history]), nan=0.0),
            -1.0,
            1.0,
        )
        y = np.clip(
            np.nan_to_num(np.concatenate([item[1] for item in history]), nan=0.0),
            -1.0,
            1.0,
        )
        penalty = np.eye(x.shape[1], dtype=float) * self.config.ridge_alpha
        try:
            gram = np.einsum("ni,nj->ij", x, x, optimize=False)
            rhs = np.einsum("ni,n->i", x, y, optimize=False)
            coefficients = np.linalg.solve(gram + penalty, rhs)
            return coefficients if np.all(np.isfinite(coefficients)) else None
        except np.linalg.LinAlgError:
            return None

    def select(
        self,
        rows: Sequence[dict[str, Any]],
        fallback: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        coefficients = self.coefficients()
        if coefficients is None or len(rows) < 5:
            return [dict(row) for row in fallback], {
                "mode": "fallback",
                "history_periods": len(self._history),
            }
        matrix = self._matrix(rows)
        scores = np.nan_to_num(
            np.sum(matrix * coefficients.reshape(1, -1), axis=1),
            nan=0.0,
            posinf=1.0,
            neginf=-1.0,
        )
        scored = []
        for source, score in zip(rows, scores):
            row = dict(source)
            row["online_ridge_score"] = float(score)
            scored.append(row)
        selected = sorted(
            scored,
            key=lambda row: (-float(row["online_ridge_score"]), str(row["ts_code"])),
        )[: self.config.top_n]
        shifted = {
            str(row["ts_code"]): max(float(row["online_ridge_score"]) - min(scores) + 0.01, 0.01)
            for row in selected
        }
        total = sum(shifted.values())
        for row in selected:
            row["weight"] = shifted[str(row["ts_code"])] / total
        return selected, {
            "mode": "online_ridge",
            "history_periods": len(self._history),
            "coefficients": {
                feature: float(value)
                for feature, value in zip(self.config.features, coefficients)
            },
        }

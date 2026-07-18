"""Auditable annual walk-forward boosted rank selector for passive ETFs."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

import numpy as np

from backtest.passive_etf_supervised_selector import _matrix, _percentile_column


ALL_PRICE_FEATURES = (
    "momentum_1m",
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "momentum_12m_skip1m",
    "relative_strength_3m",
    "relative_strength_6m",
    "residual_momentum_6m",
    "trend_1m_3m_consistency",
    "distance_high_12m",
    "volatility_1m",
    "volatility_3m",
    "volatility_6m",
    "downside_volatility_3m",
    "drawdown_3m",
    "drawdown_6m",
    "max_drawdown_6m",
    "positive_day_ratio_3m",
    "market_beta_6m",
    "market_correlation_6m",
    "return_autocorrelation_3m",
    "ulcer_index_6m",
)


@dataclass(frozen=True)
class BoostedEtfPolicy:
    name: str
    features: tuple[str, ...]
    history_periods: int
    top_fraction: float
    drawdown_penalty: float
    estimators: int
    top_n: int


BOOSTED_ETF_POLICIES = (
    BoostedEtfPolicy(
        "direct_boost_rank_all_price_h120_q20_dd2_e16_top3",
        ALL_PRICE_FEATURES,
        120,
        0.20,
        2.0,
        16,
        3,
    ),
)

_SELECTION_CACHE: dict[tuple[str, date, int], dict[str, float]] = {}
_MODEL_CACHE: dict[tuple[str, int, int], list[dict[str, float | int]] | None] = {}


def policy_by_name(name: str) -> BoostedEtfPolicy:
    base_name = name
    if name.startswith("blend_index_boost_rank_"):
        base_name = "direct_" + name.removeprefix("blend_index_").rsplit("_w", 1)[0]
    return next(policy for policy in BOOSTED_ETF_POLICIES if policy.name == base_name)


def _balanced_weights(labels: np.ndarray) -> np.ndarray:
    positive = labels == 1
    negative = ~positive
    weights = np.zeros(len(labels), dtype=float)
    weights[positive] = 0.5 / max(int(positive.sum()), 1)
    weights[negative] = 0.5 / max(int(negative.sum()), 1)
    return weights / weights.sum()


def _fit_model(
    grouped: dict[date, list[dict[str, Any]]],
    cutoff: date,
    policy: BoostedEtfPolicy,
) -> list[dict[str, float | int]] | None:
    known_dates = sorted(
        day
        for day, rows in grouped.items()
        if date.fromisoformat(str(rows[0]["end_snapshot"])) < cutoff
    )[-policy.history_periods :]
    if len(known_dates) < min(16, policy.history_periods):
        return None
    matrices = []
    targets = []
    for day in known_dates:
        rows = grouped[day]
        matrices.append(_matrix(rows, policy.features))
        utility = [
            float(row["forward_return_3m"])
            + policy.drawdown_penalty * float(row["forward_max_drawdown_3m"])
            for row in rows
        ]
        targets.append(_percentile_column(utility))
    x = np.vstack(matrices)
    rank_target = np.concatenate(targets)
    labels = np.where(
        rank_target >= 1.0 - 2.0 * policy.top_fraction, 1, -1
    ).astype(np.int8)
    weights = _balanced_weights(labels)
    available = []
    for feature in range(x.shape[1]):
        valid = np.isfinite(x[:, feature])
        for threshold in (-0.75, -0.50, -0.25, 0.0, 0.25, 0.50, 0.75):
            for polarity in (-1, 1):
                predictions = np.where(
                    valid,
                    np.where(x[:, feature] >= threshold, polarity, -polarity),
                    -1,
                ).astype(np.int8)
                available.append((feature, threshold, polarity, predictions))
    stumps: list[dict[str, float | int]] = []
    for _ in range(policy.estimators):
        best_index = -1
        best_error = 1.0
        for index, (_feature, _threshold, _polarity, predictions) in enumerate(available):
            error = float(weights[predictions != labels].sum())
            if error < best_error:
                best_error = error
                best_index = index
        if best_index < 0 or best_error >= 0.495:
            break
        feature, threshold, polarity, predictions = available.pop(best_index)
        error = min(0.495, max(0.005, best_error))
        alpha = 0.65 * 0.5 * math.log((1.0 - error) / error)
        stumps.append(
            {
                "feature": feature,
                "threshold": threshold,
                "polarity": polarity,
                "alpha": alpha,
            }
        )
        weights *= np.exp(-alpha * labels * predictions)
        weights /= weights.sum()
    return stumps or None


def _score(matrix: np.ndarray, stumps: list[dict[str, float | int]]) -> np.ndarray:
    output = np.zeros(matrix.shape[0], dtype=float)
    for stump in stumps:
        values = matrix[:, int(stump["feature"])]
        polarity = int(stump["polarity"])
        prediction = np.where(
            np.isfinite(values),
            np.where(values >= float(stump["threshold"]), polarity, -polarity),
            -1,
        )
        output += float(stump["alpha"]) * prediction
    return output


def _fallback_scores(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    beta = _percentile_column([row.get("market_beta_6m") for row in rows])
    volatility = _percentile_column([row.get("volatility_1m") for row in rows])
    distance = _percentile_column([row.get("distance_high_12m") for row in rows])
    return -0.35 * beta - 0.30 * volatility + 0.35 * distance


def select_boosted_etfs(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    policy: BoostedEtfPolicy,
) -> dict[str, float]:
    """Select at a quarter boundary using a model frozen at that year's start."""

    cache_key = (policy.name, snapshot, id(observations))
    if cache_key in _SELECTION_CACHE:
        return dict(_SELECTION_CACHE[cache_key])
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)
    current = grouped.get(snapshot, [])
    if not current:
        _SELECTION_CACHE[cache_key] = {}
        return {}
    cutoff = date(snapshot.year, 1, 1)
    model_key = (policy.name, snapshot.year, id(observations))
    if model_key not in _MODEL_CACHE:
        _MODEL_CACHE[model_key] = _fit_model(grouped, cutoff, policy)
    model = _MODEL_CACHE[model_key]
    matrix = _matrix(current, policy.features)
    scores = _fallback_scores(current) if model is None else _score(matrix, model)
    order = sorted(
        range(len(current)),
        key=lambda index: (-float(scores[index]), str(current[index]["ts_code"])),
    )[: policy.top_n]
    selected = np.asarray([float(scores[index]) for index in order])
    shifted = selected - min(float(selected.min()), 0.0) + 0.10
    total = float(shifted.sum())
    weights = {
        str(current[index]["ts_code"]): float(weight) / total
        for index, weight in zip(order, shifted)
    }
    _SELECTION_CACHE[cache_key] = dict(weights)
    return weights

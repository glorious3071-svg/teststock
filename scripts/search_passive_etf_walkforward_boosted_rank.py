#!/usr/bin/env python3
"""Screen an auditable walk-forward boosted rank selector for passive ETFs.

Models are refit once per calendar year.  A year's model can use only quarterly
labels whose ``end_snapshot`` is strictly before January 1 of that year.  All
features are converted to point-in-time cross-sectional ranks before fitting.
The weak learners are one-feature threshold stumps, keeping every decision
inspectable and avoiding an external ML runtime dependency.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.search_passive_etf_walkforward_ridge import (
    DATASET,
    cross_section_matrix,
    fallback_scores,
    path_summary,
    percentile_vector,
)


OUTPUT = ROOT / "data/backtests/passive_etf_walkforward_boosted_rank_report.json"

FEATURE_SETS = {
    "stable": (
        "market_beta_6m",
        "volatility_1m",
        "distance_high_12m",
        "log_amount_1m",
        "volatility_3m",
        "volatility_6m",
        "max_drawdown_6m",
        "downside_volatility_3m",
        "momentum_12m_skip1m",
        "momentum_12m",
        "return_autocorrelation_3m",
    ),
    "tail": (
        "market_beta_6m",
        "distance_high_12m",
        "volatility_1m",
        "volatility_3m",
        "volatility_6m",
        "downside_volatility_3m",
        "max_drawdown_6m",
        "historical_var_5pct_3m",
        "historical_cvar_5pct_3m",
        "historical_var_5pct_6m",
        "historical_cvar_5pct_6m",
        "maximum_daily_loss_3m",
        "negative_day_ratio_3m",
        "ulcer_index_6m",
        "volatility_acceleration_1m_3m",
        "return_autocorrelation_3m",
    ),
    "all_price": (
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
    ),
}


@dataclass(frozen=True)
class BoostedRankPolicy:
    name: str
    feature_set: str
    history_periods: int
    top_fraction: float
    drawdown_penalty: float
    estimators: int
    top_n: int


def prepare(payload: dict[str, Any], feature_set: str) -> list[dict[str, Any]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in payload["candidate_observations"]:
        grouped.setdefault(date.fromisoformat(str(row["snapshot"])), []).append(row)
    features = FEATURE_SETS[feature_set]
    output = []
    for snapshot, rows in sorted(grouped.items()):
        utility = [
            float(row["forward_return_3m"])
            for row in rows
        ]
        output.append(
            {
                "snapshot": snapshot,
                "end_snapshot": date.fromisoformat(str(rows[0]["end_snapshot"])),
                "rows": rows,
                "matrix": cross_section_matrix(rows, features),
                "return_rank": percentile_vector(utility),
            }
        )
    return output


def balanced_weights(labels: np.ndarray) -> np.ndarray:
    positive = labels == 1
    negative = ~positive
    weights = np.zeros(len(labels), dtype=float)
    weights[positive] = 0.5 / max(int(positive.sum()), 1)
    weights[negative] = 0.5 / max(int(negative.sum()), 1)
    return weights / weights.sum()


def fit_model(
    training: list[dict[str, Any]], policy: BoostedRankPolicy
) -> list[dict[str, float | int]] | None:
    if len(training) < min(16, policy.history_periods):
        return None
    matrices = []
    utilities = []
    for item in training[-policy.history_periods :]:
        rows = item["rows"]
        utility = [
            float(row["forward_return_3m"])
            + policy.drawdown_penalty * float(row["forward_max_drawdown_3m"])
            for row in rows
        ]
        matrices.append(item["matrix"])
        utilities.append(percentile_vector(utility))
    x = np.vstack(matrices)
    y_rank = np.concatenate(utilities)
    cutoff = 1.0 - 2.0 * policy.top_fraction
    labels = np.where(y_rank >= cutoff, 1, -1).astype(np.int8)
    weights = balanced_weights(labels)
    candidates = []
    for feature in range(x.shape[1]):
        valid = np.isfinite(x[:, feature])
        for threshold in (-0.75, -0.50, -0.25, 0.0, 0.25, 0.50, 0.75):
            for polarity in (-1, 1):
                predictions = np.where(
                    valid,
                    np.where(x[:, feature] >= threshold, polarity, -polarity),
                    -1,
                ).astype(np.int8)
                candidates.append((feature, threshold, polarity, predictions))
    stumps: list[dict[str, float | int]] = []
    available = list(candidates)
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


def score(matrix: np.ndarray, stumps: list[dict[str, float | int]]) -> np.ndarray:
    scores = np.zeros(matrix.shape[0], dtype=float)
    for stump in stumps:
        values = matrix[:, int(stump["feature"])]
        polarity = int(stump["polarity"])
        predictions = np.where(
            np.isfinite(values),
            np.where(values >= float(stump["threshold"]), polarity, -polarity),
            -1,
        )
        scores += float(stump["alpha"]) * predictions
    return scores


def evaluate(prepared: list[dict[str, Any]], policy: BoostedRankPolicy) -> dict[str, Any]:
    models: dict[int, list[dict[str, float | int]] | None] = {}
    for year in sorted({item["snapshot"].year for item in prepared}):
        cutoff = date(year, 1, 1)
        known = [item for item in prepared if item["end_snapshot"] < cutoff]
        models[year] = fit_model(known, policy)
    predictions = []
    online_count = 0
    for item in prepared:
        model = models[item["snapshot"].year]
        scores = fallback_scores(item["rows"]) if model is None else score(item["matrix"], model)
        online_count += int(model is not None)
        order = sorted(
            range(len(item["rows"])),
            key=lambda index: (-float(scores[index]), str(item["rows"][index]["ts_code"])),
        )[: policy.top_n]
        selected = np.asarray([float(scores[index]) for index in order])
        shifted = selected - min(float(selected.min()), 0.0) + 0.10
        weights = shifted / shifted.sum()
        predictions.append(
            {
                "snapshot": item["snapshot"].isoformat(),
                "basket_return": sum(
                    float(weight) * float(item["rows"][index]["forward_return_3m"])
                    for weight, index in zip(weights, order)
                ),
                "basket_average_drawdown": sum(
                    float(weight) * float(item["rows"][index]["forward_max_drawdown_3m"])
                    for weight, index in zip(weights, order)
                ),
            }
        )
    summary = path_summary(predictions)
    summary["online_snapshot_count"] = online_count
    return {"policy": asdict(policy), "summary": summary}


def policies(feature_sets: list[str]) -> list[BoostedRankPolicy]:
    output = []
    for feature_set in feature_sets:
        for history in (60, 120):
            for top_fraction in (0.20, 0.30):
                for penalty in (0.0, 1.0, 2.0):
                    for estimators in (8, 16):
                        for top_n in (1, 3):
                            output.append(
                                BoostedRankPolicy(
                                    f"boost_rank_{feature_set}_h{history}_q{int(top_fraction*100)}_dd{penalty:g}_e{estimators}_top{top_n}",
                                    feature_set,
                                    history,
                                    top_fraction,
                                    penalty,
                                    estimators,
                                    top_n,
                                )
                            )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--feature-set", action="append", choices=sorted(FEATURE_SETS))
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    selected = args.feature_set or list(FEATURE_SETS)
    prepared = {name: prepare(payload, name) for name in selected}
    results = [evaluate(prepared[policy.feature_set], policy) for policy in policies(selected)]
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": "annual walk-forward boosted cross-sectional ranks with strict label release",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<58} min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

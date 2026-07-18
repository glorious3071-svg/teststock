#!/usr/bin/env python3
"""Screen point-in-time walk-forward ridge selectors on passive ETF labels.

This is a research screen, not the production backtest.  Every coefficient at
snapshot T is fit only with three-month labels whose ``end_snapshot <= T``.
Candidates are ranked within each historical cross-section so changes in ETF
count and feature scale cannot leak later-universe information into early eras.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.monthly_online_selector import _average_ranks
from backtest.phase_schedule import shift_month_end


DATASET = ROOT / "data/backtests/passive_etf_quarterly_supervised_dataset.json"
OUTPUT = ROOT / "data/backtests/passive_etf_walkforward_ridge_screen_report.json"

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
    ),
    "price_risk": (
        "momentum_1m",
        "momentum_3m",
        "momentum_6m",
        "momentum_12m",
        "momentum_12m_skip1m",
        "relative_strength_3m",
        "relative_strength_6m",
        "residual_momentum_6m",
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
    ),
    "index_stable": (
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
        "index_turnover_acceleration_1m_6m",
        "index_trend_acceleration_3m_vs_6m",
        "index_etf_positive_turnover_pressure_1m",
        "index_etf_amount_crowding_percentile_3y",
    ),
}


@dataclass(frozen=True)
class RidgePolicy:
    name: str
    feature_set: str
    history_periods: int
    alpha: float
    top_n: int
    drawdown_penalty: float
    same_regime_only: bool = False
    basis: str = "linear"


def expand_basis(matrix: np.ndarray, basis: str) -> np.ndarray:
    if basis == "linear":
        return matrix
    if basis == "quadratic":
        return np.column_stack([matrix, matrix * matrix])
    if basis == "hinge":
        return np.column_stack(
            [
                matrix,
                matrix * matrix,
                np.maximum(matrix, 0.0),
                np.maximum(-matrix, 0.0),
            ]
        )
    raise ValueError(f"unknown basis: {basis}")


def percentile_vector(values: list[float | None]) -> np.ndarray:
    usable = [(index, float(value)) for index, value in enumerate(values) if value is not None]
    output = np.zeros(len(values), dtype=float)
    if len(usable) <= 1:
        return output
    ranks = _average_ranks([value for _index, value in usable])
    denominator = len(usable) - 1
    for (index, _value), rank in zip(usable, ranks):
        output[index] = 2.0 * rank / denominator - 1.0
    return output


def cross_section_matrix(rows: list[dict[str, Any]], features: tuple[str, ...]) -> np.ndarray:
    columns = [percentile_vector([row.get(feature) for row in rows]) for feature in features]
    return np.column_stack(columns)


def ranked_target(rows: list[dict[str, Any]], drawdown_penalty: float) -> np.ndarray:
    utility = [
        float(row["forward_return_3m"])
        + drawdown_penalty * float(row["forward_max_drawdown_3m"])
        for row in rows
    ]
    return percentile_vector(utility)


def fit_coefficients(
    history: list[tuple[date, str, np.ndarray, np.ndarray]],
    snapshot: date,
    regime: str,
    policy: RidgePolicy,
) -> tuple[np.ndarray | None, int]:
    known = [item for item in history if item[0] <= snapshot]
    if policy.same_regime_only:
        known = [item for item in known if item[1] == regime]
    known = known[-policy.history_periods :]
    if len(known) < min(12, policy.history_periods):
        return None, len(known)
    xs = []
    ys = []
    for _end, _regime, matrix, target in known:
        scale = max(matrix.shape[0], 1) ** -0.5
        xs.append(matrix * scale)
        ys.append(target * scale)
    x = np.vstack(xs)
    y = np.concatenate(ys)
    try:
        # Some Accelerate-backed NumPy builds emit stale floating-point status
        # warnings for these finite, bounded matrices; validate the result
        # explicitly instead of surfacing those irrelevant warnings.
        with np.errstate(all="ignore"):
            gram = x.T @ x + np.eye(x.shape[1]) * policy.alpha
            coefficients = np.linalg.solve(gram, x.T @ y)
    except np.linalg.LinAlgError:
        return None, len(known)
    return (coefficients if np.all(np.isfinite(coefficients)) else None), len(known)


def fallback_scores(rows: list[dict[str, Any]]) -> np.ndarray:
    """Use the stable all-era directions until enough labels are known."""

    beta = percentile_vector([row.get("market_beta_6m") for row in rows])
    vol = percentile_vector([row.get("volatility_1m") for row in rows])
    distance = percentile_vector([row.get("distance_high_12m") for row in rows])
    return -0.35 * beta - 0.30 * vol + 0.35 * distance


def path_summary(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the strict engine's fixed phase anchors, not the first data rows.

    Feature coverage starts after the backtest anchor.  Shifting phase starts
    to the first twelve available rows omits the binding early phases and
    admits later windows.  Missing early observations are therefore neutral
    and explicit; they never move the calendar.
    """

    by_snapshot = {str(row["snapshot"]): row for row in predictions}
    strict_anchor = date(2005, 2, 28)
    cases = []
    for phase in range(12):
        start_date = shift_month_end(strict_anchor, phase)
        anchors = [shift_month_end(start_date, quarter * 3) for quarter in range(80)]
        rows = [
            by_snapshot[anchor.isoformat()]
            for anchor in anchors
            if anchor.isoformat() in by_snapshot
        ]
        missing = [
            anchor.isoformat()
            for anchor in anchors
            if anchor.isoformat() not in by_snapshot
        ]
        factor = math.prod(1.0 + float(row["basket_return"]) for row in rows)
        cases.append(
            {
                "phase_month_offset": phase,
                "start_snapshot": start_date.isoformat(),
                "end_snapshot": shift_month_end(start_date, 240).isoformat(),
                "anchor_count": len(anchors),
                "observed_period_count": len(rows),
                "missing_snapshot_count": len(missing),
                "missing_snapshots": missing,
                "capital_factor": factor,
                "minimum_average_constituent_drawdown": min(
                    (float(row["basket_average_drawdown"]) for row in rows),
                    default=0.0,
                ),
            }
        )
    complete = [case for case in cases if case["missing_snapshot_count"] == 0]
    return {
        "case_count": len(cases),
        "complete_20y_case_count": len(complete),
        "strict_anchor": strict_anchor.isoformat(),
        "min_capital_factor": min(case["capital_factor"] for case in cases),
        "median_capital_factor": statistics.median(
            case["capital_factor"] for case in cases
        ),
        "min_period_count": min(case["observed_period_count"] for case in cases),
        "worst_average_constituent_drawdown": min(
            case["minimum_average_constituent_drawdown"] for case in cases
        ),
        "cases": cases,
    }


def evaluate(
    grouped: list[tuple[date, list[dict[str, Any]]]],
    policy: RidgePolicy,
) -> dict[str, Any]:
    features = FEATURE_SETS[policy.feature_set]
    history: list[tuple[date, str, np.ndarray, np.ndarray]] = []
    prepared = []
    for snapshot, rows in grouped:
        matrix = expand_basis(cross_section_matrix(rows, features), policy.basis)
        target = ranked_target(rows, policy.drawdown_penalty)
        end_snapshot = date.fromisoformat(str(rows[0]["end_snapshot"]))
        regime = str(rows[0]["market_regime"])
        prepared.append((snapshot, end_snapshot, regime, rows, matrix, target))
        history.append((end_snapshot, regime, matrix, target))

    predictions = []
    online_count = 0
    for snapshot, _end, regime, rows, matrix, _target in prepared:
        coefficients, history_count = fit_coefficients(
            history, snapshot, regime, policy
        )
        if coefficients is None:
            scores = fallback_scores(rows)
            mode = "fallback"
        else:
            scores = matrix @ coefficients
            mode = "ridge"
            online_count += 1
        order = sorted(
            range(len(rows)),
            key=lambda index: (-float(scores[index]), str(rows[index]["ts_code"])),
        )[: policy.top_n]
        selected_scores = np.asarray([float(scores[index]) for index in order])
        shifted = selected_scores - min(float(np.min(selected_scores)), 0.0) + 0.10
        weights = shifted / shifted.sum()
        predictions.append(
            {
                "snapshot": snapshot.isoformat(),
                "mode": mode,
                "history_count": history_count,
                "codes": [str(rows[index]["ts_code"]) for index in order],
                "basket_return": sum(
                    float(weight) * float(rows[index]["forward_return_3m"])
                    for weight, index in zip(weights, order)
                ),
                "basket_average_drawdown": sum(
                    float(weight) * float(rows[index]["forward_max_drawdown_3m"])
                    for weight, index in zip(weights, order)
                ),
            }
        )
    summary = path_summary(predictions)
    summary["online_snapshot_count"] = online_count
    return {"policy": asdict(policy), "summary": summary}


def policies(
    selected_feature_sets: set[str] | None = None,
    selected_bases: tuple[str, ...] = ("linear", "quadratic", "hinge"),
) -> Iterable[RidgePolicy]:
    for feature_set in FEATURE_SETS:
        if selected_feature_sets and feature_set not in selected_feature_sets:
            continue
        for history in (24, 60, 120):
            for alpha in (0.5, 2.0, 10.0):
                for top_n in (1, 3, 5):
                    for penalty in (0.0, 0.5, 1.0, 2.0):
                        for basis in selected_bases:
                            yield RidgePolicy(
                                f"ridge_{feature_set}_{basis}_h{history}_a{alpha:g}_top{top_n}_dd{penalty:g}",
                                feature_set,
                                history,
                                alpha,
                                top_n,
                                penalty,
                                basis=basis,
                            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--feature-set", action="append", choices=sorted(FEATURE_SETS))
    parser.add_argument("--basis", action="append", choices=("linear", "quadratic", "hinge"))
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    by_snapshot: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in payload["candidate_observations"]:
        by_snapshot[date.fromisoformat(str(row["snapshot"]))].append(row)
    grouped = sorted(by_snapshot.items())
    results = [
        evaluate(grouped, policy)
        for policy in policies(
            set(args.feature_set or []),
            tuple(args.basis or ("linear", "quadratic", "hinge")),
        )
    ]
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
            item["summary"]["worst_average_constituent_drawdown"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": "strict end_snapshot release, cross-sectional ranks, quarterly paths",
                "candidate_count": len(payload["candidate_observations"]),
                "snapshot_count": len(grouped),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<52} "
            f"min={summary['min_capital_factor']:8.2f}x "
            f"median={summary['median_capital_factor']:8.2f}x "
            f"windows={summary['min_period_count']:>2} "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

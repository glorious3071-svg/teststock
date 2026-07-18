#!/usr/bin/env python3
"""Compare V9 with a fixed scorecard built from audited stable feature groups.

This is deliberately not a parameter search.  The candidate score has two
equal-weight information groups: realized ETF risk and tracked-index
crowding/expectations.  Every admitted feature has the same IC direction in
all three historical eras in the constituent-V4 audit.  Missing values receive
a neutral rank and are reported explicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.passive_etf_supervised_selector import (  # noqa: E402
    CONSTITUENT_V4_DATASET,
    weighted_stable_combo_v9_scores,
)
from scripts.search_passive_etf_walkforward_ridge import path_summary  # noqa: E402


DEFAULT_OUTPUT = (
    ROOT / "data/backtests/passive_etf_strong_feature_scorecard_report.json"
)
RISK_GROUP = (
    ("volatility_6m", False),
    ("market_beta_6m", False),
    ("downside_volatility_3m", False),
    ("volatility_1m", False),
    ("ulcer_index_6m", False),
)
EXPECTATIONS_GROUP = (
    ("index_turnover_acceleration_1m_6m", False),
    ("index_trend_acceleration_geometric_3m_vs_6m", False),
    ("distance_high_12m", True),
    ("index_fundamental_roe_proxy", False),
)
V9_COMPONENTS = (
    ("market_beta_6m", False, 4.0),
    ("distance_high_12m", True, 1.0),
    ("return_autocorrelation_3m", False, 4.0),
    ("volatility_3m", False, 4.0),
    ("ulcer_index_6m", False, 1.0),
    ("index_fundamental_roe_proxy", False, 0.5),
    ("index_fundamental_book_growth_12m", False, 1.0),
    ("index_constituent_earnings_yield", False, 1.0),
    ("index_constituent_weight_hhi", False, 1.0),
)


def finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def rank(
    rows: list[dict[str, Any]], feature: str, higher_is_better: bool
) -> tuple[dict[str, float], float]:
    usable = sorted(
        (
            (str(row["ts_code"]), float(row[feature]))
            for row in rows
            if finite(row.get(feature))
        ),
        key=lambda item: (item[1], item[0]),
    )
    coverage = len(usable) / len(rows) if rows else 0.0
    if len(usable) <= 1:
        return ({str(row["ts_code"]): 0.5 for row in rows}, coverage)
    denominator = len(usable) - 1
    known = {code: index / denominator for index, (code, _value) in enumerate(usable)}
    values = {
        str(row["ts_code"]): known.get(str(row["ts_code"]), 0.5) for row in rows
    }
    if not higher_is_better:
        values = {code: 1.0 - value for code, value in values.items()}
    return values, coverage


def group_scores(
    rows: list[dict[str, Any]], components: tuple[tuple[str, bool], ...]
) -> tuple[dict[str, float], dict[str, float]]:
    ranked = {
        feature: rank(rows, feature, higher)
        for feature, higher in components
    }
    scores = {
        str(row["ts_code"]): sum(
            ranked[feature][0][str(row["ts_code"])] for feature, _higher in components
        )
        / len(components)
        for row in rows
    }
    coverage = {feature: ranked[feature][1] for feature, _higher in components}
    return scores, coverage


def weighted_component_scores(
    rows: list[dict[str, Any]],
    components: tuple[tuple[str, bool, float], ...],
) -> dict[str, float]:
    ranked = {
        feature: rank(rows, feature, higher)[0]
        for feature, higher, _weight in components
    }
    total_weight = sum(weight for _feature, _higher, weight in components)
    return {
        str(row["ts_code"]): sum(
            weight * ranked[feature][str(row["ts_code"])]
            for feature, _higher, weight in components
        )
        / total_weight
        for row in rows
    }


def selected_prediction(
    snapshot: date,
    rows: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    policy: str,
) -> dict[str, Any]:
    risk, risk_coverage = group_scores(rows, RISK_GROUP)
    expectations, expectations_coverage = group_scores(rows, EXPECTATIONS_GROUP)
    if policy == "v9":
        scores = weighted_stable_combo_v9_scores(observations, snapshot)
    elif policy == "v9_autocorr_to_vol6":
        scores = weighted_component_scores(
            rows,
            tuple(
                ("volatility_6m", False, weight)
                if feature == "return_autocorrelation_3m"
                else (feature, higher, weight)
                for feature, higher, weight in V9_COMPONENTS
            ),
        )
    elif policy == "v9_autocorr_to_downside3":
        scores = weighted_component_scores(
            rows,
            tuple(
                ("downside_volatility_3m", False, weight)
                if feature == "return_autocorrelation_3m"
                else (feature, higher, weight)
                for feature, higher, weight in V9_COMPONENTS
            ),
        )
    elif policy == "stable_risk_only":
        scores = risk
    elif policy == "stable_grouped":
        scores = {
            code: 0.5 * risk[code] + 0.5 * expectations[code] for code in risk
        }
    else:
        raise ValueError(f"unknown policy: {policy}")
    code = max(scores, key=lambda item: (round(float(scores[item]), 12), item))
    selected = next(row for row in rows if str(row["ts_code"]) == code)
    return {
        "snapshot": snapshot.isoformat(),
        "codes": [code],
        "basket_return": selected.get("forward_return_3m"),
        "basket_average_drawdown": selected.get("forward_max_drawdown_3m"),
        "score": float(scores[code]),
        "risk_score": float(risk[code]),
        "expectations_score": float(expectations[code]),
        "risk_feature_coverage": risk_coverage,
        "expectations_feature_coverage": expectations_coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=CONSTITUENT_V4_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    dataset = args.dataset if args.dataset.is_absolute() else ROOT / args.dataset
    output = args.output if args.output.is_absolute() else ROOT / args.output
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    observations = list(payload["candidate_observations"])
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if not finite(row.get("forward_return_3m")) or not finite(
            row.get("forward_max_drawdown_3m")
        ):
            continue
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)

    results = []
    for policy in (
        "v9",
        "v9_autocorr_to_vol6",
        "v9_autocorr_to_downside3",
        "stable_risk_only",
        "stable_grouped",
    ):
        predictions = [
            selected_prediction(snapshot, rows, observations, policy)
            for snapshot, rows in sorted(grouped.items())
        ]
        summary = path_summary(predictions)
        worst = sorted(
            predictions,
            key=lambda row: float(row["basket_return"]),
        )[:20]
        results.append(
            {
                "policy": policy,
                "summary": summary,
                "worst_quarters": worst,
                "predictions": predictions,
            }
        )

    report = {
        "method": "fixed stable-feature group scorecard with no weight search",
        "dataset": str(dataset),
        "risk_group": RISK_GROUP,
        "expectations_group": EXPECTATIONS_GROUP,
        "v9_components": V9_COMPONENTS,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for result in results:
        summary = result["summary"]
        print(
            f"{result['policy']:<20} min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"dd={summary['worst_average_constituent_drawdown'] * 100:6.2f}%"
        )
        for row in result["predictions"]:
            if row["snapshot"] in {"2018-01-31", "2018-09-30"}:
                print(
                    f"  {row['snapshot']} {row['codes'][0]} "
                    f"ret={float(row['basket_return']) * 100:+.2f}% "
                    f"dd={float(row['basket_average_drawdown']) * 100:+.2f}%"
                )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

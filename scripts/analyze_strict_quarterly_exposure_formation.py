#!/usr/bin/env python3
"""Summarize which sizing layer binds in a strict quarterly audit report."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def safe_mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    cases = [case for result in report["results"] for case in result["cases"]]
    stage_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "active_count": 0,
            "applied_count": 0,
            "increase_count": 0,
            "decrease_count": 0,
            "applied_positive_risk_quarters": 0,
            "applied_negative_risk_quarters": 0,
            "exposure_delta_sum": 0.0,
            "risk_return_effect_sum": 0.0,
        }
    )
    initial_bindings: Counter[str] = Counter()
    positive_initial_bindings: Counter[str] = Counter()
    negative_initial_bindings: Counter[str] = Counter()
    zero_exposure_reasons: Counter[str] = Counter()
    positive_zero_exposure_reasons: Counter[str] = Counter()
    base_weights: Counter[str] = Counter()
    scorecard_limits: list[float] = []
    cppi_limits: list[float] = []
    final_exposures: list[float] = []
    positive_exposures: list[float] = []
    negative_exposures: list[float] = []
    feature_cap_values: list[float] = []
    feature_cap_positive_returns: list[float] = []
    feature_cap_negative_returns: list[float] = []
    row_count = 0

    for case in cases:
        for row in case.get("decision_rows", []):
            row_count += 1
            formation = row["exposure_formation"]
            risk_return = float(row.get("realized_risk_return") or 0.0)
            final_exposure = float(formation["final_exposure"])
            final_exposures.append(final_exposure)
            scorecard_limits.append(float(formation["scorecard_limit"]))
            cppi_limits.append(float(formation["cppi_limit"]))
            base_weights[f'{float(formation["base_weight"]):.6f}'] += 1
            if risk_return > 0:
                positive_exposures.append(final_exposure)
            elif risk_return < 0:
                negative_exposures.append(final_exposure)

            bindings = formation["initial_binding_limits"]
            for binding in bindings:
                initial_bindings[binding] += 1
                if risk_return > 0:
                    positive_initial_bindings[binding] += 1
                elif risk_return < 0:
                    negative_initial_bindings[binding] += 1

            last_decrease = None
            for stage in formation["trace"]:
                if stage["stage"] == "initial_min":
                    continue
                stats = stage_stats[stage["stage"]]
                stats["active_count"] += int(bool(stage["active"]))
                stats["applied_count"] += int(bool(stage["applied"]))
                if not stage["applied"]:
                    continue
                before = float(stage["before"])
                after = float(stage["after"])
                delta = after - before
                stats["exposure_delta_sum"] += delta
                stats["risk_return_effect_sum"] += delta * risk_return
                if delta > 0:
                    stats["increase_count"] += 1
                else:
                    stats["decrease_count"] += 1
                    last_decrease = stage["stage"]
                if risk_return > 0:
                    stats["applied_positive_risk_quarters"] += 1
                elif risk_return < 0:
                    stats["applied_negative_risk_quarters"] += 1
                if stage["stage"] == "feature_exposure_cap":
                    raw_value = stage["details"].get("value")
                    if isinstance(raw_value, (int, float)):
                        feature_cap_values.append(float(raw_value))
                    if risk_return > 0:
                        feature_cap_positive_returns.append(risk_return)
                    elif risk_return < 0:
                        feature_cap_negative_returns.append(risk_return)

            if final_exposure <= 1e-12:
                reason = last_decrease or "+".join(bindings)
                zero_exposure_reasons[reason] += 1
                if risk_return > 0:
                    positive_zero_exposure_reasons[reason] += 1

    for stats in stage_stats.values():
        count = stats["applied_count"]
        stats["average_exposure_delta_when_applied"] = (
            stats["exposure_delta_sum"] / count if count else None
        )

    path_rows = [
        {
            "phase": case["phase_month_offset"],
            "lag": case["execution_lag_days"],
            "final_capital_wan": case["final_capital_wan"],
            "max_drawdown": case["max_drawdown"],
            "average_exposure": case["average_exposure"],
        }
        for case in cases
    ]
    worst_case = min(cases, key=lambda item: item["final_capital"])
    best_case = max(cases, key=lambda item: item["final_capital"])

    return {
        "case_count": len(cases),
        "decision_count": row_count,
        "path_summary": {
            "min_final_capital_wan": min(row["final_capital_wan"] for row in path_rows),
            "median_final_capital_wan": statistics.median(
                row["final_capital_wan"] for row in path_rows
            ),
            "max_final_capital_wan": max(row["final_capital_wan"] for row in path_rows),
            "worst_max_drawdown": min(row["max_drawdown"] for row in path_rows),
            "worst_case": {
                "phase": worst_case["phase_month_offset"],
                "lag": worst_case["execution_lag_days"],
                "final_capital_wan": worst_case["final_capital_wan"],
                "max_drawdown": worst_case["max_drawdown"],
                "average_exposure": worst_case["average_exposure"],
            },
            "best_case": {
                "phase": best_case["phase_month_offset"],
                "lag": best_case["execution_lag_days"],
                "final_capital_wan": best_case["final_capital_wan"],
                "max_drawdown": best_case["max_drawdown"],
                "average_exposure": best_case["average_exposure"],
            },
        },
        "exposure_summary": {
            "mean_final_exposure": safe_mean(final_exposures),
            "median_final_exposure": statistics.median(final_exposures),
            "mean_positive_quarter_exposure": safe_mean(positive_exposures),
            "mean_negative_quarter_exposure": safe_mean(negative_exposures),
            "scorecard_limit_p10_p50_p90": [
                percentile(scorecard_limits, fraction) for fraction in (0.1, 0.5, 0.9)
            ],
            "cppi_limit_p10_p50_p90": [
                percentile(cppi_limits, fraction) for fraction in (0.1, 0.5, 0.9)
            ],
            "base_weight_counts": dict(base_weights.most_common()),
        },
        "initial_binding_counts": dict(initial_bindings.most_common()),
        "positive_quarter_initial_binding_counts": dict(
            positive_initial_bindings.most_common()
        ),
        "negative_quarter_initial_binding_counts": dict(
            negative_initial_bindings.most_common()
        ),
        "stage_stats": dict(stage_stats),
        "zero_exposure_reasons": dict(zero_exposure_reasons.most_common()),
        "positive_zero_exposure_reasons": dict(
            positive_zero_exposure_reasons.most_common()
        ),
        "feature_cap_diagnostic": {
            "applied_count": len(feature_cap_values),
            "value_p10_p50_p90": [
                percentile(feature_cap_values, fraction)
                for fraction in (0.1, 0.5, 0.9)
            ],
            "positive_risk_quarter_count": len(feature_cap_positive_returns),
            "negative_risk_quarter_count": len(feature_cap_negative_returns),
            "mean_positive_risk_return": safe_mean(feature_cap_positive_returns),
            "mean_negative_risk_return": safe_mean(feature_cap_negative_returns),
        },
        "paths": sorted(path_rows, key=lambda item: item["final_capital_wan"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.report.open(encoding="utf-8") as handle:
        report = json.load(handle)
    summary = summarize(report)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

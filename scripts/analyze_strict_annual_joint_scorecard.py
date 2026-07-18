#!/usr/bin/env python3
"""Search low-degree walk-forward annual return and path-risk scorecards."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.monthly_direction_model import (
    MonthlyDirectionPolicy,
    predict_binned_direction,
)
from scripts.analyze_strict_annual_scorecard_feature_ic import annual_observations


RETURN_FEATURE_SETS = {
    "score1": ("scorecard_score",),
    "score_ppi_shibor3": (
        "scorecard_score",
        "scorecard_input_ppi_yoy",
        "domestic_shibor_on_level",
    ),
    "score_ppi_baa_shibor4": (
        "scorecard_score",
        "scorecard_input_ppi_yoy",
        "external_baa_aaa_quality_spread_change_3m",
        "domestic_shibor_on_level",
    ),
}

RISK_FEATURE_SETS = {
    "turnover1": ("market_turnover_21d",),
    "turnover_retmax_vol3": (
        "market_turnover_21d",
        "basket_return_3m_max",
        "cs300_vol_1m",
    ),
    "turnover_retmax_vol_boom4": (
        "market_turnover_21d",
        "basket_return_3m_max",
        "cs300_vol_1m",
        "scorecard_input_enterprise_boom_index",
    ),
    "turnover_etfmom_vol_boom4": (
        "market_turnover_21d",
        "selected_etf_momentum_12m",
        "cs300_vol_1m",
        "scorecard_input_enterprise_boom_index",
    ),
}


def policy(
    name: str,
    features: tuple[str, ...],
    history: int,
    bins: int,
    shrink: float,
    threshold: float,
) -> MonthlyDirectionPolicy:
    required = max(1, len(features) - 1)
    return MonthlyDirectionPolicy(
        name,
        -2.0,
        99.0,
        99.0,
        99.0,
        99.0,
        min_history=8,
        features=features,
        minimum_vote_count_for_boost=required,
        model_type="binned",
        history_months=history,
        target_clip=0.50,
        binned_bins=bins,
        binned_shrink_count=shrink,
        positive_score_gt=threshold,
    )


def allowed(decision: dict[str, Any], config: MonthlyDirectionPolicy) -> bool:
    return bool(
        decision.get("score") is not None
        and float(decision["score"]) > config.positive_score_gt
        and int(decision.get("vote_count") or 0)
        >= config.minimum_vote_count_for_boost
    )


def evaluate(
    rows_by_path: dict[tuple[int, int], list[dict[str, Any]]],
    return_policy: MonthlyDirectionPolicy,
    risk_policy: MonthlyDirectionPolicy,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    path_stats = []
    for path, rows in sorted(rows_by_path.items()):
        history: list[dict[str, Any]] = []
        path_selected: list[dict[str, Any]] = []
        path_rejected: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: item["entry_day"]):
            return_decision = predict_binned_direction(
                history, row["features"], return_policy
            )
            risk_history = [
                {
                    "features": item["features"],
                    "forward_return": item["forward_worst_quarter_drawdown"],
                }
                for item in history
            ]
            risk_decision = predict_binned_direction(
                risk_history, row["features"], risk_policy
            )
            target = path_selected if (
                allowed(return_decision, return_policy)
                and allowed(risk_decision, risk_policy)
            ) else path_rejected
            target.append(row)
            history.append(
                {
                    "features": row["features"],
                    "forward_return": row["forward_return"],
                    "forward_worst_quarter_drawdown": row[
                        "forward_worst_quarter_drawdown"
                    ],
                }
            )
        selected.extend(path_selected)
        rejected.extend(path_rejected)
        path_stats.append(
            {
                "phase": path[0],
                "lag": path[1],
                "selected_count": len(path_selected),
                "rejected_count": len(path_rejected),
                "selected_mean_return": statistics.mean(
                    row["forward_return"] for row in path_selected
                ) if path_selected else None,
                "rejected_mean_return": statistics.mean(
                    row["forward_return"] for row in path_rejected
                ) if path_rejected else None,
            }
        )
    selected_returns = [row["forward_return"] for row in selected]
    rejected_returns = [row["forward_return"] for row in rejected]
    selected_drawdowns = [
        row["forward_worst_quarter_drawdown"] for row in selected
    ]
    rejected_drawdowns = [
        row["forward_worst_quarter_drawdown"] for row in rejected
    ]
    balanced = all(
        row["selected_count"] >= 2 and row["rejected_count"] >= 2
        for row in path_stats
    )
    path_edges = [
        row["selected_mean_return"] - row["rejected_mean_return"]
        for row in path_stats
        if row["selected_mean_return"] is not None
        and row["rejected_mean_return"] is not None
    ]
    return {
        "return_policy": return_policy.__dict__,
        "risk_policy": risk_policy.__dict__,
        "balanced_all_paths": balanced,
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "selected_mean_return": statistics.mean(selected_returns)
        if selected_returns else None,
        "rejected_mean_return": statistics.mean(rejected_returns)
        if rejected_returns else None,
        "return_edge": (
            statistics.mean(selected_returns) - statistics.mean(rejected_returns)
            if selected_returns and rejected_returns
            else None
        ),
        "selected_tail10_rate": sum(value <= -0.10 for value in selected_drawdowns)
        / len(selected_drawdowns) if selected_drawdowns else None,
        "rejected_tail10_rate": sum(value <= -0.10 for value in rejected_drawdowns)
        / len(rejected_drawdowns) if rejected_drawdowns else None,
        "tail10_rate_reduction": (
            sum(value <= -0.10 for value in rejected_drawdowns)
            / len(rejected_drawdowns)
            - sum(value <= -0.10 for value in selected_drawdowns)
            / len(selected_drawdowns)
            if selected_drawdowns and rejected_drawdowns
            else None
        ),
        "median_selected_per_path": statistics.median(
            row["selected_count"] for row in path_stats
        ),
        "median_rejected_per_path": statistics.median(
            row["rejected_count"] for row in path_stats
        ),
        "minimum_path_return_edge": min(path_edges) if path_edges else -999.0,
        "path_stats": path_stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    rows = annual_observations(report)
    rows_by_path: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_path[(row["phase"], row["lag"])].append(row)
    results = []
    for return_name, return_features in RETURN_FEATURE_SETS.items():
        for risk_name, risk_features in RISK_FEATURE_SETS.items():
            for history in (12, 20):
                for bins in (2, 3):
                    for shrink in (2.0, 4.0, 8.0):
                        for return_threshold in (0.04, 0.08, 0.12):
                            for risk_threshold in (-0.15, -0.12, -0.10):
                                results.append(
                                    evaluate(
                                        rows_by_path,
                                        policy(
                                            f"annual_return_{return_name}_h{history}_b{bins}_s{shrink:g}_t{return_threshold:.2f}",
                                            return_features,
                                            history,
                                            bins,
                                            shrink,
                                            return_threshold,
                                        ),
                                        policy(
                                            f"annual_risk_{risk_name}_h{history}_b{bins}_s{shrink:g}_t{abs(risk_threshold):.2f}",
                                            risk_features,
                                            history,
                                            bins,
                                            shrink,
                                            risk_threshold,
                                        ),
                                    )
                                )
    results.sort(
        key=lambda row: (
            row["balanced_all_paths"],
            row["minimum_path_return_edge"],
            row["tail10_rate_reduction"],
            row["return_edge"],
        ),
        reverse=True,
    )
    payload = {
        "method": "walk-forward annual entry binned return model plus independent worst-quarter drawdown gate",
        "source_report": str(args.report),
        "annual_observation_count": len(rows),
        "configuration_count": len(results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for row in results[:20]:
        print(
            f"{row['return_policy']['name']} + {row['risk_policy']['name']} "
            f"edge={row['return_edge']:+.2%} minpath={row['minimum_path_return_edge']:+.2%} "
            f"tail_reduction={row['tail10_rate_reduction']:+.2%} "
            f"selected={row['median_selected_per_path']:.1f} "
            f"balanced={row['balanced_all_paths']}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

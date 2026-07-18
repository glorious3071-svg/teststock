#!/usr/bin/env python3
"""Screen walk-forward path-risk gates for the corrected quarterly return model."""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.monthly_direction_model import MonthlyDirectionPolicy, predict_binned_direction
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (
    QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION,
)


RISK_FEATURE_SETS = {
    "etf_path3": (
        "selected_etf_volatility_6m",
        "selected_etf_downside_volatility_3m",
        "selected_etf_max_drawdown_6m",
    ),
    "credit_etf3": (
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
        "selected_etf_volatility_6m",
    ),
    "domestic_path4": (
        "selected_etf_volatility_6m",
        "domestic_gov_curve_10y1y_percentile_3y",
        "basket_return_1m_dispersion",
        "basket_drawdown_3m",
    ),
    "combined6": (
        "selected_etf_volatility_6m",
        "selected_etf_downside_volatility_3m",
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
        "domestic_gov_curve_10y1y_percentile_3y",
        "basket_return_1m_dispersion",
    ),
    "credit_momentum6": (
        "selected_etf_volatility_6m",
        "selected_etf_momentum_12m_skip1m",
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
        "domestic_gov_curve_10y1y_percentile_3y",
        "basket_drawdown_3m",
    ),
    "credit_spread_path6": (
        "external_baa10y_change_3m",
        "external_baa_aaa_quality_spread_change_3m",
        "external_nfci_change_3m",
        "selected_etf_volatility_6m",
        "selected_etf_momentum_12m_skip1m",
        "basket_drawdown_3m",
    ),
    "slow_bear6": (
        "selected_etf_momentum_12m_skip1m",
        "basket_drawdown_3m",
        "cs300_return_6m",
        "pboc_outlook_net_tone",
        "domestic_m1_m2_scissors_change_3m",
        "domestic_gov_curve_10y1y_percentile_3y",
    ),
    # Re-audited against the production V9 selected-ETF sample after preserving
    # true missing values.  Each feature has the same next-quarter drawdown
    # direction in all 12 phase offsets and all three historical eras.
    "selected_risk3": (
        "selected_etf_volatility_3m",
        "selected_etf_volatility_6m",
        "selected_etf_market_beta_6m",
    ),
    "selected_credit5": (
        "selected_etf_volatility_3m",
        "selected_etf_volatility_6m",
        "selected_etf_market_beta_6m",
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
    ),
    "selected_curve_credit6": (
        "selected_etf_volatility_3m",
        "selected_etf_volatility_6m",
        "selected_etf_market_beta_6m",
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
        "domestic_gov_curve_10y1y_percentile_3y",
    ),
}


def risk_policies(
    selected_feature_sets: set[str] | None = None,
    fixed_only: bool = False,
) -> list[MonthlyDirectionPolicy]:
    output = []
    for feature_set, features in RISK_FEATURE_SETS.items():
        if selected_feature_sets and feature_set not in selected_feature_sets:
            continue
        settings = (
            ((24, 8.0, -0.12),)
            if fixed_only
            else itertools.product(
                (24, 40), (4.0, 8.0), (-0.12, -0.10, -0.08, -0.06, -0.04)
            )
        )
        for history, shrink, threshold in settings:
            output.append(
                MonthlyDirectionPolicy(
                    name=(
                        f"pathrisk_{feature_set}_h{history}_s{int(shrink)}_"
                        f"t{int(abs(threshold) * 100):02d}"
                    ),
                    negative_score_lte=-2.0,
                    negative_exposure_cap=99.0,
                    overheat_exposure_cap=99.0,
                    rebound_overheat_exposure_cap=99.0,
                    crisis_exposure_cap=99.0,
                    min_history=12,
                    features=features,
                    minimum_vote_count_for_cap=min(3, len(features)),
                    minimum_vote_count_for_boost=min(3, len(features)),
                    model_type="binned",
                    history_months=history,
                    target_clip=0.50,
                    binned_bins=3,
                    binned_shrink_count=shrink,
                    positive_score_gt=threshold,
                )
            )
    return output


def allowed(decision: dict[str, Any], policy: MonthlyDirectionPolicy) -> bool:
    return bool(
        decision.get("score") is not None
        and float(decision["score"]) > policy.positive_score_gt
        and int(decision.get("vote_count") or 0)
        >= policy.minimum_vote_count_for_boost
    )


def evaluate_case(
    case: dict[str, Any], risk_policy: MonthlyDirectionPolicy
) -> dict[str, Any]:
    return_policy = replace(
        QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION,
        nonnegative_exposure_multiplier=1.0,
    )
    return_history: list[dict[str, Any]] = []
    risk_history: list[dict[str, Any]] = []
    selected: list[tuple[float, float]] = []
    rejected: list[tuple[float, float]] = []
    return_positive_count = 0
    risk_rejection_count = 0
    for row in case["decision_rows"]:
        features = dict(row["market_state"])
        return_decision = predict_binned_direction(
            return_history, features, return_policy
        )
        risk_decision = predict_binned_direction(risk_history, features, risk_policy)
        return_ok = allowed(return_decision, return_policy)
        risk_ok = allowed(risk_decision, risk_policy)
        realized = float(row["realized_risk_return"])
        realized_drawdown = float(row["realized_risk_max_drawdown"])
        if return_ok:
            return_positive_count += 1
            if risk_ok:
                selected.append((realized, realized_drawdown))
            else:
                risk_rejection_count += 1
                rejected.append((realized, realized_drawdown))
        return_history.append({"features": features, "forward_return": realized})
        risk_history.append(
            {"features": features, "forward_return": realized_drawdown}
        )
    return {
        "phase": int(case["phase_month_offset"]),
        "lag": int(case["execution_lag_days"]),
        "return_positive_count": return_positive_count,
        "selected_count": len(selected),
        "risk_rejection_count": risk_rejection_count,
        "selected_return_sum": sum(item[0] for item in selected),
        "rejected_return_sum": sum(item[0] for item in rejected),
        "selected_drawdown10_count": sum(item[1] <= -0.10 for item in selected),
        "rejected_drawdown10_count": sum(item[1] <= -0.10 for item in rejected),
        "selected_mean_return": statistics.mean(item[0] for item in selected)
        if selected
        else None,
        "selected_drawdown10_rate": sum(item[1] <= -0.10 for item in selected)
        / len(selected)
        if selected
        else None,
        "rejected_mean_return": statistics.mean(item[0] for item in rejected)
        if rejected
        else None,
        "rejected_drawdown10_rate": sum(item[1] <= -0.10 for item in rejected)
        / len(rejected)
        if rejected
        else None,
    }


def evaluate(report: dict[str, Any], policy: MonthlyDirectionPolicy) -> dict[str, Any]:
    cases = [
        evaluate_case(case, policy)
        for result in report["results"]
        for case in result["cases"]
    ]
    selected_count = sum(case["selected_count"] for case in cases)
    rejected_count = sum(case["risk_rejection_count"] for case in cases)
    selected_mean = (
        sum(case["selected_return_sum"] for case in cases) / selected_count
        if selected_count
        else None
    )
    rejected_mean = (
        sum(case["rejected_return_sum"] for case in cases) / rejected_count
        if rejected_count
        else None
    )
    selected_dd10 = (
        sum(case["selected_drawdown10_count"] for case in cases) / selected_count
        if selected_count
        else None
    )
    rejected_dd10 = (
        sum(case["rejected_drawdown10_count"] for case in cases) / rejected_count
        if rejected_count
        else None
    )
    return {
        "risk_policy": asdict(policy),
        "summary": {
            "case_count": len(cases),
            "min_selected_count": min(case["selected_count"] for case in cases),
            "median_selected_count": statistics.median(
                case["selected_count"] for case in cases
            ),
            "min_rejected_count": min(
                case["risk_rejection_count"] for case in cases
            ),
            "median_rejected_count": statistics.median(
                case["risk_rejection_count"] for case in cases
            ),
            "aggregate_selected_mean_return": selected_mean,
            "aggregate_rejected_mean_return": rejected_mean,
            "aggregate_return_spread": selected_mean - rejected_mean
            if selected_mean is not None and rejected_mean is not None
            else None,
            "aggregate_selected_drawdown10_rate": selected_dd10,
            "aggregate_rejected_drawdown10_rate": rejected_dd10,
            "drawdown10_rejection_edge": rejected_dd10 - selected_dd10
            if selected_dd10 is not None and rejected_dd10 is not None
            else None,
            "balanced_sample_pass": min(
                min(case["selected_count"] for case in cases),
                min(case["risk_rejection_count"] for case in cases),
            )
            >= 8,
        },
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feature-set", action="append", choices=sorted(RISK_FEATURE_SETS))
    parser.add_argument(
        "--fixed-only",
        action="store_true",
        help="Use only the predeclared H24/S8/T12 policy; do not search model settings.",
    )
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    results = [
        evaluate(report, policy)
        for policy in risk_policies(set(args.feature_set or []), args.fixed_only)
    ]
    results.sort(
        key=lambda item: (
            item["summary"]["balanced_sample_pass"],
            item["summary"]["drawdown10_rejection_edge"] or -999.0,
            item["summary"]["aggregate_return_spread"] or -999.0,
        ),
        reverse=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "method": (
                    "walk-forward corrected return selection intersected with a "
                    "separate completed-quarter path-drawdown gate"
                ),
                "source_report": str(args.report),
                "return_policy": asdict(
                    QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION
                ),
                "risk_feature_sets": RISK_FEATURE_SETS,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:25]:
        summary = item["summary"]
        print(
            f"{item['risk_policy']['name']:<42} "
            f"n={summary['median_selected_count']:4.0f}/"
            f"{summary['median_rejected_count']:4.0f} "
            f"ret_edge={summary['aggregate_return_spread']:+.3%} "
            f"dd10_edge={summary['drawdown10_rejection_edge']:+.3%} "
            f"balanced={summary['balanced_sample_pass']}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

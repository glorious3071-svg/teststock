#!/usr/bin/env python3
"""Walk-forward screen for low-parameter quarterly direction models."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.monthly_direction_model import (
    MonthlyDirectionPolicy,
    predict_binned_direction,
)


FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "return4": (
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
    ),
    "domestic6": (
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
        "etf_share_growth_1q_positive_ratio",
        "domestic_m1_m2_scissors_change_3m",
    ),
    "valuation8": (
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
        "etf_share_growth_1q_positive_ratio",
        "domestic_m1_m2_scissors_change_3m",
        "market_pe_ttm_percentile_3y",
        "domestic_gov_curve_10y1y_change_3m",
    ),
    "liquidity8": (
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
        "etf_share_growth_1q_positive_ratio",
        "domestic_m1_m2_scissors_change_3m",
        "external_nfci_change_3m",
        "market_turnover_change_1m",
    ),
    "price_breadth8": (
        "cs300_return_1m",
        "cs300_return_3m",
        "cs300_return_6m",
        "cs300_drawdown_3m",
        "cs300_ma_6m_distance",
        "basket_drawdown_6m",
        "breadth_return_1m_positive",
        "breadth_return_3m_positive",
    ),
    # Production-V9 selected-ETF diagnostics after the missing-value audit.
    # Six-month momentum and twelve-month momentum carry opposite but stable
    # next-quarter return information, which lets the model observe whether
    # the recent half-year is improving relative to the older half-year.
    "selected_momentum2": (
        "selected_etf_momentum_6m",
        "selected_etf_momentum_12m",
    ),
    "selected_turning4": (
        "selected_etf_momentum_6m",
        "selected_etf_momentum_12m",
        "selected_etf_drawdown_6m",
        "selected_etf_positive_day_ratio_3m",
    ),
    "pboc_selected4": (
        "pboc_outlook_net_tone",
        "selected_etf_momentum_6m",
        "selected_etf_momentum_12m",
        "selected_etf_drawdown_6m",
    ),
}


def policy_grid(
    selected_feature_sets: set[str] | None = None,
    fixed_only: bool = False,
) -> list[MonthlyDirectionPolicy]:
    output = []
    for feature_set, features in FEATURE_SETS.items():
        if selected_feature_sets and feature_set not in selected_feature_sets:
            continue
        settings = (
            ((40, 4.0, 0.01),)
            if fixed_only
            else (
                (history, shrink, threshold)
                for history in (24, 40, None)
                for shrink in (4.0, 8.0, 16.0)
                for threshold in (0.0, 0.01, 0.02)
            )
        )
        for history_periods, shrink, positive_threshold in settings:
            history_label = history_periods or 99
            output.append(
                MonthlyDirectionPolicy(
                    name=(
                        f"binned_{feature_set}_h{history_label}_b3_"
                        f"s{int(shrink)}_t{int(positive_threshold * 100):02d}"
                    ),
                    negative_score_lte=-2.0,
                    negative_exposure_cap=99.0,
                    overheat_exposure_cap=99.0,
                    rebound_overheat_exposure_cap=99.0,
                    crisis_exposure_cap=99.0,
                    min_history=12,
                    features=features,
                    minimum_vote_count_for_cap=min(4, len(features)),
                    minimum_vote_count_for_boost=min(4, len(features)),
                    model_type="binned",
                    history_months=history_periods,
                    target_clip=0.15,
                    binned_bins=3,
                    binned_shrink_count=shrink,
                    positive_score_gt=positive_threshold,
                )
            )
    return output


def evaluate_case(case: dict[str, Any], policy: MonthlyDirectionPolicy) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    selected_returns: list[float] = []
    rejected_returns: list[float] = []
    selected_drawdowns: list[float] = []
    rejected_drawdowns: list[float] = []
    scored_count = 0
    for row in case["decision_rows"]:
        prediction = predict_binned_direction(
            history,
            row["market_state"],
            policy,
        )
        realized = float(row["realized_risk_return"])
        realized_drawdown = row.get("realized_risk_max_drawdown")
        if prediction["score"] is not None:
            scored_count += 1
            if (
                float(prediction["score"]) > policy.positive_score_gt
                and int(prediction["vote_count"])
                >= policy.minimum_vote_count_for_boost
            ):
                selected_returns.append(realized)
                if realized_drawdown is not None:
                    selected_drawdowns.append(float(realized_drawdown))
            else:
                rejected_returns.append(realized)
                if realized_drawdown is not None:
                    rejected_drawdowns.append(float(realized_drawdown))
        history.append(
            {
                "features": dict(row["market_state"]),
                "forward_return": realized,
            }
        )
    return {
        "phase": case["phase_month_offset"],
        "lag": case["execution_lag_days"],
        "scored_count": scored_count,
        "selected_count": len(selected_returns),
        "rejected_count": len(rejected_returns),
        "selected_return_sum": sum(selected_returns),
        "rejected_return_sum": sum(rejected_returns),
        "selected_mean_return": (
            statistics.mean(selected_returns) if selected_returns else None
        ),
        "selected_positive_rate": (
            sum(value > 0 for value in selected_returns) / len(selected_returns)
            if selected_returns
            else None
        ),
        "selected_loss10_rate": (
            sum(value <= -0.10 for value in selected_returns) / len(selected_returns)
            if selected_returns
            else None
        ),
        "rejected_mean_return": (
            statistics.mean(rejected_returns) if rejected_returns else None
        ),
        "selected_mean_max_drawdown": (
            statistics.mean(selected_drawdowns) if selected_drawdowns else None
        ),
        "selected_maxdrawdown10_rate": (
            sum(value <= -0.10 for value in selected_drawdowns)
            / len(selected_drawdowns)
            if selected_drawdowns
            else None
        ),
        "rejected_mean_max_drawdown": (
            statistics.mean(rejected_drawdowns) if rejected_drawdowns else None
        ),
    }


def evaluate(report: dict[str, Any], policy: MonthlyDirectionPolicy) -> dict[str, Any]:
    cases = [
        evaluate_case(case, policy)
        for result in report["results"]
        for case in result["cases"]
    ]
    selected_means = [
        float(case["selected_mean_return"])
        for case in cases
        if case["selected_mean_return"] is not None
    ]
    positive_rates = [
        float(case["selected_positive_rate"])
        for case in cases
        if case["selected_positive_rate"] is not None
    ]
    loss10_rates = [
        float(case["selected_loss10_rate"])
        for case in cases
        if case["selected_loss10_rate"] is not None
    ]
    rejected_means = [
        float(case["rejected_mean_return"])
        for case in cases
        if case["rejected_mean_return"] is not None
    ]
    return_spreads = [
        float(case["selected_mean_return"]) - float(case["rejected_mean_return"])
        for case in cases
        if case["selected_mean_return"] is not None
        and case["rejected_mean_return"] is not None
    ]
    selected_total_count = sum(case["selected_count"] for case in cases)
    rejected_total_count = sum(case["rejected_count"] for case in cases)
    selected_drawdown10_rates = [
        float(case["selected_maxdrawdown10_rate"])
        for case in cases
        if case["selected_maxdrawdown10_rate"] is not None
    ]
    aggregate_selected_mean = (
        sum(case["selected_return_sum"] for case in cases) / selected_total_count
        if selected_total_count
        else None
    )
    aggregate_rejected_mean = (
        sum(case["rejected_return_sum"] for case in cases) / rejected_total_count
        if rejected_total_count
        else None
    )
    return {
        "policy": asdict(policy),
        "summary": {
            "case_count": len(cases),
            "min_selected_count": min(case["selected_count"] for case in cases),
            "median_selected_count": statistics.median(
                case["selected_count"] for case in cases
            ),
            "min_rejected_count": min(case["rejected_count"] for case in cases),
            "median_rejected_count": statistics.median(
                case["rejected_count"] for case in cases
            ),
            "min_selected_mean_return": (
                min(selected_means) if selected_means else None
            ),
            "median_selected_mean_return": (
                statistics.median(selected_means) if selected_means else None
            ),
            "min_selected_positive_rate": (
                min(positive_rates) if positive_rates else None
            ),
            "median_selected_positive_rate": (
                statistics.median(positive_rates) if positive_rates else None
            ),
            "max_selected_loss10_rate": max(loss10_rates) if loss10_rates else None,
            "median_selected_loss10_rate": (
                statistics.median(loss10_rates) if loss10_rates else None
            ),
            "median_rejected_mean_return": (
                statistics.median(rejected_means) if rejected_means else None
            ),
            "median_return_spread": (
                statistics.median(return_spreads) if return_spreads else None
            ),
            "aggregate_selected_mean_return": aggregate_selected_mean,
            "aggregate_rejected_mean_return": aggregate_rejected_mean,
            "aggregate_return_spread": (
                aggregate_selected_mean - aggregate_rejected_mean
                if aggregate_selected_mean is not None
                and aggregate_rejected_mean is not None
                else None
            ),
            "max_selected_maxdrawdown10_rate": (
                max(selected_drawdown10_rates)
                if selected_drawdown10_rates
                else None
            ),
            "balanced_sample_pass": (
                min(case["selected_count"] for case in cases) >= 12
                and min(case["rejected_count"] for case in cases) >= 8
            ),
        },
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feature-set", action="append", choices=sorted(FEATURE_SETS))
    parser.add_argument(
        "--fixed-only",
        action="store_true",
        help="Use only H40/S4/T01; do not search model settings.",
    )
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    results = [
        evaluate(report, policy)
        for policy in policy_grid(set(args.feature_set or []), args.fixed_only)
    ]
    results.sort(
        key=lambda item: (
            item["summary"]["balanced_sample_pass"],
            item["summary"]["aggregate_return_spread"]
            if item["summary"]["aggregate_return_spread"] is not None
            else -999.0,
            item["summary"]["min_selected_mean_return"]
            if item["summary"]["min_selected_mean_return"] is not None
            else -999.0,
        ),
        reverse=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "method": (
                    "same-path strict walk-forward equal-frequency bins; all "
                    "thresholds and target means use completed prior quarters only"
                ),
                "source_report": str(args.report),
                "feature_sets": FEATURE_SETS,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        spread = summary["median_return_spread"]
        spread_label = f"{spread:+.3%}" if spread is not None else "n/a"
        aggregate_spread = summary["aggregate_return_spread"]
        aggregate_spread_label = (
            f"{aggregate_spread:+.3%}" if aggregate_spread is not None else "n/a"
        )
        min_mean = summary["min_selected_mean_return"]
        med_mean = summary["median_selected_mean_return"]
        min_mean_label = f"{min_mean:+.3%}" if min_mean is not None else "n/a"
        med_mean_label = f"{med_mean:+.3%}" if med_mean is not None else "n/a"
        print(
            f"{item['policy']['name']:<43} "
            f"n={summary['median_selected_count']:4.0f}/"
            f"{summary['median_rejected_count']:2.0f} "
            f"min_mean={min_mean_label} "
            f"med_mean={med_mean_label} "
            f"agg_spread={aggregate_spread_label} "
            f"med_spread={spread_label} "
            f"balanced={summary['balanced_sample_pass']}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

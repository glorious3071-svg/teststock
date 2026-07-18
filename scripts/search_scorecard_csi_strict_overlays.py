#!/usr/bin/env python3
"""Search low-dimensional risk overlays against the strict scorecard+CSI target.

This script reuses ``validate_scorecard_csi_generalization.py`` instead of
duplicating backtest logic.  It is intended as a research harness: candidates
are measured against the strict target, but no parameters are adopted here.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.validate_scorecard_csi_generalization as validator
from scripts.backtest_scorecard_csi_quarterly_risk import DEFAULT_OVERLAY

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_overlay_search.json"
OUT_CSV = OUT_DIR / "scorecard_csi_overlay_search.csv"


def candidate_specs() -> list[dict[str, Any]]:
    return [
        {"name": "baseline"},
        {"name": "weak_repair_cap10", "weak_repair_cap_pct": 10.0},
        {"name": "weak_repair_cap20", "weak_repair_cap_pct": 20.0},
        {"name": "weak_repair_cap40", "weak_repair_cap_pct": 40.0},
        {
            "name": "falling_knife_early_minus6",
            "falling_knife_cs300_6m_lte": -6.0,
            "falling_knife_cap_pct": 0.0,
        },
        {
            "name": "falling_knife_early_minus8",
            "falling_knife_cs300_6m_lte": -8.0,
            "falling_knife_cap_pct": 0.0,
        },
        {
            "name": "h1_rally_takeprofit25",
            "h1_rally_return_gte": 0.25,
            "risk_cap_pct": 10.0,
        },
        {
            "name": "h1_rally_takeprofit30_cap0",
            "h1_rally_return_gte": 0.30,
            "risk_cap_pct": 0.0,
        },
        {
            "name": "weak_momentum_cap60",
            "weak_momentum_exhaustion_cap_pct": 60.0,
            "weak_momentum_exhaustion_cs300_6m_gt": 10.0,
        },
        {
            "name": "post_stimulus_cap60",
            "post_stimulus_exhaustion_cap_pct": 60.0,
            "post_stimulus_exhaustion_cs300_6m_gt": 10.0,
        },
        {
            "name": "stagflation_cap30",
            "stagflation_defensive_cap_pct": 30.0,
            "stagflation_pmi_below_52_months_gte": 3,
            "stagflation_us10y_chg_bp_gte": 25.0,
        },
    ]


def make_overlay(spec: dict[str, Any]):
    kwargs = {k: v for k, v in spec.items() if k != "name"}
    return replace(DEFAULT_OVERLAY, name=spec["name"], **kwargs)


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return validator.matrix_summary(items)


def evaluate_candidate(spec: dict[str, Any], full: bool) -> dict[str, Any]:
    overlay = make_overlay(spec)
    validator.DEFAULT_OVERLAY = overlay

    base = validator.run_variant("quarterly", 0, include_rows=False)
    drift = [validator.run_variant("quarterly", lag, include_rows=False) for lag in validator.EXECUTION_LAGS]
    frequency = [validator.run_variant(freq, 0, include_rows=False) for freq in validator.REVIEW_FREQUENCIES]

    if full:
        annual_month_drift = [
            validator.run_random_annual_drift(phase, lag, include_rows=False)
            for phase in validator.MONTH_DRIFT_PHASES
            for lag in validator.MONTH_DRIFT_EXECUTION_LAGS
        ]
        quarterly_month_drift = [
            validator.run_random_quarterly_drift(phase, lag, include_rows=False)
            for phase in validator.MONTH_DRIFT_PHASES
            for lag in validator.MONTH_DRIFT_EXECUTION_LAGS
        ]
        monthly_pressure = [
            validator.run_monthly_pressure(lag, include_rows=False)
            for lag in validator.MONTHLY_PRESSURE_EXECUTION_LAGS
        ]
    else:
        annual_month_drift = [
            validator.run_random_annual_drift(phase, 0, include_rows=False)
            for phase in validator.MONTH_DRIFT_PHASES
        ]
        quarterly_month_drift = [
            validator.run_random_quarterly_drift(phase, 0, include_rows=False)
            for phase in validator.MONTH_DRIFT_PHASES
        ]
        monthly_pressure = [validator.run_monthly_pressure(0, include_rows=False)]

    groups = {
        "drift": drift,
        "frequency": frequency,
        "annual_month_drift": annual_month_drift,
        "quarterly_month_drift": quarterly_month_drift,
        "monthly_pressure": monthly_pressure,
    }
    summaries = {name: matrix_summary(items) for name, items in groups.items()}
    all_items = [base] + [item for items in groups.values() for item in items]
    strict_pass_count = sum(1 for item in all_items if item["target_met"])
    worst_mdd = min(item["max_drawdown"] for item in all_items)
    min_final = min(item["final_capital"] for item in all_items)
    min_annualized = min(item["annualized_return"] for item in all_items)
    # Prefer candidates that improve worst-case capital, then drawdown, without
    # hiding base-case failure.
    objective = (min_final / validator.TARGET_CAPITAL) + min(worst_mdd - validator.TARGET_MDD, 0.0) * 2.0
    target_met = all(item["target_met"] for item in all_items)
    return {
        "name": spec["name"],
        "full": full,
        "overlay": asdict(overlay),
        "base_final_capital_wan": base["final_capital_wan"],
        "base_max_drawdown": base["max_drawdown"],
        "strict_pass_count": strict_pass_count,
        "strict_case_count": len(all_items),
        "target_met": target_met,
        "objective": objective,
        "min_final_capital_wan": min_final / 10_000.0,
        "min_annualized_return": min_annualized,
        "worst_max_drawdown": worst_mdd,
        "summaries": summaries,
    }


def write_outputs(rows: list[dict[str, Any]], full: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search low-dimensional quarterly overlays against the strict scorecard+CSI target.",
        "mode": "full" if full else "quick",
        "target_capital": validator.TARGET_CAPITAL,
        "target_mdd": validator.TARGET_MDD,
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "full",
            "base_final_capital_wan",
            "base_max_drawdown",
            "strict_pass_count",
            "strict_case_count",
            "target_met",
            "objective",
            "min_final_capital_wan",
            "min_annualized_return",
            "worst_max_drawdown",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Run all lag/phase cases instead of lag-0 drift screen.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    specs = candidate_specs()
    if args.limit:
        specs = specs[: args.limit]
    rows = []
    for spec in specs:
        row = evaluate_candidate(spec, args.full)
        rows.append(row)
        print(
            f"{row['name']:<30} pass={row['strict_pass_count']:>3}/{row['strict_case_count']:<3} "
            f"base={row['base_final_capital_wan']:8.1f}万 "
            f"min={row['min_final_capital_wan']:8.1f}万 "
            f"worst_mdd={row['worst_max_drawdown'] * 100:6.1f}% "
            f"objective={row['objective']:.4f}"
        )
    rows.sort(key=lambda item: item["objective"], reverse=True)
    write_outputs(rows, args.full)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0 if rows and rows[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

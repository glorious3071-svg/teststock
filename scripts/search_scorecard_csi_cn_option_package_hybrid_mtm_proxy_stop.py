#!/usr/bin/env python3
"""Search hybrid stops: real option MTM when available, CS300 proxy before it exists."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_right
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_cn_option_package_daily_mtm_stop import (
    DailyMtmPricer,
    MtmStopRule,
    build_mtm_cases,
    build_rules,
    period_return_with_stop,
)
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases

OUT_DIR = ROOT / "data" / "backtests"


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        prefix = OUT_DIR / (
            "scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop_"
            f"{args.underlying_mode}_miss{args.missing_package_policy}"
        )
    return prefix.with_suffix(".json"), prefix.with_suffix(".csv")


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date) -> float | None:
    idx = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[idx][1] if idx >= 0 else None


def proxy_path(series: dict[str, list[tuple[dt.date, float]]], start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    rows = series.get(CS300_CODE, [])
    start_price = price_at(rows, start)
    if not start_price or start_price <= 0:
        return []
    left = bisect_right(rows, (start, math.inf))
    right = bisect_right(rows, (end, math.inf))
    return [
        {"day": day, "risky_to_date": close / start_price - 1.0}
        for day, close in rows[left:right]
        if close and close > 0
    ]


def period_return_hybrid_stop(
    period: dict[str, Any],
    rule: MtmStopRule,
    series: dict[str, list[tuple[dt.date, float]]],
) -> tuple[float, bool, str]:
    if period["points"]:
        period_return, stopped = period_return_with_stop(period, rule)
        return period_return, stopped, "option_mtm"

    no_stop_risky = period["base_without_package"] + period["package_end_return"]
    safe_return = period["safe_return"]
    points = proxy_path(series, period["start_exec"], period["end_exec"])
    if not points:
        return rule.normal_exposure * no_stop_risky + (1.0 - rule.normal_exposure) * safe_return, False, "none"
    total_days = max(len(points), 1)
    for idx, point in enumerate(points, start=1):
        fraction = min(1.0, idx / total_days)
        risky_to_date = float(point["risky_to_date"])
        blended_to_date = rule.normal_exposure * risky_to_date + (1.0 - rule.normal_exposure) * safe_return * fraction
        if blended_to_date <= rule.stop_loss_pct:
            remaining = max(0.0, 1.0 - fraction)
            risky_after = no_stop_risky - risky_to_date
            after_stop = (
                rule.post_stop_exposure * risky_after
                + (1.0 - rule.post_stop_exposure) * safe_return * remaining
            )
            return blended_to_date + after_stop, True, "cs300_proxy"
    return rule.normal_exposure * no_stop_risky + (1.0 - rule.normal_exposure) * safe_return, False, "cs300_proxy"


def run_case(mtm_case: dict[str, Any], rule: MtmStopRule, series: dict[str, list[tuple[dt.date, float]]]) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    stop_months = 0
    proxy_stop_months = 0
    option_stop_months = 0
    severe_loss_months = 0
    severe_loss_stopped = 0
    proxy_months = 0
    option_mtm_months = 0
    for period in mtm_case["periods"]:
        no_stop_risky = period["base_without_package"] + period["package_end_return"]
        if no_stop_risky <= -0.10:
            severe_loss_months += 1
        period_return, stopped, trigger_source = period_return_hybrid_stop(period, rule, series)
        if trigger_source == "option_mtm":
            option_mtm_months += 1
        elif trigger_source == "cs300_proxy":
            proxy_months += 1
        if stopped:
            stop_months += 1
            if trigger_source == "option_mtm":
                option_stop_months += 1
            elif trigger_source == "cs300_proxy":
                proxy_stop_months += 1
            if no_stop_risky <= -0.10:
                severe_loss_stopped += 1
        capital *= 1.0 + period_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{mtm_case['phase_month_offset']}_lag{mtm_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": mtm_case["phase_month_offset"],
        "execution_lag_days": mtm_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "stop_months": stop_months,
        "proxy_stop_months": proxy_stop_months,
        "option_stop_months": option_stop_months,
        "severe_loss_months": severe_loss_months,
        "severe_loss_stopped": severe_loss_stopped,
        "proxy_months": proxy_months,
        "option_mtm_months": option_mtm_months,
    }


def matrix_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
        "median_stop_months": statistics.median(item["stop_months"] for item in cases),
        "median_proxy_stop_months": statistics.median(item["proxy_stop_months"] for item in cases),
        "median_option_stop_months": statistics.median(item["option_stop_months"] for item in cases),
        "median_severe_loss_months": statistics.median(item["severe_loss_months"] for item in cases),
        "median_severe_loss_stopped": statistics.median(item["severe_loss_stopped"] for item in cases),
        "median_proxy_months": statistics.median(item["proxy_months"] for item in cases),
        "median_option_mtm_months": statistics.median(item["option_mtm_months"] for item in cases),
    }


def evaluate_rule(mtm_cases: list[dict[str, Any]], rule: MtmStopRule, series: dict[str, list[tuple[dt.date, float]]]) -> dict[str, Any]:
    cases = [run_case(mtm_case, rule, series) for mtm_case in mtm_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search hybrid stop rules: option MTM when available, CS300 daily proxy otherwise.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "note": "Before listed option MTM exists, CS300 daily close is used as an executable proxy risk trigger.",
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "stop_loss_pct",
        "normal_exposure",
        "post_stop_exposure",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_stop_months",
        "median_proxy_stop_months",
        "median_option_stop_months",
        "median_severe_loss_months",
        "median_severe_loss_stopped",
        "median_proxy_months",
        "median_option_mtm_months",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search hybrid option-MTM/CS300-proxy stop rules.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    raw_cases, setup_meta = setup_cases(args)
    package = load_package_shape()
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        pricer = HistoricalCnPackagePricer(
            conn,
            package,
            args.underlying_mode,
            args.max_quote_stale_days,
            args.slippage_bps_per_leg,
            args.missing_package_policy,
        )
        mtm = DailyMtmPricer(pricer, csi_series)
        mtm_cases = build_mtm_cases(raw_cases, mtm)
    finally:
        conn.close()

    meta = {
        **{key: value for key, value in setup_meta.items() if key != "csi_series"},
        "daily_mtm_coverage": mtm.summary(),
    }
    results = [evaluate_rule(mtm_cases, rule, csi_series) for rule in build_rules()]
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    json_path, csv_path = write_outputs(results, meta, args)
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['rule']['name']:<28} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"stop_med={summary['median_stop_months']:.1f}"
        )
    best = results[0]["summary"]
    print(
        f"Wrote {json_path}; rules={len(results)} best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {csv_path}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

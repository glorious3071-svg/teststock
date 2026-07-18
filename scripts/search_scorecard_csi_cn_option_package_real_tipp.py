#!/usr/bin/env python3
"""Search TIPP/CPPI wrappers over the historical listed China ETF option package."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME, run_case as run_core_case
from scripts.backtest_scorecard_csi_blended_protection import load_option_data, precompute_csi_paths, precompute_option_paths
from scripts.backtest_scorecard_csi_crypto_satellite_mix import CORE_RULE_BY_NAME, SATELLITE_RULE_BY_NAME, crypto_period_returns
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import load_data
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields
from scripts.search_scorecard_csi_cn_option_package_real_history import (
    HistoricalCnPackagePricer,
    as_date,
    load_package_shape,
)

OUT_DIR = ROOT / "data" / "backtests"


@dataclass(frozen=True)
class TippRule:
    name: str
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    min_exposure: float


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
        return prefix.with_suffix(".json"), prefix.with_suffix(".csv")
    else:
        prefix = OUT_DIR / (
            "scorecard_csi_cn_option_package_real_tipp_"
            f"{args.underlying_mode}_miss{args.missing_package_policy}"
        )
        return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def build_rules() -> list[TippRule]:
    rules: list[TippRule] = []
    for mode in ["tipp", "cppi"]:
        floor_values = [0.82, 0.84, 0.86, 0.88, 0.90, 0.92, 0.95]
        multipliers = [2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0]
        max_exposures = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
        for floor_pct in floor_values:
            for multiplier in multipliers:
                for max_exposure in max_exposures:
                    if multiplier * (1.0 - floor_pct) > max_exposure * 1.75:
                        continue
                    rules.append(
                        TippRule(
                            name=(
                                f"cnreal_{mode}_f{int(floor_pct * 100)}"
                                f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                            ),
                            mode=mode,
                            floor_pct=floor_pct,
                            multiplier=multiplier,
                            max_exposure=max_exposure,
                            min_exposure=0.0,
                        )
                    )
    return rules


def setup_cases(args) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    package = load_package_shape()
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        core_rule = CORE_RULE_BY_NAME["core_xbcppi_sub12_spread95_call108"]
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].phase_rule_name},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].option_rule_name},
        )
        crypto_data = load_data(conn)
        pricer = HistoricalCnPackagePricer(
            conn,
            package,
            args.underlying_mode,
            args.max_quote_stale_days,
            args.slippage_bps_per_leg,
            args.missing_package_policy,
        )

        core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
                core_cache[(core_rule.name, phase, lag)] = case
                periods_by_case[(phase, lag)] = [
                    (as_date(row["start_exec"]), as_date(row["end_exec"]))
                    for row in case["rows"]
                ]

        satellite_rule = SATELLITE_RULE_BY_NAME["sat_crypto_cppi"]
        satellite_returns: dict[tuple[int, int], list[float]] = {}
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                satellite_returns[(phase, lag)] = crypto_period_returns(
                    crypto_data,
                    satellite_rule,
                    periods_by_case[(phase, lag)],
                    phase,
                    lag,
                )

        raw_cases: list[dict[str, Any]] = []
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                core_case = core_cache[(core_rule.name, phase, lag)]
                safe_returns: list[float] = []
                raw_returns: list[float] = []
                periods: list[dict[str, Any]] = []
                listed_package_months = 0
                missing_package_months = 0
                for row, sat_return in zip(core_case["rows"], satellite_returns[(phase, lag)]):
                    start_exec = as_date(row["start_exec"])
                    end_exec = as_date(row["end_exec"])
                    package_return, meta = pricer.package_return(csi_series, start_exec, end_exec, INITIAL_CAPITAL)
                    if meta["source"] == "listed_contract":
                        listed_package_months += 1
                    else:
                        missing_package_months += 1
                    safe_return = float(row["safe_return"])
                    raw_return = (
                        0.95 * float(row["period_return"])
                        + 0.08 * sat_return
                        + (1.0 - 0.95 - 0.08) * safe_return
                        + package_return
                    )
                    safe_returns.append(safe_return)
                    raw_returns.append(raw_return)
                    periods.append(
                        {
                            "start_exec": start_exec,
                            "end_exec": end_exec,
                            "safe_return": safe_return,
                            "core_period_return": float(row["period_return"]),
                            "satellite_return": sat_return,
                            "cs300_6m": row.get("cs300_6m"),
                            "vix": row.get("vix"),
                            "base_guard_active": row.get("base_guard_active"),
                            "package_source": meta["source"],
                        }
                    )
                raw_cases.append(
                    {
                        "phase_month_offset": phase,
                        "execution_lag_days": lag,
                        "raw_returns": raw_returns,
                        "safe_returns": safe_returns,
                        "periods": periods,
                        "listed_package_months": listed_package_months,
                        "missing_package_months": missing_package_months,
                    }
                )
    finally:
        conn.close()

    worst_monthly_returns = [min(raw_case["raw_returns"]) for raw_case in raw_cases]
    severe_loss_month_counts = [sum(1 for value in raw_case["raw_returns"] if value <= -0.10) for raw_case in raw_cases]
    meta = {
        "package_shape_source": asdict(package),
        "underlying_mode": args.underlying_mode,
        "missing_package_policy": args.missing_package_policy,
        "quote_dates_available": sum(len(items) for items in pricer.quote_dates.values()),
        "quote_dates_by_opt_code": {key: len(value) for key, value in pricer.quote_dates.items()},
        "quote_dates_used": len(pricer.used_quote_dates),
        "missing_reasons": pricer.missing_reasons,
        "raw_return_diagnostics": {
            "case_count": len(raw_cases),
            "global_worst_monthly_return": min(worst_monthly_returns) if worst_monthly_returns else None,
            "cases_with_monthly_loss_lte_10pct": sum(1 for count in severe_loss_month_counts if count > 0),
            "total_months_lte_10pct": sum(severe_loss_month_counts),
            "median_months_lte_10pct_per_case": statistics.median(severe_loss_month_counts)
            if severe_loss_month_counts
            else 0,
        },
    }
    return raw_cases, meta


def run_wrapped_case(raw_case: dict[str, Any], rule: TippRule) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    guard_months = 0
    for raw_return, safe_return in zip(raw_case["raw_returns"], raw_case["safe_returns"]):
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        if exposure <= 1e-9:
            guard_months += 1
        period_return = exposure * raw_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        curve.append(capital)
        exposures.append(exposure)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{raw_case['phase_month_offset']}_lag{raw_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": raw_case["phase_month_offset"],
        "execution_lag_days": raw_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "max_exposure_used": max(exposures) if exposures else 0.0,
        "guard_months": guard_months,
        "listed_package_months": raw_case["listed_package_months"],
        "missing_package_months": raw_case["missing_package_months"],
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
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in cases),
        "median_guard_months": statistics.median(item["guard_months"] for item in cases),
        "median_listed_package_months": statistics.median(item["listed_package_months"] for item in cases),
        "median_missing_package_months": statistics.median(item["missing_package_months"] for item in cases),
    }


def evaluate_rule(raw_cases: list[dict[str, Any]], rule: TippRule) -> dict[str, Any]:
    cases = [run_wrapped_case(raw_case, rule) for raw_case in raw_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "objective": "Search TIPP/CPPI wrappers over raw historical listed China ETF option packages.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "missing_package_policy": args.missing_package_policy,
            "underlying_mode": args.underlying_mode,
            "note": "No modeled monthly loss floor is applied. TIPP/CPPI changes exposure to the raw real-package return stream.",
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "mode",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "min_exposure",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_avg_exposure",
        "median_guard_months",
        "median_listed_package_months",
        "median_missing_package_months",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search TIPP/CPPI wrappers over historical listed China ETF option packages.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    raw_cases, meta = setup_cases(args)
    results = []
    for rule in build_rules():
        result = evaluate_rule(raw_cases, rule)
        results.append(result)
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
            f"{item['rule']['name']:<34} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_exposure']:.2f}"
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

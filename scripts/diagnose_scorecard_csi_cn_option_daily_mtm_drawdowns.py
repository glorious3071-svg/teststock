#!/usr/bin/env python3
"""Diagnose drawdown events for real option-leg daily MTM stop rules."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL
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


def rule_by_name(name: str) -> MtmStopRule:
    for rule in build_rules():
        if rule.name == name:
            return rule
    raise KeyError(f"Unknown MTM stop rule: {name}")


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        clean = "_".join(args.rules)
        prefix = OUT_DIR / f"scorecard_csi_cn_option_package_daily_mtm_drawdown_{clean}"
    return prefix.with_suffix(".json"), prefix.with_suffix(".csv")


def run_case_with_rows(mtm_case: dict[str, Any], rule: MtmStopRule) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    rows = []
    max_dd = 0.0
    trough_row: dict[str, Any] | None = None
    for idx, period in enumerate(mtm_case["periods"], start=1):
        start_capital = capital
        start_peak = peak
        no_stop_risky = period["base_without_package"] + period["package_end_return"]
        period_return, stopped = period_return_with_stop(period, rule)
        capital *= 1.0 + period_return
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        drawdown = capital / peak - 1.0
        if drawdown < max_dd:
            max_dd = drawdown
        row = {
            "idx": idx,
            "phase_month_offset": mtm_case["phase_month_offset"],
            "execution_lag_days": mtm_case["execution_lag_days"],
            "start_exec": period["start_exec"],
            "end_exec": period["end_exec"],
            "start_capital": start_capital,
            "end_capital": capital,
            "start_peak": start_peak,
            "end_peak": peak,
            "period_return": period_return,
            "drawdown": drawdown,
            "stopped": stopped,
            "no_stop_risky_return": no_stop_risky,
            "base_without_package": period["base_without_package"],
            "package_end_return": period["package_end_return"],
            "safe_return": period["safe_return"],
            "source": period["source"],
            "daily_points": period["daily_points"],
            "severe_loss": no_stop_risky <= -0.10,
        }
        rows.append(row)
        if trough_row is None or drawdown < float(trough_row["drawdown"]):
            trough_row = row
    return {
        "name": f"{rule.name}_phase{mtm_case['phase_month_offset']}_lag{mtm_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": mtm_case["phase_month_offset"],
        "execution_lag_days": mtm_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "max_drawdown": max_dd,
        "target_met": capital >= TARGET_CAPITAL and max_dd >= TARGET_MDD,
        "trough": trough_row,
        "rows": rows,
    }


def leading_drawdown_window(rows: list[dict[str, Any]], trough_idx: int, before: int, after: int) -> list[dict[str, Any]]:
    start = max(0, trough_idx - 1 - before)
    end = min(len(rows), trough_idx + after)
    return rows[start:end]


def diagnose_rule(mtm_cases: list[dict[str, Any]], rule: MtmStopRule, window_before: int, window_after: int) -> dict[str, Any]:
    cases = [run_case_with_rows(mtm_case, rule) for mtm_case in mtm_cases]
    cases.sort(key=lambda item: item["max_drawdown"])
    worst_case = cases[0]
    trough = worst_case["trough"] or {}
    window = leading_drawdown_window(worst_case["rows"], int(trough.get("idx") or 1), window_before, window_after)
    return {
        "rule": asdict(rule),
        "case_count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "worst_case": {
            key: value
            for key, value in worst_case.items()
            if key not in {"rows"}
        },
        "trough_window": window,
        "worst_cases": [
            {
                "name": item["name"],
                "phase_month_offset": item["phase_month_offset"],
                "execution_lag_days": item["execution_lag_days"],
                "final_capital_wan": item["final_capital_wan"],
                "max_drawdown": item["max_drawdown"],
                "trough_start_exec": (item["trough"] or {}).get("start_exec"),
                "trough_end_exec": (item["trough"] or {}).get("end_exec"),
                "trough_period_return": (item["trough"] or {}).get("period_return"),
                "trough_no_stop_risky_return": (item["trough"] or {}).get("no_stop_risky_return"),
                "trough_stopped": (item["trough"] or {}).get("stopped"),
            }
            for item in cases[:10]
        ],
    }


def write_outputs(report: dict[str, Any], csv_rows: list[dict[str, Any]], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "rule",
        "case_name",
        "phase_month_offset",
        "execution_lag_days",
        "final_capital_wan",
        "max_drawdown",
        "trough_start_exec",
        "trough_end_exec",
        "trough_period_return",
        "trough_no_stop_risky_return",
        "trough_stopped",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in csv_rows:
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose drawdowns for real option-leg daily MTM stop rules.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--rules", nargs="+", default=["mtmstop04_x100_post0", "mtmstop04_x300_post0"])
    parser.add_argument("--window-before", type=int, default=6)
    parser.add_argument("--window-after", type=int, default=3)
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

    diagnostics = []
    csv_rows = []
    for rule_name in args.rules:
        rule = rule_by_name(rule_name)
        diag = diagnose_rule(mtm_cases, rule, args.window_before, args.window_after)
        diagnostics.append(diag)
        for item in diag["worst_cases"]:
            csv_rows.append(
                {
                    "rule": rule_name,
                    "case_name": item["name"],
                    **item,
                }
            )
        print(
            f"{rule_name}: pass={diag['pass_count']}/{diag['case_count']} "
            f"min={diag['min_final_capital_wan']:.1f}w "
            f"worst_mdd={diag['worst_max_drawdown']:.1%} "
            f"trough={diag['worst_case']['trough']['start_exec']}~{diag['worst_case']['trough']['end_exec']}"
        )
    report = {
        "objective": "Diagnose worst drawdown windows for daily MTM stop rules.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
        },
        "daily_mtm_coverage": mtm.summary(),
        "setup_meta": {key: value for key, value in setup_meta.items() if key != "csi_series"},
        "diagnostics": diagnostics,
    }
    json_path, csv_path = write_outputs(report, csv_rows, args)
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

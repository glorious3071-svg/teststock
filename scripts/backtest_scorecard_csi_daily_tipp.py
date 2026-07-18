#!/usr/bin/env python3
"""Backtest daily TIPP/CPPI wrappers on phase-diversified CSI sleeves.

Monthly TIPP improved the frontier but still suffered month-gap losses.  This
experiment keeps the same phase-diversified scorecard+CSI return engine and
applies the portfolio-insurance wrapper daily.  The wrapper is tested across all
12 month phases and the standard execution-lag set.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_daily_guard import (
    defensive_daily_return,
    sleeve_daily_return,
    trade_dates_between,
)
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    monthly_boundaries,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_phase_ensemble import RULES as PHASE_RULES
from scripts.backtest_scorecard_csi_phase_ensemble import PhaseEnsembleRule, ensemble_state
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_daily_tipp_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_daily_tipp_search.csv"


@dataclass(frozen=True)
class DailyTippRule:
    name: str
    phase_rule_name: str
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    min_exposure: float = 0.0
    drawdown_scale_lte: float = -1.0
    drawdown_scale: float = 1.0


PHASE_RULE_NAMES = [
    "phase12_lever120_us10y",
    "phase12_lever150_cash",
    "phase12_guard60_us10y",
    "phase12_mean_us10y",
]

PHASE_RULE_BY_NAME = {rule.name: rule for rule in PHASE_RULES}


def build_rules() -> list[DailyTippRule]:
    rules: list[DailyTippRule] = []
    for phase_rule_name in PHASE_RULE_NAMES:
        short = phase_rule_name.removeprefix("phase12_").replace("_", "")
        for floor_pct in [0.84, 0.86, 0.88, 0.90, 0.92, 0.95]:
            for multiplier in [4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0]:
                for max_exposure in [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]:
                    rules.append(
                        DailyTippRule(
                            f"dtipp_{short}_f{int(floor_pct * 100)}_m{int(multiplier)}_x{int(max_exposure * 100)}",
                            phase_rule_name,
                            "tipp",
                            floor_pct,
                            multiplier,
                            max_exposure,
                        )
                    )
            for multiplier, max_exposure in [(6.0, 1.0), (8.0, 1.25), (10.0, 1.5), (12.0, 2.0)]:
                rules.append(
                    DailyTippRule(
                        f"dcppi_{short}_f{int(floor_pct * 100)}_m{int(multiplier)}_x{int(max_exposure * 100)}",
                        phase_rule_name,
                        "cppi",
                        floor_pct,
                        multiplier,
                        max_exposure,
                    )
                )
        for floor_pct in [0.86, 0.88, 0.90]:
            for multiplier, max_exposure in [(8.0, 1.5), (10.0, 2.0), (12.0, 2.5)]:
                rules.append(
                    DailyTippRule(
                        f"dtipp_{short}_f{int(floor_pct * 100)}_m{int(multiplier)}_x{int(max_exposure * 100)}_dd8s50",
                        phase_rule_name,
                        "tipp",
                        floor_pct,
                        multiplier,
                        max_exposure,
                        drawdown_scale_lte=-0.08,
                        drawdown_scale=0.50,
                    )
                )
    return rules


RULES = build_rules()


def phase_rule_by_name(name: str) -> PhaseEnsembleRule:
    if name not in PHASE_RULE_BY_NAME:
        raise KeyError(f"Unknown phase rule: {name}")
    return PHASE_RULE_BY_NAME[name]


def precompute_daily_paths(
    conn,
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    holdings: dict[int, list[str]],
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    paths: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for phase_rule_name in PHASE_RULE_NAMES:
        phase_rule = phase_rule_by_name(phase_rule_name)
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                rows: list[dict[str, Any]] = []
                for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
                    start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
                    end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
                    target_pct, _monthly_ret, sleeves, reasons = ensemble_state(
                        conn,
                        series,
                        holdings,
                        phase_rule,
                        start_snapshot,
                        start_exec,
                        end_exec,
                        0.0,
                    )
                    previous_day = start_exec
                    for day in trade_dates_between(trade_dates, start_exec, end_exec):
                        sleeve_ret = sleeve_daily_return(series, sleeves, previous_day, day)
                        defensive_ret = defensive_daily_return(series, yields, phase_rule, previous_day, day)
                        equity_weight = target_pct / 100.0
                        non_equity_ret = cash_return(previous_day, day) if equity_weight > 1.0 else defensive_ret
                        base_ret = equity_weight * sleeve_ret + (1.0 - equity_weight) * non_equity_ret
                        rows.append(
                            {
                                "day": day,
                                "period": start_snapshot,
                                "base_return": base_ret,
                                "safe_return": cash_return(previous_day, day),
                                "target_equity_pct": target_pct,
                                "reasons": reasons,
                            }
                        )
                        previous_day = day
                paths[(phase_rule_name, phase, lag)] = rows
    return paths


def run_case(paths: dict[tuple[str, int, int], list[dict[str, Any]]], rule: DailyTippRule, phase: int, lag: int) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    drawdown_scaled_days = 0
    for row in paths[(rule.phase_rule_name, phase, lag)]:
        peak = max(peak, capital)
        drawdown = capital / peak - 1.0
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        if drawdown <= rule.drawdown_scale_lte:
            exposure *= rule.drawdown_scale
            drawdown_scaled_days += 1
        capital *= 1.0 + exposure * float(row["base_return"]) + (1.0 - exposure) * float(row["safe_return"])
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
        exposures.append(exposure)
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": f"{rule.name}_phase{phase}_lag{lag}",
        "rule": rule.name,
        "phase_rule_name": rule.phase_rule_name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "median_exposure": statistics.median(exposures) if exposures else 0.0,
        "max_realized_exposure": max(exposures) if exposures else 0.0,
        "drawdown_scaled_days": drawdown_scaled_days,
    }


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in items),
        "median_exposure": statistics.median(item["median_exposure"] for item in items),
        "median_max_realized_exposure": statistics.median(item["max_realized_exposure"] for item in items),
        "median_drawdown_scaled_days": statistics.median(item["drawdown_scaled_days"] for item in items),
    }


def evaluate_rule(paths: dict[tuple[str, int, int], list[dict[str, Any]]], rule: DailyTippRule) -> dict[str, Any]:
    cases = [run_case(paths, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {
        "rule": asdict(rule),
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test daily TIPP/CPPI wrappers on phase-diversified scorecard+CSI sleeves across all month phases and execution lags.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "max_drawdown_frequency": "daily",
        "model_limits": "Daily portfolio-insurance wrapper over cached CSI daily prices; no intraday gap, borrow, tax, liquidity, or execution-slippage model.",
        "phase_rule_names": PHASE_RULE_NAMES,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "phase_rule_name",
            "mode",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "min_exposure",
            "drawdown_scale_lte",
            "drawdown_scale",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_exposure",
            "median_max_realized_exposure",
            "median_drawdown_scaled_days",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        paths = precompute_daily_paths(conn, series, yields, trade_dates, holdings)
    finally:
        conn.close()

    results = []
    for rule in RULES:
        result = evaluate_rule(paths, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:76]:<76} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_exposure'] * 100:5.1f}%"
        )

    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    write_outputs(results)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

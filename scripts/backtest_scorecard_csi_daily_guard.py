#!/usr/bin/env python3
"""Test daily risk guards on phase-diversified scorecard+CSI sleeves.

The monthly/quarterly experiments show that month-end decisions cannot prevent
large intra-month losses. This script keeps the phase-ensemble sleeve
construction, but marks the portfolio daily and cuts exposure after observable
daily stop or CS300 trend triggers.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    GOLD_CODE,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    month_end_shift,
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
from scripts.backtest_scorecard_csi_phase_ensemble import (
    RULES as PHASE_RULES,
    PhaseEnsembleRule,
    ensemble_state,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import (
    US10Y_PROXY,
    load_us10y_yields,
    us10y_duration_return,
)

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_daily_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_daily_guard_search.csv"


@dataclass(frozen=True)
class DailyGuardRule:
    name: str
    phase_rule_name: str
    stop_loss_pct: float
    guard_cap_pct: float
    cs300_trailing_days: int
    cs300_trailing_dd_lte: float
    cs300_ma_days: int
    reentry_mode: str
    min_guard_days: int = 3
    guard_asset: str = "phase_defensive"
    hedge_cost_annual: float = 0.0


RULES = [
    DailyGuardRule("daily_base_phase12_us10y", "phase12_lever120_us10y", -1.0, 120.0, 0, -1.0, 0, "next_month"),
    DailyGuardRule("daily_stop5_cap0", "phase12_lever120_us10y", -0.05, 0.0, 0, -1.0, 0, "next_month"),
    DailyGuardRule("daily_stop8_cap0", "phase12_lever120_us10y", -0.08, 0.0, 0, -1.0, 0, "next_month"),
    DailyGuardRule("daily_stop5_cap30", "phase12_lever120_us10y", -0.05, 30.0, 0, -1.0, 0, "next_month"),
    DailyGuardRule("daily_cs300_20d_dd8_cap0", "phase12_lever120_us10y", -1.0, 0.0, 20, -0.08, 0, "ma_recover"),
    DailyGuardRule("daily_cs300_20d_dd6_cap0", "phase12_lever120_us10y", -1.0, 0.0, 20, -0.06, 0, "ma_recover"),
    DailyGuardRule("daily_ma60_cap0", "phase12_lever120_us10y", -1.0, 0.0, 0, -1.0, 60, "ma_recover"),
    DailyGuardRule("daily_stop5_ma60_cap0", "phase12_lever120_us10y", -0.05, 0.0, 20, -0.08, 60, "ma_recover"),
    DailyGuardRule("daily_stop5_ma60_cap30", "phase12_lever120_us10y", -0.05, 30.0, 20, -0.08, 60, "ma_recover"),
    DailyGuardRule("daily_mean_us10y_stop5_cap0", "phase12_mean_us10y", -0.05, 0.0, 0, -1.0, 0, "next_month"),
    DailyGuardRule("daily_mean_us10y_ma60_cap0", "phase12_mean_us10y", -1.0, 0.0, 0, -1.0, 60, "ma_recover"),
    DailyGuardRule("daily_stop5_inverse", "phase12_lever120_us10y", -0.05, 0.0, 0, -1.0, 0, "next_month", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
    DailyGuardRule("daily_stop8_inverse", "phase12_lever120_us10y", -0.08, 0.0, 0, -1.0, 0, "next_month", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
    DailyGuardRule("daily_stop5_cap30_inverse", "phase12_lever120_us10y", -0.05, 30.0, 0, -1.0, 0, "next_month", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
    DailyGuardRule("daily_ma60_inverse", "phase12_lever120_us10y", -1.0, 0.0, 0, -1.0, 60, "ma_recover", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
    DailyGuardRule("daily_stop5_ma60_inverse", "phase12_lever120_us10y", -0.05, 0.0, 20, -0.08, 60, "ma_recover", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
    DailyGuardRule("daily_mean_stop5_inverse", "phase12_mean_us10y", -0.05, 0.0, 0, -1.0, 0, "next_month", guard_asset="inverse_cs300", hedge_cost_annual=0.02),
]


def phase_rule_by_name(name: str) -> PhaseEnsembleRule:
    for rule in PHASE_RULES:
        if rule.name == name:
            return rule
    raise KeyError(f"Unknown phase ensemble rule: {name}")


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def daily_return(rows: list[tuple[date, float]], start: date, end: date) -> float:
    start_price = price_at(rows, start)
    end_price = price_at(rows, end)
    if not start_price or not end_price or start_price <= 0:
        return 0.0
    return end_price / start_price - 1.0


def sleeve_daily_return(series: dict[str, list[tuple[date, float]]], sleeves: list[dict[str, Any]], start: date, end: date) -> float:
    sleeve_returns = []
    for sleeve in sleeves:
        codes = sleeve["codes"]
        code_returns = [daily_return(series.get(code, []), start, end) for code in codes]
        sleeve_returns.append(sum(code_returns) / len(code_returns) if code_returns else 0.0)
    return sum(sleeve_returns) / len(sleeve_returns) if sleeve_returns else 0.0


def trade_dates_between(trade_dates: list[date], start: date, end: date) -> list[date]:
    left = bisect_right(trade_dates, start)
    right = bisect_right(trade_dates, end)
    return trade_dates[left:right]


def moving_average(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    if days <= 0:
        return None
    i = bisect_right(rows, (boundary, math.inf))
    window = rows[max(0, i - days) : i]
    if len(window) < max(5, min(days, 20)):
        return None
    return statistics.mean(value for _day, value in window)


def trailing_drawdown(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    if days <= 0:
        return None
    i = bisect_right(rows, (boundary, math.inf))
    window = rows[max(0, i - days) : i]
    if len(window) < max(5, min(days, 20)):
        return None
    current = window[-1][1]
    high = max(value for _day, value in window)
    if high <= 0:
        return None
    return current / high - 1.0


def guard_triggered(
    series: dict[str, list[tuple[date, float]]],
    rule: DailyGuardRule,
    day: date,
    month_drawdown: float,
) -> tuple[bool, list[str]]:
    reasons = []
    cs300 = series[CS300_CODE]
    if month_drawdown <= rule.stop_loss_pct:
        reasons.append("monthly_stop_loss")
    dd = trailing_drawdown(cs300, day, rule.cs300_trailing_days)
    if dd is not None and dd <= rule.cs300_trailing_dd_lte:
        reasons.append("cs300_trailing_drawdown")
    ma = moving_average(cs300, day, rule.cs300_ma_days)
    price = price_at(cs300, day)
    if ma is not None and price is not None and price < ma:
        reasons.append("cs300_below_ma")
    return bool(reasons), reasons


def can_reenter(series: dict[str, list[tuple[date, float]]], rule: DailyGuardRule, day: date, guard_days: int) -> bool:
    if rule.reentry_mode == "next_month":
        return False
    if guard_days < rule.min_guard_days:
        return False
    if rule.cs300_ma_days <= 0:
        return True
    ma = moving_average(series[CS300_CODE], day, rule.cs300_ma_days)
    price = price_at(series[CS300_CODE], day)
    return ma is not None and price is not None and price >= ma


def defensive_daily_return(
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    phase_rule: PhaseEnsembleRule,
    start: date,
    end: date,
) -> float:
    if phase_rule.defensive_asset == US10Y_PROXY:
        return us10y_duration_return(yields, start, end)
    if phase_rule.defensive_asset == "gold_if_up":
        return daily_return(series.get(GOLD_CODE, []), start, end)
    return cash_return(start, end)


def guarded_defensive_daily_return(
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    phase_rule: PhaseEnsembleRule,
    rule: DailyGuardRule,
    start: date,
    end: date,
    guarded: bool,
) -> float:
    if guarded and rule.guard_asset == "inverse_cs300":
        hedge_cost = rule.hedge_cost_annual * max((end - start).days, 0) / 365.25
        return -daily_return(series[CS300_CODE], start, end) - hedge_cost
    if guarded and rule.guard_asset == "cash":
        return cash_return(start, end)
    return defensive_daily_return(series, yields, phase_rule, start, end)


def summarize(name: str, capital: float, curve: list[float], rows: list[dict[str, Any]]) -> dict[str, Any]:
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": name,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def run_case(
    conn,
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    holdings: dict[int, list[str]],
    rule: DailyGuardRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    phase_rule = phase_rule_by_name(rule.phase_rule_name)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows = []
    guard_events = 0

    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset):
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        month_start_capital = capital
        target_pct, _monthly_ret, sleeves, base_reasons = ensemble_state(
            conn,
            series,
            holdings,
            phase_rule,
            start_snapshot,
            start_exec,
            end_exec,
            capital / peak - 1.0,
        )
        current_pct = target_pct
        guarded = False
        guard_days = 0
        month_reasons = list(base_reasons)
        previous_day = start_exec
        for day in trade_dates_between(trade_dates, start_exec, end_exec):
            equity_return = sleeve_daily_return(series, sleeves, previous_day, day)
            def_return = guarded_defensive_daily_return(
                series,
                yields,
                phase_rule,
                rule,
                previous_day,
                day,
                guarded,
            )
            financing_return = cash_return(previous_day, day)
            equity_weight = current_pct / 100.0
            non_equity_return = financing_return if equity_weight > 1.0 else def_return
            portfolio_return = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
            capital *= 1.0 + portfolio_return
            peak = max(peak, capital)
            curve.append(capital)
            month_drawdown = capital / month_start_capital - 1.0
            if guarded:
                guard_days += 1
                if can_reenter(series, rule, day, guard_days):
                    current_pct = target_pct
                    guarded = False
                    month_reasons.append("daily_guard_reentry")
            else:
                hit, reasons = guard_triggered(series, rule, day, month_drawdown)
                if hit and current_pct > rule.guard_cap_pct:
                    current_pct = rule.guard_cap_pct
                    guarded = True
                    guard_days = 0
                    guard_events += 1
                    month_reasons.extend(reasons)
            previous_day = day

        if include_rows:
            rows.append(
                {
                    "period": start_snapshot.isoformat(),
                    "phase_month_offset": phase_month_offset,
                    "execution_lag_days": execution_lag_days,
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "target_equity_pct": target_pct,
                    "ending_equity_pct": current_pct,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "month_return": capital / month_start_capital - 1.0,
                    "rebalance_reasons": sorted(set(month_reasons)),
                }
            )

    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_rule": rule.phase_rule_name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "guard_events": guard_events,
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
        "median_guard_events": statistics.median(item["guard_events"] for item in items),
    }


def evaluate_rule(
    conn,
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    holdings: dict[int, list[str]],
    rule: DailyGuardRule,
) -> dict[str, Any]:
    cases = [
        run_case(conn, series, yields, trade_dates, holdings, rule, phase, lag)
        for phase in MONTH_PHASES
        for lag in EXECUTION_LAGS
    ]
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
        "objective": "Test daily risk guards on phase-diversified scorecard+CSI sleeves across all month phase and execution-lag cases.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "max_drawdown_frequency": "daily",
        "inverse_cs300_proxy": (
            "Rules with guard_asset=inverse_cs300 use a synthetic -CS300 daily return "
            "after the daily guard fires, less the configured annual hedge cost. This "
            "is a hedge proxy for research, not proof of an executable product."
        ),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_events",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {"name": item["rule"]["name"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [d for d, _px in series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        results = []
        for rule in RULES:
            result = evaluate_rule(conn, series, yields, trade_dates, holdings, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name:<30} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
                f"guards={summary['median_guard_events']:.0f}"
            )
    finally:
        conn.close()
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

#!/usr/bin/env python3
"""Test phase-diversified scorecard+CSI portfolio construction.

This experiment directly targets month-start fragility. Instead of letting one
calendar cut point own the whole portfolio, each month blends several sleeves
whose CSI baskets and scorecard snapshots are staggered across prior month-end
phases. The strict validation still runs every external month phase and
execution lag; the ensemble is only useful if it improves the worst cases.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from bisect import bisect_right
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
    apply_year_for_snapshot,
    cash_return,
    holding_codes_for_snapshot,
    load_price_series,
    month_end_shift,
    monthly_boundaries,
    period_return,
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
from scripts.backtest_scorecard_csi_quarterly_risk import DEFAULT_RULE, TARGET_MDD, scorecard_detail
from scripts.backtest_scorecard_csi_vol_target import (
    US10Y_PROXY,
    load_us10y_yields,
    us10y_duration_return,
)

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_phase_ensemble_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_phase_ensemble_search.csv"

_SCORECARD_CACHE: dict[tuple[int, date], dict[str, Any]] = {}


@dataclass(frozen=True)
class PhaseEnsembleRule:
    name: str
    sleeve_offsets: tuple[int, ...]
    target_mode: str
    target_multiplier: float
    max_equity_pct: float
    min_equity_pct: float
    opportunity_score_lte: int
    opportunity_floor_pct: float
    trend_months: int
    trend_lte: float
    trend_cap_pct: float
    drawdown_lte: float
    drawdown_cap_pct: float
    defensive_asset: str
    defensive_trend_months: int = 6
    weak_repair_cap_pct: float = 999.0
    weak_repair_score_lte: int = -99
    weak_repair_pmi_below_52_months_gte: int = 999
    weak_repair_pmi_3m_lt: float = -999.0
    weak_repair_ppi_lt: float = -999.0
    extreme_rally_cap_pct: float = 999.0
    extreme_rally_6m_gte: float = 999.0
    extreme_rally_3m_gte: float = 999.0
    stagflation_cap_pct: float = 999.0
    stagflation_pmi_below_52_months_gte: int = 999
    stagflation_ppi_gte: float = 999.0
    stagflation_cs300_6m_lte: float = -999.0


RULES = [
    PhaseEnsembleRule("phase3_mean_cash", (0, 1, 2), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, "cash"),
    PhaseEnsembleRule("phase6_mean_cash", (0, 1, 2, 3, 4, 5), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, "cash"),
    PhaseEnsembleRule("phase12_mean_cash", tuple(range(12)), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, "cash"),
    PhaseEnsembleRule("phase12_max_cash", tuple(range(12)), "max", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, "cash"),
    PhaseEnsembleRule("phase12_mean_dd_cash", tuple(range(12)), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 45.0, "cash"),
    PhaseEnsembleRule("phase12_max_dd_cash", tuple(range(12)), "max", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 45.0, "cash"),
    PhaseEnsembleRule("phase12_mean_us10y", tuple(range(12)), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, US10Y_PROXY),
    PhaseEnsembleRule("phase12_mean_dd_us10y", tuple(range(12)), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 45.0, US10Y_PROXY),
    PhaseEnsembleRule("phase12_mean_gold", tuple(range(12)), "mean", 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.0, 100.0, "gold_if_up"),
    PhaseEnsembleRule("phase12_lever120_cash", tuple(range(12)), "mean", 1.25, 120.0, 0.0, -3, 110.0, 6, -0.12, 85.0, -1.0, 120.0, "cash"),
    PhaseEnsembleRule("phase12_lever150_cash", tuple(range(12)), "mean", 1.45, 150.0, 0.0, -3, 130.0, 6, -0.12, 95.0, -1.0, 150.0, "cash"),
    PhaseEnsembleRule("phase12_lever120_dd_cash", tuple(range(12)), "mean", 1.25, 120.0, 0.0, -3, 110.0, 6, -0.12, 85.0, -0.08, 55.0, "cash"),
    PhaseEnsembleRule("phase12_lever120_us10y", tuple(range(12)), "mean", 1.25, 120.0, 0.0, -3, 110.0, 6, -0.12, 85.0, -1.0, 120.0, US10Y_PROXY),
    PhaseEnsembleRule(
        "phase12_guard60_cash",
        tuple(range(12)),
        "mean",
        1.25,
        120.0,
        0.0,
        -3,
        110.0,
        6,
        -0.12,
        85.0,
        -1.0,
        120.0,
        "cash",
        weak_repair_cap_pct=60.0,
        weak_repair_score_lte=0,
        weak_repair_pmi_below_52_months_gte=10,
        weak_repair_pmi_3m_lt=51.0,
        weak_repair_ppi_lt=0.0,
        extreme_rally_cap_pct=60.0,
        extreme_rally_6m_gte=60.0,
        extreme_rally_3m_gte=25.0,
        stagflation_cap_pct=60.0,
        stagflation_pmi_below_52_months_gte=10,
        stagflation_ppi_gte=2.0,
        stagflation_cs300_6m_lte=-10.0,
    ),
    PhaseEnsembleRule(
        "phase12_guard40_cash",
        tuple(range(12)),
        "mean",
        1.25,
        120.0,
        0.0,
        -3,
        110.0,
        6,
        -0.12,
        85.0,
        -1.0,
        120.0,
        "cash",
        weak_repair_cap_pct=40.0,
        weak_repair_score_lte=0,
        weak_repair_pmi_below_52_months_gte=10,
        weak_repair_pmi_3m_lt=51.0,
        weak_repair_ppi_lt=0.0,
        extreme_rally_cap_pct=40.0,
        extreme_rally_6m_gte=60.0,
        extreme_rally_3m_gte=25.0,
        stagflation_cap_pct=40.0,
        stagflation_pmi_below_52_months_gte=10,
        stagflation_ppi_gte=2.0,
        stagflation_cs300_6m_lte=-10.0,
    ),
    PhaseEnsembleRule(
        "phase12_guard60_us10y",
        tuple(range(12)),
        "mean",
        1.25,
        120.0,
        0.0,
        -3,
        110.0,
        6,
        -0.12,
        85.0,
        -1.0,
        120.0,
        US10Y_PROXY,
        weak_repair_cap_pct=60.0,
        weak_repair_score_lte=0,
        weak_repair_pmi_below_52_months_gte=10,
        weak_repair_pmi_3m_lt=51.0,
        weak_repair_ppi_lt=0.0,
        extreme_rally_cap_pct=60.0,
        extreme_rally_6m_gte=60.0,
        extreme_rally_3m_gte=25.0,
        stagflation_cap_pct=60.0,
        stagflation_pmi_below_52_months_gte=10,
        stagflation_ppi_gte=2.0,
        stagflation_cs300_6m_lte=-10.0,
    ),
    PhaseEnsembleRule(
        "phase12_guard40_us10y",
        tuple(range(12)),
        "mean",
        1.25,
        120.0,
        0.0,
        -3,
        110.0,
        6,
        -0.12,
        85.0,
        -1.0,
        120.0,
        US10Y_PROXY,
        weak_repair_cap_pct=40.0,
        weak_repair_score_lte=0,
        weak_repair_pmi_below_52_months_gte=10,
        weak_repair_pmi_3m_lt=51.0,
        weak_repair_ppi_lt=0.0,
        extreme_rally_cap_pct=40.0,
        extreme_rally_6m_gte=60.0,
        extreme_rally_3m_gte=25.0,
        stagflation_cap_pct=40.0,
        stagflation_pmi_below_52_months_gte=10,
        stagflation_ppi_gte=2.0,
        stagflation_cs300_6m_lte=-10.0,
    ),
]


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def scorecard_snapshot(conn, snapshot: date) -> dict[str, Any]:
    apply_year = apply_year_for_snapshot(snapshot)
    key = (apply_year, snapshot)
    if key not in _SCORECARD_CACHE:
        _SCORECARD_CACHE[key] = scorecard_detail(conn, apply_year, snapshot, DEFAULT_RULE)
    return _SCORECARD_CACHE[key]


def equal_weight_return(series: dict[str, list[tuple[date, float]]], codes: list[str], start: date, end: date) -> float:
    returns = [period_return(series, code, start, end) for code in codes]
    return sum(returns) / len(returns) if returns else 0.0


def base_target_for_detail(detail: dict[str, Any], rule: PhaseEnsembleRule) -> float:
    target = float(detail["rule_target_equity_pct"])
    if int(detail["score"]) <= rule.opportunity_score_lte:
        target = max(target, rule.opportunity_floor_pct)
    return target


def aggregate_target(values: list[float], mode: str) -> float:
    if not values:
        return 0.0
    if mode == "max":
        return max(values)
    if mode == "min":
        return min(values)
    if mode == "median":
        return statistics.median(values)
    return statistics.mean(values)


def defensive_return(
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    rule: PhaseEnsembleRule,
    start: date,
    end: date,
) -> tuple[float, str]:
    if rule.defensive_asset == US10Y_PROXY:
        return us10y_duration_return(yields, start, end), US10Y_PROXY
    if rule.defensive_asset == "gold_if_up":
        trend = period_return(series, GOLD_CODE, month_end_shift(start, -rule.defensive_trend_months), start)
        if trend > 0 and price_at(series.get(GOLD_CODE, []), start) is not None:
            return period_return(series, GOLD_CODE, start, end), GOLD_CODE
    return cash_return(start, end), "cash"


def ensemble_state(
    conn,
    series: dict[str, list[tuple[date, float]]],
    holdings: dict[int, list[str]],
    rule: PhaseEnsembleRule,
    snapshot: date,
    start_exec: date,
    end_exec: date,
    portfolio_drawdown: float,
) -> tuple[float, float, list[dict[str, Any]], list[str]]:
    sleeves = []
    sleeve_targets = []
    sleeve_returns = []
    for offset in rule.sleeve_offsets:
        sleeve_snapshot = month_end_shift(snapshot, -offset)
        codes = holding_codes_for_snapshot(holdings, sleeve_snapshot)
        detail = scorecard_snapshot(conn, sleeve_snapshot)
        target = base_target_for_detail(detail, rule)
        equity_ret = equal_weight_return(series, codes, start_exec, end_exec)
        sleeve_targets.append(target)
        sleeve_returns.append(equity_ret)
        sleeves.append(
            {
                "offset_months": offset,
                "snapshot": sleeve_snapshot.isoformat(),
                "apply_year": apply_year_for_snapshot(sleeve_snapshot),
                "score": detail["score"],
                "base_target_equity_pct": target,
                "equity_return": equity_ret,
                "codes": codes,
            }
        )

    reasons = []
    target = aggregate_target(sleeve_targets, rule.target_mode) * rule.target_multiplier
    target = min(rule.max_equity_pct, max(rule.min_equity_pct, target))
    current_detail = scorecard_snapshot(conn, snapshot)
    known = current_detail["known_inputs"]
    cs300_trend = period_return(series, CS300_CODE, month_end_shift(snapshot, -rule.trend_months), snapshot)
    cs300_3m = period_return(series, CS300_CODE, month_end_shift(snapshot, -3), snapshot)
    cs300_6m = period_return(series, CS300_CODE, month_end_shift(snapshot, -6), snapshot)
    if (
        target >= rule.extreme_rally_cap_pct
        and cs300_6m >= rule.extreme_rally_6m_gte
        and cs300_3m >= rule.extreme_rally_3m_gte
    ):
        target = min(target, rule.extreme_rally_cap_pct)
        reasons.append("extreme_rally_cap")
    if (
        target >= rule.weak_repair_cap_pct
        and int(current_detail["score"]) <= rule.weak_repair_score_lte
        and (known.get("pmi_below_52_months") or 0) >= rule.weak_repair_pmi_below_52_months_gte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < rule.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < rule.weak_repair_ppi_lt
    ):
        target = min(target, rule.weak_repair_cap_pct)
        reasons.append("weak_repair_cap")
    if (
        target >= rule.stagflation_cap_pct
        and (known.get("pmi_below_52_months") or 0) >= rule.stagflation_pmi_below_52_months_gte
        and (known.get("ppi_yoy") or 0.0) >= rule.stagflation_ppi_gte
        and cs300_6m <= rule.stagflation_cs300_6m_lte
    ):
        target = min(target, rule.stagflation_cap_pct)
        reasons.append("stagflation_cap")
    if cs300_trend <= rule.trend_lte:
        target = min(target, rule.trend_cap_pct)
        reasons.append("cs300_trend_cap")
    if portfolio_drawdown <= rule.drawdown_lte:
        target = min(target, rule.drawdown_cap_pct)
        reasons.append("portfolio_drawdown_cap")

    equity_return = statistics.mean(sleeve_returns) if sleeve_returns else 0.0
    return target, equity_return, sleeves, reasons


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
    rule: PhaseEnsembleRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows = []
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset):
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        drawdown = capital / peak - 1.0
        equity_pct, equity_return, sleeves, reasons = ensemble_state(
            conn,
            series,
            holdings,
            rule,
            start_snapshot,
            start_exec,
            end_exec,
            drawdown,
        )
        equity_weight = equity_pct / 100.0
        def_return, defensive_asset = defensive_return(series, yields, rule, start_exec, end_exec)
        financing_return = cash_return(start_exec, end_exec)
        non_equity_return = financing_return if equity_weight > 1.0 else def_return
        period_return = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
        capital *= 1.0 + period_return
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "period": start_snapshot.isoformat(),
                    "phase_month_offset": phase_month_offset,
                    "execution_lag_days": execution_lag_days,
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "target_equity_pct": equity_pct,
                    "equity_return": equity_return,
                    "defensive_asset": defensive_asset,
                    "defensive_return": def_return,
                    "period_return": period_return,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "rebalance_reasons": reasons,
                    "sleeves": sleeves,
                }
            )
    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
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
    }


def evaluate_rule(
    conn,
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    holdings: dict[int, list[str]],
    rule: PhaseEnsembleRule,
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
        "objective": "Test phase-diversified rolling scorecard+CSI sleeves across all month phase and execution-lag cases.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
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
                f"{rule.name:<28} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"min_ann={summary['min_annualized_return'] * 100:5.1f}%"
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

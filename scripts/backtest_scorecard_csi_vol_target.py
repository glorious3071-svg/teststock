#!/usr/bin/env python3
"""Search volatility-targeted scorecard+CSI portfolio variants.

This is an experiment, not a production rule. It keeps the same strict month
phase and execution-lag matrix as the generalization validator, then tests
whether explicit risk budgeting can improve robustness without hiding weak
cases behind the natural January/quarterly calendar.
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
    SPX_CODE,
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
    CASH_ANNUAL_RATE,
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import DEFAULT_RULE, TARGET_MDD, scorecard_detail

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_vol_target_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_vol_target_search.csv"

US10Y_PROXY = "US10Y_DURATION_PROXY"
_SCORECARD_CACHE: dict[tuple[int, date], dict[str, Any]] = {}


@dataclass(frozen=True)
class VolTargetRule:
    name: str
    target_vol_annual: float
    vol_window_months: int
    vol_floor_annual: float
    base_target_multiplier: float
    max_equity_pct: float
    min_equity_pct: float
    opportunity_score_lte: int
    opportunity_min_equity_pct: float
    trend_months: int
    trend_lte: float
    trend_cap_pct: float
    drawdown_lte: float
    drawdown_cap_pct: float
    defensive_asset: str
    defensive_trend_months: int = 6


RULES = [
    VolTargetRule("vol10_cap100_cash", 0.10, 12, 0.10, 1.0, 100.0, 0.0, -3, 90.0, 6, -0.12, 50.0, -0.08, 35.0, "cash"),
    VolTargetRule("vol12_cap100_cash", 0.12, 12, 0.10, 1.0, 100.0, 0.0, -3, 90.0, 6, -0.12, 60.0, -0.08, 40.0, "cash"),
    VolTargetRule("vol15_cap100_cash", 0.15, 12, 0.10, 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 50.0, "cash"),
    VolTargetRule("vol18_cap120_cash", 0.18, 12, 0.10, 1.1, 120.0, 0.0, -3, 100.0, 6, -0.12, 70.0, -0.08, 50.0, "cash"),
    VolTargetRule("vol20_cap150_cash", 0.20, 12, 0.10, 1.2, 150.0, 0.0, -3, 110.0, 6, -0.12, 80.0, -0.08, 60.0, "cash"),
    VolTargetRule("vol25_cap180_cash", 0.25, 12, 0.10, 1.25, 180.0, 0.0, -3, 125.0, 6, -0.12, 90.0, -0.08, 65.0, "cash"),
    VolTargetRule("vol12_cap100_us10y", 0.12, 12, 0.10, 1.0, 100.0, 0.0, -3, 90.0, 6, -0.12, 60.0, -0.08, 40.0, US10Y_PROXY),
    VolTargetRule("vol15_cap100_us10y", 0.15, 12, 0.10, 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 50.0, US10Y_PROXY),
    VolTargetRule("vol18_cap120_us10y", 0.18, 12, 0.10, 1.1, 120.0, 0.0, -3, 100.0, 6, -0.12, 70.0, -0.08, 50.0, US10Y_PROXY),
    VolTargetRule("vol20_cap150_us10y", 0.20, 12, 0.10, 1.2, 150.0, 0.0, -3, 110.0, 6, -0.12, 80.0, -0.08, 60.0, US10Y_PROXY),
    VolTargetRule("vol15_cap100_gold", 0.15, 12, 0.10, 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 50.0, "gold_if_up"),
    VolTargetRule("vol20_cap150_gold", 0.20, 12, 0.10, 1.2, 150.0, 0.0, -3, 110.0, 6, -0.12, 80.0, -0.08, 60.0, "gold_if_up"),
    VolTargetRule("vol15_short6_cap100_cash", 0.15, 6, 0.10, 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -0.08, 50.0, "cash"),
    VolTargetRule("vol20_short6_cap150_cash", 0.20, 6, 0.10, 1.2, 150.0, 0.0, -3, 110.0, 6, -0.12, 80.0, -0.08, 60.0, "cash"),
    VolTargetRule("vol15_no_dd_us10y", 0.15, 12, 0.10, 1.0, 100.0, 0.0, -3, 95.0, 6, -0.12, 70.0, -1.00, 100.0, US10Y_PROXY),
    VolTargetRule("vol20_no_dd_us10y", 0.20, 12, 0.10, 1.2, 150.0, 0.0, -3, 110.0, 6, -0.12, 80.0, -1.00, 150.0, US10Y_PROXY),
]


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def load_us10y_yields(conn) -> list[tuple[date, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, y10
            FROM us_tycr_daily
            WHERE y10 IS NOT NULL
            ORDER BY trade_date
            """
        )
        return [(trade_date, float(yield_pct)) for trade_date, yield_pct in cur.fetchall()]


def us10y_duration_return(yields: list[tuple[date, float]], start: date, end: date, duration: float = 7.0) -> float:
    start_yield = price_at(yields, start)
    end_yield = price_at(yields, end)
    if start_yield is None or end_yield is None:
        return cash_return(start, end)
    carry = start_yield / 100.0 * max((end - start).days, 0) / 365.25
    price_return = -duration * (end_yield - start_yield) / 100.0
    return carry + price_return


def scorecard_snapshot(conn, snapshot: date) -> dict[str, Any]:
    apply_year = apply_year_for_snapshot(snapshot)
    key = (apply_year, snapshot)
    if key not in _SCORECARD_CACHE:
        _SCORECARD_CACHE[key] = scorecard_detail(conn, apply_year, snapshot, DEFAULT_RULE)
    return _SCORECARD_CACHE[key]


def equal_weight_return(series: dict[str, list[tuple[date, float]]], codes: list[str], start: date, end: date) -> float:
    returns = [period_return(series, code, start, end) for code in codes]
    return sum(returns) / len(returns) if returns else 0.0


def trailing_equity_returns(
    series: dict[str, list[tuple[date, float]]],
    codes: list[str],
    snapshot: date,
    months: int,
) -> list[float]:
    returns = []
    end = snapshot
    for _ in range(months):
        start = month_end_shift(end, -1)
        returns.append(equal_weight_return(series, codes, start, end))
        end = start
    returns.reverse()
    return returns


def annualized_volatility(returns: list[float], floor: float) -> float:
    if len(returns) < 3:
        return floor
    stdev = statistics.pstdev(returns)
    return max(stdev * math.sqrt(12.0), floor)


def defensive_return(
    series: dict[str, list[tuple[date, float]]],
    yields: list[tuple[date, float]],
    rule: VolTargetRule,
    start: date,
    end: date,
) -> tuple[float, str]:
    if rule.defensive_asset == US10Y_PROXY:
        return us10y_duration_return(yields, start, end), US10Y_PROXY
    if rule.defensive_asset == "gold_if_up":
        trend = period_return(series, GOLD_CODE, month_end_shift(start, -rule.defensive_trend_months), start)
        if trend > 0 and price_at(series.get(GOLD_CODE, []), start) is not None:
            return period_return(series, GOLD_CODE, start, end), GOLD_CODE
    if rule.defensive_asset == "spx_if_up":
        trend = period_return(series, SPX_CODE, month_end_shift(start, -rule.defensive_trend_months), start)
        if trend > 0 and price_at(series.get(SPX_CODE, []), start) is not None:
            return period_return(series, SPX_CODE, start, end), SPX_CODE
    return cash_return(start, end), "cash"


def target_equity_pct(
    series: dict[str, list[tuple[date, float]]],
    rule: VolTargetRule,
    snapshot: date,
    codes: list[str],
    detail: dict[str, Any],
    portfolio_drawdown: float,
) -> tuple[float, float, list[str]]:
    reasons = []
    base_target = float(detail["rule_target_equity_pct"]) * rule.base_target_multiplier
    if int(detail["score"]) <= rule.opportunity_score_lte:
        base_target = max(base_target, rule.opportunity_min_equity_pct)

    trailing = trailing_equity_returns(series, codes, snapshot, rule.vol_window_months)
    realized_vol = annualized_volatility(trailing, rule.vol_floor_annual)
    vol_budget_pct = rule.target_vol_annual / realized_vol * 100.0
    target = min(base_target, vol_budget_pct, rule.max_equity_pct)
    target = max(target, rule.min_equity_pct)

    cs300_trend = period_return(series, CS300_CODE, month_end_shift(snapshot, -rule.trend_months), snapshot)
    if cs300_trend <= rule.trend_lte:
        target = min(target, rule.trend_cap_pct)
        reasons.append("cs300_trend_cap")
    if portfolio_drawdown <= rule.drawdown_lte:
        target = min(target, rule.drawdown_cap_pct)
        reasons.append("portfolio_drawdown_cap")
    if target < base_target:
        reasons.append("vol_budget_cap")
    return target, realized_vol, reasons


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
    rule: VolTargetRule,
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
        codes = holding_codes_for_snapshot(holdings, start_snapshot)
        detail = scorecard_snapshot(conn, start_snapshot)
        drawdown = capital / peak - 1.0
        equity_pct, realized_vol, reasons = target_equity_pct(series, rule, start_snapshot, codes, detail, drawdown)
        equity_weight = equity_pct / 100.0
        equity_ret = equal_weight_return(series, codes, start_exec, end_exec)
        def_ret, defensive_asset = defensive_return(series, yields, rule, start_exec, end_exec)
        financing_ret = cash_return(start_exec, end_exec)
        non_equity_ret = financing_ret if equity_weight > 1.0 else def_ret
        period_ret = equity_weight * equity_ret + (1.0 - equity_weight) * non_equity_ret
        capital *= 1.0 + period_ret
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
                    "score": detail["score"],
                    "target_equity_pct": equity_pct,
                    "realized_vol_annual": realized_vol,
                    "equity_return": equity_ret,
                    "defensive_asset": defensive_asset,
                    "defensive_return": def_ret,
                    "period_return": period_ret,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "rebalance_reasons": reasons,
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
    rule: VolTargetRule,
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
        "objective": "Test volatility-targeted scorecard+CSI risk-budget variants across all month phase and execution-lag cases.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "cash_annual_rate": CASH_ANNUAL_RATE,
        "us10y_duration_proxy": {
            "source_table": "us_tycr_daily",
            "yield_column": "y10",
            "duration": 7.0,
            "formula": "carry + duration price approximation; not a directly tradable total-return index",
        },
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

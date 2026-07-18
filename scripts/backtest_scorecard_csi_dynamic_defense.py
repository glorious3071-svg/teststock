#!/usr/bin/env python3
"""Backtest dynamic scorecard+CSI risk control with optional defensive legs.

This is an experiment, not a production rule.  It tests whether moving from a
fixed quarterly overlay to monthly trend/drawdown controls can make the
scorecard+CSI portfolio more robust across random month-start phases.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from bisect import bisect_right
from calendar import monthrange
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
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
OUT_JSON = OUT_DIR / "scorecard_csi_dynamic_defense_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_dynamic_defense_search.csv"

EXECUTION_LAGS = [0, 1, 3, 5]
MONTH_PHASES = list(range(12))
GOLD_CODE = "AU9999.SGE"
SPX_CODE = "SPX.US"


@dataclass(frozen=True)
class DynamicRule:
    name: str
    trend_3m_cap_pct: float
    trend_3m_lte: float
    trend_6m_cap_pct: float
    trend_6m_lte: float
    drawdown_cap_pct: float
    drawdown_lte: float
    opportunity_floor_score_lte: int
    opportunity_floor_pct: float
    max_equity_pct: float
    defensive_asset: str
    defensive_trend_months: int = 6


RULES = [
    DynamicRule("baseline_monthly", 100.0, -1.0, 100.0, -1.0, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend3m_cap30_cash", 30.0, -0.06, 100.0, -1.0, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend3m_cap0_cash", 0.0, -0.08, 100.0, -1.0, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend6m_cap30_cash", 100.0, -1.0, 30.0, -0.10, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend_combo_cap30_cash", 30.0, -0.06, 30.0, -0.10, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend_combo_cap0_cash", 0.0, -0.08, 20.0, -0.12, 100.0, -1.0, -3, 95.0, 100.0, "cash"),
    DynamicRule("drawdown10_cap30_cash", 100.0, -1.0, 100.0, -1.0, 30.0, -0.10, -3, 95.0, 100.0, "cash"),
    DynamicRule("drawdown8_cap0_cash", 100.0, -1.0, 100.0, -1.0, 0.0, -0.08, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend_drawdown_cash", 30.0, -0.06, 30.0, -0.10, 20.0, -0.08, -3, 95.0, 100.0, "cash"),
    DynamicRule("trend_drawdown_gold", 30.0, -0.06, 30.0, -0.10, 20.0, -0.08, -3, 95.0, 100.0, "gold_if_up"),
    DynamicRule("trend_drawdown_spx", 30.0, -0.06, 30.0, -0.10, 20.0, -0.08, -3, 95.0, 100.0, "spx_if_up"),
    DynamicRule("lower_max80_trend_cash", 30.0, -0.06, 30.0, -0.10, 20.0, -0.08, -3, 80.0, 80.0, "cash"),
]

_SCORECARD_TARGET_CACHE: dict[tuple[int, date], tuple[float, dict[str, Any]]] = {}


def month_end_shift(boundary: date, offset_months: int) -> date:
    month_index = boundary.year * 12 + boundary.month - 1 + offset_months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, monthrange(year, month)[1])


def apply_year_for_snapshot(snapshot: date) -> int:
    if snapshot.month == 12 and snapshot.day == 31:
        return snapshot.year + 1
    return snapshot.year


def holding_codes_for_snapshot(holdings: dict[int, list[str]], snapshot: date) -> list[str]:
    apply_year = apply_year_for_snapshot(snapshot)
    if apply_year >= 2014:
        return holdings.get(apply_year, []) or [CS300_CODE]
    return [CS300_CODE]


def load_price_series(conn) -> dict[str, list[tuple[date, float]]]:
    holdings = load_hybrid_holdings()
    codes = sorted({CS300_CODE, *[code for rows in holdings.values() for code in rows]})
    series: dict[str, list[tuple[date, float]]] = {code: [] for code in codes}
    with conn.cursor() as cur:
        for chunk_start in range(0, len(codes), 500):
            chunk = codes[chunk_start : chunk_start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM index_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, trade_date, close in cur.fetchall():
                series.setdefault(str(code), []).append((trade_date, float(close)))
        cur.execute(
            """
            SELECT symbol, trade_date, close
            FROM gold_daily
            WHERE symbol=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (GOLD_CODE,),
        )
        series[GOLD_CODE] = [(trade_date, float(close)) for _code, trade_date, close in cur.fetchall()]
        cur.execute(
            """
            SELECT ts_code, trade_date, close
            FROM us_index_daily
            WHERE ts_code=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (SPX_CODE,),
        )
        series[SPX_CODE] = [(trade_date, float(close)) for _code, trade_date, close in cur.fetchall()]
    return series


def price_at(series: dict[str, list[tuple[date, float]]], code: str, boundary: date) -> float | None:
    rows = series.get(code) or []
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def period_return(series: dict[str, list[tuple[date, float]]], code: str, start: date, end: date) -> float:
    start_price = price_at(series, code, start)
    end_price = price_at(series, code, end)
    if not start_price or not end_price or start_price <= 0:
        return 0.0
    return end_price / start_price - 1.0


def shifted_boundary(trade_dates: list[date], boundary: date, lag_days: int) -> date:
    if lag_days == 0:
        i = bisect_right(trade_dates, boundary) - 1
    else:
        i = bisect_right(trade_dates, boundary) + lag_days - 1
    if i < 0:
        return boundary
    if i >= len(trade_dates):
        return trade_dates[-1]
    return trade_dates[i]


def cash_return(start: date, end: date) -> float:
    return CASH_ANNUAL_RATE * max((end - start).days, 0) / 365.25


def defensive_return(series: dict[str, list[tuple[date, float]]], rule: DynamicRule, start: date, end: date) -> tuple[float, str]:
    if rule.defensive_asset == "gold_if_up":
        trend_start = month_end_shift(start, -rule.defensive_trend_months)
        trend = period_return(series, GOLD_CODE, trend_start, start)
        if trend > 0 and price_at(series, GOLD_CODE, start) is not None:
            return period_return(series, GOLD_CODE, start, end), GOLD_CODE
    if rule.defensive_asset == "spx_if_up":
        trend_start = month_end_shift(start, -rule.defensive_trend_months)
        trend = period_return(series, SPX_CODE, trend_start, start)
        if trend > 0 and price_at(series, SPX_CODE, start) is not None:
            return period_return(series, SPX_CODE, start, end), SPX_CODE
    return cash_return(start, end), "cash"


def monthly_boundaries(start_year: int, end_year: int, phase_month_offset: int) -> list[tuple[date, date]]:
    first = month_end_shift(date(start_year - 1, 12, 31), phase_month_offset)
    last = month_end_shift(date(end_year, 12, 31), phase_month_offset)
    out = []
    cur = first
    while cur < last:
        nxt = month_end_shift(cur, 1)
        out.append((cur, nxt))
        cur = nxt
    return out


def scorecard_target(conn, snapshot: date, rule: DynamicRule) -> tuple[float, dict[str, Any]]:
    apply_year = apply_year_for_snapshot(snapshot)
    key = (apply_year, snapshot)
    if key not in _SCORECARD_TARGET_CACHE:
        detail = scorecard_detail(conn, apply_year, snapshot, DEFAULT_RULE)
        _SCORECARD_TARGET_CACHE[key] = (float(detail["rule_target_equity_pct"]), detail)
    base_target, detail = _SCORECARD_TARGET_CACHE[key]
    target = base_target
    if int(detail["score"]) <= rule.opportunity_floor_score_lte:
        target = max(target, rule.opportunity_floor_pct)
    target = min(target, rule.max_equity_pct)
    return target, detail


def apply_dynamic_caps(
    target_pct: float,
    series: dict[str, list[tuple[date, float]]],
    rule: DynamicRule,
    snapshot: date,
    portfolio_drawdown: float,
) -> tuple[float, list[str]]:
    reasons = []
    cs300_3m = period_return(series, CS300_CODE, month_end_shift(snapshot, -3), snapshot)
    cs300_6m = period_return(series, CS300_CODE, month_end_shift(snapshot, -6), snapshot)
    if cs300_3m <= rule.trend_3m_lte:
        target_pct = min(target_pct, rule.trend_3m_cap_pct)
        reasons.append("cs300_3m_trend_cap")
    if cs300_6m <= rule.trend_6m_lte:
        target_pct = min(target_pct, rule.trend_6m_cap_pct)
        reasons.append("cs300_6m_trend_cap")
    if portfolio_drawdown <= rule.drawdown_lte:
        target_pct = min(target_pct, rule.drawdown_cap_pct)
        reasons.append("portfolio_drawdown_cap")
    return target_pct, reasons


def summarize(name: str, capital: float, curve: list[float], rows: list[dict[str, Any]], years: int) -> dict[str, Any]:
    mdd = max_drawdown(curve)
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
    trade_dates: list[date],
    holdings: dict[int, list[str]],
    rule: DynamicRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset)
    for start_snapshot, end_snapshot in periods:
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        codes = holding_codes_for_snapshot(holdings, start_snapshot)
        base_target, detail = scorecard_target(conn, start_snapshot, rule)
        drawdown = capital / peak - 1.0
        target_pct, reasons = apply_dynamic_caps(base_target, series, rule, start_snapshot, drawdown)
        equity_returns = [period_return(series, code, start_exec, end_exec) for code in codes]
        equity_return = sum(equity_returns) / len(equity_returns) if equity_returns else 0.0
        def_return, defensive_asset = defensive_return(series, rule, start_exec, end_exec)
        period_ret = target_pct / 100.0 * equity_return + (1.0 - target_pct / 100.0) * def_return
        capital *= 1.0 + period_ret
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "period": start_snapshot.isoformat(),
                    "execution_lag_days": execution_lag_days,
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "score": detail["score"],
                    "target_equity_pct": target_pct,
                    "equity_return": equity_return,
                    "defensive_asset": defensive_asset,
                    "defensive_return": def_return,
                    "period_return": period_ret,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "rebalance_reasons": reasons,
                }
            )
    return summarize(
        f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        END_YEAR - START_YEAR + 1,
    ) | {
        "rule": rule.name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
    }


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": median(item["final_capital_wan"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": median(item["max_drawdown"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
    }


def evaluate_rule(conn, series: dict[str, list[tuple[date, float]]], trade_dates: list[date], rule: DynamicRule) -> dict[str, Any]:
    holdings = load_hybrid_holdings()
    cases = [
        run_case(conn, series, trade_dates, holdings, rule, phase, lag, include_rows=False)
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
        "objective": "Test dynamic monthly risk/defensive controls for scorecard+CSI robustness.",
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
        trade_dates = [d for d, _px in series[CS300_CODE]]
        results = []
        for rule in RULES:
            result = evaluate_rule(conn, series, trade_dates, rule)
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

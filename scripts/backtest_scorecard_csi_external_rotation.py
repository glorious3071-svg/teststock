#!/usr/bin/env python3
"""Backtest long-sample external asset rotation as a scorecard+CSI hedge sleeve.

This is a portfolio-risk experiment for the scorecard+CSI objective. It tests
whether adding cached external ETFs/index proxies can meet the same random
month-drift target before wiring them into production holdings.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES
from scripts.backtest_scorecard_csi_midyear_risk import (
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_external_rotation_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_external_rotation_search.csv"

SYMBOLS = ["SPY", "QQQ", "TLT", "IEF", "SHY", "GLD", "DBC", "UUP", "^VIX"]
RISK_SYMBOLS = {
    "us_equity": ("QQQ", "SPY"),
    "us_equity_gold": ("QQQ", "SPY", "GLD"),
    "us_equity_commodity": ("QQQ", "SPY", "GLD", "DBC"),
    "qqq_only": ("QQQ",),
}
DEFENSE_SYMBOLS = {
    "bond_gold": ("TLT", "IEF", "GLD", "SHY"),
    "ief_gold": ("IEF", "GLD", "SHY"),
    "tlt_gold": ("TLT", "GLD"),
    "cash_like": ("SHY",),
}


@dataclass(frozen=True)
class RotationRule:
    name: str
    risk_key: str
    defense_key: str
    top_n: int
    target_vol: float
    max_leverage: float
    risk_off_vix: float
    risk_off_spy_3m: float = -0.08
    risk_off_max_leverage: float = 1.0
    drawdown_stop: float = -1.0
    drawdown_cap: float = 9.0
    fallback_symbol: str = "SHY"


RULES = [
    RotationRule("rot_balanced_vix25", "us_equity_gold", "bond_gold", 2, 0.16, 2.0, 25.0),
    RotationRule("rot_balanced_vix30", "us_equity_gold", "bond_gold", 2, 0.20, 2.5, 30.0),
    RotationRule("rot_growth_vix25", "us_equity", "bond_gold", 2, 0.25, 3.0, 25.0),
    RotationRule("rot_growth_vix30", "us_equity", "bond_gold", 2, 0.30, 3.0, 30.0),
    RotationRule("rot_aggressive_vix30", "us_equity", "bond_gold", 2, 0.35, 4.0, 30.0),
    RotationRule("rot_qqq_vix25", "qqq_only", "ief_gold", 1, 0.25, 3.0, 25.0),
    RotationRule("rot_qqq_vix30", "qqq_only", "ief_gold", 1, 0.30, 4.0, 30.0),
    RotationRule("rot_commodity_vix25", "us_equity_commodity", "bond_gold", 3, 0.20, 2.5, 25.0),
    RotationRule("rot_commodity_vix30", "us_equity_commodity", "bond_gold", 3, 0.25, 3.0, 30.0),
    RotationRule("rot_cash_guard", "us_equity", "cash_like", 2, 0.20, 2.5, 25.0),
    RotationRule("rot_low_dd_vix20", "us_equity_gold", "ief_gold", 1, 0.10, 1.0, 20.0, drawdown_stop=-0.08, drawdown_cap=0.3),
    RotationRule("rot_low_dd_vix25", "us_equity_gold", "ief_gold", 1, 0.12, 1.2, 25.0, drawdown_stop=-0.10, drawdown_cap=0.4),
    RotationRule("rot_mid_dd_vix25", "us_equity_gold", "bond_gold", 2, 0.16, 1.5, 25.0, drawdown_stop=-0.10, drawdown_cap=0.5),
]


class ExternalSeries:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates_by_symbol = {symbol: [row[0] for row in rows] for symbol, rows in rows_by_symbol.items()}
        self.feature_cache: dict[tuple[str, dt.date], tuple[float, float, float] | None] = {}
        self.return_cache: dict[tuple[str, dt.date, dt.date], float] = {}

    def index_at(self, symbol: str, day: dt.date) -> int:
        return bisect_right(self.dates_by_symbol[symbol], day) - 1

    def price(self, symbol: str, day: dt.date) -> float | None:
        idx = self.index_at(symbol, day)
        if idx < 0:
            return None
        return self.rows_by_symbol[symbol][idx][1]

    def period_return(self, symbol: str, start: dt.date, end: dt.date) -> float:
        key = (symbol, start, end)
        if key in self.return_cache:
            return self.return_cache[key]
        start_px = self.price(symbol, start)
        end_px = self.price(symbol, end)
        value = 0.0 if start_px is None or end_px is None else end_px / start_px - 1.0
        self.return_cache[key] = value
        return value

    def feature(self, symbol: str, day: dt.date) -> tuple[float, float, float] | None:
        key = (symbol, day)
        if key in self.feature_cache:
            return self.feature_cache[key]
        rows = self.rows_by_symbol[symbol]
        idx = self.index_at(symbol, day)
        if idx - 252 < 1:
            self.feature_cache[key] = None
            return None
        ret_12m = rows[idx][1] / rows[idx - 252][1] - 1.0
        ret_6m = rows[idx][1] / rows[idx - 126][1] - 1.0
        daily = [rows[j][1] / rows[j - 1][1] - 1.0 for j in range(idx - 62, idx + 1)]
        mean = sum(daily) / len(daily)
        vol = math.sqrt(sum((item - mean) ** 2 for item in daily) / len(daily)) * math.sqrt(252.0)
        self.feature_cache[key] = (ret_12m, ret_6m, vol)
        return self.feature_cache[key]


def load_external_series(conn) -> ExternalSeries:
    rows_by_symbol: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        for symbol in SYMBOLS:
            cur.execute(
                """
                SELECT trade_date, COALESCE(adj_close, close)
                FROM external_asset_daily
                WHERE symbol=%s AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (symbol, f"{START_YEAR - 2}-01-01", f"{END_YEAR + 1}-01-15"),
            )
            rows = [(row[0], float(row[1])) for row in cur.fetchall() if row[1] is not None]
            if not rows:
                raise RuntimeError(f"missing external_asset_daily rows for {symbol}")
            rows_by_symbol[symbol] = rows
    return ExternalSeries(rows_by_symbol)


def load_trade_dates(conn) -> list[dt.date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT trade_date
            FROM external_asset_daily
            WHERE symbol='SPY' AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date
            """,
            (f"{START_YEAR - 2}-01-01", f"{END_YEAR + 1}-01-15"),
        )
        return [row[0] for row in cur.fetchall()]


def shifted_boundary(trade_dates: list[dt.date], day: dt.date, lag: int) -> dt.date:
    idx = max(0, bisect_right(trade_dates, day) - 1)
    return trade_dates[min(idx + lag, len(trade_dates) - 1)]


def monthly_boundaries(phase_month_offset: int) -> list[tuple[dt.date, dt.date]]:
    year = START_YEAR
    month = 1 + phase_month_offset
    while month > 12:
        year += 1
        month -= 12
    periods = []
    while year <= END_YEAR:
        start = dt.date(year, month, 1)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        end = min(dt.date(next_year, next_month, 1), dt.date(END_YEAR + 1, 1, 1))
        periods.append((start, end))
        year, month = next_year, next_month
    return periods


def risk_off(series: ExternalSeries, snapshot: dt.date, rule: RotationRule) -> bool:
    vix = series.price("^VIX", snapshot)
    spy_feature = series.feature("SPY", snapshot)
    spy_3m = None
    idx = series.index_at("SPY", snapshot)
    if idx - 63 >= 0:
        rows = series.rows_by_symbol["SPY"]
        spy_3m = rows[idx][1] / rows[idx - 63][1] - 1.0
    return (vix is None or vix >= rule.risk_off_vix) or (spy_3m is None or spy_3m < rule.risk_off_spy_3m)


def choose_return(
    series: ExternalSeries,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    rule: RotationRule,
    portfolio_drawdown: float,
) -> tuple[float, float, str, list[str]]:
    is_risk_off = risk_off(series, snapshot, rule)
    universe = DEFENSE_SYMBOLS[rule.defense_key] if is_risk_off else RISK_SYMBOLS[rule.risk_key]
    scored = []
    for symbol in universe:
        feature = series.feature(symbol, snapshot)
        if feature is None:
            continue
        ret_12m, ret_6m, vol = feature
        raw_score = 0.7 * ret_12m + 0.3 * ret_6m
        if raw_score <= 0.0:
            continue
        scored.append((raw_score / (vol + 0.001), raw_score, vol, symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        fallback_return = series.period_return(rule.fallback_symbol, start_exec, end_exec)
        return fallback_return, 0.0, rule.fallback_symbol, ["fallback"]

    inv_vol = [1.0 / max(item[2], 0.04) for item in picks]
    denom = sum(inv_vol)
    avg_vol = sum(weight * item[2] for weight, item in zip(inv_vol, picks)) / denom
    leverage = min(rule.max_leverage, rule.target_vol / max(avg_vol, 0.04))
    if is_risk_off:
        leverage = min(leverage, rule.risk_off_max_leverage)
    if portfolio_drawdown <= rule.drawdown_stop:
        leverage = min(leverage, rule.drawdown_cap)
    asset_return = sum(
        leverage * weight / denom * series.period_return(item[3], start_exec, end_exec)
        for weight, item in zip(inv_vol, picks)
    )
    residual_return = (1.0 - leverage) * series.period_return(rule.fallback_symbol, start_exec, end_exec)
    return asset_return + residual_return, leverage, ",".join(item[3] for item in picks), ["risk_off" if is_risk_off else "risk_on"]


def run_case(series: ExternalSeries, trade_dates: list[dt.date], rule: RotationRule, phase: int, lag: int) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    risk_off_count = 0
    avg_leverage = []
    for snapshot, end_snapshot in monthly_boundaries(phase):
        start_exec = shifted_boundary(trade_dates, snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        month_return, leverage, _picks, reasons = choose_return(
            series,
            snapshot,
            start_exec,
            end_exec,
            rule,
            capital / peak - 1.0,
        )
        if "risk_off" in reasons:
            risk_off_count += 1
        avg_leverage.append(leverage)
        capital *= 1.0 + month_return
        peak = max(peak, capital)
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": f"{rule.name}_phase{phase}_lag{lag}",
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "risk_off_count": risk_off_count,
        "avg_leverage": statistics.mean(avg_leverage) if avg_leverage else 0.0,
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
        "median_risk_off_count": statistics.median(item["risk_off_count"] for item in items),
        "median_avg_leverage": statistics.median(item["avg_leverage"] for item in items),
    }


def evaluate_rule(series: ExternalSeries, trade_dates: list[dt.date], rule: RotationRule) -> dict[str, Any]:
    cases = [
        run_case(series, trade_dates, rule, phase, lag)
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
        "objective": "Test cached external ETF/index rotation as a risk/hedge sleeve for the scorecard+CSI objective.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "symbols": SYMBOLS,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
            "median_risk_off_count",
            "median_avg_leverage",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {"name": item["rule"]["name"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_external_series(conn)
        trade_dates = load_trade_dates(conn)
    finally:
        conn.close()
    results = []
    for rule in RULES:
        result = evaluate_rule(series, trade_dates, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name:<24} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
            f"risk_off={summary['median_risk_off_count']:.0f}"
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

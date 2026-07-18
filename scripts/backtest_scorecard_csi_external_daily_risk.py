#!/usr/bin/env python3
"""Backtest daily external risk controls for the scorecard+CSI target.

The monthly external-rotation experiment showed that month-end risk switches
still allow large drawdowns. This script tests whether observable daily VIX,
trend, and volatility controls on cached external ETFs can satisfy the same
all-phase target before any such sleeve is considered for production use.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
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
OUT_JSON = OUT_DIR / "scorecard_csi_external_daily_risk_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_external_daily_risk_search.csv"

SYMBOLS = ["SPY", "QQQ", "TLT", "IEF", "SHY", "GLD", "DBC", "^VIX"]
RISK_UNIVERSES = {
    "qqq": ("QQQ",),
    "us_equity": ("QQQ", "SPY"),
    "risk_mix": ("QQQ", "SPY", "GLD"),
}
DEFENSE_UNIVERSES = {
    "cash_like": ("SHY",),
    "bond_gold": ("TLT", "IEF", "GLD", "SHY"),
    "ief_gold": ("IEF", "GLD", "SHY"),
}


@dataclass(frozen=True)
class DailyRiskRule:
    name: str
    risk_key: str
    defense_key: str
    target_vol: float
    max_leverage: float
    risk_off_vix: float
    min_q_1m: float
    min_q_3m: float
    min_q_12m: float
    top_n: int = 1
    defense_min_6m: float = -0.05
    risk_off_leverage: float = 1.0
    portfolio_stop: float = -1.0
    stop_leverage: float = 0.0
    stop_reentry_drawdown: float = -1.0
    fallback_symbol: str = "SHY"


RULES = [
    DailyRiskRule("daily_q_vix25_tv20", "qqq", "bond_gold", 0.20, 2.0, 25.0, -0.05, -0.08, -0.10),
    DailyRiskRule("daily_q_vix30_tv25", "qqq", "bond_gold", 0.25, 2.5, 30.0, -0.05, -0.08, -0.10),
    DailyRiskRule("daily_q_vix30_tv30", "qqq", "bond_gold", 0.30, 3.0, 30.0, -0.08, -0.10, -0.15),
    DailyRiskRule("daily_us_vix25_tv20", "us_equity", "bond_gold", 0.20, 2.0, 25.0, -0.05, -0.08, -0.10, top_n=2),
    DailyRiskRule("daily_us_vix30_tv30", "us_equity", "bond_gold", 0.30, 3.0, 30.0, -0.08, -0.10, -0.15, top_n=2),
    DailyRiskRule("daily_mix_vix25_tv16", "risk_mix", "ief_gold", 0.16, 1.5, 25.0, -0.03, -0.05, -0.05, top_n=2),
    DailyRiskRule("daily_mix_vix30_tv22", "risk_mix", "bond_gold", 0.22, 2.0, 30.0, -0.05, -0.08, -0.10, top_n=2),
    DailyRiskRule("daily_lowdd_vix20", "risk_mix", "ief_gold", 0.10, 1.0, 20.0, 0.00, 0.00, 0.00),
    DailyRiskRule("daily_lowdd_vix25", "risk_mix", "ief_gold", 0.12, 1.2, 25.0, -0.02, -0.03, -0.05),
    DailyRiskRule("daily_stop8_tv25", "qqq", "bond_gold", 0.25, 2.5, 30.0, -0.05, -0.08, -0.10, portfolio_stop=-0.08, stop_leverage=0.0, stop_reentry_drawdown=-0.03),
    DailyRiskRule("daily_stop10_tv30", "qqq", "bond_gold", 0.30, 3.0, 30.0, -0.08, -0.10, -0.15, portfolio_stop=-0.10, stop_leverage=0.3, stop_reentry_drawdown=-0.04),
    DailyRiskRule("daily_cash_vix25_tv20", "us_equity", "cash_like", 0.20, 2.0, 25.0, -0.05, -0.08, -0.10, top_n=2),
]


class DailyData:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        # Keep the pre-2006 lookback window in the aligned calendar so 2006 month
        # phase starts do not collapse to the same first feature-ready day.
        self.common_dates = [
            day
            for day, _value in rows_by_symbol["SPY"]
            if dt.date(START_YEAR - 2, 1, 1) <= day <= dt.date(END_YEAR, 12, 31)
        ]
        self.date_to_idx = {day: idx for idx, day in enumerate(self.common_dates)}
        self.prices = {symbol: self._aligned_prices(symbol) for symbol in SYMBOLS}
        self.daily_returns = {symbol: self._daily_returns(symbol) for symbol in SYMBOLS}
        self.features = {symbol: self._features(symbol) for symbol in SYMBOLS}

    def _price_at(self, symbol: str, day: dt.date) -> float | None:
        rows = self.rows_by_symbol[symbol]
        dates = [row[0] for row in rows]
        idx = bisect_right(dates, day) - 1
        if idx < 0:
            return None
        return rows[idx][1]

    def _aligned_prices(self, symbol: str) -> list[float | None]:
        return [self._price_at(symbol, day) for day in self.common_dates]

    def _daily_returns(self, symbol: str) -> list[float]:
        prices = self.prices[symbol]
        out = [0.0]
        for idx in range(1, len(prices)):
            prev = prices[idx - 1]
            current = prices[idx]
            out.append(0.0 if prev is None or current is None or prev <= 0 else current / prev - 1.0)
        return out

    def _features(self, symbol: str) -> list[dict[str, float | None]]:
        prices = self.prices[symbol]
        returns = self.daily_returns[symbol]
        out: list[dict[str, float | None]] = []
        for idx, current in enumerate(prices):
            item: dict[str, float | None] = {"ret_1m": None, "ret_3m": None, "ret_6m": None, "ret_12m": None, "vol_63": None}
            if current is not None and current > 0:
                for label, days in [("ret_1m", 21), ("ret_3m", 63), ("ret_6m", 126), ("ret_12m", 252)]:
                    if idx - days >= 0 and prices[idx - days] is not None and prices[idx - days] > 0:
                        item[label] = current / prices[idx - days] - 1.0
                if idx - 63 >= 0:
                    window = returns[idx - 62 : idx + 1]
                    mean = sum(window) / len(window)
                    item["vol_63"] = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window)) * math.sqrt(252.0)
            out.append(item)
        return out

    def start_index(self, phase_month_offset: int, execution_lag_days: int) -> int:
        year = START_YEAR
        month = 1 + phase_month_offset
        while month > 12:
            year += 1
            month -= 12
        boundary = dt.date(year, month, 1)
        idx = max(0, bisect_left(self.common_dates, boundary))
        return min(idx + execution_lag_days, len(self.common_dates) - 1)


def load_daily_data(conn) -> DailyData:
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
                (symbol, f"{START_YEAR - 2}-01-01", f"{END_YEAR}-12-31"),
            )
            rows = [(row[0], float(row[1])) for row in cur.fetchall() if row[1] is not None]
            if not rows:
                raise RuntimeError(f"missing external_asset_daily rows for {symbol}")
            rows_by_symbol[symbol] = rows
    return DailyData(rows_by_symbol)


def select_leg(data: DailyData, rule: DailyRiskRule, idx: int, risk_off: bool) -> tuple[list[tuple[str, float]], float, str]:
    universe = DEFENSE_UNIVERSES[rule.defense_key] if risk_off else RISK_UNIVERSES[rule.risk_key]
    scored = []
    for symbol in universe:
        feat = data.features[symbol][idx - 1]
        ret_6m = feat["ret_6m"]
        ret_12m = feat["ret_12m"]
        vol = feat["vol_63"]
        if ret_6m is None or ret_12m is None or vol is None:
            continue
        raw_score = 0.65 * ret_12m + 0.35 * ret_6m
        if risk_off and ret_6m <= rule.defense_min_6m:
            continue
        if not risk_off and raw_score <= 0:
            continue
        scored.append((raw_score / (vol + 0.001), vol, symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        return [(rule.fallback_symbol, 1.0)], 0.0, "fallback"
    inv_vol = [1.0 / max(item[1], 0.04) for item in picks]
    denom = sum(inv_vol)
    avg_vol = sum(weight * item[1] for weight, item in zip(inv_vol, picks)) / denom
    leverage = min(rule.max_leverage, rule.target_vol / max(avg_vol, 0.04))
    if risk_off:
        leverage = min(leverage, rule.risk_off_leverage)
    weights = [(item[2], leverage * weight / denom) for weight, item in zip(inv_vol, picks)]
    residual = 1.0 - leverage
    if abs(residual) > 1e-9:
        weights.append((rule.fallback_symbol, residual))
    return weights, leverage, ",".join(item[2] for item in picks)


def risk_off_state(data: DailyData, rule: DailyRiskRule, idx: int) -> bool:
    q = data.features["QQQ"][idx - 1]
    vix = data.prices["^VIX"][idx - 1]
    if vix is None or vix >= rule.risk_off_vix:
        return True
    checks = [
        (q["ret_1m"], rule.min_q_1m),
        (q["ret_3m"], rule.min_q_3m),
        (q["ret_12m"], rule.min_q_12m),
    ]
    return any(value is None or value < threshold for value, threshold in checks)


def run_case(data: DailyData, rule: DailyRiskRule, phase: int, lag: int) -> dict[str, Any]:
    start_idx = data.start_index(phase, lag)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    risk_off_days = 0
    stopped_days = 0
    avg_leverage = []
    stopped = False
    for idx in range(max(start_idx + 1, 253), len(data.common_dates)):
        portfolio_drawdown = capital / peak - 1.0
        if stopped and portfolio_drawdown >= rule.stop_reentry_drawdown:
            stopped = False
        if portfolio_drawdown <= rule.portfolio_stop:
            stopped = True
        is_risk_off = risk_off_state(data, rule, idx)
        if stopped:
            weights = [(rule.fallback_symbol, 1.0 - rule.stop_leverage)]
            if rule.stop_leverage > 0:
                weights.append(("QQQ", rule.stop_leverage))
            leverage = rule.stop_leverage
            stopped_days += 1
        else:
            weights, leverage, _picks = select_leg(data, rule, idx, is_risk_off)
        if is_risk_off:
            risk_off_days += 1
        day_return = sum(weight * data.daily_returns[symbol][idx] for symbol, weight in weights)
        capital *= 1.0 + day_return
        peak = max(peak, capital)
        curve.append(capital)
        avg_leverage.append(leverage)
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
        "risk_off_days": risk_off_days,
        "stopped_days": stopped_days,
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
        "median_risk_off_days": statistics.median(item["risk_off_days"] for item in items),
        "median_stopped_days": statistics.median(item["stopped_days"] for item in items),
        "median_avg_leverage": statistics.median(item["avg_leverage"] for item in items),
    }


def evaluate_rule(data: DailyData, rule: DailyRiskRule) -> dict[str, Any]:
    cases = [run_case(data, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
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
        "objective": "Test daily VIX/trend/volatility risk controls on cached external ETF/index assets.",
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
            "median_risk_off_days",
            "median_stopped_days",
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
        data = load_daily_data(conn)
    finally:
        conn.close()
    results = []
    for rule in RULES:
        result = evaluate_rule(data, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name:<24} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
            f"risk_off_days={summary['median_risk_off_days']:.0f} "
            f"avg_lev={summary['median_avg_leverage']:.2f}"
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

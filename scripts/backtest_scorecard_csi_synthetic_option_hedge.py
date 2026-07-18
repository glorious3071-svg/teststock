#!/usr/bin/env python3
"""Backtest synthetic defined-loss option hedges with daily mark-to-market.

This experiment prices monthly/weekly reset QQQ/SPY put, put-spread, and collar
packages with Black-Scholes using cached VIX/VIX3M as implied-volatility proxies.
It is a modelled hedge-cost test, not executable option-chain evidence.
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
from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_synthetic_option_hedge_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_synthetic_option_hedge_search.csv"

SYMBOLS = ["QQQ", "SPY", "SHY", "^VIX", "VIX3M"]


@dataclass(frozen=True)
class SyntheticOptionRule:
    name: str
    underlying: str
    leverage: float
    put_strike_pct: float
    put_cover: float
    short_put_strike_pct: float = 0.0
    call_strike_pct: float = 0.0
    call_cover: float = 0.0
    iv_symbol: str = "^VIX"
    iv_multiplier: float = 1.0
    risk_off_vix: float = 1_000.0
    risk_off_leverage: float = 1.0
    risk_off_put_cover: float = 1.0
    risk_off_call_cover: float = 0.0
    financing_spread_annual: float = 0.015
    reset_trading_days: int = 0


RULES = [
    SyntheticOptionRule("qqq_w_put98_call108_lev125", "QQQ", 1.25, 0.98, 1.0, call_strike_pct=1.08, call_cover=0.8, reset_trading_days=5),
    SyntheticOptionRule("qqq_w_put100_call106_lev125", "QQQ", 1.25, 1.00, 1.0, call_strike_pct=1.06, call_cover=0.9, reset_trading_days=5),
    SyntheticOptionRule("qqq_w_put95_call108_lev150", "QQQ", 1.5, 0.95, 1.0, call_strike_pct=1.08, call_cover=1.0, reset_trading_days=5),
    SyntheticOptionRule("qqq_bi_put98_call108_lev125", "QQQ", 1.25, 0.98, 1.0, call_strike_pct=1.08, call_cover=0.8, reset_trading_days=10),
    SyntheticOptionRule("qqq_bi_put95_call108_lev150", "QQQ", 1.5, 0.95, 1.0, call_strike_pct=1.08, call_cover=1.0, reset_trading_days=10),
    SyntheticOptionRule("qqq_put100_lev100", "QQQ", 1.0, 1.00, 1.0),
    SyntheticOptionRule("qqq_put100_lev125", "QQQ", 1.25, 1.00, 1.0),
    SyntheticOptionRule("qqq_put98_call108_lev125", "QQQ", 1.25, 0.98, 1.0, call_strike_pct=1.08, call_cover=0.8),
    SyntheticOptionRule("qqq_put98_call103_lev125", "QQQ", 1.25, 0.98, 1.0, call_strike_pct=1.03, call_cover=0.8),
    SyntheticOptionRule("qqq_put99_call102_lev125", "QQQ", 1.25, 0.99, 1.0, call_strike_pct=1.02, call_cover=0.8),
    SyntheticOptionRule("qqq_put98_95spread_call108_lev125", "QQQ", 1.25, 0.98, 1.0, short_put_strike_pct=0.95, call_strike_pct=1.08, call_cover=0.8),
    SyntheticOptionRule("qqq_put98_94spread_call108_lev125", "QQQ", 1.25, 0.98, 1.0, short_put_strike_pct=0.94, call_strike_pct=1.08, call_cover=0.8),
    SyntheticOptionRule("qqq_put95_call108_lev150", "QQQ", 1.5, 0.95, 1.0, call_strike_pct=1.08, call_cover=1.0),
    SyntheticOptionRule("qqq_put95_85spread_lev150", "QQQ", 1.5, 0.95, 1.0, short_put_strike_pct=0.85),
    SyntheticOptionRule("qqq_put95_lev150", "QQQ", 1.5, 0.95, 1.0),
    SyntheticOptionRule("qqq_put90_lev200", "QQQ", 2.0, 0.90, 1.0),
    SyntheticOptionRule("qqq_put95_lev200", "QQQ", 2.0, 0.95, 1.0),
    SyntheticOptionRule("qqq_put98_lev150", "QQQ", 1.5, 0.98, 1.0),
    SyntheticOptionRule("qqq_put95_call110_lev200", "QQQ", 2.0, 0.95, 1.0, call_strike_pct=1.10, call_cover=0.75),
    SyntheticOptionRule("qqq_put95_call115_lev250", "QQQ", 2.5, 0.95, 1.0, call_strike_pct=1.15, call_cover=0.75),
    SyntheticOptionRule("qqq_put95_80spread_lev250", "QQQ", 2.5, 0.95, 1.0, short_put_strike_pct=0.80),
    SyntheticOptionRule("qqq_put95_85spread_call115", "QQQ", 2.5, 0.95, 1.0, short_put_strike_pct=0.85, call_strike_pct=1.15, call_cover=0.5),
    SyntheticOptionRule("qqq_put98_call112_lev220", "QQQ", 2.2, 0.98, 1.0, call_strike_pct=1.12, call_cover=0.8),
    SyntheticOptionRule("qqq_dynamic_vix25", "QQQ", 2.4, 0.92, 0.8, call_strike_pct=1.15, call_cover=0.5, risk_off_vix=25.0, risk_off_leverage=1.2, risk_off_put_cover=1.2),
    SyntheticOptionRule("qqq_dynamic_vix30", "QQQ", 3.0, 0.90, 0.7, call_strike_pct=1.18, call_cover=0.5, risk_off_vix=30.0, risk_off_leverage=1.5, risk_off_put_cover=1.1),
    SyntheticOptionRule("spy_put95_lev250", "SPY", 2.5, 0.95, 1.0),
    SyntheticOptionRule("spy_put95_call112_lev300", "SPY", 3.0, 0.95, 1.0, call_strike_pct=1.12, call_cover=0.8),
]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(kind: str, spot: float, strike: float, years: float, rate: float, vol: float) -> float:
    if years <= 0:
        return max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    vol = max(vol, 0.03)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if kind == "call":
        return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
    return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


class OptionData:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates = [day for day, _value in rows_by_symbol["QQQ"] if dt.date(START_YEAR - 2, 1, 1) <= day <= dt.date(END_YEAR, 12, 31)]
        self.prices = {symbol: self._aligned_prices(symbol) for symbol in SYMBOLS}
        self.returns = {symbol: self._daily_returns(symbol) for symbol in SYMBOLS}

    def _price_at(self, symbol: str, day: dt.date) -> float | None:
        rows = self.rows_by_symbol[symbol]
        dates = [item[0] for item in rows]
        idx = bisect_right(dates, day) - 1
        return rows[idx][1] if idx >= 0 else None

    def _aligned_prices(self, symbol: str) -> list[float | None]:
        return [self._price_at(symbol, day) for day in self.dates]

    def _daily_returns(self, symbol: str) -> list[float]:
        prices = self.prices[symbol]
        out = [0.0]
        for idx in range(1, len(prices)):
            prev = prices[idx - 1]
            current = prices[idx]
            out.append(0.0 if prev is None or current is None or prev <= 0 else current / prev - 1.0)
        return out

    def start_index(self, phase: int, lag: int) -> int:
        year = START_YEAR
        month = 1 + phase
        while month > 12:
            year += 1
            month -= 12
        idx = bisect_left(self.dates, dt.date(year, month, 1))
        return min(idx + lag, len(self.dates) - 1)

    def next_month_index(self, idx: int, lag: int) -> int:
        day = self.dates[idx]
        next_year = day.year + (1 if day.month == 12 else 0)
        next_month = 1 if day.month == 12 else day.month + 1
        boundary = dt.date(next_year, next_month, 1)
        boundary_idx = bisect_left(self.dates, boundary)
        return min(boundary_idx + lag, len(self.dates) - 1)

    def next_reset_index(self, idx: int, lag: int, reset_trading_days: int) -> int:
        if reset_trading_days <= 0:
            return self.next_month_index(idx, lag)
        return min(idx + reset_trading_days + lag, len(self.dates) - 1)


def load_data(conn) -> OptionData:
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
    return OptionData(rows_by_symbol)


def option_package_value(
    data: OptionData,
    rule: SyntheticOptionRule,
    idx: int,
    expiry_idx: int,
    start_spot: float,
    long_put_cover: float,
    short_put_cover: float,
    call_cover: float,
    rate: float,
    iv: float,
) -> float:
    spot = data.prices[rule.underlying][idx]
    if spot is None:
        return 0.0
    years = max((data.dates[expiry_idx] - data.dates[idx]).days / 365.25, 0.0)
    value = long_put_cover * black_scholes("put", spot, start_spot * rule.put_strike_pct, years, rate, iv)
    if rule.short_put_strike_pct > 0 and short_put_cover:
        value -= short_put_cover * black_scholes("put", spot, start_spot * rule.short_put_strike_pct, years, rate, iv)
    if rule.call_strike_pct > 0 and call_cover:
        value -= call_cover * black_scholes("call", spot, start_spot * rule.call_strike_pct, years, rate, iv)
    return value


def run_case(data: OptionData, rule: SyntheticOptionRule, phase: int, lag: int) -> dict[str, Any]:
    idx = max(data.start_index(phase, lag), 253)
    end_limit = bisect_right(data.dates, dt.date(END_YEAR, 12, 31)) - 1
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    premium_paid = 0.0
    risk_off_months = 0
    while idx < end_limit:
        expiry_idx = min(data.next_reset_index(idx, lag, rule.reset_trading_days), end_limit)
        spot = data.prices[rule.underlying][idx]
        if spot is None or spot <= 0:
            idx += 1
            continue
        vix = data.prices[rule.iv_symbol][idx] or data.prices["^VIX"][idx] or 25.0
        is_risk_off = vix >= rule.risk_off_vix
        leverage = rule.risk_off_leverage if is_risk_off else rule.leverage
        put_cover = rule.risk_off_put_cover if is_risk_off else rule.put_cover
        call_cover = rule.risk_off_call_cover if is_risk_off else rule.call_cover
        if is_risk_off:
            risk_off_months += 1
        iv = max(0.05, min(1.5, vix / 100.0 * rule.iv_multiplier))
        rate = max(0.0, data.returns["SHY"][idx] * 252.0)
        underlying_units = capital * leverage / spot
        long_put_units = underlying_units * put_cover
        short_put_units = underlying_units * put_cover if rule.short_put_strike_pct > 0 else 0.0
        call_units = underlying_units * call_cover if rule.call_strike_pct > 0 else 0.0
        package_start_value = option_package_value(
            data,
            rule,
            idx,
            expiry_idx,
            spot,
            long_put_units,
            short_put_units,
            call_units,
            rate,
            iv,
        )
        cash = capital - underlying_units * spot - package_start_value
        premium_paid += max(package_start_value, 0.0)
        prev_value = capital
        for day_idx in range(idx + 1, expiry_idx + 1):
            current_spot = data.prices[rule.underlying][day_idx]
            if current_spot is None:
                continue
            cash *= 1.0 + data.returns["SHY"][day_idx]
            if cash < 0:
                cash -= abs(cash) * rule.financing_spread_annual / 252.0
            package_value = option_package_value(
                data,
                rule,
                day_idx,
                expiry_idx,
                spot,
                long_put_units,
                short_put_units,
                call_units,
                rate,
                iv,
            )
            current_value = underlying_units * current_spot + package_value + cash
            if current_value <= 0:
                current_value = 1.0
            curve.append(current_value)
            peak = max(peak, current_value)
            prev_value = current_value
        capital = prev_value
        idx = max(expiry_idx, idx + 1)
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
        "premium_paid_wan": premium_paid / 10_000.0,
        "risk_off_months": risk_off_months,
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
        "median_premium_paid_wan": statistics.median(item["premium_paid_wan"] for item in items),
        "median_risk_off_months": statistics.median(item["risk_off_months"] for item in items),
    }


def evaluate_rule(data: OptionData, rule: SyntheticOptionRule) -> dict[str, Any]:
    cases = [run_case(data, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test synthetic monthly reset option hedges priced with VIX/VIX3M proxies.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Black-Scholes proxy using VIX/VIX3M, no option-chain strike liquidity, bid/ask, skew, or execution terms.",
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
            "median_premium_paid_wan",
            "median_risk_off_months",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {"name": item["rule"]["name"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        data = load_data(conn)
    finally:
        conn.close()
    results = []
    for rule in RULES:
        result = evaluate_rule(data, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name:<28} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
            f"prem={summary['median_premium_paid_wan']:8.1f}万"
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

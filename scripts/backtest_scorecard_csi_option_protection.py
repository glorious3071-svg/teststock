#!/usr/bin/env python3
"""Backtest CBOE option-strategy indices as priced protection sleeves.

The external daily risk tests showed that linear de-risking does not meet the
target. This experiment uses CBOE option-strategy indices such as PPUT and VXTH,
whose histories include option roll costs, as protection sleeves.
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
OUT_JSON = OUT_DIR / "scorecard_csi_option_protection_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_option_protection_search.csv"

SYMBOLS = ["QQQ", "SPY", "SHY", "GLD", "PPUT", "PUT", "BXM", "BXMD", "CLLZ", "VXTH", "VPD", "^VIX", "VVIX"]


@dataclass(frozen=True)
class OptionProtectionRule:
    name: str
    growth_symbol: str
    protection_symbol: str
    protection_weight: float
    target_vol: float
    max_leverage: float
    risk_off_vix: float
    risk_off_growth_3m: float
    risk_off_protection_weight: float
    risk_off_max_leverage: float
    portfolio_stop: float = -1.0
    stop_protection_weight: float = 1.0
    stop_max_leverage: float = 1.0
    fallback_symbol: str = "SHY"


RULES = [
    OptionProtectionRule("qqq_vxth_15_vix25", "QQQ", "VXTH", 0.15, 0.22, 2.0, 25.0, -0.08, 0.60, 1.0),
    OptionProtectionRule("qqq_vxth_25_vix25", "QQQ", "VXTH", 0.25, 0.24, 2.2, 25.0, -0.08, 0.75, 1.0),
    OptionProtectionRule("qqq_vxth_30_vix30", "QQQ", "VXTH", 0.30, 0.28, 2.8, 30.0, -0.10, 0.80, 1.2),
    OptionProtectionRule("qqq_pput_30_vix25", "QQQ", "PPUT", 0.30, 0.24, 2.2, 25.0, -0.08, 0.80, 1.0),
    OptionProtectionRule("qqq_pput_40_vix30", "QQQ", "PPUT", 0.40, 0.28, 2.8, 30.0, -0.10, 0.90, 1.2),
    OptionProtectionRule("qqq_bxmd_35_vix25", "QQQ", "BXMD", 0.35, 0.24, 2.2, 25.0, -0.08, 0.80, 1.0),
    OptionProtectionRule("qqq_cllz_40_vix25", "QQQ", "CLLZ", 0.40, 0.24, 2.2, 25.0, -0.08, 0.85, 1.0),
    OptionProtectionRule("spy_vxth_20_vix25", "SPY", "VXTH", 0.20, 0.20, 2.0, 25.0, -0.08, 0.70, 1.0),
    OptionProtectionRule("qqq_vxth_stop10", "QQQ", "VXTH", 0.20, 0.26, 2.5, 30.0, -0.10, 0.75, 1.2, portfolio_stop=-0.10, stop_protection_weight=1.0, stop_max_leverage=0.8),
    OptionProtectionRule("qqq_pput_stop10", "QQQ", "PPUT", 0.35, 0.26, 2.5, 30.0, -0.10, 0.90, 1.2, portfolio_stop=-0.10, stop_protection_weight=1.0, stop_max_leverage=0.8),
    OptionProtectionRule("qqq_vpd_30_vix25", "QQQ", "VPD", 0.30, 0.24, 2.2, 25.0, -0.08, 0.80, 1.0),
]


class DailyOptionData:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates = [day for day, _value in rows_by_symbol["QQQ"] if dt.date(START_YEAR - 2, 1, 1) <= day <= dt.date(END_YEAR, 12, 31)]
        self.prices = {symbol: self._aligned_prices(symbol) for symbol in SYMBOLS}
        self.returns = {symbol: self._daily_returns(symbol) for symbol in SYMBOLS}
        self.features = {symbol: self._features(symbol) for symbol in SYMBOLS}

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

    def _features(self, symbol: str) -> list[dict[str, float | None]]:
        prices = self.prices[symbol]
        returns = self.returns[symbol]
        out: list[dict[str, float | None]] = []
        for idx, current in enumerate(prices):
            item: dict[str, float | None] = {"ret_3m": None, "ret_12m": None, "vol_63": None}
            if current is not None and current > 0:
                if idx - 63 >= 0 and prices[idx - 63] is not None and prices[idx - 63] > 0:
                    item["ret_3m"] = current / prices[idx - 63] - 1.0
                if idx - 252 >= 0 and prices[idx - 252] is not None and prices[idx - 252] > 0:
                    item["ret_12m"] = current / prices[idx - 252] - 1.0
                if idx - 63 >= 0:
                    window = returns[idx - 62 : idx + 1]
                    mean = sum(window) / len(window)
                    item["vol_63"] = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window)) * math.sqrt(252.0)
            out.append(item)
        return out

    def start_index(self, phase: int, lag: int) -> int:
        year = START_YEAR
        month = 1 + phase
        while month > 12:
            year += 1
            month -= 12
        idx = bisect_left(self.dates, dt.date(year, month, 1))
        return min(idx + lag, len(self.dates) - 1)


def load_data(conn) -> DailyOptionData:
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
    return DailyOptionData(rows_by_symbol)


def risk_off(data: DailyOptionData, rule: OptionProtectionRule, idx: int) -> bool:
    vix = data.prices["^VIX"][idx - 1]
    growth_3m = data.features[rule.growth_symbol][idx - 1]["ret_3m"]
    return vix is None or vix >= rule.risk_off_vix or growth_3m is None or growth_3m < rule.risk_off_growth_3m


def run_case(data: DailyOptionData, rule: OptionProtectionRule, phase: int, lag: int) -> dict[str, Any]:
    start_idx = max(data.start_index(phase, lag), 253)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    risk_off_days = 0
    stopped_days = 0
    avg_leverage = []
    for idx in range(start_idx + 1, len(data.dates)):
        drawdown = capital / peak - 1.0
        stopped = drawdown <= rule.portfolio_stop
        is_risk_off = risk_off(data, rule, idx)
        protection_weight = rule.protection_weight
        max_leverage = rule.max_leverage
        if is_risk_off:
            protection_weight = rule.risk_off_protection_weight
            max_leverage = min(max_leverage, rule.risk_off_max_leverage)
            risk_off_days += 1
        if stopped:
            protection_weight = rule.stop_protection_weight
            max_leverage = min(max_leverage, rule.stop_max_leverage)
            stopped_days += 1

        growth_vol = data.features[rule.growth_symbol][idx - 1]["vol_63"] or 0.30
        protection_vol = data.features[rule.protection_symbol][idx - 1]["vol_63"] or 0.20
        blended_vol = max(0.04, (1.0 - protection_weight) * growth_vol + protection_weight * protection_vol)
        leverage = min(max_leverage, rule.target_vol / blended_vol)
        growth_weight = leverage * (1.0 - protection_weight)
        hedge_weight = leverage * protection_weight
        cash_weight = 1.0 - leverage
        day_return = (
            growth_weight * data.returns[rule.growth_symbol][idx]
            + hedge_weight * data.returns[rule.protection_symbol][idx]
            + cash_weight * data.returns[rule.fallback_symbol][idx]
        )
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


def evaluate_rule(data: DailyOptionData, rule: OptionProtectionRule) -> dict[str, Any]:
    cases = [run_case(data, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test CBOE option-strategy indices as priced protection sleeves.",
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
        data = load_data(conn)
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

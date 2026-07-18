#!/usr/bin/env python3
"""Backtest CPPI/TIPP portfolio insurance on cached daily assets.

Prior experiments showed that monthly stops, static blends, and synthetic option
collars could not satisfy the all-phase 4000w / -10% target.  This experiment
tests a different risk structure: daily dynamic exposure sizing from a capital
floor.  TIPP uses a trailing peak floor and is the relevant variant for maximum
drawdown control; CPPI uses the initial capital floor and is included as a return
contrast.

The test uses cached external_asset_daily prices only.  It is not a production
allocation rule.
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
OUT_JSON = OUT_DIR / "scorecard_csi_cppi_protection_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_cppi_protection_search.csv"

SYMBOLS = ["SPY", "QQQ", "GLD", "TLT", "IEF", "SHY", "^VIX"]
RISK_UNIVERSES = {
    "qqq": ("QQQ",),
    "spy": ("SPY",),
    "qqq_spy": ("QQQ", "SPY"),
    "growth_gold": ("QQQ", "SPY", "GLD"),
    "all_mom": ("QQQ", "SPY", "GLD", "TLT", "IEF"),
}


@dataclass(frozen=True)
class InsuranceRule:
    name: str
    mode: str
    risk_key: str
    floor_pct: float
    multiplier: float
    max_leverage: float
    top_n: int
    risk_off_vix: float = 1_000.0
    risk_off_scale: float = 1.0
    trend_12m_lte: float = -1.0
    trend_scale: float = 1.0
    min_exposure: float = 0.0
    rebalance: str = "daily"
    safe_symbol: str = "SHY"


def build_rules() -> list[InsuranceRule]:
    rules: list[InsuranceRule] = []
    for risk_key, top_n in [("qqq", 1), ("qqq_spy", 1), ("growth_gold", 2), ("all_mom", 2)]:
        for floor_pct in [0.90, 0.92, 0.95]:
            for multiplier, max_lev in [(4.0, 1.0), (6.0, 1.5), (8.0, 2.0), (12.0, 3.0), (16.0, 4.0)]:
                prefix = f"tipp_{risk_key}_top{top_n}_f{int(floor_pct*100)}_m{int(multiplier)}_l{int(max_lev*10)}"
                rules.append(InsuranceRule(prefix, "tipp", risk_key, floor_pct, multiplier, max_lev, top_n))
                rules.append(
                    InsuranceRule(
                        f"{prefix}_vix30",
                        "tipp",
                        risk_key,
                        floor_pct,
                        multiplier,
                        max_lev,
                        top_n,
                        risk_off_vix=30.0,
                        risk_off_scale=0.25,
                    )
                )
                rules.append(
                    InsuranceRule(
                        f"{prefix}_trend",
                        "tipp",
                        risk_key,
                        floor_pct,
                        multiplier,
                        max_lev,
                        top_n,
                        trend_12m_lte=0.0,
                        trend_scale=0.25,
                    )
                )
    for risk_key, top_n in [("qqq", 1), ("growth_gold", 2)]:
        for multiplier, max_lev in [(6.0, 1.5), (8.0, 2.0), (12.0, 3.0), (16.0, 4.0)]:
            prefix = f"cppi_{risk_key}_top{top_n}_f90_m{int(multiplier)}_l{int(max_lev*10)}"
            rules.append(InsuranceRule(prefix, "cppi", risk_key, 0.90, multiplier, max_lev, top_n))
    return rules


RULES = build_rules()


class DailyData:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates = [
            day
            for day, _value in rows_by_symbol["SPY"]
            if dt.date(START_YEAR - 2, 1, 1) <= day <= dt.date(END_YEAR, 12, 31)
        ]
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
            cur = prices[idx]
            out.append(0.0 if prev is None or cur is None or prev <= 0 else cur / prev - 1.0)
        return out

    def _features(self, symbol: str) -> list[dict[str, float | None]]:
        prices = self.prices[symbol]
        returns = self.returns[symbol]
        out: list[dict[str, float | None]] = []
        for idx, current in enumerate(prices):
            item: dict[str, float | None] = {"ret_3m": None, "ret_6m": None, "ret_12m": None, "vol_63": None}
            if current is not None and current > 0:
                for label, days in [("ret_3m", 63), ("ret_6m", 126), ("ret_12m", 252)]:
                    if idx - days >= 0 and prices[idx - days] is not None and prices[idx - days] > 0:
                        item[label] = current / prices[idx - days] - 1.0
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


def load_data(conn) -> DailyData:
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


def choose_risky_weights(data: DailyData, rule: InsuranceRule, idx: int) -> tuple[list[tuple[str, float]], str, float]:
    scored = []
    for symbol in RISK_UNIVERSES[rule.risk_key]:
        feat = data.features[symbol][idx - 1]
        ret_6m = feat["ret_6m"]
        ret_12m = feat["ret_12m"]
        vol = feat["vol_63"]
        if ret_6m is None or ret_12m is None or vol is None:
            continue
        score = 0.65 * ret_12m + 0.35 * ret_6m
        if score <= 0:
            continue
        scored.append((score / max(vol, 0.04), vol, symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        return [(rule.safe_symbol, 1.0)], "fallback", 0.0
    inv_vol = [1.0 / max(item[1], 0.04) for item in picks]
    denom = sum(inv_vol)
    weights = [(item[2], weight / denom) for weight, item in zip(inv_vol, picks)]
    trend_values = [float(data.features[item[2]][idx - 1]["ret_12m"] or 0.0) for item in picks]
    trend = sum(trend_values) / len(trend_values)
    return weights, ",".join(item[2] for item in picks), trend


def run_case(data: DailyData, rule: InsuranceRule, phase: int, lag: int) -> dict[str, Any]:
    start_idx = max(data.start_index(phase, lag), 253)
    end_idx = bisect_right(data.dates, dt.date(END_YEAR, 12, 31)) - 1
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    risk_off_days = 0
    avg_exposure = []
    for idx in range(start_idx + 1, end_idx + 1):
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_leverage, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        vix = data.prices["^VIX"][idx - 1] or 99.0
        weights, selected, trend = choose_risky_weights(data, rule, idx)
        if vix >= rule.risk_off_vix:
            exposure *= rule.risk_off_scale
            risk_off_days += 1
        if trend <= rule.trend_12m_lte:
            exposure *= rule.trend_scale
        day_return = 0.0
        for symbol, weight in weights:
            day_return += exposure * weight * data.returns[symbol][idx]
        day_return += (1.0 - exposure) * data.returns[rule.safe_symbol][idx]
        capital *= 1.0 + day_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
        avg_exposure.append(exposure)
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
        "avg_exposure": sum(avg_exposure) / len(avg_exposure) if avg_exposure else 0.0,
        "max_exposure": max(avg_exposure) if avg_exposure else 0.0,
        "risk_off_days": risk_off_days,
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
        "median_risk_off_days": statistics.median(item["risk_off_days"] for item in items),
    }


def evaluate_rule(data: DailyData, rule: InsuranceRule) -> dict[str, Any]:
    cases = [run_case(data, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test daily CPPI/TIPP portfolio insurance across all month phases and execution lags.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Uses cached daily ETF/index prices; TIPP controls drawdown through dynamic exposure but has no intraday gap or execution modelling.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "mode",
            "risk_key",
            "floor_pct",
            "multiplier",
            "max_leverage",
            "top_n",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_risk_off_days",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
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
            f"{rule.name[:72]:<72} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_exposure']:4.2f}"
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

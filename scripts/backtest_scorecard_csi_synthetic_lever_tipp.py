#!/usr/bin/env python3
"""Backtest synthetic leveraged Nasdaq/tech trend sleeves with daily TIPP.

The current frontier cannot reach the 4000w / -10% all-phase target with the
existing CSI sleeves, option proxies, or long-only ETF rotation. This experiment
tests a full-window synthetic leveraged alpha sleeve built from cached QQQ/XLK
daily returns, with daily portfolio insurance and trend/VIX risk-off rules.
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
OUT_JSON = OUT_DIR / "scorecard_csi_synthetic_lever_tipp_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_synthetic_lever_tipp_search.csv"

SYMBOLS = ["SPY", "QQQ", "XLK", "SHY", "TLT", "SH", "PSQ", "^VIX"]
RISK_UNIVERSES = {
    "qqq": ("QQQ",),
    "xlk": ("XLK",),
    "tech_best": ("QQQ", "XLK", "SPY"),
    "qqq_spy": ("QQQ", "SPY"),
}


@dataclass(frozen=True)
class SyntheticLeverRule:
    name: str
    mode: str
    risk_key: str
    leverage: float
    floor_pct: float
    multiplier: float
    max_exposure: float
    risk_off_vix: float
    risk_off_action: str
    trend_12m_lte: float = 0.0
    dist_ma200_lte: float = 0.0
    risk_off_scale: float = 1.0
    min_exposure: float = 0.0
    safe_symbol: str = "SHY"


def build_rules() -> list[SyntheticLeverRule]:
    rules: list[SyntheticLeverRule] = []
    alpha_specs = [
        ("qqq", 2.0),
        ("qqq", 3.0),
        ("tech_best", 2.0),
        ("tech_best", 2.5),
        ("tech_best", 3.0),
    ]
    risk_configs = [
        ("safe", 25.0, 0.0, 0.0, 1.0, "tight"),
        ("safe", 35.0, -0.10, -0.08, 1.0, "loose"),
        ("tlt", 35.0, -0.10, -0.08, 1.0, "loosetlt"),
        ("safe", 1_000.0, -0.15, -0.12, 1.0, "trend"),
    ]
    exposure_specs = [
        (4.0, 1.00),
        (6.0, 1.25),
        (8.0, 1.50),
        (10.0, 2.00),
        (12.0, 2.50),
    ]
    for risk_key, leverage in alpha_specs:
        for risk_off_action, risk_off_vix, trend_12m_lte, dist_ma200_lte, risk_off_scale, config_name in risk_configs:
            for floor_pct in [0.86, 0.88, 0.90, 0.92]:
                for multiplier, max_exposure in exposure_specs:
                    rules.append(
                        SyntheticLeverRule(
                            (
                                f"synlev_{risk_key}_l{int(leverage * 10):02d}_{config_name}"
                                f"_v{int(risk_off_vix)}_f{int(floor_pct * 100)}"
                                f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                            ),
                            "tipp",
                            risk_key,
                            leverage,
                            floor_pct,
                            multiplier,
                            max_exposure,
                            risk_off_vix,
                            risk_off_action,
                            trend_12m_lte=trend_12m_lte,
                            dist_ma200_lte=dist_ma200_lte,
                            risk_off_scale=risk_off_scale,
                        )
                    )
            for floor_pct in [0.88, 0.90]:
                for multiplier, max_exposure in [(4.0, 1.0), (6.0, 1.25), (8.0, 1.50)]:
                    rules.append(
                        SyntheticLeverRule(
                            (
                                f"syncppi_{risk_key}_l{int(leverage * 10):02d}_{config_name}"
                                f"_v{int(risk_off_vix)}_f{int(floor_pct * 100)}"
                                f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                            ),
                            "cppi",
                            risk_key,
                            leverage,
                            floor_pct,
                            multiplier,
                            max_exposure,
                            risk_off_vix,
                            risk_off_action,
                            trend_12m_lte=trend_12m_lte,
                            dist_ma200_lte=dist_ma200_lte,
                            risk_off_scale=risk_off_scale,
                        )
                    )
    return rules


RULES = build_rules()


class DailyData:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self._date_cache = {symbol: [day for day, _value in rows] for symbol, rows in rows_by_symbol.items()}
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
        dates = self._date_cache[symbol]
        idx = bisect_right(dates, day) - 1
        if idx < 0:
            return None
        return rows[idx][1]

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
            item: dict[str, float | None] = {
                "ret_6m": None,
                "ret_12m": None,
                "vol_63": None,
                "dist_ma200": None,
            }
            if current is not None and current > 0:
                for label, days in [("ret_6m", 126), ("ret_12m", 252)]:
                    if idx - days >= 0 and prices[idx - days] is not None and prices[idx - days] > 0:
                        item[label] = current / prices[idx - days] - 1.0
                if idx - 63 >= 0:
                    window = returns[idx - 62 : idx + 1]
                    mean = sum(window) / len(window)
                    item["vol_63"] = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window)) * math.sqrt(252.0)
                if idx - 200 >= 0:
                    ma_values = [value for value in prices[idx - 199 : idx + 1] if value is not None and value > 0]
                    if len(ma_values) == 200:
                        item["dist_ma200"] = current / (sum(ma_values) / len(ma_values)) - 1.0
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


def choose_risk_symbol(data: DailyData, rule: SyntheticLeverRule, idx: int) -> tuple[str, str, float]:
    scored: list[tuple[float, str, float]] = []
    for symbol in RISK_UNIVERSES[rule.risk_key]:
        feat = data.features[symbol][idx - 1]
        ret_6m = feat["ret_6m"]
        ret_12m = feat["ret_12m"]
        vol = feat["vol_63"]
        if ret_6m is None or ret_12m is None or vol is None:
            continue
        raw_score = 0.65 * ret_12m + 0.35 * ret_6m
        if raw_score <= 0:
            continue
        scored.append((raw_score / max(vol, 0.08), symbol, raw_score))
    if not scored:
        return rule.safe_symbol, "fallback", 0.0
    scored.sort(reverse=True)
    _score, symbol, raw_score = scored[0]
    return symbol, symbol, raw_score


def risk_off_return(data: DailyData, rule: SyntheticLeverRule, idx: int) -> tuple[float, str]:
    if rule.risk_off_action == "inverse":
        psq = data.returns["PSQ"][idx]
        sh = data.returns["SH"][idx]
        if psq != 0.0:
            return psq, "PSQ"
        if sh != 0.0:
            return sh, "SH"
    if rule.risk_off_action == "tlt":
        return data.returns["TLT"][idx], "TLT"
    return data.returns[rule.safe_symbol][idx], rule.safe_symbol


def synthetic_levered_return(data: DailyData, symbol: str, leverage: float, idx: int, safe_symbol: str) -> float:
    risky = data.returns[symbol][idx]
    safe = data.returns[safe_symbol][idx]
    if leverage <= 1.0:
        return leverage * risky + (1.0 - leverage) * safe
    return leverage * risky - (leverage - 1.0) * max(safe, 0.0)


def daily_alpha_return(data: DailyData, rule: SyntheticLeverRule, idx: int) -> tuple[float, str, bool]:
    qqq_feat = data.features["QQQ"][idx - 1]
    vix = data.prices["^VIX"][idx - 1] or 99.0
    trend_bad = (
        qqq_feat["ret_12m"] is None
        or qqq_feat["ret_12m"] <= rule.trend_12m_lte
        or qqq_feat["dist_ma200"] is None
        or qqq_feat["dist_ma200"] <= rule.dist_ma200_lte
    )
    risk_off = vix >= rule.risk_off_vix or trend_bad
    if risk_off:
        ret, symbol = risk_off_return(data, rule, idx)
        return ret, symbol, True
    symbol, selected, _score = choose_risk_symbol(data, rule, idx)
    if selected == "fallback":
        return data.returns[rule.safe_symbol][idx], selected, True
    return synthetic_levered_return(data, symbol, rule.leverage, idx, rule.safe_symbol), selected, False


def precompute_alpha(data: DailyData, rule: SyntheticLeverRule) -> tuple[list[float], list[str], list[bool]]:
    alpha_returns = [0.0] * len(data.dates)
    selected_symbols = [""] * len(data.dates)
    risk_off_flags = [False] * len(data.dates)
    for idx in range(1, len(data.dates)):
        alpha_ret, selected, risk_off = daily_alpha_return(data, rule, idx)
        alpha_returns[idx] = alpha_ret
        selected_symbols[idx] = selected
        risk_off_flags[idx] = risk_off
    return alpha_returns, selected_symbols, risk_off_flags


def run_case(
    data: DailyData,
    rule: SyntheticLeverRule,
    alpha: tuple[list[float], list[str], list[bool]],
    phase: int,
    lag: int,
) -> dict[str, Any]:
    start_idx = max(data.start_index(phase, lag), 253)
    end_idx = bisect_right(data.dates, dt.date(END_YEAR, 12, 31)) - 1
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    risk_off_days = 0
    inverse_days = 0
    selected_counts: dict[str, int] = {}
    alpha_returns, selected_symbols, risk_off_flags = alpha
    for idx in range(start_idx + 1, end_idx + 1):
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        alpha_ret = alpha_returns[idx]
        selected = selected_symbols[idx]
        risk_off = risk_off_flags[idx]
        if risk_off:
            exposure *= rule.risk_off_scale
            risk_off_days += 1
        if selected in {"PSQ", "SH"}:
            inverse_days += 1
        selected_counts[selected] = selected_counts.get(selected, 0) + 1
        safe_return = data.returns[rule.safe_symbol][idx]
        capital *= 1.0 + exposure * alpha_ret + (1.0 - exposure) * safe_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
        exposures.append(exposure)
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    dominant = max(selected_counts.items(), key=lambda item: item[1])[0] if selected_counts else ""
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
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "median_exposure": statistics.median(exposures) if exposures else 0.0,
        "max_realized_exposure": max(exposures) if exposures else 0.0,
        "risk_off_days": risk_off_days,
        "inverse_days": inverse_days,
        "dominant_symbol": dominant,
    }


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    dominant_counts: dict[str, int] = {}
    for item in items:
        symbol = str(item.get("dominant_symbol") or "")
        dominant_counts[symbol] = dominant_counts.get(symbol, 0) + 1
    dominant = max(dominant_counts.items(), key=lambda item: item[1])[0] if dominant_counts else ""
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
        "median_risk_off_days": statistics.median(item["risk_off_days"] for item in items),
        "median_inverse_days": statistics.median(item["inverse_days"] for item in items),
        "dominant_symbol": dominant,
    }


def evaluate_rule(data: DailyData, rule: SyntheticLeverRule) -> dict[str, Any]:
    alpha = precompute_alpha(data, rule)
    cases = [run_case(data, rule, alpha, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test synthetic leveraged Nasdaq/tech trend alpha sleeves under daily TIPP/CPPI across all month phases and execution lags.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "symbols": SYMBOLS,
        "rule_count": len(RULES),
        "model_limits": "Synthetic daily leverage from cached adjusted ETF prices; no real ETF decay, borrow availability, tax, intraday gap, liquidity, or execution-slippage model.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "mode",
            "risk_key",
            "leverage",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "risk_off_vix",
            "risk_off_action",
            "trend_12m_lte",
            "dist_ma200_lte",
            "risk_off_scale",
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
            "median_risk_off_days",
            "median_inverse_days",
            "dominant_symbol",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
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
            f"{rule.name[:86]:<86} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:10.1f}万 "
            f"median={summary['median_final_capital_wan']:10.1f}万 "
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
    best = results[0]["summary"]
    print(
        f"Wrote {OUT_JSON}; rules={len(RULES)} "
        f"best_min={best['min_final_capital_wan']:.1f}万 "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

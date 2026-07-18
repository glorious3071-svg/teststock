#!/usr/bin/env python3
"""Backtest cross-asset long/short trend-following sleeves.

Earlier cross-asset searches only used long-only ETF momentum and defensive
rotation. This experiment adds a synthetic long/short time-series momentum
sleeve as a possible crisis-alpha source, then blends it with the existing
phase-diversified scorecard+CSI engine under TIPP/CPPI sizing.

Synthetic short returns are feasibility diagnostics only; they do not model
borrow availability, margin calls, tax, slippage, or live execution constraints.
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
from scripts.backtest_scorecard_csi_blended_protection import precompute_csi_paths
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, cash_return, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_trend_follow_tipp_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_trend_follow_tipp_search.csv"

SYMBOLS = [
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "XLK",
    "XLE",
    "XLU",
    "TLT",
    "IEF",
    "SHY",
    "GLD",
    "DBC",
    "UUP",
    "LQD",
    "AGG",
    "TIP",
    "^VIX",
]

UNIVERSES = {
    "macro": ("SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "UUP"),
    "broad": ("SPY", "QQQ", "IWM", "EFA", "EEM", "XLK", "XLE", "XLU", "TLT", "IEF", "GLD", "DBC", "UUP"),
    "risk_parity": ("SPY", "EFA", "EEM", "TLT", "IEF", "LQD", "AGG", "TIP", "GLD", "DBC", "UUP"),
}


@dataclass(frozen=True)
class TrendFollowRule:
    name: str
    phase_rule_name: str
    universe_key: str
    csi_weight: float
    trend_weight: float
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    target_vol: float
    max_trend_leverage: float
    top_n: int
    signal_mode: str
    short_enabled: bool
    short_cost_annual: float
    vix_cap_gte: float = 1_000.0
    vix_scale: float = 1.0
    min_abs_signal: float = 0.03
    fallback_symbol: str = "SHY"


class ExternalSeries:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates_by_symbol = {symbol: [day for day, _value in rows] for symbol, rows in rows_by_symbol.items()}
        self.feature_cache: dict[tuple[str, dt.date], dict[str, float] | None] = {}
        self.return_cache: dict[tuple[str, dt.date, dt.date], float] = {}

    def index_at(self, symbol: str, day: dt.date) -> int:
        return bisect_right(self.dates_by_symbol[symbol], day) - 1

    def price(self, symbol: str, day: dt.date) -> float | None:
        idx = self.index_at(symbol, day)
        return self.rows_by_symbol[symbol][idx][1] if idx >= 0 else None

    def period_return(self, symbol: str, start: dt.date, end: dt.date) -> float:
        key = (symbol, start, end)
        if key in self.return_cache:
            return self.return_cache[key]
        start_px = self.price(symbol, start)
        end_px = self.price(symbol, end)
        value = 0.0 if not start_px or not end_px or start_px <= 0 else end_px / start_px - 1.0
        self.return_cache[key] = value
        return value

    def feature(self, symbol: str, day: dt.date) -> dict[str, float] | None:
        key = (symbol, day)
        if key in self.feature_cache:
            return self.feature_cache[key]
        rows = self.rows_by_symbol[symbol]
        idx = self.index_at(symbol, day)
        if idx - 252 < 1:
            self.feature_cache[key] = None
            return None
        values = [rows[j][1] for j in range(idx - 252, idx + 1)]
        if any(value <= 0 for value in values):
            self.feature_cache[key] = None
            return None
        daily = [values[j] / values[j - 1] - 1.0 for j in range(1, len(values))]
        vol_window = daily[-63:]
        mean = sum(vol_window) / len(vol_window)
        vol = math.sqrt(sum((value - mean) ** 2 for value in vol_window) / len(vol_window)) * math.sqrt(252.0)
        self.feature_cache[key] = {
            "ret_12m": values[-1] / values[0] - 1.0,
            "ret_6m": values[-1] / values[-127] - 1.0,
            "ret_3m": values[-1] / values[-64] - 1.0,
            "vol_3m": vol,
        }
        return self.feature_cache[key]


def short_name(phase_name: str) -> str:
    return phase_name.removeprefix("phase12_").replace("lever120_", "l120_").replace("guard60_", "g60_")


def build_rules() -> list[TrendFollowRule]:
    rules: list[TrendFollowRule] = []
    phase_names = ["phase12_lever120_us10y", "phase12_guard60_us10y"]
    blends = [(0.0, 1.0), (0.20, 0.80), (0.35, 0.65), (0.50, 0.50)]
    specs = [
        ("macro", 3, 0.18, 2.0),
        ("macro", 4, 0.24, 2.5),
        ("broad", 4, 0.20, 2.2),
        ("risk_parity", 4, 0.16, 1.8),
    ]
    for phase_name in phase_names:
        phase_short = short_name(phase_name)
        for universe_key, top_n, target_vol, max_trend_lev in specs:
            for csi_weight, trend_weight in blends:
                mix = f"c{int(csi_weight * 100):02d}_t{int(trend_weight * 100):02d}"
                for signal_mode in ["absolute", "relative"]:
                    for short_enabled in [True, False]:
                        side = "ls" if short_enabled else "long"
                        for floor_pct in [0.84, 0.86, 0.88, 0.90]:
                            for multiplier, max_exposure in [(6.0, 1.0), (8.0, 1.25), (10.0, 1.5)]:
                                rules.append(
                                    TrendFollowRule(
                                        (
                                            f"tf_tipp_{phase_short}_{universe_key}_{side}_{signal_mode}_{mix}"
                                            f"_f{int(floor_pct * 100)}_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                        ),
                                        phase_name,
                                        universe_key,
                                        csi_weight,
                                        trend_weight,
                                        "tipp",
                                        floor_pct,
                                        multiplier,
                                        max_exposure,
                                        target_vol,
                                        max_trend_lev,
                                        top_n,
                                        signal_mode,
                                        short_enabled,
                                        0.03,
                                    )
                                )
                        for floor_pct in [0.86, 0.88, 0.90]:
                            for multiplier, max_exposure in [(4.0, 1.0), (6.0, 1.25), (8.0, 1.5)]:
                                rules.append(
                                    TrendFollowRule(
                                        (
                                            f"tf_cppi_{phase_short}_{universe_key}_{side}_{signal_mode}_{mix}"
                                            f"_f{int(floor_pct * 100)}_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                        ),
                                        phase_name,
                                        universe_key,
                                        csi_weight,
                                        trend_weight,
                                        "cppi",
                                        floor_pct,
                                        multiplier,
                                        max_exposure,
                                        target_vol,
                                        max_trend_lev,
                                        top_n,
                                        signal_mode,
                                        short_enabled,
                                        0.03,
                                    )
                                )
    return rules


RULES = build_rules()


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


def signal_value(feature: dict[str, float], mode: str) -> float:
    if mode == "relative":
        return 0.55 * feature["ret_12m"] + 0.30 * feature["ret_6m"] + 0.15 * feature["ret_3m"]
    return feature["ret_12m"] - 0.5 * feature["vol_3m"]


def trend_period_return(
    series: ExternalSeries,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    rule: TrendFollowRule,
) -> tuple[float, dict[str, Any]]:
    scored = []
    for symbol in UNIVERSES[rule.universe_key]:
        feature = series.feature(symbol, snapshot)
        if feature is None:
            continue
        signal = signal_value(feature, rule.signal_mode)
        if abs(signal) < rule.min_abs_signal:
            continue
        if signal < 0 and not rule.short_enabled:
            continue
        direction = 1.0 if signal > 0 else -1.0
        scored.append((abs(signal) / max(feature["vol_3m"], 0.04), signal, feature["vol_3m"], direction, symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        return series.period_return(rule.fallback_symbol, start_exec, end_exec), {"picks": rule.fallback_symbol, "leverage": 0.0, "short_count": 0}
    inv_vol = [1.0 / max(item[2], 0.04) for item in picks]
    denom = sum(inv_vol)
    avg_vol = sum(weight * item[2] for weight, item in zip(inv_vol, picks)) / denom
    leverage = min(rule.max_trend_leverage, rule.target_vol / max(avg_vol, 0.04))
    vix = series.price("^VIX", snapshot) or 0.0
    if vix >= rule.vix_cap_gte:
        leverage *= rule.vix_scale
    gross_return = 0.0
    short_count = 0
    for weight, item in zip(inv_vol, picks):
        _score, _signal, _vol, direction, symbol = item
        period = series.period_return(symbol, start_exec, end_exec)
        signed_return = period if direction > 0 else -period - rule.short_cost_annual / 12.0
        if direction < 0:
            short_count += 1
        gross_return += leverage * weight / denom * signed_return
    residual_return = (1.0 - leverage) * series.period_return(rule.fallback_symbol, start_exec, end_exec)
    return gross_return + residual_return, {
        "picks": ",".join(("-" if item[3] < 0 else "+") + item[4] for item in picks),
        "leverage": leverage,
        "short_count": short_count,
    }


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    external_series: ExternalSeries,
    rule: TrendFollowRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures = []
    trend_leverages = []
    short_counts = []

    for csi_row in csi_paths[(rule.phase_rule_name, phase, lag)]:
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        trend_return, trend_meta = trend_period_return(
            external_series,
            csi_row["period"],
            csi_row["start_exec"],
            csi_row["end_exec"],
            rule,
        )
        safe_return = cash_return(csi_row["start_exec"], csi_row["end_exec"])
        residual_weight = 1.0 - rule.csi_weight - rule.trend_weight
        engine_return = rule.csi_weight * csi_row["csi_return"] + rule.trend_weight * trend_return + residual_weight * safe_return
        period_return = exposure * engine_return + (1.0 - exposure) * safe_return
        capital = max(1.0, capital * (1.0 + period_return))
        curve.append(capital)
        exposures.append(exposure)
        trend_leverages.append(float(trend_meta["leverage"]))
        short_counts.append(int(trend_meta["short_count"]))

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
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "median_exposure": statistics.median(exposures) if exposures else 0.0,
        "median_trend_leverage": statistics.median(trend_leverages) if trend_leverages else 0.0,
        "median_short_count": statistics.median(short_counts) if short_counts else 0.0,
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
        "median_exposure": statistics.median(item["median_exposure"] for item in items),
        "median_trend_leverage": statistics.median(item["median_trend_leverage"] for item in items),
        "median_short_count": statistics.median(item["median_short_count"] for item in items),
    }


def evaluate_rule(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    external_series: ExternalSeries,
    rule: TrendFollowRule,
) -> dict[str, Any]:
    cases = [run_case(csi_paths, external_series, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test synthetic cross-asset long/short trend following as a crisis-alpha sleeve blended with scorecard+CSI under TIPP/CPPI.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "symbols": SYMBOLS,
        "rule_count": len(RULES),
        "model_limits": "Synthetic short returns from ETF prices; no borrow availability, margin call, tax, transaction-cost, or live execution evidence.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "phase_rule_name",
            "universe_key",
            "csi_weight",
            "trend_weight",
            "mode",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "target_vol",
            "max_trend_leverage",
            "top_n",
            "signal_mode",
            "short_enabled",
            "short_cost_annual",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_exposure",
            "median_trend_leverage",
            "median_short_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    phase_rule_names = {rule.phase_rule_name for rule in RULES}
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(conn, csi_series, yields, trade_dates, holdings, phase_rule_names)
        external_series = load_external_series(conn)
    finally:
        conn.close()
    results = []
    for rule in RULES:
        result = evaluate_rule(csi_paths, external_series, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:92]:<92} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
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

#!/usr/bin/env python3
"""Search blended CSI scorecard + option-protected external sleeves.

This experiment tests whether portfolio-level blending can improve the strict
month-start fragility seen in standalone CSI, external rotation, and synthetic
option hedge runs.  It rebalances monthly across:

- a phase-diversified CSI scorecard sleeve;
- a modelled option-protected QQQ/SPY sleeve;
- cash/SHY financing for residual or defensive capital.

The synthetic option sleeve is still a proxy model.  It uses cached ETF prices
and VIX/VIX3M as implied-volatility proxies, not executable option-chain quotes.
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
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    month_end_shift,
    monthly_boundaries,
    period_return as csi_period_return,
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
from scripts.backtest_scorecard_csi_phase_ensemble import (
    RULES as PHASE_RULES,
    defensive_return,
    ensemble_state,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_synthetic_option_hedge import (
    SYMBOLS,
    RULES as OPTION_RULES,
    OptionData,
    SyntheticOptionRule,
    black_scholes,
    load_data as load_option_data,
)

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_blended_protection_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_blended_protection_search.csv"

PHASE_RULE_BY_NAME = {rule.name: rule for rule in PHASE_RULES}
OPTION_RULE_BY_NAME = {rule.name: rule for rule in OPTION_RULES}


@dataclass(frozen=True)
class BlendRule:
    name: str
    phase_rule_name: str
    option_rule_name: str
    csi_weight: float
    option_weight: float
    drawdown_guard_lte: float = -1.0
    drawdown_guard_scale: float = 1.0
    vix_guard_gte: float = 1_000.0
    vix_option_scale: float = 1.0
    cs300_6m_lte: float = -1_000.0
    csi_trend_scale: float = 1.0
    qqq_6m_lte: float = -1_000.0
    option_trend_scale: float = 1.0


def build_rules() -> list[BlendRule]:
    phase_names = [
        "phase12_lever120_us10y",
        "phase12_guard60_us10y",
        "phase12_guard40_us10y",
    ]
    option_names = [
        "qqq_put98_call108_lev125",
        "qqq_put98_call103_lev125",
        "qqq_put99_call102_lev125",
        "qqq_put98_95spread_call108_lev125",
        "qqq_put98_94spread_call108_lev125",
        "qqq_put98_call112_lev220",
        "qqq_put95_call115_lev250",
        "qqq_put100_lev125",
    ]
    static_weights = [
        (0.00, 1.00),
        (0.20, 0.80),
        (0.35, 0.65),
        (0.50, 0.50),
        (0.65, 0.35),
        (0.80, 0.20),
    ]
    rules: list[BlendRule] = []
    for phase_name in phase_names:
        for option_name in option_names:
            for csi_weight, option_weight in static_weights:
                suffix = f"c{int(csi_weight * 100):02d}_o{int(option_weight * 100):02d}"
                rules.append(
                    BlendRule(
                        f"blend_{phase_name}_{option_name}_{suffix}",
                        phase_name,
                        option_name,
                        csi_weight,
                        option_weight,
                    )
                )
                rules.append(
                    BlendRule(
                        f"guard8_{phase_name}_{option_name}_{suffix}",
                        phase_name,
                        option_name,
                        csi_weight,
                        option_weight,
                        drawdown_guard_lte=-0.08,
                        drawdown_guard_scale=0.25,
                    )
                )
                rules.append(
                    BlendRule(
                        f"macroguard_{phase_name}_{option_name}_{suffix}",
                        phase_name,
                        option_name,
                        csi_weight,
                        option_weight,
                        drawdown_guard_lte=-0.08,
                        drawdown_guard_scale=0.35,
                        vix_guard_gte=28.0,
                        vix_option_scale=0.45,
                        cs300_6m_lte=-0.12,
                        csi_trend_scale=0.45,
                        qqq_6m_lte=-0.10,
                        option_trend_scale=0.55,
                    )
                )
                rules.append(
                    BlendRule(
                        f"hardstop5_{phase_name}_{option_name}_{suffix}",
                        phase_name,
                        option_name,
                        csi_weight,
                        option_weight,
                        drawdown_guard_lte=-0.05,
                        drawdown_guard_scale=0.0,
                    )
                )
                rules.append(
                    BlendRule(
                        f"hardstop8_{phase_name}_{option_name}_{suffix}",
                        phase_name,
                        option_name,
                        csi_weight,
                        option_weight,
                        drawdown_guard_lte=-0.08,
                        drawdown_guard_scale=0.0,
                    )
                )
    return rules


RULES = build_rules()


def price_at(data: OptionData, symbol: str, day: dt.date) -> float | None:
    rows = data.rows_by_symbol[symbol]
    dates = [item[0] for item in rows]
    idx = bisect_right(dates, day) - 1
    return rows[idx][1] if idx >= 0 else None


def option_value(
    spot: float,
    start_spot: float,
    years: float,
    rule: SyntheticOptionRule,
    long_put_units: float,
    short_put_units: float,
    call_units: float,
    rate: float,
    iv: float,
) -> float:
    value = long_put_units * black_scholes("put", spot, start_spot * rule.put_strike_pct, years, rate, iv)
    if rule.short_put_strike_pct > 0 and short_put_units:
        value -= short_put_units * black_scholes("put", spot, start_spot * rule.short_put_strike_pct, years, rate, iv)
    if rule.call_strike_pct > 0 and call_units:
        value -= call_units * black_scholes("call", spot, start_spot * rule.call_strike_pct, years, rate, iv)
    return value


def option_period_return(
    data: OptionData,
    rule: SyntheticOptionRule,
    start_day: dt.date,
    end_day: dt.date,
) -> tuple[float, dict[str, Any]]:
    idx = bisect_left(data.dates, start_day)
    end_idx = bisect_right(data.dates, end_day) - 1
    if idx >= len(data.dates) or end_idx <= idx:
        return 0.0, {"reason": "missing_period"}
    spot = data.prices[rule.underlying][idx]
    if spot is None or spot <= 0:
        return 0.0, {"reason": "missing_spot"}
    vix = data.prices[rule.iv_symbol][idx] or data.prices["^VIX"][idx] or 25.0
    is_risk_off = vix >= rule.risk_off_vix
    leverage = rule.risk_off_leverage if is_risk_off else rule.leverage
    put_cover = rule.risk_off_put_cover if is_risk_off else rule.put_cover
    call_cover = rule.risk_off_call_cover if is_risk_off else rule.call_cover
    iv = max(0.05, min(1.5, vix / 100.0 * rule.iv_multiplier))
    rate = max(0.0, data.returns["SHY"][idx] * 252.0)
    underlying_units = leverage / spot
    long_put_units = underlying_units * put_cover
    short_put_units = underlying_units * put_cover if rule.short_put_strike_pct > 0 else 0.0
    call_units = underlying_units * call_cover if rule.call_strike_pct > 0 else 0.0
    years = max((data.dates[end_idx] - data.dates[idx]).days / 365.25, 0.0)
    package_start = option_value(
        spot,
        spot,
        years,
        rule,
        long_put_units,
        short_put_units,
        call_units,
        rate,
        iv,
    )
    cash = 1.0 - underlying_units * spot - package_start
    for day_idx in range(idx + 1, end_idx + 1):
        cash *= 1.0 + data.returns["SHY"][day_idx]
        if cash < 0:
            cash -= abs(cash) * rule.financing_spread_annual / 252.0
    end_spot = data.prices[rule.underlying][end_idx]
    if end_spot is None:
        return 0.0, {"reason": "missing_end_spot"}
    package_end = option_value(
        end_spot,
        spot,
        0.0,
        rule,
        long_put_units,
        short_put_units,
        call_units,
        rate,
        iv,
    )
    final_value = underlying_units * end_spot + package_end + cash
    return final_value - 1.0, {
        "vix": vix,
        "iv": iv,
        "leverage": leverage,
        "premium_pct": max(package_start, 0.0),
        "risk_off": is_risk_off,
    }


def external_trend(data: OptionData, symbol: str, day: dt.date, months: int) -> float:
    start = month_end_shift(day, -months)
    start_px = price_at(data, symbol, start)
    end_px = price_at(data, symbol, day)
    if not start_px or not end_px:
        return 0.0
    return end_px / start_px - 1.0


def precompute_csi_paths(
    conn,
    csi_series: dict[str, list[tuple[dt.date, float]]],
    yields: list[tuple[dt.date, float]],
    trade_dates: list[dt.date],
    holdings: dict[int, list[str]],
    phase_rule_names: set[str],
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    paths: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for phase_rule_name in sorted(phase_rule_names):
        phase_rule = PHASE_RULE_BY_NAME[phase_rule_name]
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                rows = []
                for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
                    start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
                    end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
                    csi_pct, csi_equity_return, sleeves, csi_reasons = ensemble_state(
                        conn,
                        csi_series,
                        holdings,
                        phase_rule,
                        start_snapshot,
                        start_exec,
                        end_exec,
                        0.0,
                    )
                    csi_equity_weight = csi_pct / 100.0
                    def_return, defensive_asset = defensive_return(csi_series, yields, phase_rule, start_exec, end_exec)
                    financing_return = cash_return(start_exec, end_exec)
                    non_equity_return = financing_return if csi_equity_weight > 1.0 else def_return
                    csi_return = csi_equity_weight * csi_equity_return + (1.0 - csi_equity_weight) * non_equity_return
                    cs300_6m = csi_period_return(
                        csi_series,
                        CS300_CODE,
                        month_end_shift(start_snapshot, -6),
                        start_snapshot,
                    )
                    rows.append(
                        {
                            "period": start_snapshot,
                            "start_exec": start_exec,
                            "end_exec": end_exec,
                            "csi_target_equity_pct": csi_pct,
                            "csi_return": csi_return,
                            "defensive_asset": defensive_asset,
                            "cs300_6m": cs300_6m,
                            "csi_reasons": csi_reasons,
                            "sleeves": sleeves,
                        }
                    )
                paths[(phase_rule_name, phase, lag)] = rows
    return paths


def precompute_option_paths(
    option_data: OptionData,
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    option_rule_names: set[str],
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    sample_paths: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for (_phase_rule_name, phase, lag), rows in csi_paths.items():
        sample_paths.setdefault((phase, lag), rows)
    paths: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for option_rule_name in sorted(option_rule_names):
        option_rule = OPTION_RULE_BY_NAME[option_rule_name]
        for (phase, lag), rows in sample_paths.items():
            out = []
            for row in rows:
                start_exec = row["start_exec"]
                end_exec = row["end_exec"]
                opt_return, option_meta = option_period_return(option_data, option_rule, start_exec, end_exec)
                out.append(
                    {
                        "option_return": opt_return,
                        "option_meta": option_meta,
                        "vix": price_at(option_data, "^VIX", start_exec) or 0.0,
                        "qqq_6m": external_trend(option_data, option_rule.underlying, start_exec, 6),
                    }
                )
            paths[(option_rule_name, phase, lag)] = out
    return paths


def run_case_cached(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    option_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: BlendRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    guard_months = 0
    csi_rows = csi_paths[(rule.phase_rule_name, phase_month_offset, execution_lag_days)]
    option_rows = option_paths[(rule.option_rule_name, phase_month_offset, execution_lag_days)]
    for csi_row, option_row in zip(csi_rows, option_rows):
        drawdown = capital / peak - 1.0
        csi_weight = rule.csi_weight
        option_weight = rule.option_weight
        reasons = list(csi_row["csi_reasons"])
        if drawdown <= rule.drawdown_guard_lte:
            csi_weight *= rule.drawdown_guard_scale
            option_weight *= rule.drawdown_guard_scale
            guard_months += 1
            reasons.append("blend_drawdown_guard")
        if option_row["vix"] >= rule.vix_guard_gte:
            option_weight *= rule.vix_option_scale
            reasons.append("blend_vix_guard")
        if csi_row["cs300_6m"] <= rule.cs300_6m_lte:
            csi_weight *= rule.csi_trend_scale
            reasons.append("blend_cs300_trend_guard")
        if option_row["qqq_6m"] <= rule.qqq_6m_lte:
            option_weight *= rule.option_trend_scale
            reasons.append("blend_option_trend_guard")

        residual_weight = 1.0 - csi_weight - option_weight
        residual_return = cash_return(csi_row["start_exec"], csi_row["end_exec"])
        period_ret = (
            csi_weight * csi_row["csi_return"]
            + option_weight * option_row["option_return"]
            + residual_weight * residual_return
        )
        capital *= 1.0 + period_ret
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "period": csi_row["period"].isoformat(),
                    "phase_month_offset": phase_month_offset,
                    "execution_lag_days": execution_lag_days,
                    "start_exec": csi_row["start_exec"].isoformat(),
                    "end_exec": csi_row["end_exec"].isoformat(),
                    "csi_weight": csi_weight,
                    "option_weight": option_weight,
                    "residual_weight": residual_weight,
                    "csi_target_equity_pct": csi_row["csi_target_equity_pct"],
                    "csi_return": csi_row["csi_return"],
                    "option_return": option_row["option_return"],
                    "residual_return": residual_return,
                    "period_return": period_ret,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "defensive_asset": csi_row["defensive_asset"],
                    "cs300_6m": csi_row["cs300_6m"],
                    "qqq_6m": option_row["qqq_6m"],
                    "vix": option_row["vix"],
                    "reasons": reasons,
                    "option_meta": option_row["option_meta"],
                    "sleeves": csi_row["sleeves"],
                }
            )
    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "guard_months": guard_months,
    }


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
    csi_series: dict[str, list[tuple[dt.date, float]]],
    option_data: OptionData,
    yields: list[tuple[dt.date, float]],
    trade_dates: list[dt.date],
    holdings: dict[int, list[str]],
    rule: BlendRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    phase_rule = PHASE_RULE_BY_NAME[rule.phase_rule_name]
    option_rule = OPTION_RULE_BY_NAME[rule.option_rule_name]
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    guard_months = 0
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset):
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        drawdown = capital / peak - 1.0
        csi_pct, csi_equity_return, sleeves, csi_reasons = ensemble_state(
            conn,
            csi_series,
            holdings,
            phase_rule,
            start_snapshot,
            start_exec,
            end_exec,
            drawdown,
        )
        csi_weight = rule.csi_weight
        option_weight = rule.option_weight
        reasons = list(csi_reasons)
        if drawdown <= rule.drawdown_guard_lte:
            csi_weight *= rule.drawdown_guard_scale
            option_weight *= rule.drawdown_guard_scale
            guard_months += 1
            reasons.append("blend_drawdown_guard")
        vix = price_at(option_data, "^VIX", start_exec) or 0.0
        if vix >= rule.vix_guard_gte:
            option_weight *= rule.vix_option_scale
            reasons.append("blend_vix_guard")
        cs300_6m = 0.0
        try:
            from scripts.backtest_scorecard_csi_dynamic_defense import period_return as csi_period_return

            cs300_6m = csi_period_return(csi_series, CS300_CODE, month_end_shift(start_snapshot, -6), start_snapshot)
        except Exception:
            cs300_6m = 0.0
        if cs300_6m <= rule.cs300_6m_lte:
            csi_weight *= rule.csi_trend_scale
            reasons.append("blend_cs300_trend_guard")
        qqq_6m = external_trend(option_data, option_rule.underlying, start_exec, 6)
        if qqq_6m <= rule.qqq_6m_lte:
            option_weight *= rule.option_trend_scale
            reasons.append("blend_option_trend_guard")

        csi_equity_weight = csi_pct / 100.0
        def_return, defensive_asset = defensive_return(csi_series, yields, phase_rule, start_exec, end_exec)
        csi_financing_return = cash_return(start_exec, end_exec)
        csi_non_equity_return = csi_financing_return if csi_equity_weight > 1.0 else def_return
        csi_return = csi_equity_weight * csi_equity_return + (1.0 - csi_equity_weight) * csi_non_equity_return
        opt_return, option_meta = option_period_return(option_data, option_rule, start_exec, end_exec)
        residual_weight = 1.0 - csi_weight - option_weight
        residual_return = cash_return(start_exec, end_exec)
        period_ret = csi_weight * csi_return + option_weight * opt_return + residual_weight * residual_return
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
                    "csi_weight": csi_weight,
                    "option_weight": option_weight,
                    "residual_weight": residual_weight,
                    "csi_target_equity_pct": csi_pct,
                    "csi_return": csi_return,
                    "option_return": opt_return,
                    "residual_return": residual_return,
                    "period_return": period_ret,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "defensive_asset": defensive_asset,
                    "cs300_6m": cs300_6m,
                    "qqq_6m": qqq_6m,
                    "vix": vix,
                    "reasons": reasons,
                    "option_meta": option_meta,
                    "sleeves": sleeves,
                }
            )
    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "guard_months": guard_months,
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
        "median_guard_months": statistics.median(item["guard_months"] for item in items),
    }


def evaluate_rule(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    option_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: BlendRule,
) -> dict[str, Any]:
    cases = [
        run_case_cached(csi_paths, option_paths, rule, phase, lag)
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
        "objective": "Search monthly blended CSI scorecard, synthetic option protection, and cash sleeves across all month phases and execution lags.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Monthly blend backtest; synthetic option prices use VIX/VIX3M proxy and do not include live option-chain liquidity, skew, bid/ask, or execution evidence.",
        "symbols": SYMBOLS,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "phase_rule_name",
            "option_rule_name",
            "csi_weight",
            "option_weight",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_months",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            rule = item["rule"]
            row = {**rule, **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        phase_rule_names = {rule.phase_rule_name for rule in RULES}
        option_rule_names = {rule.option_rule_name for rule in RULES}
        csi_paths = precompute_csi_paths(conn, csi_series, yields, trade_dates, holdings, phase_rule_names)
        option_paths = precompute_option_paths(option_data, csi_paths, option_rule_names)
        results = []
        for rule in RULES:
            result = evaluate_rule(csi_paths, option_paths, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name[:76]:<76} pass={summary['pass_count']:>2}/{summary['count']} "
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

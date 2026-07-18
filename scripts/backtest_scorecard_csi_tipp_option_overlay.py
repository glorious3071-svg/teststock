#!/usr/bin/env python3
"""Backtest TIPP/CPPI sizing on synthetic option-protected sleeves.

The current frontier shows two separate failures: synthetic option sleeves can
compound above the 4000w target but carry deep drawdowns, while TIPP/CPPI can
control drawdown but does not compound enough on ordinary ETF returns.  This
experiment combines the two structures: a trailing floor controls allocation to
the modelled option-protected sleeve, with residual capital in SHY.

This remains a modelled experiment, not implementation-ready option evidence.
It reuses the Black-Scholes/VIX proxy from
`backtest_scorecard_csi_synthetic_option_hedge.py`.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
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
from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_synthetic_option_hedge import (
    RULES as OPTION_RULES,
    OptionData,
    SyntheticOptionRule,
    load_data,
    option_package_value,
)

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_tipp_option_overlay_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_tipp_option_overlay_search.csv"


@dataclass(frozen=True)
class TIPPOptionRule:
    name: str
    mode: str
    option_rule_name: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    min_exposure: float = 0.0
    risk_off_vix: float = 1_000.0
    risk_off_scale: float = 1.0
    safe_symbol: str = "SHY"


OPTION_RULE_NAMES = {
    "qqq_put98_call108_lev125",
    "qqq_put100_lev125",
    "qqq_put95_call108_lev150",
    "qqq_put95_call115_lev250",
    "qqq_put98_call112_lev220",
}
OPTION_RULE_BY_NAME = {rule.name: rule for rule in OPTION_RULES if rule.name in OPTION_RULE_NAMES}
_PERIOD_CACHE: dict[tuple[str, int, int], tuple[list[float], float]] = {}


def build_rules() -> list[TIPPOptionRule]:
    rules: list[TIPPOptionRule] = []
    for option_name in sorted(OPTION_RULE_BY_NAME):
        for floor_pct in [0.86, 0.88, 0.90, 0.92, 0.95]:
            for multiplier, max_exposure in [
                (2.0, 0.50),
                (3.0, 0.75),
                (4.0, 1.00),
                (5.0, 1.00),
                (6.0, 1.25),
                (8.0, 1.50),
                (10.0, 2.00),
            ]:
                prefix = f"tippopt_{option_name}_f{int(floor_pct * 100)}_m{int(multiplier)}_x{int(max_exposure * 100)}"
                rules.append(TIPPOptionRule(prefix, "tipp", option_name, floor_pct, multiplier, max_exposure))
                rules.append(
                    TIPPOptionRule(
                        f"{prefix}_vix30",
                        "tipp",
                        option_name,
                        floor_pct,
                        multiplier,
                        max_exposure,
                        risk_off_vix=30.0,
                        risk_off_scale=0.25,
                    )
                )
    for option_name in ["qqq_put98_call108_lev125", "qqq_put98_call112_lev220", "qqq_put95_call115_lev250"]:
        for multiplier, max_exposure in [(3.0, 0.75), (4.0, 1.0), (6.0, 1.5), (8.0, 2.0)]:
            rules.append(
                TIPPOptionRule(
                    f"cppiopt_{option_name}_f90_m{int(multiplier)}_x{int(max_exposure * 100)}",
                    "cppi",
                    option_name,
                    0.90,
                    multiplier,
                    max_exposure,
                )
            )
    return rules


RULES = build_rules()


def next_index(data: OptionData, idx: int, lag: int, option_rule: SyntheticOptionRule) -> int:
    return min(data.next_reset_index(idx, lag, option_rule.reset_trading_days), bisect_right(data.dates, dt.date(END_YEAR, 12, 31)) - 1)


def simulate_option_period(
    data: OptionData,
    option_rule: SyntheticOptionRule,
    idx: int,
    expiry_idx: int,
    sleeve_capital: float,
) -> tuple[list[float], float, float]:
    cache_key = (option_rule.name, idx, expiry_idx)
    if cache_key in _PERIOD_CACHE:
        relative_values, premium_ratio = _PERIOD_CACHE[cache_key]
        values = [value * sleeve_capital for value in relative_values]
        return values, values[-1], premium_ratio * sleeve_capital
    spot = data.prices[option_rule.underlying][idx]
    if spot is None or spot <= 0 or sleeve_capital <= 0:
        return [sleeve_capital], sleeve_capital, 0.0
    vix = data.prices[option_rule.iv_symbol][idx] or data.prices["^VIX"][idx] or 25.0
    is_risk_off = vix >= option_rule.risk_off_vix
    leverage = option_rule.risk_off_leverage if is_risk_off else option_rule.leverage
    put_cover = option_rule.risk_off_put_cover if is_risk_off else option_rule.put_cover
    call_cover = option_rule.risk_off_call_cover if is_risk_off else option_rule.call_cover
    iv = max(0.05, min(1.5, vix / 100.0 * option_rule.iv_multiplier))
    rate = max(0.0, data.returns["SHY"][idx] * 252.0)
    base_capital = 1.0
    underlying_units = base_capital * leverage / spot
    long_put_units = underlying_units * put_cover
    short_put_units = underlying_units * put_cover if option_rule.short_put_strike_pct > 0 else 0.0
    call_units = underlying_units * call_cover if option_rule.call_strike_pct > 0 else 0.0
    package_start_value = option_package_value(
        data,
        option_rule,
        idx,
        expiry_idx,
        spot,
        long_put_units,
        short_put_units,
        call_units,
        rate,
        iv,
    )
    cash = base_capital - underlying_units * spot - package_start_value
    premium_paid = max(package_start_value, 0.0)
    relative_values = [base_capital]
    current_value = base_capital
    for day_idx in range(idx + 1, expiry_idx + 1):
        current_spot = data.prices[option_rule.underlying][day_idx]
        if current_spot is None:
            continue
        cash *= 1.0 + data.returns["SHY"][day_idx]
        if cash < 0:
            cash -= abs(cash) * option_rule.financing_spread_annual / 252.0
        package_value = option_package_value(
            data,
            option_rule,
            day_idx,
            expiry_idx,
            spot,
            long_put_units,
            short_put_units,
            call_units,
            rate,
            iv,
        )
        current_value = max(underlying_units * current_spot + package_value + cash, 1e-9)
        relative_values.append(current_value)
    _PERIOD_CACHE[cache_key] = (relative_values, premium_paid)
    values = [value * sleeve_capital for value in relative_values]
    return values, values[-1], premium_paid * sleeve_capital


def run_case(data: OptionData, rule: TIPPOptionRule, phase: int, lag: int) -> dict[str, Any]:
    option_rule = OPTION_RULE_BY_NAME[rule.option_rule_name]
    idx = max(data.start_index(phase, lag), 253)
    end_idx = bisect_right(data.dates, dt.date(END_YEAR, 12, 31)) - 1
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    premium_paid = 0.0
    risk_off_periods = 0

    while idx < end_idx:
        expiry_idx = next_index(data, idx, lag, option_rule)
        if expiry_idx <= idx:
            idx += 1
            continue
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        vix = data.prices["^VIX"][idx] or 99.0
        if vix >= rule.risk_off_vix:
            exposure *= rule.risk_off_scale
            risk_off_periods += 1

        sleeve_capital = capital * exposure
        safe_value = capital * (1.0 - exposure)
        sleeve_values, sleeve_end, period_premium = simulate_option_period(data, option_rule, idx, expiry_idx, sleeve_capital)
        premium_paid += period_premium
        current_total = capital
        for offset, day_idx in enumerate(range(idx + 1, expiry_idx + 1), start=1):
            safe_value *= 1.0 + data.returns[rule.safe_symbol][day_idx]
            if safe_value < 0:
                safe_value -= abs(safe_value) * option_rule.financing_spread_annual / 252.0
            sleeve_value = sleeve_values[min(offset, len(sleeve_values) - 1)]
            current_total = max(safe_value + sleeve_value, 1.0)
            curve.append(current_total)
            peak = max(peak, current_total)
        capital = max(current_total if len(sleeve_values) > 1 else safe_value + sleeve_end, 1.0)
        exposures.append(exposure)
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
        "avg_exposure": sum(exposures) / len(exposures) if exposures else 0.0,
        "max_exposure": max(exposures) if exposures else 0.0,
        "premium_paid_wan": premium_paid / 10_000.0,
        "risk_off_periods": risk_off_periods,
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
        "median_premium_paid_wan": statistics.median(item["premium_paid_wan"] for item in items),
        "median_risk_off_periods": statistics.median(item["risk_off_periods"] for item in items),
    }


def evaluate_rule(data: OptionData, rule: TIPPOptionRule) -> dict[str, Any]:
    cases = [run_case(data, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test TIPP/CPPI allocation control on modelled synthetic option-protected sleeves.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Black-Scholes proxy using VIX/VIX3M, no option-chain strike liquidity, bid/ask, skew, tax, borrow, or executable roll terms.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "mode",
            "option_rule_name",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "risk_off_vix",
            "risk_off_scale",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_premium_paid_wan",
            "median_risk_off_periods",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    if len(OPTION_RULE_BY_NAME) != len(OPTION_RULE_NAMES):
        missing = sorted(OPTION_RULE_NAMES - set(OPTION_RULE_BY_NAME))
        raise RuntimeError(f"missing synthetic option rules: {missing}")
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

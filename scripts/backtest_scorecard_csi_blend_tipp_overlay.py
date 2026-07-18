#!/usr/bin/env python3
"""Backtest TIPP/CPPI sizing on blended CSI + option-protected sleeves.

The current frontier has high-return blended sleeves that can clear the
4000w capital floor but draw down far beyond 10%.  This experiment treats those
blended sleeves as the risky engine and applies portfolio-level TIPP/CPPI
allocation control, with residual capital in cash/SHY-style financing.

This is still a monthly, modelled experiment.  It reuses the synthetic option
proxy from `backtest_scorecard_csi_blended_protection.py`; it is not executable
option-chain evidence.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blended_protection import (
    RULES as BLEND_RULES,
    BlendRule,
    load_option_data,
    precompute_csi_paths,
    precompute_option_paths,
)
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
OUT_JSON = OUT_DIR / "scorecard_csi_blend_tipp_overlay_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_blend_tipp_overlay_search.csv"

BLEND_RULE_BY_NAME = {rule.name: rule for rule in BLEND_RULES}

BASE_BLEND_NAMES = [
    "blend_phase12_lever120_us10y_qqq_put98_call112_lev220_c35_o65",
    "blend_phase12_lever120_us10y_qqq_put98_call112_lev220_c50_o50",
    "blend_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
    "blend_phase12_lever120_us10y_qqq_put98_call103_lev125_c20_o80",
    "blend_phase12_lever120_us10y_qqq_put99_call102_lev125_c20_o80",
    "blend_phase12_lever120_us10y_qqq_put98_95spread_call108_lev125_c20_o80",
    "blend_phase12_lever120_us10y_qqq_put98_94spread_call108_lev125_c20_o80",
    "guard8_phase12_guard60_us10y_qqq_put98_call108_lev125_c20_o80",
    "macroguard_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
]


@dataclass(frozen=True)
class BlendTippRule:
    name: str
    mode: str
    base_blend_name: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    min_exposure: float = 0.0
    drawdown_scale_lte: float = -1.0
    drawdown_scale: float = 1.0
    vix_guard_gte: float = 1_000.0
    vix_scale: float = 1.0


def build_rules() -> list[BlendTippRule]:
    rules: list[BlendTippRule] = []
    for base_name in BASE_BLEND_NAMES:
        if base_name not in BLEND_RULE_BY_NAME:
            raise RuntimeError(f"missing base blend rule: {base_name}")
        short = base_name.removeprefix("blend_").removeprefix("guard8_").removeprefix("macroguard_")
        short = short.replace("phase12_", "").replace("lever120_", "l120_")
        for floor_pct in [0.88, 0.90, 0.92, 0.95]:
            for multiplier, max_exposure in [(1.5, 0.35), (2.0, 0.50), (3.0, 0.75), (4.0, 1.00), (6.0, 1.25)]:
                stem = f"btipp_{short}_f{int(floor_pct * 100)}_m{int(multiplier * 10):02d}_x{int(max_exposure * 100)}"
                rules.append(BlendTippRule(stem, "tipp", base_name, floor_pct, multiplier, max_exposure))
                rules.append(
                    BlendTippRule(
                        f"{stem}_vix30",
                        "tipp",
                        base_name,
                        floor_pct,
                        multiplier,
                        max_exposure,
                        vix_guard_gte=30.0,
                        vix_scale=0.35,
                    )
                )
        for floor_pct in [0.90, 0.92]:
            for multiplier, max_exposure in [(2.0, 0.50), (4.0, 1.00), (6.0, 1.50)]:
                rules.append(
                    BlendTippRule(
                        f"bcppi_{short}_f{int(floor_pct * 100)}_m{int(multiplier * 10):02d}_x{int(max_exposure * 100)}",
                        "cppi",
                        base_name,
                        floor_pct,
                        multiplier,
                        max_exposure,
                    )
                )
    return rules


RULES = build_rules()


def blended_period_return(
    csi_row: dict[str, Any],
    option_row: dict[str, Any],
    base_rule: BlendRule,
    current_drawdown: float,
) -> tuple[float, dict[str, Any]]:
    csi_weight = base_rule.csi_weight
    option_weight = base_rule.option_weight
    reasons = list(csi_row["csi_reasons"])
    guard_active = False
    if current_drawdown <= base_rule.drawdown_guard_lte:
        csi_weight *= base_rule.drawdown_guard_scale
        option_weight *= base_rule.drawdown_guard_scale
        reasons.append("blend_drawdown_guard")
        guard_active = True
    if option_row["vix"] >= base_rule.vix_guard_gte:
        option_weight *= base_rule.vix_option_scale
        reasons.append("blend_vix_guard")
        guard_active = True
    if csi_row["cs300_6m"] <= base_rule.cs300_6m_lte:
        csi_weight *= base_rule.csi_trend_scale
        reasons.append("blend_cs300_trend_guard")
        guard_active = True
    if option_row["qqq_6m"] <= base_rule.qqq_6m_lte:
        option_weight *= base_rule.option_trend_scale
        reasons.append("blend_option_trend_guard")
        guard_active = True
    residual_weight = 1.0 - csi_weight - option_weight
    residual_return = cash_return(csi_row["start_exec"], csi_row["end_exec"])
    period_ret = (
        csi_weight * csi_row["csi_return"]
        + option_weight * option_row["option_return"]
        + residual_weight * residual_return
    )
    return period_ret, {
        "csi_weight": csi_weight,
        "option_weight": option_weight,
        "residual_weight": residual_weight,
        "residual_return": residual_return,
        "vix": option_row["vix"],
        "base_guard_active": guard_active,
        "reasons": reasons,
    }


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    option_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: BlendTippRule,
    phase: int,
    lag: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    base_rule = BLEND_RULE_BY_NAME[rule.base_blend_name]
    csi_rows = csi_paths[(base_rule.phase_rule_name, phase, lag)]
    option_rows = option_paths[(base_rule.option_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    tipp_guard_months = 0
    vix_guard_months = 0
    base_guard_months = 0
    rows: list[dict[str, Any]] = []

    for csi_row, option_row in zip(csi_rows, option_rows):
        peak = max(peak, capital)
        drawdown = capital / peak - 1.0
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        if drawdown <= rule.drawdown_scale_lte:
            exposure *= rule.drawdown_scale
            tipp_guard_months += 1
        if option_row["vix"] >= rule.vix_guard_gte:
            exposure *= rule.vix_scale
            vix_guard_months += 1
        base_return, meta = blended_period_return(csi_row, option_row, base_rule, drawdown)
        if meta["base_guard_active"]:
            base_guard_months += 1
        safe_return = cash_return(csi_row["start_exec"], csi_row["end_exec"])
        period_return = exposure * base_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
        if include_rows:
            rows.append(
                {
                    "period": csi_row["period"].isoformat(),
                    "phase_month_offset": phase,
                    "execution_lag_days": lag,
                    "start_exec": csi_row["start_exec"].isoformat(),
                    "end_exec": csi_row["end_exec"].isoformat(),
                    "exposure": exposure,
                    "base_return": base_return,
                    "safe_return": safe_return,
                    "period_return": period_return,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "floor": floor,
                    **meta,
                }
            )

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
        "max_realized_exposure": max(exposures) if exposures else 0.0,
        "median_exposure": statistics.median(exposures) if exposures else 0.0,
        "tipp_guard_months": tipp_guard_months,
        "vix_guard_months": vix_guard_months,
        "base_guard_months": base_guard_months,
        "rows": rows,
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
        "median_tipp_guard_months": statistics.median(item["tipp_guard_months"] for item in items),
        "median_vix_guard_months": statistics.median(item["vix_guard_months"] for item in items),
        "median_base_guard_months": statistics.median(item["base_guard_months"] for item in items),
    }


def evaluate_rule(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    option_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: BlendTippRule,
) -> dict[str, Any]:
    cases = [run_case(csi_paths, option_paths, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
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
        "objective": "Test portfolio-level TIPP/CPPI sizing on high-return blended CSI + synthetic option sleeves across all month phases and execution lags.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Monthly allocation wrapper over modelled synthetic option sleeves; no executable option-chain liquidity, skew, bid/ask, tax, borrow, or intramonth rebalance evidence.",
        "base_blend_names": BASE_BLEND_NAMES,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "mode",
            "base_blend_name",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "min_exposure",
            "vix_guard_gte",
            "vix_scale",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_exposure",
            "median_tipp_guard_months",
            "median_vix_guard_months",
            "median_base_guard_months",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    phase_rule_names = {BLEND_RULE_BY_NAME[name].phase_rule_name for name in BASE_BLEND_NAMES}
    option_rule_names = {BLEND_RULE_BY_NAME[name].option_rule_name for name in BASE_BLEND_NAMES}
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(conn, csi_series, yields, trade_dates, holdings, phase_rule_names)
        option_paths = precompute_option_paths(option_data, csi_paths, option_rule_names)
        results = []
        for rule in RULES:
            result = evaluate_rule(csi_paths, option_paths, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name[:82]:<82} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"avg_exp={summary['median_avg_exposure'] * 100:5.1f}%"
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

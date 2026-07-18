#!/usr/bin/env python3
"""Search daily TIPP overlays on CBOE option-protection sleeves."""

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
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES
from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_option_protection import DailyOptionData, OptionProtectionRule, load_data, risk_off
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_option_protection_tipp_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_option_protection_tipp_search.csv"


BASE_RULES = [
    OptionProtectionRule("base_qqq_vxth_30_vix30", "QQQ", "VXTH", 0.30, 0.28, 2.8, 30.0, -0.10, 0.80, 1.2),
]


@dataclass(frozen=True)
class TippOverlayRule:
    name: str
    base_rule_name: str
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    drawdown_cut_lte: float
    drawdown_cut_scale: float


def build_rules() -> list[TippOverlayRule]:
    rules: list[TippOverlayRule] = []
    for base in BASE_RULES:
        for mode in ["tipp", "cppi"]:
            for floor_pct in [0.86, 0.88, 0.90]:
                for multiplier in [8.0, 10.0]:
                    for max_exposure in [1.5, 1.75]:
                        for cut_lte, cut_scale in [(-1.0, 1.0)]:
                            rules.append(
                                TippOverlayRule(
                                    (
                                        f"optprot_{base.name}_{mode}_f{int(floor_pct * 100)}"
                                        f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                        f"_dd{int(abs(cut_lte) * 100):02d}s{int(cut_scale * 100)}"
                                    ),
                                    base.name,
                                    mode,
                                    floor_pct,
                                    multiplier,
                                    max_exposure,
                                    cut_lte,
                                    cut_scale,
                                )
                            )
    return rules


RULES = build_rules()
BASE_RULE_BY_NAME = {rule.name: rule for rule in BASE_RULES}


def base_day_return(data: DailyOptionData, rule: OptionProtectionRule, idx: int) -> tuple[float, float, bool, float]:
    is_risk = risk_off(data, rule, idx)
    protection_weight = rule.protection_weight
    max_leverage = rule.max_leverage
    if is_risk:
        protection_weight = rule.risk_off_protection_weight
        max_leverage = min(max_leverage, rule.risk_off_max_leverage)
    growth_vol = data.features[rule.growth_symbol][idx - 1]["vol_63"] or 0.30
    protection_vol = data.features[rule.protection_symbol][idx - 1]["vol_63"] or 0.20
    blended_vol = max(0.04, (1.0 - protection_weight) * growth_vol + protection_weight * protection_vol)
    leverage = min(max_leverage, rule.target_vol / blended_vol)
    growth_weight = leverage * (1.0 - protection_weight)
    hedge_weight = leverage * protection_weight
    cash_weight = 1.0 - leverage
    ret = (
        growth_weight * data.returns[rule.growth_symbol][idx]
        + hedge_weight * data.returns[rule.protection_symbol][idx]
        + cash_weight * data.returns[rule.fallback_symbol][idx]
    )
    return ret, leverage, is_risk, data.returns[rule.fallback_symbol][idx]


def precompute_base_days(data: DailyOptionData) -> dict[str, list[tuple[float, float, bool, float]]]:
    return {
        base.name: [base_day_return(data, base, idx) for idx in range(len(data.dates))]
        for base in BASE_RULES
    }


def overlay_exposure(rule: TippOverlayRule, capital: float, peak: float, initial_floor: float) -> float:
    if rule.mode == "tipp":
        floor_value = peak * rule.floor_pct
    else:
        floor_value = initial_floor
    cushion = max(0.0, capital - floor_value)
    exposure = min(rule.max_exposure, rule.multiplier * cushion / max(capital, 1.0))
    drawdown = capital / peak - 1.0
    if drawdown <= rule.drawdown_cut_lte:
        exposure *= rule.drawdown_cut_scale
    return exposure


def run_case(
    data: DailyOptionData,
    base_days: dict[str, list[tuple[float, float, bool, float]]],
    rule: TippOverlayRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    base = BASE_RULE_BY_NAME[rule.base_rule_name]
    start_idx = max(data.start_index(phase, lag), 253)
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    risk_off_days = 0
    exposure_values = []
    engine_leverages = []
    for idx in range(start_idx + 1, len(data.dates)):
        engine_return, engine_leverage, is_risk, safe_return = base_days[base.name][idx]
        exposure = overlay_exposure(rule, capital, peak, initial_floor)
        capital *= 1.0 + exposure * engine_return + (1.0 - exposure) * safe_return
        peak = max(peak, capital)
        curve.append(capital)
        exposure_values.append(exposure)
        engine_leverages.append(engine_leverage)
        if is_risk:
            risk_off_days += 1
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
        "median_overlay_exposure": statistics.median(exposure_values) if exposure_values else 0.0,
        "median_engine_leverage": statistics.median(engine_leverages) if engine_leverages else 0.0,
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
        "median_overlay_exposure": statistics.median(item["median_overlay_exposure"] for item in items),
        "median_engine_leverage": statistics.median(item["median_engine_leverage"] for item in items),
    }


def evaluate_rule(data: DailyOptionData, base_days: dict[str, list[tuple[float, float, bool, float]]], rule: TippOverlayRule) -> dict[str, Any]:
    cases = [run_case(data, base_days, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search daily TIPP overlays on CBOE option-protection sleeves.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "base_rules": [asdict(rule) for rule in BASE_RULES],
        "rule_count": len(RULES),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "base_rule_name",
        "mode",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "drawdown_cut_lte",
        "drawdown_cut_scale",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_risk_off_days",
        "median_overlay_exposure",
        "median_engine_leverage",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fields})


def main() -> int:
    conn = get_connection()
    try:
        data = load_data(conn)
    finally:
        conn.close()
    base_days = precompute_base_days(data)
    results = []
    for idx, rule in enumerate(RULES, 1):
        result = evaluate_rule(data, base_days, rule)
        results.append(result)
        summary = result["summary"]
        if idx % 250 == 0 or result["target_met"]:
            print(
                f"{idx:>5}/{len(RULES)} {rule.name[:90]:<90} "
                f"pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"exp={summary['median_overlay_exposure']:.2f}"
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
        f"best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}万 "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

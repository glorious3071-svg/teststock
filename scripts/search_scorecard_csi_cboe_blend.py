#!/usr/bin/env python3
"""Search executable CSI phase ensemble + CBOE option-strategy blends."""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blended_protection import precompute_csi_paths
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_option_protection import DailyOptionData, OptionProtectionRule, load_data as load_option_data, risk_off
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_cboe_blend_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_cboe_blend_search.csv"


BASE_CBOE_RULES = [
    OptionProtectionRule("cboe_qqq_vxth_30_vix30", "QQQ", "VXTH", 0.30, 0.28, 2.8, 30.0, -0.10, 0.80, 1.2),
    OptionProtectionRule("cboe_qqq_vxth_25_vix25", "QQQ", "VXTH", 0.25, 0.24, 2.2, 25.0, -0.08, 0.75, 1.0),
    OptionProtectionRule("cboe_qqq_pput_40_vix30", "QQQ", "PPUT", 0.40, 0.28, 2.8, 30.0, -0.10, 0.90, 1.2),
]


@dataclass(frozen=True)
class CboeBlendRule:
    name: str
    phase_rule_name: str
    cboe_rule_name: str
    csi_weight: float
    cboe_weight: float
    overlay_mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    drawdown_cut_lte: float = -1.0
    drawdown_cut_scale: float = 1.0


def build_rules() -> list[CboeBlendRule]:
    rules: list[CboeBlendRule] = []
    phase_names = ["phase12_lever120_us10y", "phase12_guard60_us10y"]
    weights = [(0.0, 1.0), (0.20, 0.80), (0.35, 0.65), (0.50, 0.50), (0.65, 0.35)]
    overlay_specs = [
        ("plain", 0.0, 0.0, 1.0, -1.0, 1.0),
        ("cppi", 0.86, 6.0, 1.25, -1.0, 1.0),
        ("cppi", 0.86, 8.0, 1.5, -1.0, 1.0),
        ("cppi", 0.88, 8.0, 1.5, -1.0, 1.0),
        ("tipp", 0.86, 6.0, 1.25, -1.0, 1.0),
        ("tipp", 0.88, 8.0, 1.5, -1.0, 1.0),
        ("tipp", 0.88, 8.0, 1.5, -0.08, 0.25),
    ]
    for phase in phase_names:
        for cboe in BASE_CBOE_RULES:
            for csi_weight, cboe_weight in weights:
                for mode, floor, mult, max_exp, cut_lte, cut_scale in overlay_specs:
                    rules.append(
                        CboeBlendRule(
                            (
                                f"cboeblend_{phase}_{cboe.name}_c{int(csi_weight * 100):02d}"
                                f"_b{int(cboe_weight * 100):02d}_{mode}_f{int(floor * 100):02d}"
                                f"_m{int(mult * 10):03d}_x{int(max_exp * 100)}"
                            ),
                            phase,
                            cboe.name,
                            csi_weight,
                            cboe_weight,
                            mode,
                            floor,
                            mult,
                            max_exp,
                            cut_lte,
                            cut_scale,
                        )
                    )
    return rules


RULES = build_rules()
CBOE_RULE_BY_NAME = {rule.name: rule for rule in BASE_CBOE_RULES}


def base_day_return(data: DailyOptionData, rule: OptionProtectionRule, idx: int) -> tuple[float, bool]:
    is_risk = risk_off(data, rule, idx)
    protection_weight = rule.risk_off_protection_weight if is_risk else rule.protection_weight
    max_leverage = min(rule.max_leverage, rule.risk_off_max_leverage) if is_risk else rule.max_leverage
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
    return ret, is_risk


def cboe_period_return(data: DailyOptionData, rule: OptionProtectionRule, start: dt.date, end: dt.date) -> tuple[float, int]:
    start_idx = max(bisect_left(data.dates, start), 253)
    end_idx = bisect_right(data.dates, end) - 1
    capital = 1.0
    risk_days = 0
    for idx in range(start_idx + 1, max(start_idx + 1, end_idx + 1)):
        day_ret, is_risk = base_day_return(data, rule, idx)
        capital *= 1.0 + day_ret
        if is_risk:
            risk_days += 1
    return capital - 1.0, risk_days


def precompute_cboe_paths(
    option_data: DailyOptionData,
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    sample_paths: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for (_phase_rule, phase, lag), rows in csi_paths.items():
        sample_paths.setdefault((phase, lag), rows)
    out: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for cboe_rule in BASE_CBOE_RULES:
        for (phase, lag), rows in sample_paths.items():
            path_rows = []
            for row in rows:
                start_raw = row["start_exec"]
                end_raw = row["end_exec"]
                start = dt.date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
                end = dt.date.fromisoformat(end_raw) if isinstance(end_raw, str) else end_raw
                ret, risk_days = cboe_period_return(option_data, cboe_rule, start, end)
                safe_return = float(row.get("safe_return") or row.get("defensive_return") or 0.0)
                path_rows.append({"period_return": ret, "risk_days": risk_days, "safe_return": safe_return})
            out[(cboe_rule.name, phase, lag)] = path_rows
    return out


def overlay_exposure(rule: CboeBlendRule, capital: float, peak: float, initial_floor: float) -> float:
    if rule.overlay_mode == "plain":
        exposure = 1.0
    else:
        floor_value = peak * rule.floor_pct if rule.overlay_mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor_value)
        exposure = min(rule.max_exposure, rule.multiplier * cushion / max(capital, 1.0))
    drawdown = capital / peak - 1.0
    if drawdown <= rule.drawdown_cut_lte:
        exposure *= rule.drawdown_cut_scale
    return exposure


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    cboe_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: CboeBlendRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    csi_rows = csi_paths[(rule.phase_rule_name, phase, lag)]
    cboe_rows = cboe_paths[(rule.cboe_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures = []
    risk_days = []
    for csi_row, cboe_row in zip(csi_rows, cboe_rows):
        raw_return = (
            rule.csi_weight * float(csi_row.get("period_return") or csi_row.get("csi_return") or 0.0)
            + rule.cboe_weight * cboe_row["period_return"]
            + (1.0 - rule.csi_weight - rule.cboe_weight) * float(csi_row.get("safe_return") or csi_row.get("defensive_return") or 0.0)
        )
        exposure = overlay_exposure(rule, capital, peak, initial_floor)
        safe_return = float(csi_row.get("safe_return") or csi_row.get("defensive_return") or 0.0)
        period_return = exposure * raw_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
        risk_days.append(cboe_row["risk_days"])
    mdd = max_drawdown(curve)
    years = 20
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
        "median_overlay_exposure": statistics.median(exposures) if exposures else 0.0,
        "median_cboe_risk_days": statistics.median(risk_days) if risk_days else 0.0,
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
        "median_overlay_exposure": statistics.median(item["median_overlay_exposure"] for item in items),
        "median_cboe_risk_days": statistics.median(item["median_cboe_risk_days"] for item in items),
    }


def evaluate_rule(csi_paths, cboe_paths, rule: CboeBlendRule) -> dict[str, Any]:
    cases = [run_case(csi_paths, cboe_paths, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search executable CSI phase ensemble plus CBOE option-strategy blends.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "phase_rule_name",
        "cboe_rule_name",
        "csi_weight",
        "cboe_weight",
        "overlay_mode",
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
        "median_overlay_exposure",
        "median_cboe_risk_days",
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
        csi_series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {rule.phase_rule_name for rule in RULES},
        )
        option_data = load_option_data(conn)
    finally:
        conn.close()
    cboe_paths = precompute_cboe_paths(option_data, csi_paths)
    results = []
    for rule in RULES:
        result = evaluate_rule(csi_paths, cboe_paths, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:90]:<90} pass={summary['pass_count']:>2}/{summary['count']} "
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

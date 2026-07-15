#!/usr/bin/env python3
"""Search TIPP wrappers for A-share-only CSI scorecard engines."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_domestic_only_regime_defense import (
    DomesticOnlyRule,
    build_domestic_cases,
    build_feature_map,
    period_return_with_listed_stop,
    period_return_with_pre_stop,
    pre_option_exposure,
)

OUT_DIR = ROOT / "data" / "backtests"


@dataclass(frozen=True)
class TippRule:
    name: str
    base_rule: DomesticOnlyRule
    floor_pct: float
    multiplier: float
    max_exposure: float
    min_exposure: float = 0.0
    drawdown_guard_lte: float = -1.0
    drawdown_guard_exposure: float = 0.0


BASE_PERIOD_RETURN_CACHE: dict[tuple[int, str], tuple[float, float]] = {}


def pct_token(value: float) -> str:
    sign = "n" if value < 0 else "p"
    return f"{sign}{abs(int(round(value * 100))):02d}"


def make_base_rule(
    phase_rule_name: str,
    listed_stop_loss_pct: float,
    listed_normal_exposure: float,
    pre_stop_loss_pct: float,
    pre_risk_exposure: float,
) -> DomesticOnlyRule:
    name = (
        f"base_{phase_rule_name.replace('phase12_', 'p12_')}"
        f"_lst{int(abs(listed_stop_loss_pct) * 1000):03d}"
        f"_lx{int(listed_normal_exposure * 100)}"
        f"_pst{int(abs(pre_stop_loss_pct) * 1000):03d}"
        f"_pr{int(pre_risk_exposure * 100)}"
    )
    return DomesticOnlyRule(
        name=name,
        phase_rule_name=phase_rule_name,
        listed_stop_loss_pct=listed_stop_loss_pct,
        listed_normal_exposure=listed_normal_exposure,
        listed_post_stop_exposure=0.0,
        pre_normal_exposure=1.0,
        pre_risk_exposure=pre_risk_exposure,
        pre_crisis_exposure=0.0,
        pre_stop_loss_pct=pre_stop_loss_pct,
        pre_post_stop_exposure=0.0,
        drawdown_guard_lte=-1.0,
        drawdown_guard_scale=1.0,
        cs300_3m_lte=-0.10,
        cs300_6m_lte=-0.18,
        cs300_12m_lte=-0.40,
        dd60_lte=-0.15,
        dd120_lte=-0.15,
        ma200_lte=0.0,
        min_bad_signals=1,
    )


def build_rules(quick: bool = False, aggressive: bool = False, max_rules: int = 0) -> list[TippRule]:
    phase_names = ["phase12_lever150_cash", "phase12_lever120_cash", "phase12_guard60_cash"]
    listed_stops = [-0.01, -0.015, -0.02]
    listed_exposures = [1.5, 2.0, 2.5, 3.0]
    pre_stops = [-0.015, -0.025]
    pre_risks = [0.0, 0.30]
    floors = [0.88, 0.90, 0.92, 0.95]
    multipliers = [2.0, 3.0, 4.0, 6.0, 8.0]
    max_exposures = [1.0, 1.25, 1.5, 2.0]
    if quick:
        phase_names = ["phase12_lever150_cash", "phase12_lever120_cash"]
        listed_stops = [-0.01, -0.02]
        listed_exposures = [2.0, 3.0]
        pre_stops = [-0.015, -0.025]
        pre_risks = [0.0, 0.30]
        floors = [0.90, 0.92, 0.95]
        multipliers = [3.0, 4.0, 6.0]
        max_exposures = [1.0, 1.5, 2.0]
    min_exposures = [0.0]
    drawdown_guards = [(-1.0, 0.0)]
    if aggressive:
        phase_names = ["phase12_lever150_cash", "phase12_lever120_cash"]
        listed_stops = [-0.005, -0.01, -0.015]
        listed_exposures = [2.5, 3.0, 3.5, 4.0]
        pre_stops = [-0.01, -0.015, -0.02]
        pre_risks = [0.0, 0.30]
        floors = [0.82, 0.84, 0.86, 0.88, 0.90]
        multipliers = [6.0, 8.0, 10.0, 12.0]
        max_exposures = [1.5, 2.0, 2.5, 3.0]
        min_exposures = [0.0, 0.05, 0.10]
        if quick:
            phase_names = ["phase12_lever150_cash"]
            listed_stops = [-0.005, -0.01]
            listed_exposures = [3.0, 4.0]
            pre_stops = [-0.01, -0.015]
            pre_risks = [0.30]
            floors = [0.84, 0.85, 0.86, 0.87, 0.88]
            multipliers = [7.0, 8.0, 9.0, 10.0]
            max_exposures = [2.0]
            min_exposures = [0.0]
            drawdown_guards = [
                (-0.06, 0.0),
                (-0.065, 0.0),
                (-0.07, 0.0),
                (-0.075, 0.0),
                (-0.08, 0.0),
                (-0.08, 0.02),
                (-0.08, 0.05),
            ]

    rules: list[TippRule] = []
    for phase_name in phase_names:
        for listed_stop in listed_stops:
            for listed_exposure in listed_exposures:
                for pre_stop in pre_stops:
                    for pre_risk in pre_risks:
                        base = make_base_rule(phase_name, listed_stop, listed_exposure, pre_stop, pre_risk)
                        for floor in floors:
                            for multiplier in multipliers:
                                for max_exposure in max_exposures:
                                    for min_exposure in min_exposures:
                                        for drawdown_guard_lte, drawdown_guard_exposure in drawdown_guards:
                                            rules.append(
                                                TippRule(
                                                    name=(
                                                        f"dtipp_{base.name}"
                                                        f"_f{int(floor * 100)}"
                                                        f"_m{int(multiplier * 10):02d}"
                                                        f"_x{int(max_exposure * 100)}"
                                                        f"_n{int(min_exposure * 100)}"
                                                        f"_gd{int(abs(drawdown_guard_lte) * 1000):03d}"
                                                        f"e{int(drawdown_guard_exposure * 100)}"
                                                    ),
                                                    base_rule=base,
                                                    floor_pct=floor,
                                                    multiplier=multiplier,
                                                    max_exposure=max_exposure,
                                                    min_exposure=min_exposure,
                                                    drawdown_guard_lte=drawdown_guard_lte,
                                                    drawdown_guard_exposure=drawdown_guard_exposure,
                                                )
                                            )
                                            if max_rules and len(rules) >= max_rules:
                                                return rules
    return rules


def base_period_return(period: dict[str, Any], base_rule: DomesticOnlyRule, feature_map) -> tuple[float, float]:
    cache_key = (id(period), base_rule.name)
    cached = BASE_PERIOD_RETURN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    phase_item = period["phase"][base_rule.phase_rule_name]
    base_without_package = float(phase_item["base_without_package"])
    no_stop_risky = base_without_package + float(period["package_end_return"])
    if period["package_source"] == "listed_contract" and period["daily_points"] > 0:
        period_ret, _stopped = period_return_with_listed_stop(period, base_without_package, base_rule)
        result = (period_ret, base_rule.listed_normal_exposure)
        BASE_PERIOD_RETURN_CACHE[cache_key] = result
        return result
    exposure, _reasons = pre_option_exposure(base_rule, feature_map[period["start_exec"]])
    period_ret, _stopped = period_return_with_pre_stop(period, no_stop_risky, exposure, base_rule)
    result = (period_ret, exposure)
    BASE_PERIOD_RETURN_CACHE[cache_key] = result
    return result


def run_case(domestic_case: dict[str, Any], rule: TippRule, feature_map) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    exposures: list[float] = []
    base_exposures: list[float] = []
    guard_months = 0
    for period in domestic_case["periods"]:
        peak = max(peak, capital)
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        wrapper_exposure = min(rule.max_exposure, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
        current_drawdown = capital / peak - 1.0 if peak > 0 else 0.0
        if current_drawdown <= rule.drawdown_guard_lte:
            wrapper_exposure = min(wrapper_exposure, rule.drawdown_guard_exposure)
        if wrapper_exposure <= 1e-9:
            guard_months += 1
        raw_ret, base_exposure = base_period_return(period, rule.base_rule, feature_map)
        safe_return = float(period["safe_return"])
        period_ret = wrapper_exposure * raw_ret + (1.0 - wrapper_exposure) * safe_return
        capital *= 1.0 + period_ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(wrapper_exposure)
        base_exposures.append(base_exposure)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{domestic_case['phase_month_offset']}_lag{domestic_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": domestic_case["phase_month_offset"],
        "execution_lag_days": domestic_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_wrapper_exposure": statistics.mean(exposures) if exposures else 0.0,
        "avg_base_exposure": statistics.mean(base_exposures) if base_exposures else 0.0,
        "guard_months": guard_months,
    }


def matrix_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
        "median_avg_wrapper_exposure": statistics.median(item["avg_wrapper_exposure"] for item in cases),
        "median_guard_months": statistics.median(item["guard_months"] for item in cases),
    }


def evaluate_rule(domestic_cases: list[dict[str, Any]], rule: TippRule, feature_map) -> dict[str, Any]:
    cases = [run_case(case, rule, feature_map) for case in domestic_cases]
    summary = matrix_summary(cases)
    return {
        "rule": {
            **{key: value for key, value in asdict(rule).items() if key != "base_rule"},
            "base_rule": asdict(rule.base_rule),
        },
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        prefix = OUT_DIR / f"scorecard_csi_domestic_only_tipp_{args.underlying_mode}_miss{args.missing_package_policy}"
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search TIPP/CPPI-style wrappers over A-share-only CSI scorecard engines.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {"no_overseas_assets": True, **meta},
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "base_name",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "min_exposure",
        "drawdown_guard_lte",
        "drawdown_guard_exposure",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_avg_wrapper_exposure",
        "median_guard_months",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {
                "name": item["rule"]["name"],
                "base_name": item["rule"]["base_rule"]["name"],
                "floor_pct": item["rule"]["floor_pct"],
                "multiplier": item["rule"]["multiplier"],
                "max_exposure": item["rule"]["max_exposure"],
                "min_exposure": item["rule"]["min_exposure"],
                "drawdown_guard_lte": item["rule"]["drawdown_guard_lte"],
                "drawdown_guard_exposure": item["rule"]["drawdown_guard_exposure"],
                **item["summary"],
            }
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search TIPP wrappers for A-share-only CSI engines.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--package-scale", type=float, default=1.0)
    parser.add_argument("--aggressive", action="store_true", help="Search lower floors, higher multipliers, and a small min exposure.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--max-rules", type=int, default=0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    domestic_cases, meta, series = build_domestic_cases(args)
    feature_map = build_feature_map(domestic_cases, series)
    rules = build_rules(quick=args.quick, aggressive=args.aggressive, max_rules=args.max_rules)
    results = [evaluate_rule(domestic_cases, rule, feature_map) for rule in rules]
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )
    json_path, csv_path = write_outputs(results, meta, args)
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['rule']['name']:<118} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_wrapper_exposure']:.2f}"
        )
    best = results[0]["summary"]
    print(
        f"Wrote {json_path}; rules={len(results)} best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {csv_path}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

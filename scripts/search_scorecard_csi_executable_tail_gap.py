#!/usr/bin/env python3
"""Focused search for executable candidates near the non-defined-loss frontier."""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME, BlendTippRule, run_case as run_core_case
from scripts.backtest_scorecard_csi_blended_protection import load_option_data, precompute_csi_paths, precompute_option_paths
from scripts.backtest_scorecard_csi_crypto_satellite_mix import (
    SatelliteMixRule,
    crypto_period_returns,
    matrix_summary,
    run_mix_case,
)
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import CryptoTippRule, load_data
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, TARGET_CAPITAL, load_hybrid_holdings
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_executable_tail_gap_search.json"
OUT_CSV = OUT_DIR / "scorecard_csi_executable_tail_gap_search.csv"


CORE_RULES = [
    BlendTippRule(
        "core_spread95_f84_m080_x175",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_95spread_call108_lev125_c20_o80",
        0.84,
        8.0,
        1.75,
    ),
    BlendTippRule(
        "core_spread94_f84_m080_x175",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_94spread_call108_lev125_c20_o80",
        0.84,
        8.0,
        1.75,
    ),
]

SATELLITE_RULES = [
    CryptoTippRule("sat_btc_cppi_f90_m6", "cppi", "btc", 0.90, 6.0, 1.0, 1),
    CryptoTippRule("sat_btc_cppi_f88_m8", "cppi", "btc", 0.88, 8.0, 1.25, 1),
]


def build_rules() -> list[SatelliteMixRule]:
    rules: list[SatelliteMixRule] = []
    weights = [(0.95, 0.08), (0.92, 0.10), (0.88, 0.14), (0.85, 0.15)]
    floors = [0.83, 0.84, 0.85, 0.86]
    multipliers = [6.0, 8.0, 10.0]
    max_exposures = [1.5, 1.75, 2.0]
    drawdown_modes = [(-1.0, 1.0), (-0.04, 0.0), (-0.06, 0.10), (-0.08, 0.25)]
    for core in CORE_RULES:
        for sat in SATELLITE_RULES:
            for core_weight, satellite_weight in weights:
                suffix = f"{core.name}_{sat.name}_c{int(core_weight * 100)}_s{int(satellite_weight * 100)}"
                for floor_pct in floors:
                    for multiplier in multipliers:
                        for max_exposure in max_exposures:
                            for drawdown_scale_lte, drawdown_scale in drawdown_modes:
                                rules.append(
                                    SatelliteMixRule(
                                        (
                                            f"tailgap_{suffix}_f{int(floor_pct * 100)}"
                                            f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                            f"_dd{int(abs(drawdown_scale_lte) * 100):02d}s{int(drawdown_scale * 100)}"
                                        ),
                                        core.name,
                                        sat.name,
                                        core_weight,
                                        satellite_weight,
                                        "tipp",
                                        floor_pct,
                                        multiplier,
                                        max_exposure,
                                        drawdown_scale_lte,
                                        drawdown_scale,
                                    )
                                )
    return rules


RULES = build_rules()


def evaluate_rule(
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: SatelliteMixRule,
) -> dict[str, Any]:
    cases = [run_mix_case(core_cache, satellite_returns, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Focused executable search near the non-defined-loss tail gap.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames = [
        "name",
        "core_rule_name",
        "satellite_rule_name",
        "core_weight",
        "satellite_weight",
        "overlay_mode",
        "overlay_floor_pct",
        "overlay_multiplier",
        "overlay_max_exposure",
        "drawdown_scale_lte",
        "drawdown_scale",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_overlay_exposure",
        "median_drawdown_scaled_months",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[rule.base_blend_name].phase_rule_name for rule in CORE_RULES},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[rule.base_blend_name].option_rule_name for rule in CORE_RULES},
        )
        crypto_data = load_data(conn)
    finally:
        conn.close()

    core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
    for core_rule in CORE_RULES:
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
                core_cache[(core_rule.name, phase, lag)] = case
                periods_by_case.setdefault(
                    (phase, lag),
                    [(dt.date.fromisoformat(row["start_exec"]), dt.date.fromisoformat(row["end_exec"])) for row in case["rows"]],
                )

    satellite_returns: dict[tuple[str, int, int], list[float]] = {}
    for sat_rule in SATELLITE_RULES:
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                satellite_returns[(sat_rule.name, phase, lag)] = crypto_period_returns(
                    crypto_data,
                    sat_rule,
                    periods_by_case[(phase, lag)],
                    phase,
                    lag,
                )

    results = []
    for idx, rule in enumerate(RULES, 1):
        result = evaluate_rule(core_cache, satellite_returns, rule)
        results.append(result)
        summary = result["summary"]
        if idx % 250 == 0 or result["target_met"]:
            print(
                f"{idx:>5}/{len(RULES)} {rule.name[:90]:<90} "
                f"pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}%"
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

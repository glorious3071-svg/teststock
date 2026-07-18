#!/usr/bin/env python3
"""Expanded TIPP/CPPI search on blended CSI + option sleeves.

This experiment formalizes the wider monthly pressure grid that was useful in
ad hoc probes: lower trailing floors and higher multipliers over the existing
high-return blended CSI + synthetic-option engines.  It writes a separate CSV so
the frontier summary can compare the expanded grid without changing the
baseline `blend_tipp_overlay` definition.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import (  # noqa: E402
    BLEND_RULE_BY_NAME,
    BlendTippRule,
    evaluate_rule,
)
from scripts.backtest_scorecard_csi_blended_protection import (  # noqa: E402
    load_option_data,
    precompute_csi_paths,
    precompute_option_paths,
)
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series  # noqa: E402
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    load_hybrid_holdings,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_blend_tipp_expanded_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_blend_tipp_expanded_search.csv"

BASE_BLEND_NAMES = [
    "blend_phase12_lever120_us10y_qqq_put95_call115_lev250_c00_o100",
    "blend_phase12_lever120_us10y_qqq_put95_call115_lev250_c50_o50",
    "blend_phase12_lever120_us10y_qqq_put98_call112_lev220_c35_o65",
    "blend_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
    "guard8_phase12_guard60_us10y_qqq_put98_call108_lev125_c20_o80",
    "macroguard_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
]


def short_name(base_name: str) -> str:
    name = base_name
    for prefix in ["blend_", "guard8_", "macroguard_"]:
        name = name.removeprefix(prefix)
    return name.replace("phase12_", "").replace("lever120_", "l120_")


def build_rules() -> list[BlendTippRule]:
    rules: list[BlendTippRule] = []
    for base_name in BASE_BLEND_NAMES:
        if base_name not in BLEND_RULE_BY_NAME:
            raise RuntimeError(f"missing base blend rule: {base_name}")
        stem_base = short_name(base_name)
        for floor_pct in [0.84, 0.86, 0.88, 0.90]:
            for multiplier in [6.0, 8.0, 10.0, 12.0]:
                for max_exposure in [1.00, 1.25, 1.50]:
                    stem = (
                        f"xbtipp_{stem_base}_f{int(floor_pct * 100)}"
                        f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                    )
                    rules.append(
                        BlendTippRule(
                            stem,
                            "tipp",
                            base_name,
                            floor_pct,
                            multiplier,
                            max_exposure,
                        )
                    )
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
        for floor_pct in [0.86, 0.88, 0.90]:
            for multiplier in [4.0, 6.0, 8.0]:
                for max_exposure in [1.00, 1.25, 1.50]:
                    rules.append(
                        BlendTippRule(
                            (
                                f"xbcppi_{stem_base}_f{int(floor_pct * 100)}"
                                f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                            ),
                            "cppi",
                            base_name,
                            floor_pct,
                            multiplier,
                            max_exposure,
                        )
                    )
    return rules


RULES = build_rules()


def compact_case(case: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in case.items() if key != "rows"}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Expanded monthly TIPP/CPPI pressure grid over blended CSI scorecard + synthetic option sleeves.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "base_blend_names": BASE_BLEND_NAMES,
        "rule_count": len(RULES),
        "model_limits": "Monthly allocation wrapper over modelled synthetic option sleeves; no executable option-chain liquidity, skew, bid/ask, tax, borrow, or intramonth rebalance evidence.",
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
    finally:
        conn.close()

    results = []
    for rule in RULES:
        result = evaluate_rule(csi_paths, option_paths, rule)
        result["cases"] = [compact_case(case) for case in result["cases"]]
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:88]:<88} pass={summary['pass_count']:>2}/{summary['count']} "
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

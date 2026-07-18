#!/usr/bin/env python3
"""Backtest modeled monthly defined-loss protection on the best satellite mix.

This is a feasibility boundary test.  It asks whether the current strongest
CSI/option core plus small BTC/crypto satellite can satisfy the all-month-phase
target if a monthly defined-loss overlay is available at explicit modeled cost
and upside give-up.  It is not option-chain execution evidence.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import (  # noqa: E402
    BLEND_RULE_BY_NAME,
    run_case as run_core_case,
)
from scripts.backtest_scorecard_csi_blended_protection import (  # noqa: E402
    load_option_data,
    precompute_csi_paths,
    precompute_option_paths,
)
from scripts.backtest_scorecard_csi_crypto_satellite_mix import (  # noqa: E402
    CORE_RULE_BY_NAME,
    SATELLITE_RULE_BY_NAME,
    crypto_period_returns,
)
from scripts.backtest_scorecard_csi_dynamic_defense import (  # noqa: E402
    EXECUTION_LAGS,
    MONTH_PHASES,
    load_price_series,
)
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields  # noqa: E402
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import load_data  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_defined_loss_overlay_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_defined_loss_overlay_search.csv"


@dataclass(frozen=True)
class DefinedLossRule:
    name: str
    core_rule_name: str
    satellite_rule_name: str
    core_weight: float
    satellite_weight: float
    monthly_loss_floor: float
    premium_monthly: float
    upside_haircut: float


def build_rules() -> list[DefinedLossRule]:
    base_mixes = [
        ("mix95_8", 0.95, 0.08),
        ("mix90_10", 0.90, 0.10),
        ("mix85_12", 0.85, 0.12),
        ("mix80_15", 0.80, 0.15),
    ]
    core_candidates = [
        ("", "core_xbcppi_sub12"),
        ("call103_", "core_xbcppi_sub12_call103"),
        ("put99call102_", "core_xbcppi_sub12_put99_call102"),
        ("spread95call108_", "core_xbcppi_sub12_spread95_call108"),
        ("spread94call108_", "core_xbcppi_sub12_spread94_call108"),
    ]
    rules: list[DefinedLossRule] = []
    for core_prefix, core_name in core_candidates:
        for mix_name, core_weight, satellite_weight in base_mixes:
            for loss_floor in [-0.01, -0.015, -0.02, -0.025, -0.03, -0.04, -0.05]:
                for premium in [0.0, 0.001, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02]:
                    for upside_haircut in [0.0, 0.05, 0.10, 0.20, 0.30]:
                        rules.append(
                            DefinedLossRule(
                                (
                                    f"defloss_{core_prefix}{mix_name}_floor{int(abs(loss_floor) * 1000):03d}"
                                    f"_prem{int(premium * 10000):03d}_up{int(upside_haircut * 100)}"
                                ),
                                core_name,
                                "sat_crypto_cppi",
                                core_weight,
                                satellite_weight,
                                loss_floor,
                                premium,
                                upside_haircut,
                            )
                        )
    return rules


RULES = build_rules()


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
        "median_protected_months": statistics.median(item["protected_months"] for item in items),
    }


def run_case(
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: DefinedLossRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    core_case = core_cache[(rule.core_rule_name, phase, lag)]
    sat_returns = satellite_returns[(rule.satellite_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    curve = [capital]
    protected_months = 0
    for row, sat_return in zip(core_case["rows"], sat_returns):
        safe_return = row["safe_return"]
        raw_return = (
            rule.core_weight * row["period_return"]
            + rule.satellite_weight * sat_return
            + (1.0 - rule.core_weight - rule.satellite_weight) * safe_return
        )
        protected_return = max(rule.monthly_loss_floor, raw_return - rule.premium_monthly)
        if protected_return == rule.monthly_loss_floor and raw_return - rule.premium_monthly < rule.monthly_loss_floor:
            protected_months += 1
        if protected_return > 0:
            protected_return *= 1.0 - rule.upside_haircut
        capital *= 1.0 + protected_return
        curve.append(capital)
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
        "protected_months": protected_months,
    }


def evaluate_rule(
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: DefinedLossRule,
) -> dict[str, Any]:
    cases = [run_case(core_cache, satellite_returns, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test modeled monthly defined-loss overlay cost boundaries on the best CSI + crypto satellite mix.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "model_limits": (
            "Monthly loss floor is modeled directly after an explicit monthly premium and upside haircut. "
            "This is not executable option-chain evidence and does not include strike availability, skew, "
            "slippage, margin, taxes, or intramonth mark-to-market."
        ),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames = [
        "name",
        "core_rule_name",
        "satellite_rule_name",
        "core_weight",
        "satellite_weight",
        "monthly_loss_floor",
        "premium_monthly",
        "upside_haircut",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_protected_months",
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
        core_rules = [
            CORE_RULE_BY_NAME[name]
            for name in [
                "core_xbcppi_sub12",
                "core_xbcppi_sub12_call103",
                "core_xbcppi_sub12_put99_call102",
                "core_xbcppi_sub12_spread95_call108",
                "core_xbcppi_sub12_spread94_call108",
            ]
        ]
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].phase_rule_name for core_rule in core_rules},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].option_rule_name for core_rule in core_rules},
        )
        crypto_data = load_data(conn)
    finally:
        conn.close()

    core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
    for core_rule in core_rules:
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
                core_cache[(core_rule.name, phase, lag)] = case
                if (phase, lag) not in periods_by_case:
                    periods_by_case[(phase, lag)] = [
                        (dt.date.fromisoformat(row["start_exec"]), dt.date.fromisoformat(row["end_exec"]))
                        for row in case["rows"]
                    ]

    satellite_rule = SATELLITE_RULE_BY_NAME["sat_crypto_cppi"]
    satellite_returns: dict[tuple[str, int, int], list[float]] = {}
    for phase in MONTH_PHASES:
        for lag in EXECUTION_LAGS:
            satellite_returns[(satellite_rule.name, phase, lag)] = crypto_period_returns(
                crypto_data,
                satellite_rule,
                periods_by_case[(phase, lag)],
                phase,
                lag,
            )

    results = []
    for rule in RULES:
        result = evaluate_rule(core_cache, satellite_returns, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:92]:<92} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}万 "
            f"median={summary['median_final_capital_wan']:9.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"prot={summary['median_protected_months']:4.1f}"
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

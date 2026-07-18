#!/usr/bin/env python3
"""Backtest modeled defined-loss overlay after adding CSI-linked hedge drag.

The option-chain floor-cost diagnostic found that listed QQQ protection becomes
stress-feasible only when paired with a CSI-linked hedge.  This script tests the
historical impact of that hedge on the strongest modeled defined-loss rule.
It is still a modeled floor backtest, not a final futures execution simulation.
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
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME, run_case as run_core_case
from scripts.backtest_scorecard_csi_blended_protection import load_option_data, precompute_csi_paths, precompute_option_paths
from scripts.backtest_scorecard_csi_crypto_satellite_mix import CORE_RULE_BY_NAME, SATELLITE_RULE_BY_NAME, crypto_period_returns
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import load_data
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    load_price_series,
    period_return,
    price_at,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_defined_loss_csi_hedge_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_defined_loss_csi_hedge_search.csv"


@dataclass(frozen=True)
class DefinedLossCsiHedgeRule:
    name: str
    core_rule_name: str
    satellite_rule_name: str
    core_weight: float
    satellite_weight: float
    monthly_loss_floor: float
    premium_monthly: float
    upside_haircut: float
    csi_hedge_pct: float
    csi_hedge_cost_annual: float
    hedge_future_code: str = "IF.CFX"
    hedge_index_fallback_code: str = CS300_CODE


def build_rules() -> list[DefinedLossCsiHedgeRule]:
    rules: list[DefinedLossCsiHedgeRule] = []
    for hedge_pct in [0.0, 0.20, 0.23, 0.25, 0.30]:
        for hedge_cost in [0.005, 0.01, 0.02]:
            for premium in [0.0, 0.0025, 0.005, 0.0075]:
                rules.append(
                    DefinedLossCsiHedgeRule(
                        (
                            f"defloss_csihedge_spread95call108_mix95_8_floor010"
                            f"_prem{int(premium * 10000):03d}_hedge{int(hedge_pct * 100):02d}"
                            f"_cost{int(hedge_cost * 10000):03d}"
                        ),
                        "core_xbcppi_sub12_spread95_call108",
                        "sat_crypto_cppi",
                        0.95,
                        0.08,
                        -0.01,
                        premium,
                        0.0,
                        hedge_pct,
                        hedge_cost,
                    )
                )
    return rules


RULES = build_rules()


def as_date(raw: Any) -> dt.date:
    return dt.date.fromisoformat(raw) if isinstance(raw, str) else raw


def load_cffex_future_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    codes = ["IF.CFX", "IH.CFX", "IC.CFX", "IM.CFX"]
    out: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, trade_date, close
            FROM fut_daily
            WHERE ts_code IN ('IF.CFX', 'IH.CFX', 'IC.CFX', 'IM.CFX')
              AND close IS NOT NULL
            ORDER BY ts_code, trade_date
            """
        )
        for code, trade_date, close in cur.fetchall():
            out.setdefault(str(code), []).append((trade_date, float(close)))
    return out


def hedge_period_return(
    series: dict[str, list[tuple[dt.date, float]]],
    rule: DefinedLossCsiHedgeRule,
    start: dt.date,
    end: dt.date,
) -> tuple[float, str]:
    future_start = price_at(series, rule.hedge_future_code, start)
    future_end = price_at(series, rule.hedge_future_code, end)
    if future_start and future_end and future_start > 0:
        return future_end / future_start - 1.0, rule.hedge_future_code
    return period_return(series, rule.hedge_index_fallback_code, start, end), rule.hedge_index_fallback_code


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
        "median_hedge_drag_months": statistics.median(item["hedge_drag_months"] for item in items),
        "median_future_hedge_months": statistics.median(item["future_hedge_months"] for item in items),
        "median_index_fallback_hedge_months": statistics.median(
            item["index_fallback_hedge_months"] for item in items
        ),
    }


def run_case(
    csi_series: dict[str, list[tuple[dt.date, float]]],
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: DefinedLossCsiHedgeRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    core_case = core_cache[(rule.core_rule_name, phase, lag)]
    sat_returns = satellite_returns[(rule.satellite_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    curve = [capital]
    protected_months = 0
    hedge_drag_months = 0
    future_hedge_months = 0
    index_fallback_hedge_months = 0
    for row, sat_return in zip(core_case["rows"], sat_returns):
        safe_return = row["safe_return"]
        start_exec = as_date(row["start_exec"])
        end_exec = as_date(row["end_exec"])
        hedge_underlying_return, hedge_source = hedge_period_return(csi_series, rule, start_exec, end_exec)
        if hedge_source == rule.hedge_future_code:
            future_hedge_months += 1
        else:
            index_fallback_hedge_months += 1
        hedge_return = (
            -rule.csi_hedge_pct * hedge_underlying_return
            - rule.csi_hedge_pct * rule.csi_hedge_cost_annual / 12.0
        )
        if hedge_return < 0:
            hedge_drag_months += 1
        raw_return = (
            rule.core_weight * row["period_return"]
            + rule.satellite_weight * sat_return
            + (1.0 - rule.core_weight - rule.satellite_weight) * safe_return
            + hedge_return
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
        "hedge_drag_months": hedge_drag_months,
        "future_hedge_months": future_hedge_months,
        "index_fallback_hedge_months": index_fallback_hedge_months,
    }


def evaluate_rule(csi_series, core_cache, satellite_returns, rule: DefinedLossCsiHedgeRule) -> dict[str, Any]:
    cases = [
        run_case(csi_series, core_cache, satellite_returns, rule, phase, lag)
        for phase in MONTH_PHASES
        for lag in EXECUTION_LAGS
    ]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test historical target impact of adding CSI-linked hedge drag to modeled defined-loss overlay.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "model_limits": (
            "Monthly floor remains modeled directly. CSI hedge uses IF.CFX continuous futures returns when "
            "available and falls back to CSI300 index returns before IF history starts. It still does not include "
            "contract-level rolls, margin, fills, taxes, or capital reservation."
        ),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "core_rule_name",
        "satellite_rule_name",
        "core_weight",
        "satellite_weight",
        "monthly_loss_floor",
        "premium_monthly",
        "upside_haircut",
        "csi_hedge_pct",
        "csi_hedge_cost_annual",
        "hedge_future_code",
        "hedge_index_fallback_code",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_protected_months",
        "median_hedge_drag_months",
        "median_future_hedge_months",
        "median_index_fallback_hedge_months",
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
        csi_series.update(load_cffex_future_series(conn))
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        core_rule = CORE_RULE_BY_NAME["core_xbcppi_sub12_spread95_call108"]
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].phase_rule_name},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].option_rule_name},
        )
        crypto_data = load_data(conn)
    finally:
        conn.close()

    core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
    for phase in MONTH_PHASES:
        for lag in EXECUTION_LAGS:
            case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
            core_cache[(core_rule.name, phase, lag)] = case
            periods_by_case[(phase, lag)] = [
                (as_date(row["start_exec"]), as_date(row["end_exec"]))
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
        result = evaluate_rule(csi_series, core_cache, satellite_returns, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:92]:<92} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
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
        f"best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

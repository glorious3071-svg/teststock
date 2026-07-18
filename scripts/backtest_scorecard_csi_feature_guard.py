#!/usr/bin/env python3
"""Backtest feature-driven pre-month risk guards for scorecard+CSI sleeves."""

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
from scripts.audit_scorecard_csi_crash_features import (
    feature_snapshot,
    load_basic_series,
    load_margin_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    monthly_boundaries,
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
from scripts.backtest_scorecard_csi_phase_ensemble import RULES as PHASE_RULES, PhaseEnsembleRule, ensemble_state
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields, us10y_duration_return

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_feature_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_feature_guard_search.csv"


@dataclass(frozen=True)
class FeatureGuardRule:
    name: str
    phase_rule_name: str
    cap_pct: float
    margin_balance_lte: float | None = None
    margin_20d_chg_gte: float | None = None
    turnover_20d_chg_gte: float | None = None
    turnover_60d_chg_gte: float | None = None
    cs300_ret_1m_gte: float | None = None
    cs300_ret_3m_gte: float | None = None
    pe_ttm_gte: float | None = None
    pb_lte: float | None = None
    combine: str = "any"


RULES = [
    FeatureGuardRule("margin_low_cap0", "phase12_lever120_us10y", 0.0, margin_balance_lte=1_042_323_138.0),
    FeatureGuardRule("margin_low_cap40", "phase12_lever120_us10y", 40.0, margin_balance_lte=1_042_323_138.0),
    FeatureGuardRule("margin_low_cap60", "phase12_lever120_us10y", 60.0, margin_balance_lte=1_042_323_138.0),
    FeatureGuardRule("margin_spike_cap40", "phase12_lever120_us10y", 40.0, margin_20d_chg_gte=0.4569),
    FeatureGuardRule("turnover_spike_cap40", "phase12_lever120_us10y", 40.0, turnover_20d_chg_gte=1.7333),
    FeatureGuardRule("short_rally_cap40", "phase12_lever120_us10y", 40.0, cs300_ret_1m_gte=0.1791),
    FeatureGuardRule("quarter_rally_cap60", "phase12_lever120_us10y", 60.0, cs300_ret_3m_gte=0.1782),
    FeatureGuardRule("valuation_hot_cap60", "phase12_lever120_us10y", 60.0, pe_ttm_gte=27.62),
    FeatureGuardRule("low_pb_cap60", "phase12_lever120_us10y", 60.0, pb_lte=1.22),
    FeatureGuardRule(
        "feature_any_cap40",
        "phase12_lever120_us10y",
        40.0,
        margin_balance_lte=1_042_323_138.0,
        margin_20d_chg_gte=0.4569,
        turnover_20d_chg_gte=1.7333,
        cs300_ret_1m_gte=0.1791,
        combine="any",
    ),
    FeatureGuardRule(
        "feature_any_cap60",
        "phase12_lever120_us10y",
        60.0,
        margin_balance_lte=1_042_323_138.0,
        margin_20d_chg_gte=0.4569,
        turnover_20d_chg_gte=1.7333,
        cs300_ret_1m_gte=0.1791,
        combine="any",
    ),
    FeatureGuardRule(
        "feature_all_cap40",
        "phase12_lever120_us10y",
        40.0,
        margin_20d_chg_gte=0.1975,
        turnover_60d_chg_gte=1.4509,
        cs300_ret_3m_gte=0.1386,
        combine="all",
    ),
    FeatureGuardRule("mean_us10y_feature_any_cap60", "phase12_mean_us10y", 60.0, margin_balance_lte=1_042_323_138.0, margin_20d_chg_gte=0.4569, turnover_20d_chg_gte=1.7333, cs300_ret_1m_gte=0.1791),
]


def phase_rule_by_name(name: str) -> PhaseEnsembleRule:
    for rule in PHASE_RULES:
        if rule.name == name:
            return rule
    raise KeyError(name)


def condition_results(features: dict[str, float | None], rule: FeatureGuardRule) -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    specs = [
        ("margin_balance_lte", "margin_balance", "<=", rule.margin_balance_lte),
        ("margin_20d_chg_gte", "margin_20d_chg", ">=", rule.margin_20d_chg_gte),
        ("turnover_20d_chg_gte", "turnover_20d_chg", ">=", rule.turnover_20d_chg_gte),
        ("turnover_60d_chg_gte", "turnover_60d_chg", ">=", rule.turnover_60d_chg_gte),
        ("cs300_ret_1m_gte", "cs300_ret_1m", ">=", rule.cs300_ret_1m_gte),
        ("cs300_ret_3m_gte", "cs300_ret_3m", ">=", rule.cs300_ret_3m_gte),
        ("pe_ttm_gte", "pe_ttm", ">=", rule.pe_ttm_gte),
        ("pb_lte", "pb", "<=", rule.pb_lte),
    ]
    for label, feature, op, threshold in specs:
        if threshold is None:
            continue
        value = features.get(feature)
        if value is None:
            checks.append((label, False))
        elif op == "<=":
            checks.append((label, float(value) <= threshold))
        else:
            checks.append((label, float(value) >= threshold))
    return checks


def apply_feature_guard(target_pct: float, features: dict[str, float | None], rule: FeatureGuardRule) -> tuple[float, list[str]]:
    checks = condition_results(features, rule)
    if not checks:
        return target_pct, []
    hit = all(value for _name, value in checks) if rule.combine == "all" else any(value for _name, value in checks)
    if not hit:
        return target_pct, []
    reasons = [name for name, value in checks if value]
    return min(target_pct, rule.cap_pct), reasons


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
    series,
    yields,
    basic,
    margin,
    trade_dates,
    holdings,
    rule: FeatureGuardRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    phase_rule = phase_rule_by_name(rule.phase_rule_name)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows = []
    guard_count = 0
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset):
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        target_pct, equity_return, _sleeves, base_reasons = ensemble_state(
            conn,
            series,
            holdings,
            phase_rule,
            start_snapshot,
            start_exec,
            end_exec,
            capital / peak - 1.0,
        )
        features = feature_snapshot(series, basic, margin, start_snapshot)
        guarded_target, guard_reasons = apply_feature_guard(target_pct, features, rule)
        if guard_reasons:
            guard_count += 1
        def_return = us10y_duration_return(yields, start_exec, end_exec)
        equity_weight = guarded_target / 100.0
        non_equity_return = cash_return(start_exec, end_exec) if equity_weight > 1.0 else def_return
        month_return = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
        capital *= 1.0 + month_return
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "period": start_snapshot.isoformat(),
                    "phase_month_offset": phase_month_offset,
                    "execution_lag_days": execution_lag_days,
                    "target_equity_pct": target_pct,
                    "guarded_equity_pct": guarded_target,
                    "equity_return": equity_return,
                    "defensive_return": def_return,
                    "month_return": month_return,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "rebalance_reasons": "|".join(sorted(set(base_reasons + guard_reasons))),
                }
            )
    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_rule": rule.phase_rule_name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "guard_count": guard_count,
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
        "median_guard_count": statistics.median(item["guard_count"] for item in items),
    }


def evaluate_rule(conn, series, yields, basic, margin, trade_dates, holdings, rule: FeatureGuardRule) -> dict[str, Any]:
    cases = [
        run_case(conn, series, yields, basic, margin, trade_dates, holdings, rule, phase, lag)
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
        "objective": "Test feature-driven pre-month risk guards for scorecard+CSI phase ensembles.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_audit": str(ROOT / "data" / "backtests" / "scorecard_csi_crash_feature_audit.json"),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_count",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {"name": item["rule"]["name"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        basic = load_basic_series(conn)
        margin = load_margin_series(conn)
        trade_dates = [d for d, _px in series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        results = []
        for rule in RULES:
            result = evaluate_rule(conn, series, yields, basic, margin, trade_dates, holdings, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name:<32} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
                f"guards={summary['median_guard_count']:.0f}"
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

#!/usr/bin/env python3
"""Backtest FRED macro-risk guards on scorecard+CSI phase ensembles."""

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
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    monthly_boundaries,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_phase_ensemble import RULES as PHASE_RULES, PhaseEnsembleRule, ensemble_state
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields, us10y_duration_return

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_macro_risk_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_macro_risk_guard_search.csv"

SERIES_IDS = ["BAMLH0A0HYM2", "BAMLC0A0CM", "NFCI", "ANFCI", "DFF", "DGS10", "DGS2", "DTWEXBGS"]


@dataclass(frozen=True)
class MacroRiskRule:
    name: str
    phase_rule_name: str
    cap_pct: float
    hy_oas_gte: float | None = None
    corp_oas_gte: float | None = None
    nfci_gte: float | None = None
    anfci_gte: float | None = None
    curve_10y2y_lte: float | None = None
    hy_oas_3m_chg_gte: float | None = None
    dollar_6m_chg_gte: float | None = None
    fedfunds_6m_chg_gte: float | None = None
    combine: str = "any"


RULES = [
    MacroRiskRule("macro_credit_stress_cap40", "phase12_lever120_us10y", 40.0, hy_oas_gte=6.0, corp_oas_gte=2.0),
    MacroRiskRule("macro_credit_stress_cap60", "phase12_lever120_us10y", 60.0, hy_oas_gte=6.0, corp_oas_gte=2.0),
    MacroRiskRule("macro_financial_conditions_cap40", "phase12_lever120_us10y", 40.0, nfci_gte=0.0, anfci_gte=0.0),
    MacroRiskRule("macro_financial_conditions_cap60", "phase12_lever120_us10y", 60.0, nfci_gte=0.0, anfci_gte=0.0),
    MacroRiskRule("macro_curve_inversion_cap60", "phase12_lever120_us10y", 60.0, curve_10y2y_lte=0.0),
    MacroRiskRule("macro_spread_widening_cap40", "phase12_lever120_us10y", 40.0, hy_oas_3m_chg_gte=1.0),
    MacroRiskRule("macro_spread_widening_cap60", "phase12_lever120_us10y", 60.0, hy_oas_3m_chg_gte=1.0),
    MacroRiskRule("macro_dollar_tightening_cap60", "phase12_lever120_us10y", 60.0, dollar_6m_chg_gte=0.08, fedfunds_6m_chg_gte=0.75),
    MacroRiskRule("macro_any_cap40", "phase12_lever120_us10y", 40.0, hy_oas_gte=6.0, nfci_gte=0.0, hy_oas_3m_chg_gte=1.0, dollar_6m_chg_gte=0.08),
    MacroRiskRule("macro_any_cap60", "phase12_lever120_us10y", 60.0, hy_oas_gte=6.0, nfci_gte=0.0, hy_oas_3m_chg_gte=1.0, dollar_6m_chg_gte=0.08),
    MacroRiskRule("macro_all_tight_cap40", "phase12_lever120_us10y", 40.0, hy_oas_gte=4.5, nfci_gte=-0.2, curve_10y2y_lte=0.25, combine="all"),
    MacroRiskRule("macro_all_tight_cap60", "phase12_lever120_us10y", 60.0, hy_oas_gte=4.5, nfci_gte=-0.2, curve_10y2y_lte=0.25, combine="all"),
    MacroRiskRule("macro_mean_any_cap60", "phase12_mean_us10y", 60.0, hy_oas_gte=6.0, nfci_gte=0.0, hy_oas_3m_chg_gte=1.0),
]


def phase_rule_by_name(name: str) -> PhaseEnsembleRule:
    for rule in PHASE_RULES:
        if rule.name == name:
            return rule
    raise KeyError(name)


def load_macro_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    symbols = [f"FRED:{series_id}" for series_id in SERIES_IDS]
    placeholders = ",".join(["%s"] * len(symbols))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, trade_date, COALESCE(adj_close, close)
            FROM external_asset_daily
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, trade_date
            """,
            symbols,
        )
        out = {series_id: [] for series_id in SERIES_IDS}
        for symbol, trade_date, value in cur.fetchall():
            if value is not None:
                out[symbol.split(":", 1)[1]].append((trade_date, float(value)))
    missing = [series_id for series_id, rows in out.items() if not rows]
    if missing:
        raise RuntimeError(f"missing FRED macro rows for {missing}; run scripts/import_fred_macro_series.py")
    return out


def value_at(rows: list[tuple[dt.date, float]], day: dt.date) -> float | None:
    idx = bisect_right(rows, (day, float("inf"))) - 1
    return rows[idx][1] if idx >= 0 else None


def value_change(rows: list[tuple[dt.date, float]], day: dt.date, days: int, pct: bool = False) -> float | None:
    current = value_at(rows, day)
    idx = bisect_left(rows, (day - dt.timedelta(days=days), -float("inf")))
    if current is None or idx >= len(rows):
        return None
    past = rows[idx][1]
    if pct:
        return current / past - 1.0 if past else None
    return current - past


def macro_features(series: dict[str, list[tuple[dt.date, float]]], snapshot: dt.date) -> dict[str, float | None]:
    dgs10 = value_at(series["DGS10"], snapshot)
    dgs2 = value_at(series["DGS2"], snapshot)
    return {
        "hy_oas": value_at(series["BAMLH0A0HYM2"], snapshot),
        "corp_oas": value_at(series["BAMLC0A0CM"], snapshot),
        "nfci": value_at(series["NFCI"], snapshot),
        "anfci": value_at(series["ANFCI"], snapshot),
        "curve_10y2y": dgs10 - dgs2 if dgs10 is not None and dgs2 is not None else None,
        "hy_oas_3m_chg": value_change(series["BAMLH0A0HYM2"], snapshot, 90),
        "dollar_6m_chg": value_change(series["DTWEXBGS"], snapshot, 180, pct=True),
        "fedfunds_6m_chg": value_change(series["DFF"], snapshot, 180),
    }


def check_rule(features: dict[str, float | None], rule: MacroRiskRule) -> list[str]:
    specs = [
        ("hy_oas_gte", "hy_oas", ">=", rule.hy_oas_gte),
        ("corp_oas_gte", "corp_oas", ">=", rule.corp_oas_gte),
        ("nfci_gte", "nfci", ">=", rule.nfci_gte),
        ("anfci_gte", "anfci", ">=", rule.anfci_gte),
        ("curve_10y2y_lte", "curve_10y2y", "<=", rule.curve_10y2y_lte),
        ("hy_oas_3m_chg_gte", "hy_oas_3m_chg", ">=", rule.hy_oas_3m_chg_gte),
        ("dollar_6m_chg_gte", "dollar_6m_chg", ">=", rule.dollar_6m_chg_gte),
        ("fedfunds_6m_chg_gte", "fedfunds_6m_chg", ">=", rule.fedfunds_6m_chg_gte),
    ]
    checks: list[tuple[str, bool]] = []
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
    if not checks:
        return []
    hit = all(value for _label, value in checks) if rule.combine == "all" else any(value for _label, value in checks)
    return [label for label, value in checks if value] if hit else []


def run_case(
    conn,
    series,
    yields,
    macro,
    trade_dates,
    holdings,
    rule: MacroRiskRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    phase_rule = phase_rule_by_name(rule.phase_rule_name)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    guard_count = 0
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        target_pct, equity_return, _sleeves, _reasons = ensemble_state(
            conn,
            series,
            holdings,
            phase_rule,
            start_snapshot,
            start_exec,
            end_exec,
            capital / peak - 1.0,
        )
        hits = check_rule(macro_features(macro, start_snapshot), rule)
        guarded_pct = min(target_pct, rule.cap_pct) if hits else target_pct
        if hits:
            guard_count += 1
        def_return = us10y_duration_return(yields, start_exec, end_exec)
        equity_weight = guarded_pct / 100.0
        non_equity_return = cash_return(start_exec, end_exec) if equity_weight > 1.0 else def_return
        month_return = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
        capital *= 1.0 + month_return
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
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


def evaluate_rule(conn, series, yields, macro, trade_dates, holdings, rule: MacroRiskRule) -> dict[str, Any]:
    cases = [run_case(conn, series, yields, macro, trade_dates, holdings, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test FRED macro/credit-risk pre-month guards on scorecard+CSI phase ensembles.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_series": [f"FRED:{series_id}" for series_id in SERIES_IDS],
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "phase_rule_name",
            "cap_pct",
            "combine",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        macro = load_macro_series(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        results = []
        for rule in RULES:
            result = evaluate_rule(conn, series, yields, macro, trade_dates, holdings, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name:<36} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"guards={summary['median_guard_count']:5.1f}"
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

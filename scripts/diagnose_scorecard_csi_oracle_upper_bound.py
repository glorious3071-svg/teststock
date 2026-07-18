#!/usr/bin/env python3
"""Diagnose oracle upper bounds for the scorecard+CSI return engine.

This is intentionally *not* a production backtest.  It uses future monthly
returns to answer a feasibility question: if a perfect risk model could know the
next month's phase-ensemble loss in advance, would the current return engine be
able to satisfy the 4000w / -10% all-drift target?

Outputs avoid the `scorecard_csi_*_search.csv` naming convention so the normal
frontier summary does not treat these lookahead rules as investable candidates.
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

from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
ROWS_CSV = OUT_DIR / "scorecard_csi_crash_feature_rows.csv"
OUT_JSON = OUT_DIR / "scorecard_csi_oracle_upper_bound_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_oracle_upper_bound.csv"


@dataclass(frozen=True)
class OracleRule:
    name: str
    trigger_month_return_lte: float
    cap_pct: float
    defensive_mode: str = "us10y"
    trigger_equity_return_lte: float | None = None
    trigger_drawdown_lte: float | None = None


RULES = [
    OracleRule("oracle_avoid_negative_cap0", 0.0, 0.0),
    OracleRule("oracle_avoid_loss2_cap0", -0.02, 0.0),
    OracleRule("oracle_avoid_loss4_cap0", -0.04, 0.0),
    OracleRule("oracle_avoid_loss6_cap0", -0.06, 0.0),
    OracleRule("oracle_avoid_loss8_cap0", -0.08, 0.0),
    OracleRule("oracle_avoid_loss10_cap0", -0.10, 0.0),
    OracleRule("oracle_avoid_negative_cap40", 0.0, 40.0),
    OracleRule("oracle_avoid_loss4_cap40", -0.04, 40.0),
    OracleRule("oracle_avoid_loss8_cap40", -0.08, 40.0),
    OracleRule("oracle_avoid_loss4_cap60", -0.04, 60.0),
    OracleRule("oracle_avoid_loss8_cap60", -0.08, 60.0),
    OracleRule("oracle_equity_loss5_cap0", 1.0, 0.0, trigger_equity_return_lte=-0.05),
    OracleRule("oracle_equity_loss8_cap0", 1.0, 0.0, trigger_equity_return_lte=-0.08),
    OracleRule("oracle_portfolio_dd5_cap0", 1.0, 0.0, trigger_drawdown_lte=-0.05),
    OracleRule("oracle_loss4_cash_cap0", -0.04, 0.0, defensive_mode="cash"),
    OracleRule("oracle_loss8_cash_cap0", -0.08, 0.0, defensive_mode="cash"),
]


def load_rows() -> list[dict[str, Any]]:
    if not ROWS_CSV.exists():
        raise RuntimeError(f"missing {ROWS_CSV}; run scripts/audit_scorecard_csi_crash_features.py")
    rows: list[dict[str, Any]] = []
    with ROWS_CSV.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "phase_month_offset": int(raw["phase_month_offset"]),
                    "execution_lag_days": int(raw["execution_lag_days"]),
                    "snapshot": raw["snapshot"],
                    "target_equity_pct": float(raw["target_equity_pct"]),
                    "equity_return": float(raw["equity_return"]),
                    "defensive_return": float(raw["defensive_return"]),
                    "month_return": float(raw["month_return"]),
                    "portfolio_drawdown": float(raw["portfolio_drawdown"]),
                }
            )
    rows.sort(key=lambda item: (item["phase_month_offset"], item["execution_lag_days"], item["snapshot"]))
    return rows


def cash_return_proxy(row: dict[str, Any]) -> float:
    # Crash-feature rows do not store explicit cash return.  Use zero as the
    # conservative upper-bound cash proxy; the US10Y proxy stays in the row.
    return 0.0


def should_guard(row: dict[str, Any], rule: OracleRule) -> bool:
    if rule.trigger_equity_return_lte is not None:
        return row["equity_return"] <= rule.trigger_equity_return_lte
    if rule.trigger_drawdown_lte is not None:
        return row["portfolio_drawdown"] <= rule.trigger_drawdown_lte
    return row["month_return"] <= rule.trigger_month_return_lte


def guarded_month_return(row: dict[str, Any], rule: OracleRule) -> tuple[float, bool]:
    if not should_guard(row, rule):
        return row["month_return"], False
    target_pct = min(row["target_equity_pct"], rule.cap_pct)
    equity_weight = target_pct / 100.0
    defensive = cash_return_proxy(row) if rule.defensive_mode == "cash" else row["defensive_return"]
    return equity_weight * row["equity_return"] + (1.0 - equity_weight) * defensive, True


def run_case(rows: list[dict[str, Any]], rule: OracleRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [row for row in rows if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag]
    capital = INITIAL_CAPITAL
    curve = [capital]
    guard_count = 0
    avoided_loss_sum = 0.0
    for row in case_rows:
        month_return, guarded = guarded_month_return(row, rule)
        if guarded:
            guard_count += 1
            avoided_loss_sum += max(0.0, row["month_return"] - month_return)
        capital *= 1.0 + month_return
        if capital <= 0:
            capital = 1.0
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
        "avoided_loss_sum": avoided_loss_sum,
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


def evaluate_rule(rows: list[dict[str, Any]], rule: OracleRule) -> dict[str, Any]:
    cases = [run_case(rows, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
    summary = matrix_summary(cases)
    return {
        "rule": asdict(rule),
        "summary": summary,
        "cases": cases,
        "target_met": summary["pass_count"] == summary["count"],
    }


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Lookahead oracle upper-bound diagnostic for current scorecard+CSI return engine.",
        "not_investable": True,
        "lookahead_fields": ["month_return", "equity_return", "portfolio_drawdown"],
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_rows": str(ROWS_CSV),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "trigger_month_return_lte",
            "trigger_equity_return_lte",
            "trigger_drawdown_lte",
            "cap_pct",
            "defensive_mode",
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
    rows = load_rows()
    results = []
    for rule in RULES:
        result = evaluate_rule(rows, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name:<30} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}万 "
            f"median={summary['median_final_capital_wan']:9.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guards={summary['median_guard_count']:5.1f}"
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
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backtest walk-forward calendar/phase loss guards.

The oracle diagnostic says the current scorecard+CSI return engine can hit the
strict target only if ordinary negative months are avoided with very high recall.
This experiment tests a deliberately simple ex-ante prior: whether historical
loss rates by calendar month, rebalance phase, and execution lag are stable enough
to cap exposure before fragile timing windows.

The model trains only on snapshots from prior years. It does not use future
returns from the evaluated year.
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

from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
ROWS_CSV = OUT_DIR / "scorecard_csi_crash_feature_rows.csv"
OUT_JSON = OUT_DIR / "scorecard_csi_calendar_loss_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_calendar_loss_guard_search.csv"


@dataclass(frozen=True)
class CalendarGuardRule:
    name: str
    bucket: str
    label_threshold: float
    min_count: int
    loss_rate_gte: float
    avg_return_lte: float
    cap_pct: float
    cooldown_months: int = 0


def build_rules() -> list[CalendarGuardRule]:
    rules: list[CalendarGuardRule] = []
    # Full bucket grids are mostly inert: phase-only and phase/lag buckets rarely
    # trigger when trained walk-forward. Keep the focused set that can actually
    # express calendar fragility while retaining enough prior-year support.
    buckets = ["month", "month_phase", "month_lag"]
    for bucket in buckets:
        for label_threshold in [0.0, -0.01, -0.02]:
            label = "neg" if label_threshold == 0.0 else f"loss{abs(label_threshold) * 100:.0f}"
            for min_count in [8, 16, 32]:
                for loss_rate_gte in [0.45, 0.50]:
                    for cap_pct in [0.0, 20.0, 40.0, 60.0]:
                        rules.append(
                            CalendarGuardRule(
                                f"cal_{label}_{bucket}_n{min_count}_r{int(loss_rate_gte * 100)}_cap{int(cap_pct)}",
                                bucket,
                                label_threshold,
                                min_count,
                                loss_rate_gte,
                                -999.0,
                                cap_pct,
                            )
                        )
                for avg_return_lte in [-0.005, 0.0]:
                    for cap_pct in [0.0, 20.0, 40.0]:
                        rules.append(
                            CalendarGuardRule(
                                f"cal_{label}_{bucket}_n{min_count}_avg{int(avg_return_lte * 1000)}_cap{int(cap_pct)}",
                                bucket,
                                label_threshold,
                                min_count,
                                2.0,
                                avg_return_lte,
                                cap_pct,
                            )
                        )
    return rules


RULES = build_rules()


def load_rows() -> list[dict[str, Any]]:
    if not ROWS_CSV.exists():
        raise RuntimeError(f"missing feature rows: {ROWS_CSV}; run scripts/audit_scorecard_csi_crash_features.py first")
    rows: list[dict[str, Any]] = []
    with ROWS_CSV.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            snapshot = raw["snapshot"]
            rows.append(
                {
                    "phase_month_offset": int(raw["phase_month_offset"]),
                    "execution_lag_days": int(raw["execution_lag_days"]),
                    "snapshot": snapshot,
                    "year": int(snapshot[:4]),
                    "month": int(snapshot[5:7]),
                    "target_equity_pct": float(raw["target_equity_pct"]),
                    "equity_return": float(raw["equity_return"]),
                    "defensive_return": float(raw["defensive_return"]),
                    "month_return": float(raw["month_return"]),
                }
            )
    rows.sort(key=lambda item: (item["snapshot"], item["phase_month_offset"], item["execution_lag_days"]))
    return rows


def bucket_key(row: dict[str, Any], bucket: str) -> tuple[Any, ...]:
    if bucket == "month":
        return (row["month"],)
    if bucket == "phase":
        return (row["phase_month_offset"],)
    if bucket == "month_phase":
        return (row["month"], row["phase_month_offset"])
    if bucket == "month_lag":
        return (row["month"], row["execution_lag_days"])
    if bucket == "phase_lag":
        return (row["phase_month_offset"], row["execution_lag_days"])
    if bucket == "month_phase_lag":
        return (row["month"], row["phase_month_offset"], row["execution_lag_days"])
    raise ValueError(f"unknown bucket {bucket}")


def is_loss(row: dict[str, Any], threshold: float) -> bool:
    return float(row["month_return"]) <= threshold


def build_year_stats(rows: list[dict[str, Any]], rule: CalendarGuardRule) -> dict[int, dict[tuple[Any, ...], dict[str, float]]]:
    out: dict[int, dict[tuple[Any, ...], dict[str, float]]] = {}
    years = sorted({row["year"] for row in rows})
    for year in years:
        train = [row for row in rows if row["year"] < year]
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in train:
            grouped.setdefault(bucket_key(row, rule.bucket), []).append(row)
        stats: dict[tuple[Any, ...], dict[str, float]] = {}
        for key, items in grouped.items():
            count = len(items)
            losses = sum(1 for item in items if is_loss(item, rule.label_threshold))
            stats[key] = {
                "count": float(count),
                "loss_rate": losses / count if count else 0.0,
                "avg_return": statistics.mean(float(item["month_return"]) for item in items),
            }
        out[year] = stats
    return out


def should_guard(row: dict[str, Any], stats: dict[tuple[Any, ...], dict[str, float]], rule: CalendarGuardRule) -> tuple[bool, dict[str, float]]:
    item = stats.get(bucket_key(row, rule.bucket))
    if item is None or item["count"] < rule.min_count:
        return False, {"count": 0.0, "loss_rate": 0.0, "avg_return": 0.0}
    flagged = item["loss_rate"] >= rule.loss_rate_gte or item["avg_return"] <= rule.avg_return_lte
    return flagged, item


def run_case(rows: list[dict[str, Any]], year_stats: dict[int, dict[tuple[Any, ...], dict[str, float]]], rule: CalendarGuardRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [row for row in rows if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag]
    capital = INITIAL_CAPITAL
    curve = [capital]
    guard_count = 0
    loss_months = 0
    loss_guard_hits = 0
    cooldown = 0
    guarded_loss_return_sum = 0.0
    missed_loss_return_sum = 0.0

    for row in case_rows:
        stats = year_stats.get(row["year"], {})
        flagged, bucket_stats = should_guard(row, stats, rule)
        if flagged:
            cooldown = max(cooldown, rule.cooldown_months + 1)
        if is_loss(row, rule.label_threshold):
            loss_months += 1
            if flagged or cooldown > 0:
                loss_guard_hits += 1
                guarded_loss_return_sum += float(row["month_return"])
            else:
                missed_loss_return_sum += float(row["month_return"])
        target_pct = float(row["target_equity_pct"])
        if cooldown > 0:
            target_pct = min(target_pct, rule.cap_pct)
            guard_count += 1
            cooldown -= 1
        equity_weight = target_pct / 100.0
        month_return = equity_weight * float(row["equity_return"]) + (1.0 - equity_weight) * float(row["defensive_return"])
        capital = max(1.0, capital * (1.0 + month_return))
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
        "loss_months": loss_months,
        "loss_guard_hits": loss_guard_hits,
        "loss_recall": loss_guard_hits / loss_months if loss_months else 0.0,
        "guarded_loss_return_sum": guarded_loss_return_sum,
        "missed_loss_return_sum": missed_loss_return_sum,
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
        "median_loss_recall": statistics.median(item["loss_recall"] for item in items),
        "median_missed_loss_return_sum": statistics.median(item["missed_loss_return_sum"] for item in items),
    }


def evaluate_rule(rows: list[dict[str, Any]], rule: CalendarGuardRule) -> dict[str, Any]:
    year_stats = build_year_stats(rows, rule)
    cases = [run_case(rows, year_stats, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
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
        "objective": "Test walk-forward calendar/phase priors as high-recall ordinary-loss guards.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_rows": str(ROWS_CSV),
        "rule_count": len(RULES),
        "model_limits": "Uses only calendar month, validation phase, and execution lag buckets trained from prior years.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "bucket",
            "label_threshold",
            "min_count",
            "loss_rate_gte",
            "avg_return_lte",
            "cap_pct",
            "cooldown_months",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_count",
            "median_loss_recall",
            "median_missed_loss_return_sum",
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
            f"{rule.name[:78]:<78} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guards={summary['median_guard_count']:5.1f} "
            f"recall={summary['median_loss_recall'] * 100:5.1f}%"
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

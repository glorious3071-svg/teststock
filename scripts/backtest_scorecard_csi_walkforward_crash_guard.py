#!/usr/bin/env python3
"""Backtest walk-forward crash-risk guards for scorecard+CSI sleeves.

This experiment converts the crash-feature audit into an ex-ante risk model.
For each monthly snapshot it trains only on earlier snapshots, computes a simple
standardized bad-vs-ok feature score, and caps equity exposure when the current
month ranks in the high-risk tail of the historical training distribution.

The model is deliberately simple and auditable.  It is a feature-engineering
test, not a production allocation rule.
"""

from __future__ import annotations

import csv
import json
import math
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
OUT_JSON = OUT_DIR / "scorecard_csi_walkforward_crash_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_walkforward_crash_guard_search.csv"

FEATURES = [
    "cs300_ret_1m",
    "cs300_ret_3m",
    "cs300_ret_6m",
    "cs300_ret_12m",
    "cs300_vol_20d",
    "cs300_vol_60d",
    "cs300_vol_120d",
    "cs300_dd_20d",
    "cs300_dd_60d",
    "cs300_dd_120d",
    "cs300_dist_ma60",
    "cs300_dist_ma120",
    "cs300_dist_ma250",
    "pb",
    "pb_pct_3y",
    "pe_ttm",
    "pe_ttm_pct_3y",
    "turnover_rate",
    "turnover_rate_pct_3y",
    "turnover_20d_chg",
    "turnover_60d_chg",
    "margin_balance",
    "margin_20d_chg",
    "margin_60d_chg",
    "margin_120d_chg",
]


@dataclass(frozen=True)
class WalkForwardRule:
    name: str
    cap_pct: float
    flag_quantile: float
    min_train_months: int
    min_bad_count: int
    max_features: int
    feature_group: str = "all"
    cooldown_months: int = 0


RULES = [
    WalkForwardRule("wf_all_q80_cap40", 40.0, 0.80, 60, 20, 8),
    WalkForwardRule("wf_all_q85_cap40", 40.0, 0.85, 60, 20, 8),
    WalkForwardRule("wf_all_q90_cap40", 40.0, 0.90, 60, 20, 8),
    WalkForwardRule("wf_all_q85_cap60", 60.0, 0.85, 60, 20, 8),
    WalkForwardRule("wf_all_q90_cap60", 60.0, 0.90, 60, 20, 8),
    WalkForwardRule("wf_all_q92_cap60", 60.0, 0.92, 60, 20, 8),
    WalkForwardRule("wf_all_q85_cap0", 0.0, 0.85, 60, 20, 8),
    WalkForwardRule("wf_all_q90_cap0", 0.0, 0.90, 60, 20, 8),
    WalkForwardRule("wf_all_q85_cap80", 80.0, 0.85, 60, 20, 8),
    WalkForwardRule("wf_all_q90_cap80", 80.0, 0.90, 60, 20, 8),
    WalkForwardRule("wf_all_q85_cap40_cd1", 40.0, 0.85, 60, 20, 8, cooldown_months=1),
    WalkForwardRule("wf_all_q90_cap40_cd2", 40.0, 0.90, 60, 20, 8, cooldown_months=2),
    WalkForwardRule("wf_price_q85_cap40", 40.0, 0.85, 60, 20, 8, feature_group="price"),
    WalkForwardRule("wf_price_q90_cap60", 60.0, 0.90, 60, 20, 8, feature_group="price"),
    WalkForwardRule("wf_margin_q80_cap40", 40.0, 0.80, 60, 20, 5, feature_group="margin"),
    WalkForwardRule("wf_margin_q85_cap60", 60.0, 0.85, 60, 20, 5, feature_group="margin"),
    WalkForwardRule("wf_valuation_q80_cap40", 40.0, 0.80, 60, 20, 5, feature_group="valuation"),
    WalkForwardRule("wf_turnover_q80_cap40", 40.0, 0.80, 60, 20, 5, feature_group="turnover"),
]

_MODEL_CACHE: dict[tuple[str, int, int, int], dict[str, dict[str, Any] | None]] = {}


def parse_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def feature_names(rule: WalkForwardRule) -> list[str]:
    if rule.feature_group == "price":
        return [name for name in FEATURES if name.startswith("cs300_")]
    if rule.feature_group == "margin":
        return [name for name in FEATURES if name.startswith("margin_")]
    if rule.feature_group == "valuation":
        return [name for name in FEATURES if name.startswith(("pb", "pe_ttm"))]
    if rule.feature_group == "turnover":
        return [name for name in FEATURES if name.startswith("turnover")]
    return FEATURES


def load_rows() -> list[dict[str, Any]]:
    if not ROWS_CSV.exists():
        raise RuntimeError(f"missing crash feature rows: {ROWS_CSV}; run scripts/audit_scorecard_csi_crash_features.py first")
    rows: list[dict[str, Any]] = []
    with ROWS_CSV.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            item: dict[str, Any] = {
                "phase_month_offset": int(raw["phase_month_offset"]),
                "execution_lag_days": int(raw["execution_lag_days"]),
                "snapshot": raw["snapshot"],
                "target_equity_pct": float(raw["target_equity_pct"]),
                "equity_return": float(raw["equity_return"]),
                "defensive_return": float(raw["defensive_return"]),
                "large_loss": str(raw["large_loss"]).lower() == "true",
            }
            for feature in FEATURES:
                item[feature] = parse_float(raw.get(feature))
            rows.append(item)
    rows.sort(key=lambda item: (item["snapshot"], item["phase_month_offset"], item["execution_lag_days"]))
    return rows


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def build_model(train_rows: list[dict[str, Any]], names: list[str], max_features: int, min_bad_count: int) -> dict[str, Any] | None:
    bad_rows = [row for row in train_rows if row["large_loss"]]
    if len(bad_rows) < min_bad_count:
        return None
    specs = []
    for name in names:
        values = [row[name] for row in train_rows if row[name] is not None]
        bad_values = [row[name] for row in bad_rows if row[name] is not None]
        ok_values = [row[name] for row in train_rows if not row["large_loss"] and row[name] is not None]
        if len(values) < 80 or len(bad_values) < 10 or len(ok_values) < 40:
            continue
        center = median(values)
        mad = median([abs(value - center) for value in values])
        scale = max(mad * 1.4826, statistics.pstdev(values), 1e-9)
        bad_center = median(bad_values)
        ok_center = median(ok_values)
        weight = (bad_center - ok_center) / scale
        if abs(weight) < 0.05:
            continue
        specs.append({"feature": name, "center": center, "scale": scale, "weight": weight, "strength": abs(weight)})
    specs.sort(key=lambda item: item["strength"], reverse=True)
    specs = specs[:max_features]
    if not specs:
        return None
    scores = [score_row(row, specs) for row in train_rows]
    return {"features": specs, "scores": scores}


def score_row(row: dict[str, Any], specs: list[dict[str, Any]]) -> float:
    score = 0.0
    used = 0
    for spec in specs:
        value = row.get(spec["feature"])
        if value is None:
            continue
        z = max(-5.0, min(5.0, (float(value) - spec["center"]) / spec["scale"]))
        score += spec["weight"] * z
        used += 1
    return score / math.sqrt(used) if used else 0.0


def quantile(values: list[float], q: float) -> float:
    clean = sorted(values)
    if not clean:
        return math.inf
    idx = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * q))))
    return clean[idx]


def train_models_by_snapshot(rows: list[dict[str, Any]], rule: WalkForwardRule) -> dict[str, dict[str, Any] | None]:
    names = feature_names(rule)
    snapshots = sorted({row["snapshot"] for row in rows})
    years = sorted({int(snapshot[:4]) for snapshot in snapshots})
    by_year: dict[int, dict[str, Any] | None] = {}
    for year in years:
        train = [row for row in rows if int(row["snapshot"][:4]) < year]
        if len({row["snapshot"] for row in train}) < rule.min_train_months:
            by_year[year] = None
            continue
        model = build_model(train, names, rule.max_features, rule.min_bad_count)
        if model is not None:
            model["threshold"] = quantile(model["scores"], rule.flag_quantile)
            model["train_rows"] = len(train)
            model["train_bad_count"] = sum(1 for row in train if row["large_loss"])
        by_year[year] = model
    models: dict[str, dict[str, Any] | None] = {}
    for snapshot in snapshots:
        models[snapshot] = by_year[int(snapshot[:4])]
    return models


def run_case(rows: list[dict[str, Any]], models: dict[str, dict[str, Any] | None], rule: WalkForwardRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [
        row
        for row in rows
        if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag
    ]
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    guard_count = 0
    score_values: list[float] = []
    cooldown = 0
    for row in case_rows:
        target_pct = float(row["target_equity_pct"])
        guarded_target = target_pct
        model = models.get(row["snapshot"])
        risk_score = 0.0
        if model is not None:
            risk_score = score_row(row, model["features"])
            if risk_score >= float(model["threshold"]):
                cooldown = max(cooldown, rule.cooldown_months + 1)
        if cooldown > 0:
            guarded_target = min(guarded_target, rule.cap_pct)
            guard_count += 1
            cooldown -= 1
        equity_weight = guarded_target / 100.0
        month_return = equity_weight * float(row["equity_return"]) + (1.0 - equity_weight) * float(row["defensive_return"])
        capital *= 1.0 + month_return
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        score_values.append(risk_score)
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
        "median_score": statistics.median(score_values) if score_values else 0.0,
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
        "median_score": statistics.median(item["median_score"] for item in items),
    }


def evaluate_rule(rows: list[dict[str, Any]], rule: WalkForwardRule) -> dict[str, Any]:
    cache_key = (rule.feature_group, rule.min_train_months, rule.min_bad_count, rule.max_features)
    if cache_key not in _MODEL_CACHE:
        _MODEL_CACHE[cache_key] = train_models_by_snapshot(rows, rule)
    models = _MODEL_CACHE[cache_key]
    cases = [run_case(rows, models, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
    summary = matrix_summary(cases)
    trained_models = [model for model in models.values() if model is not None]
    model_summary = {
        "trained_snapshot_count": len(trained_models),
        "median_train_rows": statistics.median(model["train_rows"] for model in trained_models) if trained_models else 0,
        "median_train_bad_count": statistics.median(model["train_bad_count"] for model in trained_models) if trained_models else 0,
        "latest_features": [item["feature"] for item in trained_models[-1]["features"]] if trained_models else [],
    }
    return {
        "rule": asdict(rule),
        "summary": summary,
        "model_summary": model_summary,
        "cases": cases,
        "target_met": summary["pass_count"] == summary["count"],
    }


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test walk-forward crash-risk model caps on scorecard+CSI phase ensemble rows.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_rows": str(ROWS_CSV),
        "model_limits": "Linear bad-vs-ok standardized feature score trained only on prior snapshots; no option execution or live liquidity model.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "feature_group",
            "cap_pct",
            "flag_quantile",
            "min_train_months",
            "min_bad_count",
            "max_features",
            "cooldown_months",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_count",
            "trained_snapshot_count",
            "median_train_bad_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"], **item["model_summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    rows = load_rows()
    results = []
    for rule in RULES:
        result = evaluate_rule(rows, rule)
        results.append(result)
        summary = result["summary"]
        model_summary = result["model_summary"]
        print(
            f"{rule.name:<28} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guards={summary['median_guard_count']:5.1f} "
            f"trained={model_summary['trained_snapshot_count']:3}"
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

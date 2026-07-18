#!/usr/bin/env python3
"""Backtest boosted walk-forward loss guards for scorecard+CSI sleeves.

Linear and single-layer stump guards did not capture enough ordinary negative
months.  This experiment keeps the same no-lookahead structure but trains a
small AdaBoost-style ensemble of one-feature threshold stumps using only prior
years.  The model remains auditable: every risk score is a weighted sum of
observable threshold rules.
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_external_feature_guard import load_rows as load_enriched_rows
from scripts.backtest_scorecard_csi_external_feature_guard import feature_names as external_feature_names
from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_walkforward_loss_guard import FEATURES as LOCAL_FEATURES

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_boosted_loss_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_boosted_loss_guard_search.csv"

QUANTILE_POINTS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
_MODEL_CACHE: dict[tuple[str, float, int, int, int, int], dict[str, dict[str, Any] | None]] = {}


@dataclass(frozen=True)
class BoostedLossRule:
    name: str
    label_threshold: float
    feature_group: str
    cap_pct: float
    flag_quantile: float
    max_estimators: int
    max_pool: int = 80
    min_train_months: int = 60
    min_loss_count: int = 120
    cooldown_months: int = 0


def build_rules() -> list[BoostedLossRule]:
    rules: list[BoostedLossRule] = []
    for group in ["local", "external", "combined", "risk_market"]:
        min_loss = 80 if group in {"external", "risk_market"} else 120
        for label_threshold in [0.0, -0.005, -0.01, -0.02]:
            suffix = "neg" if label_threshold == 0.0 else f"loss{abs(label_threshold) * 100:.1f}".replace(".", "p")
            for max_estimators in [12, 24]:
                for flag_quantile in [0.45, 0.55, 0.65, 0.75]:
                    q_name = int(round(flag_quantile * 100))
                    for cap_pct in [0.0, 20.0, 40.0, 60.0]:
                        rules.append(
                            BoostedLossRule(
                                f"boost_{suffix}_{group}_e{max_estimators}_q{q_name}_cap{int(cap_pct)}",
                                label_threshold,
                                group,
                                cap_pct,
                                flag_quantile,
                                max_estimators,
                                min_loss_count=min_loss,
                            )
                        )
    for group in ["combined", "risk_market"]:
        for label_threshold in [0.0, -0.01]:
            suffix = "neg" if label_threshold == 0.0 else "loss1p0"
            for max_estimators in [12, 24]:
                for flag_quantile in [0.55, 0.70]:
                    q_name = int(round(flag_quantile * 100))
                    for cooldown_months in [1, 2]:
                        rules.append(
                            BoostedLossRule(
                                f"boost_{suffix}_{group}_e{max_estimators}_q{q_name}_cap40_cd{cooldown_months}",
                                label_threshold,
                                group,
                                40.0,
                                flag_quantile,
                                max_estimators,
                                cooldown_months=cooldown_months,
                            )
                        )
    return rules


RULES = build_rules()


def is_loss(row: dict[str, Any], threshold: float) -> bool:
    return float(row["month_return"]) <= threshold


def quantile(values: list[float], q: float) -> float:
    clean = sorted(values)
    if not clean:
        return math.inf
    idx = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * q))))
    return clean[idx]


def feature_names(group: str) -> list[str]:
    if group == "local":
        return LOCAL_FEATURES
    if group == "combined":
        return external_feature_names("combined")
    return external_feature_names(group)


def valid_feature_values(rows: list[dict[str, Any]], name: str) -> list[float]:
    out = []
    for row in rows:
        value = row.get(name)
        if value is not None and math.isfinite(float(value)):
            out.append(float(value))
    return out


def stump_prediction(row: dict[str, Any], stump: dict[str, Any]) -> int:
    value = row.get(stump["feature"])
    if value is None:
        return -1 if stump["missing_ok"] else 1
    condition = float(value) >= stump["threshold"] if stump["direction"] == "gte" else float(value) <= stump["threshold"]
    pred = 1 if condition else -1
    return pred if stump["condition_is_loss"] else -pred


def initial_weights(labels: list[int]) -> list[float]:
    pos = sum(1 for label in labels if label == 1)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return [1.0 / len(labels)] * len(labels)
    return [(0.5 / pos if label == 1 else 0.5 / neg) for label in labels]


def candidate_pool(
    train_rows: list[dict[str, Any]],
    labels: list[int],
    names: list[str],
    weights: list[float],
    max_pool: int,
) -> list[dict[str, Any]]:
    labels_arr = np.asarray(labels, dtype=np.int8)
    weights_arr = np.asarray(weights, dtype=np.float64)
    n_rows = len(train_rows)
    candidates = []
    for name in names:
        values = np.asarray(
            [
                float(row[name])
                if row.get(name) is not None and math.isfinite(float(row[name]))
                else np.nan
                for row in train_rows
            ],
            dtype=np.float64,
        )
        valid = np.isfinite(values)
        valid_count = int(valid.sum())
        if valid_count < 100:
            continue
        thresholds = sorted({float(np.quantile(values[valid], q)) for q in QUANTILE_POINTS})
        coverage = valid_count / n_rows
        for threshold in thresholds:
            for direction in ["gte", "lte"]:
                condition = (values >= threshold) if direction == "gte" else (values <= threshold)
                condition = np.where(valid, condition, False)
                true_count = int(condition.sum())
                split_share = true_count / n_rows
                if split_share < 0.05 or split_share > 0.95:
                    continue
                for condition_is_loss in [True, False]:
                    if condition_is_loss:
                        predictions = np.where(valid, np.where(condition, 1, -1), 1).astype(np.int8)
                    else:
                        predictions = np.where(valid, np.where(condition, -1, 1), 1).astype(np.int8)
                    error = float(weights_arr[predictions != labels_arr].sum())
                    edge = abs(0.5 - error) * coverage
                    if edge <= 0.005:
                        continue
                    candidates.append(
                        {
                            "feature": name,
                            "threshold": threshold,
                            "direction": direction,
                            "condition_is_loss": condition_is_loss,
                            "missing_ok": False,
                            "initial_error": error,
                            "edge": edge,
                            "_predictions": predictions,
                        }
                    )
    candidates.sort(key=lambda item: item["edge"], reverse=True)
    return candidates[:max_pool]


def fit_boosted_model(
    train_rows: list[dict[str, Any]],
    names: list[str],
    label_threshold: float,
    max_estimators: int,
    max_pool: int,
    min_loss_count: int,
) -> dict[str, Any] | None:
    labels = [1 if is_loss(row, label_threshold) else -1 for row in train_rows]
    loss_count = sum(1 for label in labels if label == 1)
    ok_count = len(labels) - loss_count
    if loss_count < min_loss_count or ok_count < min_loss_count:
        return None
    labels_arr = np.asarray(labels, dtype=np.int8)
    weights_arr = np.asarray(initial_weights(labels), dtype=np.float64)
    weights = weights_arr.tolist()
    pool = candidate_pool(train_rows, labels, names, weights, max_pool)
    if not pool:
        return None

    stumps: list[dict[str, Any]] = []
    available = list(pool)
    shrinkage = 0.65
    for _idx in range(max_estimators):
        best = None
        best_idx = -1
        best_error = 1.0
        for idx, candidate in enumerate(available):
            predictions = candidate["_predictions"]
            error = float(weights_arr[predictions != labels_arr].sum())
            if error < best_error:
                best = candidate
                best_idx = idx
                best_error = error
        if best is None or best_error >= 0.495:
            break
        best_error = min(0.495, max(0.005, best_error))
        alpha = shrinkage * 0.5 * math.log((1.0 - best_error) / best_error)
        stump = dict(best)
        predictions = stump.pop("_predictions")
        stump["alpha"] = alpha
        stump["weighted_error"] = best_error
        stumps.append(stump)
        weights_arr = weights_arr * np.exp(-alpha * labels_arr * predictions)
        denom = float(weights_arr.sum())
        if denom <= 0:
            break
        weights_arr = weights_arr / denom
        del available[best_idx]
        if not available:
            break
    if not stumps:
        return None
    scores = [score_row(row, stumps) for row in train_rows]
    return {
        "stumps": stumps,
        "scores": scores,
        "train_rows": len(train_rows),
        "train_loss_count": loss_count,
    }


def score_row(row: dict[str, Any], stumps: list[dict[str, Any]]) -> float:
    return sum(float(stump["alpha"]) * stump_prediction(row, stump) for stump in stumps)


def train_models_by_snapshot(rows: list[dict[str, Any]], rule: BoostedLossRule) -> dict[str, dict[str, Any] | None]:
    names = feature_names(rule.feature_group)
    snapshots = sorted({row["snapshot"] for row in rows})
    years = sorted({int(snapshot[:4]) for snapshot in snapshots})
    by_year: dict[int, dict[str, Any] | None] = {}
    for year in years:
        train = [row for row in rows if int(row["snapshot"][:4]) < year]
        if len({row["snapshot"] for row in train}) < rule.min_train_months:
            by_year[year] = None
            continue
        by_year[year] = fit_boosted_model(
            train,
            names,
            rule.label_threshold,
            rule.max_estimators,
            rule.max_pool,
            rule.min_loss_count,
        )
    return {snapshot: by_year[int(snapshot[:4])] for snapshot in snapshots}


def models_with_thresholds(rows: list[dict[str, Any]], rule: BoostedLossRule) -> dict[str, dict[str, Any] | None]:
    cache_key = (
        rule.feature_group,
        rule.label_threshold,
        rule.max_estimators,
        rule.max_pool,
        rule.min_train_months,
        rule.min_loss_count,
    )
    if cache_key not in _MODEL_CACHE:
        _MODEL_CACHE[cache_key] = train_models_by_snapshot(rows, rule)
    return {
        snapshot: {**model, "threshold": quantile(model["scores"], rule.flag_quantile)} if model is not None else None
        for snapshot, model in _MODEL_CACHE[cache_key].items()
    }


def run_case(rows: list[dict[str, Any]], models: dict[str, dict[str, Any] | None], rule: BoostedLossRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [row for row in rows if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag]
    capital = INITIAL_CAPITAL
    curve = [capital]
    guard_count = 0
    loss_guard_hits = 0
    loss_months = 0
    cooldown = 0
    for row in case_rows:
        guarded_target = float(row["target_equity_pct"])
        model = models.get(row["snapshot"])
        flagged = False
        if model is not None:
            risk_score = score_row(row, model["stumps"])
            flagged = risk_score >= float(model["threshold"])
            if flagged:
                cooldown = max(cooldown, rule.cooldown_months + 1)
        if is_loss(row, rule.label_threshold):
            loss_months += 1
            loss_guard_hits += int(flagged)
        if cooldown > 0:
            guarded_target = min(guarded_target, rule.cap_pct)
            guard_count += 1
            cooldown -= 1
        equity_weight = guarded_target / 100.0
        month_return = equity_weight * float(row["equity_return"]) + (1.0 - equity_weight) * float(row["defensive_return"])
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
        "loss_months": loss_months,
        "loss_guard_hits": loss_guard_hits,
        "loss_recall": loss_guard_hits / loss_months if loss_months else 0.0,
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
    }


def evaluate_rule(rows: list[dict[str, Any]], rule: BoostedLossRule) -> dict[str, Any]:
    models = models_with_thresholds(rows, rule)
    cases = [run_case(rows, models, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
    summary = matrix_summary(cases)
    trained_models = [model for model in models.values() if model is not None]
    model_summary = {
        "trained_snapshot_count": len(trained_models),
        "median_train_rows": statistics.median(model["train_rows"] for model in trained_models) if trained_models else 0,
        "median_train_loss_count": statistics.median(model["train_loss_count"] for model in trained_models) if trained_models else 0,
        "latest_stumps": [
            {
                "feature": item["feature"],
                "direction": item["direction"],
                "threshold": item["threshold"],
                "condition_is_loss": item["condition_is_loss"],
                "alpha": item["alpha"],
            }
            for item in (trained_models[-1]["stumps"][:8] if trained_models else [])
        ],
    }
    return {
        "rule": asdict(rule),
        "summary": summary,
        "model_summary": model_summary,
        "cases": cases,
        "target_met": summary["pass_count"] == summary["count"],
    }


def label_balance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for threshold in [0.0, -0.005, -0.01, -0.02, -0.04, -0.08]:
        count = sum(1 for row in rows if is_loss(row, threshold))
        out.append({"label_threshold": threshold, "count": count, "pct": count / len(rows) if rows else 0.0})
    return out


def write_outputs(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test boosted walk-forward ordinary-loss guards on scorecard+CSI phase ensemble rows.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "label_balance": label_balance(rows),
        "model_limits": "Small AdaBoost-style ensemble of one-feature threshold stumps trained only on prior-year snapshots; no future labels are used for live scores.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "feature_group",
            "label_threshold",
            "cap_pct",
            "flag_quantile",
            "max_estimators",
            "max_pool",
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
            "trained_snapshot_count",
            "median_train_loss_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"], **item["model_summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    rows = load_enriched_rows()
    print(
        "label_balance "
        + " ".join(
            f"{item['label_threshold']:g}:{item['count']}/{len(rows)}({item['pct'] * 100:.1f}%)"
            for item in label_balance(rows)
        )
    )
    results = []
    for rule in RULES:
        result = evaluate_rule(rows, rule)
        results.append(result)
        summary = result["summary"]
        model_summary = result["model_summary"]
        print(
            f"{rule.name:<48} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guards={summary['median_guard_count']:5.1f} "
            f"recall={summary['median_loss_recall'] * 100:5.1f}% "
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
    write_outputs(rows, results)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

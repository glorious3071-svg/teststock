#!/usr/bin/env python3
"""Backtest walk-forward stump-ensemble loss guards for scorecard+CSI sleeves.

The linear negative-month guard tests whether existing features have a simple
monotonic risk signal.  This script tests the next level up: an auditable
nonlinear ensemble of one-feature threshold stumps trained only on prior-year
snapshots.  It is still a diagnostic allocation layer, not a production signal.
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
OUT_JSON = OUT_DIR / "scorecard_csi_stump_loss_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_stump_loss_guard_search.csv"

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
class StumpRule:
    name: str
    label_threshold: float
    feature_group: str
    max_stumps: int
    cap_pct: float
    flag_quantile: float
    min_train_months: int = 60
    min_loss_count: int = 120
    cooldown_months: int = 0


def build_rules() -> list[StumpRule]:
    rules: list[StumpRule] = []
    for label_threshold in [0.0, -0.005, -0.01, -0.02]:
        suffix = "neg" if label_threshold == 0.0 else f"loss{abs(label_threshold) * 100:.1f}".replace(".", "p")
        for max_stumps in [6, 12, 24]:
            for flag_quantile in [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
                q_name = int(round(flag_quantile * 100))
                for cap_pct in [0.0, 20.0, 40.0, 60.0]:
                    rules.append(
                        StumpRule(
                            f"stump_{suffix}_all_k{max_stumps}_q{q_name}_cap{int(cap_pct)}",
                            label_threshold,
                            "all",
                            max_stumps,
                            cap_pct,
                            flag_quantile,
                        )
                    )
    for feature_group in ["price", "margin", "valuation", "turnover"]:
        for label_threshold in [0.0, -0.01]:
            suffix = "neg" if label_threshold == 0.0 else "loss1p0"
            for flag_quantile in [0.35, 0.50, 0.65]:
                q_name = int(round(flag_quantile * 100))
                for cap_pct in [0.0, 30.0, 60.0]:
                    rules.append(
                        StumpRule(
                            f"stump_{suffix}_{feature_group}_k12_q{q_name}_cap{int(cap_pct)}",
                            label_threshold,
                            feature_group,
                            12,
                            cap_pct,
                            flag_quantile,
                            min_loss_count=80,
                        )
                    )
    for label_threshold in [0.0, -0.01]:
        suffix = "neg" if label_threshold == 0.0 else "loss1p0"
        for flag_quantile in [0.40, 0.55, 0.70]:
            q_name = int(round(flag_quantile * 100))
            for cooldown_months in [1, 2]:
                rules.append(
                    StumpRule(
                        f"stump_{suffix}_all_k12_q{q_name}_cap40_cd{cooldown_months}",
                        label_threshold,
                        "all",
                        12,
                        40.0,
                        flag_quantile,
                        cooldown_months=cooldown_months,
                    )
                )
    return rules


RULES = build_rules()
MAX_STUMPS = max(rule.max_stumps for rule in RULES)
QUANTILE_POINTS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
_MODEL_CACHE: dict[tuple[str, float, int, int], dict[str, dict[str, Any] | None]] = {}
_THRESHOLD_MODEL_CACHE: dict[tuple[str, float, int, int, int, float], dict[str, dict[str, Any] | None]] = {}
_TRAIN_ROWS_BY_YEAR: dict[int, list[dict[str, Any]]] = {}


def parse_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def feature_names(group: str) -> list[str]:
    if group == "price":
        return [name for name in FEATURES if name.startswith("cs300_")]
    if group == "margin":
        return [name for name in FEATURES if name.startswith("margin_")]
    if group == "valuation":
        return [name for name in FEATURES if name.startswith(("pb", "pe_ttm"))]
    if group == "turnover":
        return [name for name in FEATURES if name.startswith("turnover")]
    return FEATURES


def load_rows() -> list[dict[str, Any]]:
    if not ROWS_CSV.exists():
        raise RuntimeError(f"missing feature rows: {ROWS_CSV}; run scripts/audit_scorecard_csi_crash_features.py first")
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
                "month_return": float(raw["month_return"]),
            }
            for feature in FEATURES:
                item[feature] = parse_float(raw.get(feature))
            rows.append(item)
    rows.sort(key=lambda item: (item["snapshot"], item["phase_month_offset"], item["execution_lag_days"]))
    return rows


def is_loss(row: dict[str, Any], threshold: float) -> bool:
    return float(row["month_return"]) <= threshold


def quantile(values: list[float], q: float) -> float:
    clean = sorted(values)
    if not clean:
        return math.inf
    idx = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * q))))
    return clean[idx]


def entropy(pos: int, total: int) -> float:
    if total <= 0 or pos <= 0 or pos >= total:
        return 0.0
    p = pos / total
    return -(p * math.log(p) + (1.0 - p) * math.log(1.0 - p))


def logit_rate(pos: int, total: int, prior: float) -> float:
    p = (pos + prior * 8.0) / (total + 8.0)
    p = min(0.995, max(0.005, p))
    return math.log(p / (1.0 - p))


def candidate_thresholds(values: list[float]) -> list[float]:
    if len(values) < 80:
        return []
    return sorted({quantile(values, q) for q in QUANTILE_POINTS})


def train_model(
    train_rows: list[dict[str, Any]],
    names: list[str],
    label_threshold: float,
    min_loss_count: int,
) -> dict[str, Any] | None:
    labels = [is_loss(row, label_threshold) for row in train_rows]
    loss_count = sum(labels)
    ok_count = len(labels) - loss_count
    if loss_count < min_loss_count or ok_count < min_loss_count:
        return None
    prior = loss_count / len(labels)
    base_entropy = entropy(loss_count, len(labels))
    candidates = []
    for name in names:
        values = [row[name] for row in train_rows if row[name] is not None]
        for threshold in candidate_thresholds(values):
            for direction in ["gte", "lte"]:
                true_loss = true_total = false_loss = false_total = 0
                for row, label in zip(train_rows, labels):
                    value = row.get(name)
                    if value is None:
                        continue
                    condition = value >= threshold if direction == "gte" else value <= threshold
                    if condition:
                        true_total += 1
                        true_loss += int(label)
                    else:
                        false_total += 1
                        false_loss += int(label)
                if true_total < 40 or false_total < 40:
                    continue
                weighted_entropy = (
                    true_total / len(labels) * entropy(true_loss, true_total)
                    + false_total / len(labels) * entropy(false_loss, false_total)
                )
                info_gain = base_entropy - weighted_entropy
                true_rate = true_loss / true_total
                false_rate = false_loss / false_total
                lift = abs(true_rate - false_rate)
                if info_gain <= 0 or lift < 0.025:
                    continue
                candidates.append(
                    {
                        "feature": name,
                        "threshold": threshold,
                        "direction": direction,
                        "info_gain": info_gain,
                        "lift": lift,
                        "true_logit": logit_rate(true_loss, true_total, prior),
                        "false_logit": logit_rate(false_loss, false_total, prior),
                        "true_rate": true_rate,
                        "false_rate": false_rate,
                        "true_total": true_total,
                    }
                )
    candidates.sort(key=lambda item: (item["info_gain"], item["lift"]), reverse=True)

    selected = []
    used_features: dict[str, int] = {}
    for item in candidates:
        if used_features.get(item["feature"], 0) >= 3:
            continue
        selected.append(item)
        used_features[item["feature"]] = used_features.get(item["feature"], 0) + 1
        if len(selected) >= MAX_STUMPS:
            break
    if not selected:
        return None
    return {"stumps": selected, "prior": prior, "train_rows": len(train_rows), "train_loss_count": loss_count}


def score_row(row: dict[str, Any], stumps: list[dict[str, Any]], prior: float) -> float:
    if not stumps:
        return math.log(prior / (1.0 - prior))
    total = 0.0
    used = 0
    for stump in stumps:
        value = row.get(stump["feature"])
        if value is None:
            continue
        condition = value >= stump["threshold"] if stump["direction"] == "gte" else value <= stump["threshold"]
        total += stump["true_logit"] if condition else stump["false_logit"]
        used += 1
    if not used:
        return math.log(prior / (1.0 - prior))
    return total / math.sqrt(used)


def train_models_by_snapshot(rows: list[dict[str, Any]], rule: StumpRule) -> dict[str, dict[str, Any] | None]:
    names = feature_names(rule.feature_group)
    snapshots = sorted({row["snapshot"] for row in rows})
    years = sorted({int(snapshot[:4]) for snapshot in snapshots})
    by_year: dict[int, dict[str, Any] | None] = {}
    for year in years:
        train = train_rows_before_year(rows, year)
        if len({row["snapshot"] for row in train}) < rule.min_train_months:
            by_year[year] = None
            continue
        by_year[year] = train_model(train, names, rule.label_threshold, rule.min_loss_count)
    return {snapshot: by_year[int(snapshot[:4])] for snapshot in snapshots}


def train_rows_before_year(rows: list[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    if year not in _TRAIN_ROWS_BY_YEAR:
        _TRAIN_ROWS_BY_YEAR[year] = [row for row in rows if int(row["snapshot"][:4]) < year]
    return _TRAIN_ROWS_BY_YEAR[year]


def model_for_rule(base_model: dict[str, Any] | None, rule: StumpRule, train_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if base_model is None:
        return None
    stumps = base_model["stumps"][: rule.max_stumps]
    scores = [score_row(row, stumps, base_model["prior"]) for row in train_rows]
    return {
        **base_model,
        "stumps": stumps,
        "threshold": quantile(scores, rule.flag_quantile),
    }


def models_with_thresholds(rows: list[dict[str, Any]], rule: StumpRule) -> dict[str, dict[str, Any] | None]:
    cache_key = (rule.feature_group, rule.label_threshold, rule.min_train_months, rule.min_loss_count)
    if cache_key not in _MODEL_CACHE:
        _MODEL_CACHE[cache_key] = train_models_by_snapshot(rows, rule)
    threshold_key = (*cache_key, rule.max_stumps, rule.flag_quantile)
    if threshold_key in _THRESHOLD_MODEL_CACHE:
        return _THRESHOLD_MODEL_CACHE[threshold_key]
    out: dict[str, dict[str, Any] | None] = {}
    for snapshot, base_model in _MODEL_CACHE[cache_key].items():
        year = int(snapshot[:4])
        train = train_rows_before_year(rows, year)
        out[snapshot] = model_for_rule(base_model, rule, train) if train else None
    _THRESHOLD_MODEL_CACHE[threshold_key] = out
    return out


def run_case(rows: list[dict[str, Any]], models: dict[str, dict[str, Any] | None], rule: StumpRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [
        row
        for row in rows
        if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag
    ]
    capital = INITIAL_CAPITAL
    curve = [capital]
    guard_count = 0
    loss_guard_hits = 0
    loss_months = 0
    cooldown = 0
    for row in case_rows:
        target_pct = float(row["target_equity_pct"])
        guarded_target = target_pct
        model = models.get(row["snapshot"])
        flagged = False
        if model is not None:
            risk_score = score_row(row, model["stumps"], model["prior"])
            flagged = risk_score >= float(model["threshold"])
            if flagged:
                cooldown = max(cooldown, rule.cooldown_months + 1)
        if is_loss(row, rule.label_threshold):
            loss_months += 1
            if flagged:
                loss_guard_hits += 1
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


def evaluate_rule(rows: list[dict[str, Any]], rule: StumpRule) -> dict[str, Any]:
    models = models_with_thresholds(rows, rule)
    cases = [run_case(rows, models, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
    summary = matrix_summary(cases)
    trained_models = [model for model in models.values() if model is not None]
    model_summary = {
        "trained_snapshot_count": len(trained_models),
        "median_train_rows": statistics.median(model["train_rows"] for model in trained_models) if trained_models else 0,
        "median_train_loss_count": statistics.median(model["train_loss_count"] for model in trained_models) if trained_models else 0,
        "latest_stumps": [
            f"{item['feature']} {item['direction']} {item['threshold']:.6g}"
            for item in (trained_models[-1]["stumps"] if trained_models else [])
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
    balances = []
    for threshold in [0.0, -0.005, -0.01, -0.02, -0.04, -0.08]:
        count = sum(1 for row in rows if is_loss(row, threshold))
        balances.append({"label_threshold": threshold, "count": count, "pct": count / len(rows) if rows else 0.0})
    return balances


def write_outputs(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test walk-forward nonlinear stump-ensemble loss guards on scorecard+CSI phase ensemble rows.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_rows": str(ROWS_CSV),
        "label_balance": label_balance(rows),
        "model_limits": "Prior-year-only threshold-stump ensemble; no future returns in signal construction.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "feature_group",
            "label_threshold",
            "max_stumps",
            "cap_pct",
            "flag_quantile",
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
    rows = load_rows()
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
            f"{rule.name:<42} pass={summary['pass_count']:>2}/{summary['count']} "
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

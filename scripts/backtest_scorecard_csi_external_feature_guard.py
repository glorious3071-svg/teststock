#!/usr/bin/env python3
"""Backtest external-feature walk-forward loss guards for scorecard+CSI sleeves.

This experiment augments the existing scorecard+CSI feature rows with observable
external market, option-strategy, volatility, and macro features cached in
external_asset_daily.  It tests whether full-window external signals improve the
ordinary negative-month risk model enough to move the strict all-phase target.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_midyear_risk import END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_walkforward_loss_guard import FEATURES as LOCAL_FEATURES

OUT_DIR = ROOT / "data" / "backtests"
ROWS_CSV = OUT_DIR / "scorecard_csi_crash_feature_rows.csv"
OUT_JSON = OUT_DIR / "scorecard_csi_external_feature_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_external_feature_guard_search.csv"

PRICE_SYMBOLS = [
    "SPY",
    "QQQ",
    "TLT",
    "IEF",
    "SHY",
    "GLD",
    "PPUT",
    "PUT",
    "BXM",
    "BXMD",
    "CLLZ",
    "EFA",
    "EEM",
    "IWM",
    "XLK",
    "XLE",
    "XLU",
    "HYG",
    "LQD",
    "AGG",
    "TIP",
]
VOL_SYMBOLS = ["^VIX"]
FRED_SYMBOLS = ["FRED:NFCI", "FRED:ANFCI", "FRED:DFF", "FRED:DGS10", "FRED:DGS2", "FRED:DTWEXBGS"]
EXTERNAL_SYMBOLS = PRICE_SYMBOLS + VOL_SYMBOLS + FRED_SYMBOLS


@dataclass(frozen=True)
class ExternalFeatureRule:
    name: str
    label_threshold: float
    feature_group: str
    cap_pct: float
    flag_quantile: float
    min_train_months: int = 60
    min_loss_count: int = 120
    max_features: int = 14
    cooldown_months: int = 0


def build_rules() -> list[ExternalFeatureRule]:
    rules: list[ExternalFeatureRule] = []
    for group in ["external", "combined", "risk_market", "option", "macro"]:
        min_loss = 80 if group in {"option", "macro"} else 120
        max_features = 10 if group in {"option", "macro"} else 16
        for label_threshold in [0.0, -0.005, -0.01, -0.02]:
            suffix = "neg" if label_threshold == 0.0 else f"loss{abs(label_threshold) * 100:.1f}".replace(".", "p")
            for flag_quantile in [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
                q_name = int(round(flag_quantile * 100))
                for cap_pct in [0.0, 20.0, 40.0, 60.0]:
                    rules.append(
                        ExternalFeatureRule(
                            f"ext_{suffix}_{group}_q{q_name}_cap{int(cap_pct)}",
                            label_threshold,
                            group,
                            cap_pct,
                            flag_quantile,
                            min_loss_count=min_loss,
                            max_features=max_features,
                        )
                    )
    for group in ["external", "combined"]:
        for label_threshold in [0.0, -0.01]:
            suffix = "neg" if label_threshold == 0.0 else "loss1p0"
            for flag_quantile in [0.40, 0.55, 0.70]:
                q_name = int(round(flag_quantile * 100))
                for cooldown_months in [1, 2]:
                    rules.append(
                        ExternalFeatureRule(
                            f"ext_{suffix}_{group}_q{q_name}_cap40_cd{cooldown_months}",
                            label_threshold,
                            group,
                            40.0,
                            flag_quantile,
                            max_features=16,
                            cooldown_months=cooldown_months,
                        )
                    )
    return rules


RULES = build_rules()
_MODEL_CACHE: dict[tuple[str, float, int, int, int], dict[str, dict[str, Any] | None]] = {}
_ENRICHED_ROWS: list[dict[str, Any]] | None = None


def parse_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def safe_name(symbol: str) -> str:
    return symbol.replace("^", "v").replace(":", "_").lower()


def value_at(rows: list[tuple[dt.date, float]], day: dt.date) -> float | None:
    idx = bisect_right(rows, (day, float("inf"))) - 1
    return rows[idx][1] if idx >= 0 else None


def trailing_values(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> list[float]:
    idx = bisect_right(rows, (day, float("inf")))
    return [value for _date, value in rows[max(0, idx - days) : idx]]


def ret_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    idx = bisect_left(rows, (day - dt.timedelta(days=days), -float("inf")))
    if current is None or idx >= len(rows):
        return None
    past = rows[idx][1]
    return current / past - 1.0 if past and past > 0 else None


def chg_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    idx = bisect_left(rows, (day - dt.timedelta(days=days), -float("inf")))
    if current is None or idx >= len(rows):
        return None
    return current - rows[idx][1]


def vol_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    values = trailing_values(rows, day, days + 1)
    returns = [cur / prev - 1.0 for prev, cur in zip(values, values[1:]) if prev > 0]
    if len(returns) < max(20, days // 3):
        return None
    return statistics.pstdev(returns) * math.sqrt(252.0)


def drawdown_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    values = trailing_values(rows, day, days)
    current = value_at(rows, day)
    if current is None or len(values) < max(20, days // 3):
        return None
    high = max(values)
    return current / high - 1.0 if high > 0 else None


def ma_distance_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    values = trailing_values(rows, day, days)
    current = value_at(rows, day)
    if current is None or len(values) < max(20, days // 3):
        return None
    ma = statistics.mean(values)
    return current / ma - 1.0 if ma > 0 else None


def percentile_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    values = trailing_values(rows, day, days)
    if current is None or len(values) < max(60, days // 3):
        return None
    return sum(1 for value in values if value <= current) / len(values)


def load_external_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    placeholders = ",".join(["%s"] * len(EXTERNAL_SYMBOLS))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, trade_date, COALESCE(adj_close, close)
            FROM external_asset_daily
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, trade_date
            """,
            EXTERNAL_SYMBOLS,
        )
        out = {symbol: [] for symbol in EXTERNAL_SYMBOLS}
        for symbol, trade_date, value in cur.fetchall():
            if value is not None:
                out[symbol].append((trade_date, float(value)))
    missing = [symbol for symbol, rows in out.items() if not rows]
    if missing:
        raise RuntimeError(f"missing external_asset_daily rows for {missing}")
    return out


def external_features(series: dict[str, list[tuple[dt.date, float]]], snapshot: dt.date) -> dict[str, float | None]:
    features: dict[str, float | None] = {}
    for symbol in PRICE_SYMBOLS:
        rows = series[symbol]
        name = safe_name(symbol)
        for label, days in [("ret_1m", 21), ("ret_3m", 63), ("ret_6m", 126), ("ret_12m", 252)]:
            features[f"ext_{name}_{label}"] = ret_at(rows, snapshot, days)
        features[f"ext_{name}_vol_3m"] = vol_at(rows, snapshot, 63)
        features[f"ext_{name}_dd_3m"] = drawdown_at(rows, snapshot, 63)
        features[f"ext_{name}_dist_ma200"] = ma_distance_at(rows, snapshot, 200)
    vix_rows = series["^VIX"]
    features["ext_vix_level"] = value_at(vix_rows, snapshot)
    features["ext_vix_pct_1y"] = percentile_at(vix_rows, snapshot, 252)
    features["ext_vix_chg_1m"] = chg_at(vix_rows, snapshot, 21)
    features["ext_vix_chg_3m"] = chg_at(vix_rows, snapshot, 63)

    for symbol in FRED_SYMBOLS:
        rows = series[symbol]
        name = safe_name(symbol)
        features[f"ext_{name}_level"] = value_at(rows, snapshot)
        features[f"ext_{name}_chg_3m"] = chg_at(rows, snapshot, 90)
        features[f"ext_{name}_chg_6m"] = chg_at(rows, snapshot, 180)
        features[f"ext_{name}_pct_3y"] = percentile_at(rows, snapshot, 756)

    dgs10 = features.get("ext_fred_dgs10_level")
    dgs2 = features.get("ext_fred_dgs2_level")
    features["ext_us_curve_10y2y"] = dgs10 - dgs2 if dgs10 is not None and dgs2 is not None else None
    features["ext_qqq_spy_rel_3m"] = rel_return(series, "QQQ", "SPY", snapshot, 63)
    features["ext_tlt_spy_rel_3m"] = rel_return(series, "TLT", "SPY", snapshot, 63)
    features["ext_put_spy_rel_3m"] = rel_return(series, "PUT", "SPY", snapshot, 63)
    features["ext_pput_spy_rel_3m"] = rel_return(series, "PPUT", "SPY", snapshot, 63)
    features["ext_bxm_spy_rel_3m"] = rel_return(series, "BXM", "SPY", snapshot, 63)
    return features


def rel_return(series: dict[str, list[tuple[dt.date, float]]], left: str, right: str, day: dt.date, days: int) -> float | None:
    lret = ret_at(series[left], day, days)
    rret = ret_at(series[right], day, days)
    return lret - rret if lret is not None and rret is not None else None


def load_base_rows() -> list[dict[str, Any]]:
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
            for feature in LOCAL_FEATURES:
                item[feature] = parse_float(raw.get(feature))
            rows.append(item)
    rows.sort(key=lambda item: (item["snapshot"], item["phase_month_offset"], item["execution_lag_days"]))
    return rows


def load_rows() -> list[dict[str, Any]]:
    global _ENRICHED_ROWS
    if _ENRICHED_ROWS is not None:
        return _ENRICHED_ROWS
    base_rows = load_base_rows()
    conn = get_connection()
    try:
        series = load_external_series(conn)
    finally:
        conn.close()
    by_snapshot = {
        snapshot: external_features(series, dt.date.fromisoformat(snapshot))
        for snapshot in sorted({row["snapshot"] for row in base_rows})
    }
    _ENRICHED_ROWS = [{**row, **by_snapshot[row["snapshot"]]} for row in base_rows]
    return _ENRICHED_ROWS


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def is_loss(row: dict[str, Any], threshold: float) -> bool:
    return float(row["month_return"]) <= threshold


def feature_names(group: str) -> list[str]:
    external = [name for name in load_rows()[0] if name.startswith("ext_")]
    if group == "combined":
        return LOCAL_FEATURES + external
    if group == "risk_market":
        keep = [
            "spy",
            "qqq",
            "tlt",
            "ief",
            "shy",
            "gld",
            "vix",
            "efa",
            "eem",
            "iwm",
            "xlk",
            "xle",
            "xlu",
            "hyg",
            "lqd",
            "agg",
            "tip",
        ]
        return [name for name in external if any(token in name for token in keep)]
    if group == "option":
        keep = ["pput", "put", "bxm", "bxmd", "cllz"]
        return [name for name in external if any(token in name for token in keep)]
    if group == "macro":
        keep = ["fred", "curve"]
        return [name for name in external if any(token in name for token in keep)]
    return external


def build_model(
    train_rows: list[dict[str, Any]],
    names: list[str],
    max_features: int,
    min_loss_count: int,
    label_threshold: float,
) -> dict[str, Any] | None:
    loss_rows = [row for row in train_rows if is_loss(row, label_threshold)]
    ok_rows = [row for row in train_rows if not is_loss(row, label_threshold)]
    if len(loss_rows) < min_loss_count or len(ok_rows) < min_loss_count:
        return None
    specs = []
    for name in names:
        values = [row[name] for row in train_rows if row.get(name) is not None]
        loss_values = [row[name] for row in loss_rows if row.get(name) is not None]
        ok_values = [row[name] for row in ok_rows if row.get(name) is not None]
        if len(values) < 100 or len(loss_values) < 40 or len(ok_values) < 40:
            continue
        center = median(values)
        mad = median([abs(value - center) for value in values])
        scale = max(mad * 1.4826, statistics.pstdev(values), 1e-9)
        loss_center = median(loss_values)
        ok_center = median(ok_values)
        weight = (loss_center - ok_center) / scale
        if abs(weight) < 0.03:
            continue
        coverage = len(values) / len(train_rows)
        specs.append({"feature": name, "center": center, "scale": scale, "weight": weight, "strength": abs(weight) * coverage})
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


def train_models_by_snapshot(rows: list[dict[str, Any]], rule: ExternalFeatureRule) -> dict[str, dict[str, Any] | None]:
    names = feature_names(rule.feature_group)
    snapshots = sorted({row["snapshot"] for row in rows})
    years = sorted({int(snapshot[:4]) for snapshot in snapshots})
    by_year: dict[int, dict[str, Any] | None] = {}
    for year in years:
        train = [row for row in rows if int(row["snapshot"][:4]) < year]
        if len({row["snapshot"] for row in train}) < rule.min_train_months:
            by_year[year] = None
            continue
        model = build_model(train, names, rule.max_features, rule.min_loss_count, rule.label_threshold)
        if model is not None:
            model["train_rows"] = len(train)
            model["train_loss_count"] = sum(1 for row in train if is_loss(row, rule.label_threshold))
        by_year[year] = model
    return {snapshot: by_year[int(snapshot[:4])] for snapshot in snapshots}


def models_with_thresholds(rows: list[dict[str, Any]], rule: ExternalFeatureRule) -> dict[str, dict[str, Any] | None]:
    cache_key = (
        rule.feature_group,
        rule.label_threshold,
        rule.min_train_months,
        rule.min_loss_count,
        rule.max_features,
    )
    if cache_key not in _MODEL_CACHE:
        _MODEL_CACHE[cache_key] = train_models_by_snapshot(rows, rule)
    return {
        snapshot: {**model, "threshold": quantile(model["scores"], rule.flag_quantile)} if model is not None else None
        for snapshot, model in _MODEL_CACHE[cache_key].items()
    }


def run_case(rows: list[dict[str, Any]], models: dict[str, dict[str, Any] | None], rule: ExternalFeatureRule, phase: int, lag: int) -> dict[str, Any]:
    case_rows = [row for row in rows if row["phase_month_offset"] == phase and row["execution_lag_days"] == lag]
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
            score = score_row(row, model["features"])
            flagged = score >= float(model["threshold"])
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


def evaluate_rule(rows: list[dict[str, Any]], rule: ExternalFeatureRule) -> dict[str, Any]:
    models = models_with_thresholds(rows, rule)
    cases = [run_case(rows, models, rule, phase, lag) for phase in range(12) for lag in [0, 1, 3, 5]]
    summary = matrix_summary(cases)
    trained_models = [model for model in models.values() if model is not None]
    model_summary = {
        "trained_snapshot_count": len(trained_models),
        "median_train_rows": statistics.median(model["train_rows"] for model in trained_models) if trained_models else 0,
        "median_train_loss_count": statistics.median(model["train_loss_count"] for model in trained_models) if trained_models else 0,
        "latest_features": [item["feature"] for item in trained_models[-1]["features"]] if trained_models else [],
    }
    return {
        "rule": asdict(rule),
        "summary": summary,
        "model_summary": model_summary,
        "cases": cases,
        "target_met": summary["pass_count"] == summary["count"],
    }


def feature_coverage(rows: list[dict[str, Any]]) -> dict[str, float]:
    names = feature_names("external")
    return {name: sum(1 for row in rows if row.get(name) is not None) / len(rows) for name in names}


def label_balance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    balances = []
    for threshold in [0.0, -0.005, -0.01, -0.02, -0.04, -0.08]:
        count = sum(1 for row in rows if is_loss(row, threshold))
        balances.append({"label_threshold": threshold, "count": count, "pct": count / len(rows) if rows else 0.0})
    return balances


def write_outputs(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test external full-window feature walk-forward loss guards on scorecard+CSI phase ensemble rows.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "source_rows": str(ROWS_CSV),
        "external_symbols": EXTERNAL_SYMBOLS,
        "label_balance": label_balance(rows),
        "feature_coverage": feature_coverage(rows),
        "model_limits": "Linear loss-vs-ok standardized score trained only on prior-year snapshots; external features are as-of snapshot values from local cache.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "feature_group",
            "label_threshold",
            "cap_pct",
            "flag_quantile",
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
    coverage = feature_coverage(rows)
    print(
        "feature_coverage "
        + " ".join(
            f"{name}:{coverage[name] * 100:.1f}%"
            for name in sorted(coverage)
            if coverage[name] < 0.95
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

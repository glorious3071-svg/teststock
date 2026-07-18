#!/usr/bin/env python3
"""Screen walk-forward quarterly exposure rules for a selected passive ETF.

The selection model and exposure model are both point-in-time.  At a month-end
snapshot, the exposure learner can use only selected-ETF windows whose
``end_snapshot`` is no later than that month-end.  Portfolio exposure is then
frozen for the full next three-month window.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.passive_etf_supervised_selector import (
    DEFAULT_DATASET,
    PRICE_RISK_FEATURES,
    STABLE_FEATURES,
    SupervisedEtfPolicy,
    select_supervised_etfs,
    select_weighted_stable_combo_v3_top1,
    select_weighted_stable_combo_v5_top1,
    select_weighted_stable_combo_v9_top1,
    weighted_stable_combo_v3_scores,
    weighted_stable_combo_v5_scores,
    weighted_stable_combo_v9_scores,
)


OUTPUT = ROOT / "data/backtests/passive_etf_absolute_exposure_screen_report.json"
STABLE_MARKET_FEATURES = (
    "selected_momentum_12m_skip1m",
    "selected_momentum_12m",
    "market_domestic_sf_rolling_3m_yoy",
    "market_domestic_m1_m2_scissors_change_3m",
    "market_fund_total_issuance_percentile_3y",
    "market_fund_active_issuance_percentile_3y",
    "spread_log_amount_1m",
    "market_market_pb_percentile_3y",
    "market_market_pe_ttm_percentile_3y",
    "market_domestic_shibor_on_percentile_3y",
    "market_external_broad_dollar_return_6m",
    "market_external_broad_dollar_ma_10m_distance",
    "market_external_baa10y_change_1m",
    "market_external_baa10y_change_3m",
    "market_external_aaa10y_change_3m",
    "market_external_baa_aaa_quality_spread_percentile_3y",
    "market_pboc_outlook_net_tone",
    "market_pboc_outlook_net_tone_change",
    "market_pboc_outlook_risk_density",
    "market_pboc_report_age_days",
)
SELECTOR = SupervisedEtfPolicy(
    "absolute_risk_base_stable_h120_a10_top1_dd2",
    STABLE_FEATURES,
    120,
    10.0,
    1,
    2.0,
)
V3_SELECTED_FEATURES = STABLE_FEATURES + (
    "ulcer_index_6m",
    "index_fundamental_roe_proxy",
)
V5_SELECTED_FEATURES = V3_SELECTED_FEATURES + (
    "volatility_6m",
    "downside_volatility_3m",
    "max_drawdown_6m",
    "index_fundamental_book_growth_12m",
    "index_constituent_earnings_yield",
    "index_constituent_weight_hhi",
)
V9_SELECTED_FEATURES = tuple(
    dict.fromkeys(
        V5_SELECTED_FEATURES
        + PRICE_RISK_FEATURES
        + (
            "amount_acceleration_1m_6m",
            "amount_crowding_percentile_3y",
            "return_amount_correlation_3m",
            "historical_cvar_5pct_3m",
            "maximum_daily_loss_3m",
            "negative_day_ratio_3m",
            "return_skewness_3m",
            "return_excess_kurtosis_3m",
            "days_since_high_6m",
            "volatility_acceleration_1m_3m",
        )
    )
)


@dataclass(frozen=True)
class ExposurePolicy:
    name: str
    history_periods: int
    ridge_alpha: float
    utility_drawdown_penalty: float
    threshold_quantile: float
    risk_off_exposure: float
    warmup_exposure: float
    target_mode: str = "utility"
    loss_threshold: float = -0.10


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[position]


def median(values: list[float | None]) -> float:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(clean) if clean else 0.0


def percentile(values: list[float], q: float) -> float:
    return quantile(values, q) if values else 0.0


def fallback_weights(rows: list[dict[str, Any]]) -> dict[str, float]:
    def rank(feature: str, higher: bool) -> dict[str, float]:
        ordered = sorted(
            ((str(row["ts_code"]), float(row.get(feature) or 0.0)) for row in rows),
            key=lambda item: (item[1], item[0]),
        )
        denominator = max(len(ordered) - 1, 1)
        output = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
        return output if higher else {code: 1.0 - value for code, value in output.items()}

    beta = rank("market_beta_6m", False)
    vol = rank("volatility_1m", False)
    distance = rank("distance_high_12m", True)
    code = max(
        (str(row["ts_code"]) for row in rows),
        key=lambda item: (0.35 * beta[item] + 0.30 * vol[item] + 0.35 * distance[item], item),
    )
    return {code: 1.0}


def selected_observations(
    rows: list[dict[str, Any]],
    selector_version: str = "legacy",
) -> list[dict[str, Any]]:
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)
    output = []
    for snapshot, current in sorted(grouped.items()):
        if selector_version == "v9":
            weights = select_weighted_stable_combo_v9_top1(rows, snapshot)
            selector_scores = weighted_stable_combo_v9_scores(rows, snapshot)
            selected_feature_names = V9_SELECTED_FEATURES
        elif selector_version == "v5":
            weights = select_weighted_stable_combo_v5_top1(rows, snapshot)
            selector_scores = weighted_stable_combo_v5_scores(rows, snapshot)
            selected_feature_names = V5_SELECTED_FEATURES
        elif selector_version == "v3":
            weights = select_weighted_stable_combo_v3_top1(rows, snapshot)
            selector_scores = weighted_stable_combo_v3_scores(rows, snapshot)
            selected_feature_names = V3_SELECTED_FEATURES
        else:
            weights = select_supervised_etfs(rows, snapshot, SELECTOR)
            selector_scores = {}
            selected_feature_names = STABLE_FEATURES
        weights = weights or fallback_weights(current)
        ordered_scores = sorted(selector_scores.values(), reverse=True)
        selected = [row for row in current if str(row["ts_code"]) in weights]
        selected_by_code = {str(row["ts_code"]): row for row in selected}
        if any(
            code not in selected_by_code
            or selected_by_code[code].get("forward_return_3m") is None
            or selected_by_code[code].get("forward_max_drawdown_3m") is None
            for code in weights
        ):
            # The newest snapshot remains eligible for production selection,
            # but it is not a completed research label until the full next
            # quarter has elapsed.
            continue
        selected_features: dict[str, float | None] = {}
        for feature in selected_feature_names:
            available = [
                (float(weight), float(selected_by_code[code][feature]))
                for code, weight in weights.items()
                if code in selected_by_code
                and selected_by_code[code].get(feature) is not None
                and math.isfinite(float(selected_by_code[code][feature]))
            ]
            available_weight = sum(weight for weight, _value in available)
            selected_features[f"selected_{feature}"] = (
                sum(weight * value for weight, value in available) / available_weight
                if available_weight > 0.0
                else None
            )
            selected_features[f"selected_{feature}_coverage"] = available_weight
        cross_features = {}
        for feature in selected_feature_names:
            values = [
                float(row[feature])
                for row in current
                if row.get(feature) is not None and math.isfinite(float(row[feature]))
            ]
            cross_features[f"median_{feature}"] = (
                statistics.median(values) if values else None
            )
            cross_features[f"spread_{feature}"] = (
                percentile(values, 0.75) - percentile(values, 0.25)
                if values
                else None
            )
            cross_features[f"coverage_{feature}"] = len(values) / len(current)
        regime = str(current[0]["market_regime"])
        momentum_3m = [
            float(row["momentum_3m"])
            for row in current
            if row.get("momentum_3m") is not None
            and math.isfinite(float(row["momentum_3m"]))
        ]
        distance_high_12m = [
            float(row["distance_high_12m"])
            for row in current
            if row.get("distance_high_12m") is not None
            and math.isfinite(float(row["distance_high_12m"]))
        ]
        market_return_6m = current[0].get("market_return_6m")
        output.append(
            {
                "snapshot": snapshot.isoformat(),
                "end_snapshot": current[0]["end_snapshot"],
                "selected_codes": sorted(weights),
                "forward_return_3m": sum(
                    weight * float(selected_by_code[code]["forward_return_3m"])
                    for code, weight in weights.items()
                    if code in selected_by_code
                ),
                "forward_max_drawdown_3m": sum(
                    weight * float(selected_by_code[code]["forward_max_drawdown_3m"])
                    for code, weight in weights.items()
                    if code in selected_by_code
                ),
                "market_return_6m": (
                    float(market_return_6m)
                    if market_return_6m is not None
                    and math.isfinite(float(market_return_6m))
                    else None
                ),
                "candidate_count_log": math.log1p(len(current)),
                "regime_bull": 1.0 if regime == "bull" else 0.0,
                "regime_neutral": 1.0 if regime == "neutral" else 0.0,
                "regime_bear": 1.0 if regime == "bear" else 0.0,
                "breadth_momentum_3m_positive": (
                    sum(value > 0.0 for value in momentum_3m) / len(momentum_3m)
                    if momentum_3m
                    else None
                ),
                "breadth_momentum_3m_coverage": len(momentum_3m) / len(current),
                "breadth_within_10pct_high": (
                    sum(value >= -0.10 for value in distance_high_12m)
                    / len(distance_high_12m)
                    if distance_high_12m
                    else None
                ),
                "breadth_distance_high_12m_coverage": (
                    len(distance_high_12m) / len(current)
                ),
                "selector_score_top1": ordered_scores[0] if ordered_scores else None,
                "selector_score_margin": (
                    ordered_scores[0] - ordered_scores[1]
                    if len(ordered_scores) >= 2
                    else None
                ),
                "selector_score_dispersion": (
                    statistics.pstdev(ordered_scores)
                    if len(ordered_scores) >= 2
                    else None
                ),
                **selected_features,
                **cross_features,
            }
        )
    return output


def feature_names(rows: list[dict[str, Any]], feature_set: str = "base") -> list[str]:
    if feature_set == "stable_market":
        return [name for name in STABLE_MARKET_FEATURES if any(row.get(name) is not None for row in rows)]
    excluded = {
        "snapshot",
        "end_snapshot",
        "selected_codes",
        "forward_return_3m",
        "forward_max_drawdown_3m",
    }
    names = sorted(set().union(*(row.keys() for row in rows)) - excluded)
    if feature_set == "all_market":
        return [name for name in names if any(isinstance(row.get(name), (int, float)) for row in rows)]
    return [name for name in names if not name.startswith("market_")]


def fit_predict(
    history: list[dict[str, Any]],
    current: dict[str, Any],
    names: list[str],
    policy: ExposurePolicy,
) -> tuple[float | None, float | None]:
    history = history[-policy.history_periods :]
    if len(history) < min(16, policy.history_periods):
        return None, None
    minimum_coverage = max(8, int(len(history) * 0.50))
    names = [
        name
        for name in names
        if sum(
            row.get(name) is not None and math.isfinite(float(row[name]))
            for row in history
        )
        >= minimum_coverage
    ]
    if not names:
        return None, None
    raw = np.asarray(
        [
            [
                float(row[name])
                if row.get(name) is not None and math.isfinite(float(row[name]))
                else np.nan
                for name in names
            ]
            for row in history
        ],
        dtype=float,
    )
    center = np.nanmedian(raw, axis=0)
    center = np.where(np.isfinite(center), center, 0.0)
    filled = np.where(np.isfinite(raw), raw, center.reshape(1, -1))
    scale = np.std(filled, axis=0)
    scale = np.where(scale > 1e-9, scale, 1.0)
    x = np.clip((filled - center) / scale, -5.0, 5.0)
    x = np.column_stack([np.ones(len(x)), x])
    y = np.asarray(
        [
            (
                1.0
                if float(row["forward_max_drawdown_3m"]) <= policy.loss_threshold
                else 0.0
            )
            if policy.target_mode == "loss"
            else float(row["forward_return_3m"])
            + policy.utility_drawdown_penalty * float(row["forward_max_drawdown_3m"])
            for row in history
        ],
        dtype=float,
    )
    penalty = np.eye(x.shape[1]) * policy.ridge_alpha
    penalty[0, 0] = 0.0
    try:
        with np.errstate(all="ignore"):
            coefficients = np.linalg.solve(x.T @ x + penalty, x.T @ y)
    except np.linalg.LinAlgError:
        return None, None
    if not np.all(np.isfinite(coefficients)):
        return None, None
    current_x = np.asarray(
        [1.0]
        + [
            float(
                np.clip(
                    (
                        float(current[name])
                        if current.get(name) is not None
                        and math.isfinite(float(current[name]))
                        else center[index]
                    )
                    - center[index],
                    -5.0 * scale[index],
                    5.0 * scale[index],
                )
                / scale[index]
            )
            for index, name in enumerate(names)
        ]
    )
    with np.errstate(all="ignore"):
        prediction = float(current_x @ coefficients)
        fitted_array = x @ coefficients
    if not math.isfinite(prediction) or not np.all(np.isfinite(fitted_array)):
        return None, None
    fitted = [float(value) for value in fitted_array]
    return prediction, quantile(fitted, policy.threshold_quantile)


def evaluate(
    rows: list[dict[str, Any]],
    policy: ExposurePolicy,
    selected_features: list[str] | None = None,
) -> dict[str, Any]:
    names = selected_features or feature_names(rows)
    decisions = []
    for current in rows:
        snapshot = date.fromisoformat(str(current["snapshot"]))
        history = [
            row
            for row in rows
            if date.fromisoformat(str(row["end_snapshot"])) <= snapshot
        ]
        prediction, threshold = fit_predict(history, current, names, policy)
        if prediction is None or threshold is None:
            exposure = policy.warmup_exposure
        else:
            risk_on = (
                prediction <= threshold
                if policy.target_mode == "loss"
                else prediction >= threshold
            )
            exposure = 1.0 if risk_on else policy.risk_off_exposure
        decisions.append(
            {
                "snapshot": current["snapshot"],
                "exposure": exposure,
                "portfolio_return": exposure * float(current["forward_return_3m"]),
                "approximate_window_drawdown": exposure
                * float(current["forward_max_drawdown_3m"]),
            }
        )
    starts = decisions[:12]
    cases = []
    for start in starts:
        start_date = date.fromisoformat(str(start["snapshot"]))
        selected = []
        for row in decisions:
            day = date.fromisoformat(str(row["snapshot"]))
            elapsed = (day.year - start_date.year) * 12 + day.month - start_date.month
            if elapsed >= 0 and elapsed % 3 == 0:
                selected.append(row)
        selected = selected[:80]
        capital = 1.0
        peak = 1.0
        worst = 0.0
        for row in selected:
            window_low = capital * (1.0 + float(row["approximate_window_drawdown"]))
            worst = min(worst, window_low / peak - 1.0)
            capital *= 1.0 + float(row["portfolio_return"])
            peak = max(peak, capital)
            worst = min(worst, capital / peak - 1.0)
        cases.append(
            {
                "start_snapshot": start["snapshot"],
                "period_count": len(selected),
                "capital_factor": capital,
                "approximate_max_drawdown": worst,
            }
        )
    return {
        "policy": asdict(policy),
        "summary": {
            "min_capital_factor": min(case["capital_factor"] for case in cases),
            "median_capital_factor": statistics.median(case["capital_factor"] for case in cases),
            "worst_approximate_max_drawdown": min(
                case["approximate_max_drawdown"] for case in cases
            ),
            "median_exposure": statistics.median(row["exposure"] for row in decisions),
            "risk_off_rate": sum(row["exposure"] < 1.0 for row in decisions) / len(decisions),
            "cases": cases,
        },
    }


def policies(target_mode: str = "utility", quick: bool = False) -> list[ExposurePolicy]:
    output = []
    if target_mode == "loss":
        histories = (24, 60) if quick else (24, 60, 120)
        alphas = (10.0, 100.0) if quick else (0.5, 2.0, 10.0)
        labels = (-0.10, -0.15) if quick else (-0.08, -0.10, -0.15)
        thresholds = (0.35, 0.50, 0.65) if quick else (0.20, 0.35, 0.50, 0.65, 0.80)
        risk_offs = (0.0,) if quick else (0.0, 0.10, 0.25)
        warmups = (0.25, 0.50) if quick else (0.0, 0.25, 0.50)
        for history in histories:
            for alpha in alphas:
                for loss_threshold in labels:
                    for threshold in thresholds:
                        for risk_off in risk_offs:
                            for warmup in warmups:
                                output.append(
                                    ExposurePolicy(
                                        f"loss_h{history}_a{alpha:g}_label{int(abs(loss_threshold)*100)}_q{int(threshold*100)}_cap{int(risk_off*100)}_warm{int(warmup*100)}",
                                        history,
                                        alpha,
                                        0.0,
                                        threshold,
                                        risk_off,
                                        warmup,
                                        "loss",
                                        loss_threshold,
                                    )
                                )
        return output
    histories = (60, 120) if quick else (24, 60, 120)
    alphas = (2.0, 10.0) if quick else (0.5, 2.0, 10.0)
    penalties = (0.0, 1.0, 2.0) if quick else (0.0, 0.5, 1.0, 2.0)
    thresholds = (0.35, 0.50, 0.65) if quick else (0.20, 0.35, 0.50, 0.65)
    risk_offs = (0.0, 0.25) if quick else (0.0, 0.25, 0.50)
    warmups = (0.25, 0.50) if quick else (0.0, 0.25, 0.50)
    for history in histories:
        for alpha in alphas:
            for penalty in penalties:
                for threshold in thresholds:
                    for risk_off in risk_offs:
                        for warmup in warmups:
                            output.append(
                                ExposurePolicy(
                                    f"abs_h{history}_a{alpha:g}_dd{penalty:g}_q{int(threshold*100)}_cap{int(risk_off*100)}_warm{int(warmup*100)}",
                                    history,
                                    alpha,
                                    penalty,
                                    threshold,
                                    risk_off,
                                    warmup,
                                )
                            )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--selected-dataset", type=Path)
    parser.add_argument(
        "--feature-set",
        choices=("base", "stable_market", "all_market"),
        default="base",
    )
    parser.add_argument("--target-mode", choices=("utility", "loss"), default="utility")
    parser.add_argument(
        "--selector-version", choices=("legacy", "v3", "v5"), default="legacy"
    )
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.selected_dataset:
        payload = json.loads(args.selected_dataset.read_text(encoding="utf-8"))
        selected = list(payload["selected_observations"])
    else:
        payload = json.loads(args.dataset.read_text(encoding="utf-8"))
        selected = selected_observations(
            list(payload["candidate_observations"]), args.selector_version
        )
    selected_features = feature_names(selected, args.feature_set)
    results = [
        evaluate(selected, policy, selected_features)
        for policy in policies(args.target_mode, args.quick)
    ]
    results.sort(
        key=lambda item: (
            item["summary"]["worst_approximate_max_drawdown"] >= -0.10,
            item["summary"]["min_capital_factor"],
            item["summary"]["worst_approximate_max_drawdown"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": "walk-forward absolute quarterly utility ridge; quarterly exposure frozen",
                "selector": asdict(SELECTOR),
                "observation_count": len(selected),
                "feature_set": args.feature_set,
                "features": selected_features,
                "target_mode": args.target_mode,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<42} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"mdd~={summary['worst_approximate_max_drawdown']*100:6.2f}% "
            f"off={summary['risk_off_rate']*100:5.1f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

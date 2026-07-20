#!/usr/bin/env python3
"""Compare point-in-time structural ETF selector recipes on structural quarters."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.passive_etf_supervised_selector import (  # noqa: E402
    SHARE_V5_DATASET,
    load_candidate_observations,
    structural_subtheme_group_for_text,
    structural_theme_group_for_text,
    weighted_structural_conditional_rotation_scores,
    weighted_structural_cooling_rotation_scores,
    weighted_structural_multistate_rotation_scores,
    weighted_structural_reflation_rotation_scores,
    weighted_structural_resilience_scores,
    weighted_structural_mainline_scores,
)
from backtest.structural_adaptation import STRUCTURAL_ADAPTATION_GATE  # noqa: E402
from scripts.validate_scorecard_csi_structural_adaptation import (  # noqa: E402
    load_domestic_passive_etf_series,
    parse_date,
    period_return,
    structural_rows_for_case,
)


FeatureSpec = tuple[str, bool, float]
RecipeFn = Callable[[list[dict[str, Any]], date], dict[str, float]]


def rank_score(rows: list[dict[str, Any]], specs: tuple[FeatureSpec, ...]) -> dict[str, float]:
    if not rows:
        return {}

    def ranks(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in rows
                if row.get(feature) is not None
                and math.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in rows}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in rows
        }

    total_weight = sum(weight for _feature, _higher, weight in specs)
    components = tuple(
        (ranks(feature, higher), weight / total_weight)
        for feature, higher, weight in specs
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in rows
    }


def current_recipe(observations: list[dict[str, Any]], snapshot: date) -> dict[str, float]:
    return weighted_structural_mainline_scores(observations, snapshot)


MOMENTUM_BREADTH_SPECS: tuple[FeatureSpec, ...] = (
    ("relative_strength_3m", True, 3.2),
    ("relative_strength_6m", True, 2.6),
    ("momentum_3m", True, 2.2),
    ("momentum_6m", True, 1.8),
    ("positive_day_ratio_3m", True, 1.4),
    ("index_trend_acceleration_geometric_3m_vs_6m", True, 1.2),
    ("market_correlation_6m", False, 1.2),
    ("residual_momentum_6m", True, 1.0),
    ("log_amount_1m", True, 0.7),
    ("amount_acceleration_1m_6m", True, 0.8),
    ("etf_share_growth_1q", True, 0.5),
    ("index_policy_score", True, 0.4),
    ("drawdown_3m", True, 0.6),
    ("amount_crowding_percentile_3y", False, 0.5),
    ("negative_day_ratio_3m", False, 0.7),
    ("historical_cvar_5pct_3m", True, 0.4),
)

LOW_CORR_TREND_SPECS: tuple[FeatureSpec, ...] = (
    ("relative_strength_3m", True, 2.6),
    ("relative_strength_6m", True, 2.2),
    ("market_correlation_6m", False, 2.0),
    ("residual_momentum_6m", True, 1.8),
    ("index_trend_acceleration_geometric_3m_vs_6m", True, 1.4),
    ("positive_day_ratio_3m", True, 1.1),
    ("momentum_3m", True, 1.0),
    ("amount_acceleration_1m_6m", True, 0.8),
    ("index_policy_score", True, 0.6),
    ("distance_high_12m", True, 0.4),
    ("amount_crowding_percentile_3y", False, 0.8),
    ("negative_day_ratio_3m", False, 0.8),
)

LIQUIDITY_FLOW_SPECS: tuple[FeatureSpec, ...] = (
    ("relative_strength_3m", True, 2.2),
    ("relative_strength_6m", True, 1.8),
    ("momentum_3m", True, 1.4),
    ("positive_day_ratio_3m", True, 1.2),
    ("log_amount_1m", True, 1.2),
    ("amount_acceleration_1m_6m", True, 1.4),
    ("etf_share_growth_1q", True, 1.0),
    ("etf_subscription_flow_1q", True, 0.9),
    ("index_etf_positive_turnover_pressure_1m", True, 0.8),
    ("market_correlation_6m", False, 0.8),
    ("amount_crowding_percentile_3y", False, 0.8),
    ("negative_day_ratio_3m", False, 0.7),
)


def recipe_from_specs(specs: tuple[FeatureSpec, ...]) -> RecipeFn:
    def recipe(observations: list[dict[str, Any]], _snapshot: date) -> dict[str, float]:
        return rank_score(observations, specs)

    return recipe


def code_theme_groups(metas: dict[str, dict[str, Any]]) -> dict[str, str]:
    groups = {}
    for code, meta in metas.items():
        text = f"{meta.get('name') or ''} {meta.get('index_name') or ''}"
        groups[code] = structural_theme_group_for_text(text)
    return groups


def code_subtheme_groups(metas: dict[str, dict[str, Any]]) -> dict[str, str]:
    groups = {}
    for code, meta in metas.items():
        text = f"{meta.get('name') or ''} {meta.get('index_name') or ''}"
        groups[code] = structural_subtheme_group_for_text(text)
    return groups


def percentile(values: dict[str, float], *, higher_is_better: bool = True) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(ordered) - 1, 1)
    ranks = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
    if higher_is_better:
        return ranks
    return {code: 1.0 - rank for code, rank in ranks.items()}


def finite_feature(row: dict[str, Any], feature: str) -> float | None:
    value = row.get(feature)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def group_breadth_scores(
    observations: list[dict[str, Any]],
    specs: tuple[FeatureSpec, ...],
    groups_by_code: dict[str, str],
    *,
    group_weight: float,
) -> dict[str, float]:
    base = rank_score(observations, specs)
    if not base:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        code = str(row["ts_code"])
        grouped[groups_by_code.get(code, "other")].append(row)
    group_raw: dict[str, float] = {}
    for group, rows in grouped.items():
        if group == "other" or len(rows) < 2:
            continue
        momentum_3m = [finite_feature(row, "momentum_3m") for row in rows]
        momentum_6m = [finite_feature(row, "momentum_6m") for row in rows]
        rs_3m = [finite_feature(row, "relative_strength_3m") for row in rows]
        flow = [finite_feature(row, "etf_share_growth_1q") for row in rows]
        positives = [
            1.0
            for value in momentum_3m
            if value is not None and value > 0.0
        ]
        usable_momentum_3m = [value for value in momentum_3m if value is not None]
        usable_momentum_6m = [value for value in momentum_6m if value is not None]
        usable_rs_3m = [value for value in rs_3m if value is not None]
        usable_flow = [value for value in flow if value is not None]
        if not usable_momentum_3m or not usable_momentum_6m:
            continue
        breadth = len(positives) / len(usable_momentum_3m)
        group_raw[group] = (
            0.35 * statistics.mean(usable_momentum_3m)
            + 0.20 * statistics.mean(usable_momentum_6m)
            + 0.20 * (statistics.mean(usable_rs_3m) if usable_rs_3m else 0.0)
            + 0.15 * breadth
            + 0.10 * (statistics.mean(usable_flow) if usable_flow else 0.0)
        )
    group_rank = percentile(group_raw, higher_is_better=True)
    if not group_rank:
        return base
    own_weight = max(0.0, min(1.0, 1.0 - group_weight))
    return {
        code: own_weight * score
        + group_weight * group_rank.get(groups_by_code.get(code, "other"), 0.5)
        for code, score in base.items()
    }


def risk_aware_local_mainline_scores(
    observations: list[dict[str, Any]],
    groups_by_code: dict[str, str],
    subthemes_by_code: dict[str, str],
) -> dict[str, float]:
    """Diagnostic-only local-mainline score with explicit tail-risk controls."""

    if not observations:
        return {}
    base_specs: tuple[FeatureSpec, ...] = (
        ("relative_strength_3m", True, 2.0),
        ("relative_strength_6m", True, 1.5),
        ("momentum_3m", True, 1.4),
        ("momentum_6m", True, 1.0),
        ("positive_day_ratio_3m", True, 1.2),
        ("amount_acceleration_1m_6m", True, 1.0),
        ("etf_share_growth_1q", True, 0.8),
        ("market_correlation_6m", False, 0.7),
        ("drawdown_3m", True, 1.1),
        ("maximum_daily_loss_3m", True, 1.1),
        ("historical_cvar_5pct_3m", True, 1.0),
        ("amount_crowding_percentile_3y", False, 0.8),
    )
    base = rank_score(observations, base_specs)
    if not base:
        return {}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[subthemes_by_code.get(str(row["ts_code"]), "other")].append(row)
    group_raw: dict[str, float] = {}
    for group, rows in grouped.items():
        momentum_3m = [finite_feature(row, "momentum_3m") for row in rows]
        momentum_6m = [finite_feature(row, "momentum_6m") for row in rows]
        rs_3m = [finite_feature(row, "relative_strength_3m") for row in rows]
        max_loss = [finite_feature(row, "maximum_daily_loss_3m") for row in rows]
        crowding = [finite_feature(row, "amount_crowding_percentile_3y") for row in rows]
        usable_m3 = [value for value in momentum_3m if value is not None]
        usable_m6 = [value for value in momentum_6m if value is not None]
        usable_rs = [value for value in rs_3m if value is not None]
        usable_loss = [value for value in max_loss if value is not None]
        usable_crowd = [value for value in crowding if value is not None]
        if not usable_m3:
            continue
        breadth = sum(1 for value in usable_m3 if value > 0.0) / len(usable_m3)
        tail = statistics.mean(usable_loss) if usable_loss else -0.05
        crowd = statistics.mean(usable_crowd) if usable_crowd else 0.5
        group_raw[group] = (
            0.32 * statistics.mean(usable_m3)
            + 0.18 * (statistics.mean(usable_m6) if usable_m6 else 0.0)
            + 0.18 * (statistics.mean(usable_rs) if usable_rs else 0.0)
            + 0.18 * breadth
            + 0.10 * tail
            - 0.04 * max(0.0, crowd - 0.85)
        )
        if group in {"semiconductor", "digital_hot", "communication", "healthcare"}:
            group_raw[group] += 0.05
    group_rank = percentile(group_raw, higher_is_better=True)
    scores = {}
    for row in observations:
        code = str(row["ts_code"])
        group = groups_by_code.get(code, "other")
        subtheme = subthemes_by_code.get(code, "other")
        maximum_loss = finite_feature(row, "maximum_daily_loss_3m")
        cvar = finite_feature(row, "historical_cvar_5pct_3m")
        crowding = finite_feature(row, "amount_crowding_percentile_3y")
        blocked = (
            maximum_loss is not None
            and maximum_loss < -0.105
            or cvar is not None
            and cvar < -0.070
            or crowding is not None
            and crowding > 0.97
        )
        if blocked:
            scores[code] = base[code] * 0.25
            continue
        theme_bonus = 0.0
        if subtheme in {"semiconductor", "digital_hot", "communication", "healthcare"}:
            theme_bonus += 0.08
        if group in {"finance", "broad_value"}:
            theme_bonus -= 0.06
        scores[code] = (
            0.62 * base[code]
            + 0.38 * group_rank.get(subtheme, 0.5)
            + theme_bonus
        )
    return scores


def group_feature_metrics(
    observations: list[dict[str, Any]],
    groups_by_code: dict[str, str],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[groups_by_code.get(str(row["ts_code"]), "other")].append(row)
    metrics: dict[str, dict[str, float]] = {}
    for group, rows in grouped.items():
        values: dict[str, float] = {}
        for feature in (
            "momentum_1m",
            "momentum_3m",
            "momentum_6m",
            "relative_strength_3m",
            "drawdown_3m",
            "market_correlation_6m",
            "amount_crowding_percentile_3y",
            "etf_share_growth_1q",
        ):
            usable = [
                value
                for row in rows
                if (value := finite_feature(row, feature)) is not None
            ]
            values[feature] = statistics.mean(usable) if usable else 0.0
        values["breadth_3m_positive"] = (
            sum(
                1
                for row in rows
                if (value := finite_feature(row, "momentum_3m")) is not None
                and value > 0.0
            )
            / max(
                sum(
                    1
                    for row in rows
                    if finite_feature(row, "momentum_3m") is not None
                ),
                1,
            )
        )
        metrics[group] = values
    return metrics


def growth_exhaustion_active(
    observations: list[dict[str, Any]],
    subthemes_by_code: dict[str, str],
) -> bool:
    metrics = group_feature_metrics(observations, subthemes_by_code)
    growth_groups = [
        metrics[group]
        for group in ("digital_hot", "semiconductor", "communication")
        if group in metrics
    ]
    if not growth_groups:
        return False
    return any(
        (
            group["momentum_3m"] >= 0.25
            and group["amount_crowding_percentile_3y"] >= 0.80
        )
        or (
            group["momentum_6m"] >= 0.25
            and group["etf_share_growth_1q"] >= 1.00
            and group["amount_crowding_percentile_3y"] >= 0.80
        )
        or (
            group["momentum_6m"] >= 0.25
            and group["momentum_1m"] <= 0.00
            and group["drawdown_3m"] <= -0.10
        )
        for group in growth_groups
    )


def late_cycle_defensive_rotation_scores(
    observations: list[dict[str, Any]],
    groups_by_code: dict[str, str],
    subthemes_by_code: dict[str, str],
) -> dict[str, float]:
    """Diagnostic-only rotation away from exhausted growth leadership."""

    if not observations:
        return {}
    if not growth_exhaustion_active(observations, subthemes_by_code):
        return weighted_structural_multistate_rotation_scores(
            observations,
            parse_date(observations[0]["snapshot"]),
            groups_by_code,
            subthemes_by_code,
        )

    defensive_specs: tuple[FeatureSpec, ...] = (
        ("amount_crowding_percentile_3y", False, 1.8),
        ("market_correlation_6m", False, 1.3),
        ("drawdown_3m", True, 1.2),
        ("historical_cvar_5pct_3m", True, 1.0),
        ("maximum_daily_loss_3m", True, 1.0),
        ("momentum_6m", True, 0.9),
        ("relative_strength_6m", True, 0.8),
        ("positive_day_ratio_3m", True, 0.7),
        ("etf_share_growth_1q", True, 0.6),
        ("index_constituent_earnings_yield_change_12m", True, 0.6),
        ("index_constituent_roe_change_12m", True, 0.5),
        ("index_pb_history_percentile_3y", False, 0.5),
    )
    base = rank_score(observations, defensive_specs)
    metrics = group_feature_metrics(observations, subthemes_by_code)
    group_raw: dict[str, float] = {}
    for group, values in metrics.items():
        defensive_bonus = 0.0
        if group in {"resources", "consumer", "finance", "healthcare", "utilities"}:
            defensive_bonus += 0.12
        if group in {"digital_hot", "semiconductor", "communication", "new_energy"}:
            defensive_bonus -= 0.20
        group_raw[group] = (
            0.24 * (1.0 - values["amount_crowding_percentile_3y"])
            + 0.20 * max(values["momentum_6m"], 0.0)
            + 0.16 * values["breadth_3m_positive"]
            + 0.14 * (1.0 - values["market_correlation_6m"])
            + 0.12 * values["drawdown_3m"]
            + 0.08 * values["etf_share_growth_1q"]
            + defensive_bonus
        )
    group_rank = percentile(group_raw, higher_is_better=True)
    scores: dict[str, float] = {}
    for row in observations:
        code = str(row["ts_code"])
        group = subthemes_by_code.get(code, "other")
        score = 0.46 * base[code] + 0.54 * group_rank.get(group, 0.45)
        if group in {"digital_hot", "semiconductor", "communication", "new_energy"}:
            score *= 0.55
        scores[code] = score
    return scores


RECIPES: dict[str, RecipeFn] = {
    "current": current_recipe,
    "momentum_breadth_v2": recipe_from_specs(MOMENTUM_BREADTH_SPECS),
    "low_corr_trend_v2": recipe_from_specs(LOW_CORR_TREND_SPECS),
    "liquidity_flow_v2": recipe_from_specs(LIQUIDITY_FLOW_SPECS),
    "reflation_rotation_v1": weighted_structural_reflation_rotation_scores,
    "resilience_v1": weighted_structural_resilience_scores,
}


def recipes_with_group_breadth(
    groups_by_code: dict[str, str],
    subthemes_by_code: dict[str, str] | None = None,
) -> dict[str, RecipeFn]:
    recipes = dict(RECIPES)

    def liquidity_group_breadth(
        observations: list[dict[str, Any]],
        _snapshot: date,
    ) -> dict[str, float]:
        return group_breadth_scores(
            observations,
            LIQUIDITY_FLOW_SPECS,
            groups_by_code,
            group_weight=0.25,
        )

    def low_corr_group_breadth(
        observations: list[dict[str, Any]],
        _snapshot: date,
    ) -> dict[str, float]:
        return group_breadth_scores(
            observations,
            LOW_CORR_TREND_SPECS,
            groups_by_code,
            group_weight=0.30,
        )

    recipes["liquidity_group_breadth_v1"] = liquidity_group_breadth
    recipes["lowcorr_group_breadth_v1"] = low_corr_group_breadth

    def risk_aware_mainline(
        observations: list[dict[str, Any]],
        _snapshot: date,
    ) -> dict[str, float]:
        return risk_aware_local_mainline_scores(
            observations,
            groups_by_code,
            subthemes_by_code or {},
        )

    recipes["risk_aware_local_mainline_v1"] = risk_aware_mainline

    def conditional_rotation(
        observations: list[dict[str, Any]],
        snapshot: date,
    ) -> dict[str, float]:
        return weighted_structural_conditional_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
        )

    recipes["conditional_rotation_v1"] = conditional_rotation
    if subthemes_by_code is not None:
        def multistate_rotation(
            observations: list[dict[str, Any]],
            snapshot: date,
        ) -> dict[str, float]:
            return weighted_structural_multistate_rotation_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )

        recipes["multistate_rotation_v1"] = multistate_rotation

        def cooling_rotation(
            observations: list[dict[str, Any]],
            snapshot: date,
        ) -> dict[str, float]:
            return weighted_structural_cooling_rotation_scores(
                observations,
                snapshot,
                subthemes_by_code,
            )

        recipes["cooling_rotation_v1"] = cooling_rotation

        def late_cycle_defensive_rotation(
            observations: list[dict[str, Any]],
            _snapshot: date,
        ) -> dict[str, float]:
            return late_cycle_defensive_rotation_scores(
                observations,
                groups_by_code,
                subthemes_by_code,
            )

        recipes["late_cycle_defensive_rotation_v1"] = late_cycle_defensive_rotation
    return recipes


def selected_equal_return(
    codes: list[str],
    series: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
) -> float | None:
    returns = [
        period_return(series.get(code, []), start, end)
        for code in codes
    ]
    usable = [ret for ret in returns if ret is not None]
    return statistics.mean(usable) if usable else None


def family_counts(codes: list[str], groups_by_code: dict[str, str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for code in codes:
        counts[groups_by_code.get(code, "other")] += 1
    return dict(counts)


def dominant_family(codes: list[str], groups_by_code: dict[str, str]) -> str | None:
    counts = family_counts(codes, groups_by_code)
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


def profile_structural_families(
    structural_rows: list[dict[str, Any]],
    groups_by_code: dict[str, str],
    subthemes_by_code: dict[str, str],
) -> dict[str, Any]:
    theme_counter: Counter[str] = Counter()
    subtheme_counter: Counter[str] = Counter()
    selected_subtheme_counter: Counter[str] = Counter()
    missed_rows: list[dict[str, Any]] = []
    for row in structural_rows:
        top10_codes = list(row.get("top10_codes") or [])
        selected_codes = list(row.get("selected_codes") or [])
        top_theme = dominant_family(top10_codes, groups_by_code)
        top_subtheme = dominant_family(top10_codes, subthemes_by_code)
        selected_subtheme = dominant_family(selected_codes, subthemes_by_code)
        if top_theme:
            theme_counter[top_theme] += 1
        if top_subtheme:
            subtheme_counter[top_subtheme] += 1
        if selected_subtheme:
            selected_subtheme_counter[selected_subtheme] += 1
        if selected_codes and set(selected_codes).isdisjoint(top10_codes):
            missed_rows.append(
                {
                    "decision_date": row["decision_date"],
                    "period_end_date": row["period_end_date"],
                    "top10_subtheme_counts": family_counts(top10_codes, subthemes_by_code),
                    "selected_subtheme_counts": family_counts(selected_codes, subthemes_by_code),
                    "top10_codes": top10_codes,
                    "selected_codes": selected_codes,
                    "top10_equal_return": row.get("top10_equal_return"),
                    "portfolio_return": row.get("portfolio_return"),
                }
            )
    return {
        "top10_dominant_theme_counts": dict(theme_counter),
        "top10_dominant_subtheme_counts": dict(subtheme_counter),
        "current_selected_dominant_subtheme_counts": dict(selected_subtheme_counter),
        "scorecard_missed_rows": missed_rows[:20],
    }


def evaluate_recipe(
    name: str,
    recipe: RecipeFn,
    structural_rows: list[dict[str, Any]],
    observations_by_snapshot: dict[date, list[dict[str, Any]]],
    series: dict[str, list[tuple[date, float]]],
    groups_by_code: dict[str, str],
    subthemes_by_code: dict[str, str],
    top_n: int,
) -> dict[str, Any]:
    rows = []
    scores_cache: dict[date, dict[str, float]] = {}
    for row in structural_rows:
        signal_date = parse_date(row.get("signal_date", row["decision_date"]))
        start = parse_date(row["decision_date"])
        end = parse_date(row["period_end_date"])
        scores = scores_cache.get(signal_date)
        if scores is None:
            observations = observations_by_snapshot.get(signal_date, [])
            scores = recipe(observations, signal_date)
            scores_cache[signal_date] = scores
        selected = sorted(
            scores,
            key=lambda code: (round(scores[code], 12), code),
            reverse=True,
        )[:top_n]
        selected_return = selected_equal_return(selected, series, start, end)
        top10_return = row.get("top10_equal_return")
        capture = (
            selected_return / top10_return
            if selected_return is not None
            and top10_return is not None
            and top10_return > 0
            else None
        )
        overlap = len(set(selected).intersection(row.get("top10_codes") or []))
        rows.append(
            {
                "decision_date": row["decision_date"],
                "period_end_date": row["period_end_date"],
                "selected": selected,
                "overlap": overlap,
                "top10_subtheme_counts": family_counts(
                    list(row.get("top10_codes") or []),
                    subthemes_by_code,
                ),
                "selected_subtheme_counts": family_counts(selected, subthemes_by_code),
                "top10_theme": dominant_family(
                    list(row.get("top10_codes") or []),
                    groups_by_code,
                ),
                "top10_subtheme": dominant_family(
                    list(row.get("top10_codes") or []),
                    subthemes_by_code,
                ),
                "selected_subtheme": dominant_family(selected, subthemes_by_code),
                "selected_return": selected_return,
                "capture_ratio": capture,
                "outperformed_broad": (
                    selected_return is not None
                    and selected_return > float(row["broad_return"])
                ),
            }
        )
    valid_capture = [row["capture_ratio"] for row in rows if row["capture_ratio"] is not None]
    hit_rows = [row for row in rows if row["overlap"] > 0]
    pass_rows = [
        row
        for row in rows
        if row["capture_ratio"] is not None and row["capture_ratio"] >= 0.30
    ]
    return {
        "name": name,
        "top_n": top_n,
        "row_count": len(rows),
        "hit_rate": len(hit_rows) / len(rows) if rows else None,
        "avg_overlap": statistics.mean(row["overlap"] for row in rows) if rows else None,
        "capture_pass_rate": len(pass_rows) / len(valid_capture) if valid_capture else None,
        "avg_capture_ratio": statistics.mean(valid_capture) if valid_capture else None,
        "median_capture_ratio": statistics.median(valid_capture) if valid_capture else None,
        "benchmark_win_rate": (
            sum(1 for row in rows if row["outperformed_broad"]) / len(rows)
            if rows
            else None
        ),
        "worst_rows": sorted(
            rows,
            key=lambda row: row["capture_ratio"] if row["capture_ratio"] is not None else -999.0,
        )[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    cases = payload["results"][args.result_index]["cases"]
    metas, series = load_domestic_passive_etf_series(60)
    groups_by_code = code_theme_groups(metas)
    subthemes_by_code = code_subtheme_groups(metas)
    observations = load_candidate_observations(SHARE_V5_DATASET)
    observations_by_snapshot: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        observations_by_snapshot[parse_date(row["snapshot"])].append(row)

    cross_section_cache: dict[tuple[date, date], dict[str, Any]] = {}
    structural_rows = []
    for case in cases:
        structural_rows.extend(
            row
            for row in structural_rows_for_case(
                case,
                metas,
                series,
                STRUCTURAL_ADAPTATION_GATE,
                mainline_observations=None,
                cross_section_cache=cross_section_cache,
            )
            if not row["strong_risk_ban"]
        )

    results = [
        evaluate_recipe(
            name,
            recipe,
            structural_rows,
            observations_by_snapshot,
            series,
            groups_by_code,
            subthemes_by_code,
            args.top_n,
        )
        for name, recipe in recipes_with_group_breadth(
            groups_by_code,
            subthemes_by_code,
        ).items()
    ]
    results.sort(
        key=lambda item: (
            item["capture_pass_rate"] if item["capture_pass_rate"] is not None else -1.0,
            item["hit_rate"] if item["hit_rate"] is not None else -1.0,
        ),
        reverse=True,
    )
    output = {
        "source_report": str(report_path.relative_to(ROOT)),
        "top_n": args.top_n,
        "structural_row_count": len(structural_rows),
        "structural_family_profile": profile_structural_families(
            structural_rows,
            groups_by_code,
            subthemes_by_code,
        ),
        "results": results,
    }
    if args.output:
        out_path = args.output if args.output.is_absolute() else ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_path}")
    for item in results:
        print(
            f"{item['name']} top{item['top_n']} "
            f"hit={(item['hit_rate'] or 0.0):.4f} "
            f"capture_pass={(item['capture_pass_rate'] or 0.0):.4f} "
            f"median_capture={(item['median_capture_ratio'] or 0.0):.4f} "
            f"win={(item['benchmark_win_rate'] or 0.0):.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

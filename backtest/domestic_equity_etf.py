"""Point-in-time mapping from selected A-share indices to listed passive ETFs."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from backtest.domestic_defensive_etf import classify_defensive_etf
from backtest.passive_etf_online_selector import (
    learned_online_feature_weights,
    load_observations,
)
from backtest.passive_etf_boosted_selector import (
    BOOSTED_ETF_POLICIES,
    policy_by_name as boosted_policy_by_name,
    select_boosted_etfs,
)
from backtest.passive_etf_supervised_selector import (
    ENRICHED_V2_DATASET,
    FUNDAMENTAL_V3_DATASET,
    CONSTITUENT_V4_DATASET,
    SHARE_V5_DATASET,
    SUPERVISED_ETF_POLICIES,
    load_candidate_observations,
    policy_by_name as supervised_policy_by_name,
    select_static_stable_combo,
    select_static_stable_combo_top3,
    select_weighted_stable_combo_top3,
    select_weighted_stable_combo_top1,
    select_weighted_stable_combo_v2_top1,
    select_weighted_stable_combo_v3_top1,
    select_weighted_stable_combo_v4_top1,
    select_weighted_stable_combo_v5_top1,
    select_weighted_stable_combo_v6_top1,
    select_weighted_stable_combo_v7_top1,
    select_weighted_stable_combo_v9_top1,
    select_weighted_stable_combo_v10_top1,
    weighted_stable_combo_v2_scores,
    weighted_stable_combo_v3_scores,
    weighted_stable_combo_v4_scores,
    weighted_stable_combo_v5_scores,
    weighted_stable_combo_v6_scores,
    weighted_stable_combo_v7_scores,
    weighted_stable_combo_v9_scores,
    weighted_stable_combo_v10_scores,
    select_supervised_etfs,
)


OVERSEAS_CODE_PREFIXES = ("513", "517", "520")
OVERSEAS_KEYWORDS = (
    "港股",
    "沪港深",
    "恒生",
    "纳指",
    "标普",
    "日经",
    "德国",
    "法国",
    "美国",
    "中概",
    "海外",
    "全球",
    "东南亚",
    "沙特",
)
EARLY_BROAD_PROXY_CODES = (
    "510050.SH",  # SSE 50 ETF
    "510180.SH",  # SSE 180 ETF
    "159901.SZ",  # SZSE 100 ETF
    "159902.SZ",  # SME ETF
    "510880.SH",  # Dividend ETF
)
MAX_ETF_PRICE_STALENESS_DAYS = 14


def has_recent_etf_price(
    series: dict[str, list[tuple[date, float]]],
    code: str,
    snapshot: date,
    max_staleness_days: int = MAX_ETF_PRICE_STALENESS_DAYS,
) -> bool:
    """Return whether an ETF has a genuinely current point-in-time quote.

    A non-empty lifetime series is insufficient: historical import gaps used
    to make an ETF look like a zero-return, zero-volatility asset because the
    last old price was carried forward.  Month-end selection therefore
    requires a recent observable quote.
    """

    values = series.get(code) or []
    index = bisect_right(values, (snapshot, float("inf")))
    return bool(
        index
        and 0 <= (snapshot - values[index - 1][0]).days <= max_staleness_days
    )


@dataclass(frozen=True)
class EquityEtfMeta:
    code: str
    name: str
    index_code: str
    index_name: str
    list_date: date
    first_trade_date: date


@dataclass(frozen=True)
class DirectEtfSelectorPolicy:
    name: str
    top_n: int
    momentum_12m_weight: float
    momentum_6m_weight: float
    momentum_3m_weight: float
    low_volatility_weight: float
    score_power: float = 1.0
    low_beta_6m_weight: float = 0.0
    distance_high_12m_weight: float = 0.0
    low_volatility_1m_weight: float = 0.0
    deduplicate_tracking_index: bool = False
    direct_blend_weight: float = 1.0
    strong_trend_blend_weight: float | None = None
    strong_trend_return_3m_threshold: float = 0.08
    strong_trend_return_6m_threshold: float = 0.0
    strong_trend_ma_6m_distance_threshold: float = 0.0
    strong_trend_basket_drawdown_6m_threshold: float = -0.05


DIRECT_ETF_POLICIES = (
    DirectEtfSelectorPolicy("direct_momentum_top1", 1, 0.20, 0.45, 0.25, 0.10, 1.0),
    DirectEtfSelectorPolicy("direct_momentum_top3", 3, 0.20, 0.45, 0.25, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_momentum_top5", 5, 0.20, 0.45, 0.25, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_trend_top3", 3, 0.10, 0.35, 0.40, 0.15, 2.0),
    DirectEtfSelectorPolicy("direct_reversal_top3", 3, 0.30, -0.15, -0.45, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_reversal_top5", 5, 0.30, -0.15, -0.45, 0.10, 2.0),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top1", 1, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_momentum_top3", 3, 0.20, 0.0, 0.0, 0.0, 2.0,
        0.30, 0.25, 0.25, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_momentum_top5", 5, 0.20, 0.0, 0.0, 0.0, 2.0,
        0.30, 0.25, 0.25, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_regime_ic_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_regime_ic_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_online_ic_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_online_ic_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            policy.name, policy.top_n, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True,
        )
        for policy in SUPERVISED_ETF_POLICIES
    ),
    *(
        DirectEtfSelectorPolicy(
            policy.name, policy.top_n, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True,
        )
        for policy in BOOSTED_ETF_POLICIES
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_boost_rank_all_price_h120_q20_dd2_e16_top3_w{int(weight*100)}",
            3, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.125, 0.25, 0.50)
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_combo_beta_autocorr_vol3_top1",
        1,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_combo_beta_distance_autocorr_vol3_top3",
        3,
        0.0,
        0.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_static_top1_w{int(weight*100)}",
            1,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            True,
            weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75)
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_static_top3_w{int(weight*100)}",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            2.0,
            0.0,
            0.0,
            0.0,
            True,
            weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_top3",
        3, 0.0, 0.0, 0.0, 0.0, 4.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_top3_w{int(weight*100)}",
            3, 0.0, 0.0, 0.0, 0.0, 4.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v2_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v2_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v3_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v3_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v4_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v4_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v5_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v6_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v6_top1_regime_w49_s71",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.71,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v7_flow010_top1_regime_w49_s75",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.75,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v7_flow050_top1_regime_w49_s75",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.75,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v10_b050_v500_roe100_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v10_b050_v500_roe075_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v5_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (
            0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325,
            0.35, 0.40, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50,
            0.60, 0.70, 0.80, 0.90,
        )
    ),
    *(
        DirectEtfSelectorPolicy(
            (
                f"blend_index_weighted_stable_v5_top1_regime"
                f"_w{int(base_weight*100)}_s{int(strong_weight*100)}"
            ),
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, base_weight, strong_weight,
        )
        for base_weight, strong_weight in (
            (0.25, 0.60), (0.25, 0.70), (0.25, 0.80),
            (0.35, 0.60), (0.35, 0.70), (0.35, 0.80),
            (0.40, 0.60), (0.40, 0.70), (0.40, 0.80),
            (0.49, 0.60), (0.49, 0.65), (0.49, 0.70),
            (0.49, 0.71), (0.49, 0.72), (0.49, 0.75), (0.49, 0.76),
            (0.49, 0.77), (0.49, 0.78), (0.49, 0.79), (0.49, 0.80),
            (0.49, 0.81), (0.49, 0.82), (0.49, 0.83), (0.49, 0.84),
            (0.49, 0.85), (0.49, 0.86), (0.49, 0.87), (0.49, 0.88),
            (0.49, 0.89), (0.49, 0.90),
            (0.49, 0.92), (0.49, 0.94), (0.49, 0.96), (0.49, 0.98),
            (0.49, 1.00),
        )
    ),
)

_DIRECT_SELECTION_CACHE: dict[
    tuple[int, int, int, str, date], dict[str, float]
] = {}
_DIRECT_DIAGNOSTICS_CACHE: dict[
    tuple[int, int, str, date], dict[str, float]
] = {}
_INDEX_MAPPING_CACHE: dict[tuple[object, ...], dict[str, float]] = {}

SELECTED_ETF_DIAGNOSTIC_FIELDS = {
    "momentum_1m": "selected_etf_momentum_1m",
    "momentum_3m": "selected_etf_momentum_3m",
    "momentum_6m": "selected_etf_momentum_6m",
    "volatility_1m": "selected_etf_volatility_1m",
    "volatility_3m": "selected_etf_volatility_3m",
    "volatility_6m": "selected_etf_volatility_6m",
    "downside_volatility_3m": "selected_etf_downside_volatility_3m",
    "market_beta_6m": "selected_etf_market_beta_6m",
    "max_drawdown_6m": "selected_etf_max_drawdown_6m",
    "momentum_12m": "selected_etf_momentum_12m",
    "momentum_12m_skip1m": "selected_etf_momentum_12m_skip1m",
    "relative_strength_3m": "selected_etf_relative_strength_3m",
    "relative_strength_6m": "selected_etf_relative_strength_6m",
    "drawdown_3m": "selected_etf_drawdown_3m",
    "drawdown_6m": "selected_etf_drawdown_6m",
    "ulcer_index_6m": "selected_etf_ulcer_index_6m",
    "amount_acceleration_1m_6m": "selected_etf_amount_acceleration_1m_6m",
    "amount_crowding_percentile_3y": "selected_etf_amount_crowding_percentile_3y",
    "historical_cvar_5pct_3m": "selected_etf_historical_cvar_5pct_3m",
    "maximum_daily_loss_3m": "selected_etf_maximum_daily_loss_3m",
    "negative_day_ratio_3m": "selected_etf_negative_day_ratio_3m",
    "days_since_high_6m": "selected_etf_days_since_high_6m",
    "volatility_acceleration_1m_3m": "selected_etf_volatility_acceleration_1m_3m",
}


def selected_score_diagnostics(
    scores: dict[str, float],
    observations: list[dict[str, Any]],
    eligible: set[str],
    snapshot: date,
) -> dict[str, float]:
    """Describe the actual top-scored ETF using only snapshot-known fields."""

    ordered = sorted(
        (
            (str(code), float(score))
            for code, score in scores.items()
            if str(code) in eligible and math.isfinite(float(score))
        ),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )
    output = {
        "selector_score_top1": ordered[0][1] if ordered else 0.0,
        "selector_score_margin": (
            ordered[0][1] - ordered[1][1] if len(ordered) >= 2 else 0.0
        ),
        "selector_score_dispersion": (
            statistics.pstdev(score for _code, score in ordered)
            if len(ordered) >= 2
            else 0.0
        ),
        "selector_score_candidate_count": float(len(ordered)),
    }
    if not ordered:
        return output
    selected_code = ordered[0][0]
    selected_row = next(
        (
            row
            for row in observations
            if str(row.get("ts_code")) == selected_code
            and date.fromisoformat(str(row["snapshot"])) == snapshot
        ),
        None,
    )
    if selected_row is None:
        return output
    for source, target in SELECTED_ETF_DIAGNOSTIC_FIELDS.items():
        raw = selected_row.get(source)
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            output[target] = float(raw)
    return output


def direct_selector_diagnostics(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    policy: DirectEtfSelectorPolicy,
) -> dict[str, float]:
    """Point-in-time score concentration for the static v2/v3 selectors."""

    cache_key = (id(metas_by_index), id(series), policy.name, snapshot)
    cached = _DIRECT_DIAGNOSTICS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    if "weighted_stable_v10_b050_v500" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        roe_weight = 0.75 if "roe075" in policy.name else 1.0
        scores = weighted_stable_combo_v10_scores(
            observations, snapshot, roe_weight
        )
    elif "weighted_stable_v9_roe050" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v9_scores(observations, snapshot)
    elif "weighted_stable_v7_flow" in policy.name:
        observations = load_candidate_observations(SHARE_V5_DATASET)
        flow_weight = 0.10 if "flow010" in policy.name else 0.50
        scores = weighted_stable_combo_v7_scores(
            observations, snapshot, flow_weight
        )
    elif "weighted_stable_v6_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v6_scores(observations, snapshot)
    elif "weighted_stable_v5_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v5_scores(observations, snapshot)
    elif "weighted_stable_v4_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v4_scores(observations, snapshot)
    elif "weighted_stable_v3_top1" in policy.name:
        observations = load_candidate_observations(FUNDAMENTAL_V3_DATASET)
        scores = weighted_stable_combo_v3_scores(observations, snapshot)
    elif "weighted_stable_v2_top1" in policy.name:
        observations = load_candidate_observations(ENRICHED_V2_DATASET)
        scores = weighted_stable_combo_v2_scores(observations, snapshot)
    else:
        return {}
    eligible = {
        meta.code
        for values in metas_by_index.values()
        for meta in values
        if meta.list_date <= snapshot
        and meta.first_trade_date <= snapshot
        and has_recent_etf_price(series, meta.code, snapshot)
    }
    output = selected_score_diagnostics(scores, observations, eligible, snapshot)
    _DIRECT_DIAGNOSTICS_CACHE[cache_key] = dict(output)
    return output


def _percentile(values: dict[str, float], *, higher_is_better: bool = True) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(ordered) - 1, 1)
    ranks = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
    return ranks if higher_is_better else {code: 1.0 - rank for code, rank in ranks.items()}


def direct_blend_share(
    policy: DirectEtfSelectorPolicy,
    market_state: dict[str, object],
) -> float:
    """Choose a point-in-time blend share once per three-month holding window."""

    strong_weight = policy.strong_trend_blend_weight
    if strong_weight is None:
        return float(policy.direct_blend_weight)
    required = (
        "cs300_return_3m",
        "cs300_return_6m",
        "cs300_ma_6m_distance",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return float(policy.direct_blend_weight)
    strong_trend = (
        float(market_state["cs300_return_3m"])
        >= policy.strong_trend_return_3m_threshold
        and float(market_state["cs300_return_6m"])
        >= policy.strong_trend_return_6m_threshold
        and float(market_state["cs300_ma_6m_distance"])
        >= policy.strong_trend_ma_6m_distance_threshold
        and float(market_state["basket_drawdown_6m"])
        >= policy.strong_trend_basket_drawdown_6m_threshold
    )
    return float(strong_weight if strong_trend else policy.direct_blend_weight)


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def select_direct_equity_etfs(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    policy: DirectEtfSelectorPolicy,
    benchmark_series: list[tuple[date, float]] | None = None,
) -> dict[str, float]:
    cache_key = (
        id(metas_by_index),
        id(series),
        id(benchmark_series),
        policy.name,
        snapshot,
    )
    cached = _DIRECT_SELECTION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    metas = {
        meta.code: meta
        for values in metas_by_index.values()
        for meta in values
        if has_recent_etf_price(series, meta.code, snapshot)
    }
    if policy.name == "direct_stable_combo_beta_autocorr_vol3_top1" or policy.name.startswith(
        "blend_index_static_top1_"
    ):
        weights = select_static_stable_combo(load_candidate_observations(), snapshot)
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_stable_combo_beta_distance_autocorr_vol3_top3" or policy.name.startswith(
        "blend_index_static_top3_"
    ):
        weights = select_static_stable_combo_top3(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_supervised_"):
        weights = select_supervised_etfs(
            load_candidate_observations(),
            snapshot,
            supervised_policy_by_name(policy.name),
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_top3" or policy.name.startswith(
        "blend_index_weighted_stable_top3_"
    ):
        weights = select_weighted_stable_combo_top3(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_top1" or policy.name.startswith(
        "blend_index_weighted_stable_top1_"
    ):
        weights = select_weighted_stable_combo_top1(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v2_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v2_top1_"
    ):
        weights = select_weighted_stable_combo_v2_top1(
            load_candidate_observations(ENRICHED_V2_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v3_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v3_top1_"
    ):
        weights = select_weighted_stable_combo_v3_top1(
            load_candidate_observations(FUNDAMENTAL_V3_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v4_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v4_top1_"
    ):
        weights = select_weighted_stable_combo_v4_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v10_b050_v500"):
        roe_weight = 0.75 if "roe075" in policy.name else 1.0
        weights = select_weighted_stable_combo_v10_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET),
            snapshot,
            roe_weight,
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v9_roe050"):
        weights = select_weighted_stable_combo_v9_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v5_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v5_top1_"
    ):
        weights = select_weighted_stable_combo_v5_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v7_flow"):
        flow_weight = 0.10 if "flow010" in policy.name else 0.50
        weights = select_weighted_stable_combo_v7_top1(
            load_candidate_observations(SHARE_V5_DATASET), snapshot, flow_weight
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v6_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v6_top1_"
    ):
        weights = select_weighted_stable_combo_v6_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_boost_rank_") or policy.name.startswith(
        "blend_index_boost_rank_"
    ):
        weights = select_boosted_etfs(
            load_candidate_observations(), snapshot, boosted_policy_by_name(policy.name)
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    benchmark_returns: dict[date, float] = {}
    benchmark_end = 0
    if benchmark_series:
        benchmark_end = bisect_right(benchmark_series, (snapshot, math.inf))
        for previous, current in zip(
            benchmark_series[max(0, benchmark_end - 127) : benchmark_end - 1],
            benchmark_series[max(0, benchmark_end - 126) : benchmark_end],
        ):
            if previous[1] > 0:
                benchmark_returns[current[0]] = current[1] / previous[1] - 1.0
    metrics: dict[str, dict[str, float]] = {}
    for code, meta in metas.items():
        if meta.list_date > snapshot or meta.first_trade_date > snapshot:
            continue
        rows = series.get(code, [])
        end = bisect_right(rows, (snapshot, math.inf))
        if end < 253:
            continue
        prices = [value for _day, value in rows[:end]]
        if min(prices[-253:]) <= 0:
            continue
        daily_returns_6m = [
            prices[index] / prices[index - 1] - 1.0
            for index in range(end - 126, end)
        ]
        daily_returns = daily_returns_6m[-63:]
        recent_rows = rows[end - 127 : end]
        asset_by_day = {
            current[0]: current[1] / previous[1] - 1.0
            for previous, current in zip(recent_rows[:-1], recent_rows[1:])
            if previous[1] > 0
        }
        common_days = sorted(set(asset_by_day) & set(benchmark_returns))
        beta = 1.0
        market_correlation = 0.0
        market_return_6m = 0.0
        market_return_3m = 0.0
        if len(common_days) >= 60:
            asset_values = [asset_by_day[day] for day in common_days]
            benchmark_values = [benchmark_returns[day] for day in common_days]
            asset_mean = statistics.mean(asset_values)
            benchmark_mean = statistics.mean(benchmark_values)
            variance = statistics.pvariance(benchmark_values)
            if variance > 0:
                beta = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / variance
            asset_std = statistics.pstdev(asset_values)
            market_std = statistics.pstdev(benchmark_values)
            if asset_std > 0 and market_std > 0:
                market_correlation = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / asset_std / market_std
            market_return_6m = math.prod(1.0 + value for value in benchmark_values) - 1.0
            market_return_3m = math.prod(1.0 + value for value in benchmark_values[-63:]) - 1.0
        metrics[code] = {
            "m1": prices[-1] / prices[-22] - 1.0,
            "m12": prices[-1] / prices[-253] - 1.0,
            "m12_skip1": prices[-22] / prices[-253] - 1.0,
            "m6": prices[-1] / prices[-127] - 1.0,
            "m3": prices[-1] / prices[-64] - 1.0,
            "low_vol": statistics.pstdev(daily_returns) * math.sqrt(252.0),
            "low_vol_1m": statistics.pstdev(daily_returns[-21:]) * math.sqrt(252.0),
            "low_beta_6m": beta,
            "distance_high_12m": prices[-1] / max(prices[-252:]) - 1.0,
            "drawdown_3m": prices[-1] / max(prices[-63:]) - 1.0,
            "drawdown_6m": prices[-1] / max(prices[-126:]) - 1.0,
            "max_drawdown_6m": _max_drawdown(prices[-126:]),
            "positive_day_ratio_3m": sum(value > 0 for value in daily_returns) / 63.0,
            "market_correlation_6m": market_correlation,
            "relative_strength_3m": prices[-1] / prices[-64] - 1.0 - market_return_3m,
            "residual_momentum_6m": prices[-1] / prices[-127] - 1.0 - beta * market_return_6m,
        }
        metrics[code].update(
            {
                "momentum_1m": metrics[code]["m1"],
                "momentum_3m": metrics[code]["m3"],
                "momentum_12m": metrics[code]["m12"],
                "momentum_12m_skip1m": metrics[code]["m12_skip1"],
                "volatility_1m": metrics[code]["low_vol_1m"],
                "volatility_3m": metrics[code]["low_vol"],
            }
        )
    if policy.deduplicate_tracking_index:
        kept: dict[str, str] = {}
        for code in sorted(metrics):
            meta = metas[code]
            current = kept.get(meta.index_code)
            if current is None or (
                meta.list_date,
                meta.first_trade_date,
                code,
            ) < (
                metas[current].list_date,
                metas[current].first_trade_date,
                current,
            ):
                kept[meta.index_code] = code
        metrics = {code: metrics[code] for code in kept.values()}
    if not metrics:
        _DIRECT_SELECTION_CACHE[cache_key] = {}
        return {}
    benchmark_values = [
        value
        for _day, value in (benchmark_series or [])[max(0, benchmark_end - 127) : benchmark_end]
    ]
    benchmark_return_6m = (
        benchmark_values[-1] / benchmark_values[-127] - 1.0
        if len(benchmark_values) >= 127
        else 0.0
    )
    current_regime = (
        "bull" if benchmark_return_6m >= 0.10 else "bear" if benchmark_return_6m <= -0.10 else "neutral"
    )
    if policy.name.startswith("direct_online_ic_"):
        learned = learned_online_feature_weights(
            load_observations(),
            snapshot,
            current_regime,
        )
        learned = {
            feature: item
            for feature, item in learned.items()
            if all(feature in values for values in metrics.values())
        }
        if not learned:
            _DIRECT_SELECTION_CACHE[cache_key] = {}
            return {}
        online_ranks = {
            feature: _percentile(
                {code: item[feature] for code, item in metrics.items()}
            )
            for feature in learned
        }
        total_weight = sum(item["weight"] for item in learned.values())
        scores = {}
        for code in metrics:
            score = 0.0
            for feature, item in learned.items():
                rank = online_ranks[feature][code]
                score += item["weight"] * (
                    rank if item["orientation"] > 0 else 1.0 - rank
                )
            scores[code] = score / total_weight
        selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
        powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
        total = sum(powered.values())
        weights = {code: value / total for code, value in powered.items()}
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_regime_ic_"):
        regime = current_regime
        regime_fields = {
            "bull": (
                ("max_drawdown_6m", 0.20, True),
                ("drawdown_6m", 0.15, True),
                ("drawdown_3m", 0.10, True),
                ("residual_momentum_6m", 0.15, True),
                ("low_vol", 0.10, False),
                ("low_beta_6m", 0.10, False),
                ("distance_high_12m", 0.10, True),
                ("positive_day_ratio_3m", 0.10, True),
            ),
            "neutral": (
                ("low_beta_6m", 0.25, False),
                ("m12_skip1", 0.20, True),
                ("low_vol_1m", 0.15, False),
                ("m12", 0.15, True),
                ("low_vol", 0.10, False),
                ("drawdown_6m", 0.10, False),
                ("distance_high_12m", 0.05, True),
            ),
            "bear": (
                ("low_vol", 0.20, False),
                ("positive_day_ratio_3m", 0.15, False),
                ("market_correlation_6m", 0.15, True),
                ("relative_strength_3m", 0.10, False),
                ("m1", 0.10, False),
                ("low_vol_1m", 0.10, False),
                ("residual_momentum_6m", 0.10, False),
                ("distance_high_12m", 0.10, True),
            ),
        }[regime]
        regime_ranks = {
            field: _percentile(
                {code: item[field] for code, item in metrics.items()},
                higher_is_better=higher,
            )
            for field, _weight, higher in regime_fields
        }
        scores = {
            code: sum(
                weight * regime_ranks[field][code]
                for field, weight, _higher in regime_fields
            )
            for code in metrics
        }
        selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
        powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
        total = sum(powered.values())
        weights = {code: value / total for code, value in powered.items()}
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    ranks = {
        "m12": _percentile({code: item["m12"] for code, item in metrics.items()}),
        "m6": _percentile({code: item["m6"] for code, item in metrics.items()}),
        "m3": _percentile({code: item["m3"] for code, item in metrics.items()}),
        "low_vol": _percentile(
            {code: item["low_vol"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "low_vol_1m": _percentile(
            {code: item["low_vol_1m"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "low_beta_6m": _percentile(
            {code: item["low_beta_6m"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "distance_high_12m": _percentile(
            {code: item["distance_high_12m"] for code, item in metrics.items()}
        ),
    }
    scores = {
        code: (
            abs(policy.momentum_12m_weight)
            * (ranks["m12"][code] if policy.momentum_12m_weight >= 0 else 1.0 - ranks["m12"][code])
            + abs(policy.momentum_6m_weight)
            * (ranks["m6"][code] if policy.momentum_6m_weight >= 0 else 1.0 - ranks["m6"][code])
            + abs(policy.momentum_3m_weight)
            * (ranks["m3"][code] if policy.momentum_3m_weight >= 0 else 1.0 - ranks["m3"][code])
            + abs(policy.low_volatility_weight)
            * (
                ranks["low_vol"][code]
                if policy.low_volatility_weight >= 0
                else 1.0 - ranks["low_vol"][code]
            )
            + policy.low_beta_6m_weight * ranks["low_beta_6m"][code]
            + policy.distance_high_12m_weight * ranks["distance_high_12m"][code]
            + policy.low_volatility_1m_weight * ranks["low_vol_1m"][code]
        )
        for code in metrics
    }
    selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
    powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
    total = sum(powered.values())
    weights = {code: value / total for code, value in powered.items()}
    _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
    return weights


def _is_overseas(code: str, name: str, index_name: str, index_code: str) -> bool:
    text = f"{code} {name} {index_name} {index_code}"
    return (
        code.startswith(OVERSEAS_CODE_PREFIXES)
        or any(keyword in text for keyword in OVERSEAS_KEYWORDS)
        or index_code.endswith((".HI", ".OTH"))
    )


def _return_map(
    rows: list[tuple[date, float]],
    snapshot: date,
    observations: int,
) -> dict[date, float]:
    end = bisect_right(rows, (snapshot, math.inf))
    window = rows[max(0, end - observations - 1) : end]
    return {
        current[0]: current[1] / previous[1] - 1.0
        for previous, current in zip(window[:-1], window[1:])
        if previous[1] > 0 and (current[0] - previous[0]).days <= 14
    }


def blended_etf_diagnostics(
    weights: dict[str, float],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    benchmark_series: list[tuple[date, float]] | None = None,
    *,
    metas_by_index: dict[str, list[EquityEtfMeta]] | None = None,
    index_series: dict[str, list[tuple[date, float]]] | None = None,
) -> dict[str, float]:
    """Point-in-time risk metrics for the ETF basket that will actually be held."""

    positive = {
        code: float(weight)
        for code, weight in weights.items()
        if weight > 0 and series.get(code)
    }
    total = sum(positive.values())
    if total <= 0:
        return {}
    normalized = {code: weight / total for code, weight in positive.items()}
    code_to_index = {
        meta.code: meta.index_code
        for metas in (metas_by_index or {}).values()
        for meta in metas
    }
    return_maps: dict[str, dict[date, float]] = {}
    for code in normalized:
        actual = _return_map(series[code], snapshot, 252)
        index_code = code_to_index.get(code)
        proxy = (
            _return_map(index_series[index_code], snapshot, 252)
            if index_series is not None
            and index_code is not None
            and index_series.get(index_code)
            else {}
        )
        # The tracker is the point-in-time exposure proxy before ETF listing;
        # actual ETF returns always win on dates where both are available.
        return_maps[code] = {**proxy, **actual}
    # A small newly listed sleeve must not erase the history of the complete
    # basket.  Build each historical return from the weights observable on
    # that day, while requiring at least 80% of today's target weight to have
    # a valid one-day return.  The previous all-series intersection made the
    # 6m volatility disappear whenever any selected ETF had <126 observations.
    all_dates = sorted(set().union(*(set(values) for values in return_maps.values())))
    basket_return_map: dict[date, float] = {}
    for day in all_dates:
        available = [
            (normalized[code], returns[day])
            for code, returns in return_maps.items()
            if day in returns
        ]
        covered_weight = sum(weight for weight, _value in available)
        if covered_weight >= 0.80 - 1e-12:
            basket_return_map[day] = (
                sum(weight * value for weight, value in available) / covered_weight
            )
    ordered_dates = sorted(basket_return_map)
    basket_returns = [basket_return_map[day] for day in ordered_dates]
    if not basket_returns:
        return {}

    output: dict[str, float] = {}

    def volatility(observations: int) -> float | None:
        if len(basket_returns) < observations:
            return None
        return statistics.pstdev(basket_returns[-observations:]) * math.sqrt(252.0)

    for observations, field in (
        (21, "selected_etf_volatility_1m"),
        (63, "selected_etf_volatility_3m"),
        (126, "selected_etf_volatility_6m"),
    ):
        value = volatility(observations)
        if value is not None:
            output[field] = value

    for observations, field in (
        (21, "selected_etf_momentum_1m"),
        (63, "selected_etf_momentum_3m"),
        (126, "selected_etf_momentum_6m"),
    ):
        if len(basket_returns) >= observations:
            output[field] = (
                math.prod(1.0 + value for value in basket_returns[-observations:])
                - 1.0
            )

    for observations, field in (
        (63, "selected_etf_drawdown_3m"),
        (126, "selected_etf_drawdown_6m"),
    ):
        if len(basket_returns) >= observations:
            factors = [1.0]
            for value in basket_returns[-observations:]:
                factors.append(factors[-1] * (1.0 + value))
            output[field] = factors[-1] / max(factors) - 1.0

    if len(basket_returns) >= 63:
        recent_3m = basket_returns[-63:]
        downside = [min(value, 0.0) for value in recent_3m]
        output["selected_etf_downside_volatility_3m"] = (
            math.sqrt(statistics.mean(value * value for value in downside))
            * math.sqrt(252.0)
        )
        tail_count = max(1, int(math.ceil(len(recent_3m) * 0.05)))
        output["selected_etf_historical_cvar_5pct_3m"] = statistics.mean(
            sorted(recent_3m)[:tail_count]
        )
        output["selected_etf_maximum_daily_loss_3m"] = min(recent_3m)
        output["selected_etf_negative_day_ratio_3m"] = (
            sum(value < 0.0 for value in recent_3m) / len(recent_3m)
        )
        output["selected_etf_positive_day_ratio_3m"] = (
            sum(value > 0.0 for value in recent_3m) / len(recent_3m)
        )
        vol_1m = output.get("selected_etf_volatility_1m")
        vol_3m = output.get("selected_etf_volatility_3m")
        if vol_1m is not None and vol_3m is not None and vol_3m > 0.0:
            output["selected_etf_volatility_acceleration_1m_3m"] = (
                vol_1m / vol_3m - 1.0
            )
    if len(basket_returns) >= 126:
        factors = [1.0]
        for value in basket_returns[-126:]:
            factors.append(factors[-1] * (1.0 + value))
        peak = factors[0]
        drawdowns = []
        for value in factors:
            peak = max(peak, value)
            drawdowns.append(value / peak - 1.0)
        output["selected_etf_max_drawdown_6m"] = min(drawdowns)
        output["selected_etf_ulcer_index_6m"] = math.sqrt(
            statistics.mean(value * value for value in drawdowns)
        )
        high = max(factors)
        output["selected_etf_days_since_high_6m"] = float(
            next(
                (
                    offset
                    for offset, value in enumerate(reversed(factors))
                    if value >= high * (1.0 - 1e-12)
                ),
                len(factors) - 1,
            )
        )
    if len(basket_returns) >= 252:
        output["selected_etf_momentum_12m"] = (
            math.prod(1.0 + value for value in basket_returns[-252:]) - 1.0
        )
        output["selected_etf_momentum_12m_skip1m"] = (
            math.prod(1.0 + value for value in basket_returns[-252:-21]) - 1.0
        )

    if benchmark_series and len(basket_returns) >= 126:
        benchmark = _return_map(benchmark_series, snapshot, 126)
        paired_dates = [day for day in ordered_dates[-126:] if day in benchmark]
        if len(paired_dates) >= 60:
            asset_values = [basket_return_map[day] for day in paired_dates]
            benchmark_values = [benchmark[day] for day in paired_dates]
            variance = statistics.pvariance(benchmark_values)
            if variance > 0:
                asset_mean = statistics.mean(asset_values)
                benchmark_mean = statistics.mean(benchmark_values)
                output["selected_etf_market_beta_6m"] = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / variance
    return output


def _correlation(left: dict[date, float], right: dict[date, float]) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < 20:
        return None
    xs = [left[day] for day in common]
    ys = [right[day] for day in common]
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx <= 0 or sy <= 0:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    return statistics.mean((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx / sy


def load_equity_etf_return_universe(
    conn,
) -> tuple[dict[str, list[EquityEtfMeta]], dict[str, list[tuple[date, float]]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name,
                   e.list_date, MIN(f.trade_date)
            FROM passive_etf e
            JOIN fund_daily f ON f.ts_code=e.ts_code
            WHERE e.index_ts_code IS NOT NULL
              AND e.list_date IS NOT NULL
              AND (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
              AND e.ts_code NOT LIKE '%%.OF'
              AND f.close IS NOT NULL
            GROUP BY e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
            ORDER BY e.index_ts_code, e.list_date, e.ts_code
            """
        )
        metas_by_index: dict[str, list[EquityEtfMeta]] = {}
        for code, name, index_code, index_name, list_date, first_trade_date in cur.fetchall():
            code = str(code)
            name = str(name or code)
            index_code = str(index_code)
            index_name = str(index_name or index_code)
            if _is_overseas(code, name, index_name, index_code):
                continue
            # Domestic commodity-index ETFs remain valid passive-index risk
            # candidates. Only bond and spot-gold benchmark ETFs are reserved
            # exclusively for the defensive sleeve.
            defensive_category = classify_defensive_etf(code, name, index_name)
            if defensive_category in {"bond", "gold"}:
                continue
            meta = EquityEtfMeta(
                code=code,
                name=name,
                index_code=index_code,
                index_name=index_name,
                list_date=list_date,
                first_trade_date=first_trade_date,
            )
            metas_by_index.setdefault(index_code, []).append(meta)

        codes = sorted(meta.code for metas in metas_by_index.values() for meta in metas)
        series: dict[str, list[tuple[date, float]]] = {code: [] for code in codes}
        for start in range(0, len(codes), 300):
            chunk = codes[start : start + 300]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, pct_chg
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            cumulative = {code: 100.0 for code in chunk}
            for code, trade_date, pct_chg in cur.fetchall():
                code = str(code)
                if series[code] and pct_chg is not None:
                    cumulative[code] *= 1.0 + float(pct_chg) / 100.0
                series[code].append((trade_date, cumulative[code]))
    return metas_by_index, series


def map_indices_to_etfs(
    index_weights: dict[str, float],
    snapshot: date,
    metas_by_index: dict[str, list[EquityEtfMeta]],
    *,
    etf_series: dict[str, list[tuple[date, float]]] | None = None,
    allow_early_broad_proxy: bool = False,
    allow_correlation_proxy: bool = False,
    index_series: dict[str, list[tuple[date, float]]] | None = None,
    correlation_lookback: int = 252,
    minimum_correlation: float = 0.30,
) -> dict[str, float]:
    cache_key = (
        id(metas_by_index),
        id(etf_series),
        id(index_series),
        snapshot,
        tuple(sorted((str(code), float(weight)) for code, weight in index_weights.items())),
        allow_early_broad_proxy,
        allow_correlation_proxy,
        correlation_lookback,
        minimum_correlation,
    )
    cached = _INDEX_MAPPING_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    all_metas = [meta for metas in metas_by_index.values() for meta in metas]
    etf_weights: dict[str, float] = {}
    for index_code, weight in index_weights.items():
        candidates = [
            meta
            for meta in metas_by_index.get(index_code, [])
            if meta.list_date <= snapshot and meta.first_trade_date <= snapshot
            and (etf_series is None or has_recent_etf_price(etf_series, meta.code, snapshot))
        ]
        if not candidates:
            if allow_correlation_proxy and index_series is not None:
                target_returns = _return_map(
                    index_series.get(index_code, []),
                    snapshot,
                    correlation_lookback,
                )
                proxy_candidates = []
                for tracked_index, tracker_metas in metas_by_index.items():
                    live = [
                        meta
                        for meta in tracker_metas
                        if meta.list_date <= snapshot and meta.first_trade_date <= snapshot
                        and (
                            etf_series is None
                            or has_recent_etf_price(etf_series, meta.code, snapshot)
                        )
                    ]
                    if not live:
                        continue
                    tracked_returns = _return_map(
                        index_series.get(tracked_index, []),
                        snapshot,
                        correlation_lookback,
                    )
                    value = _correlation(target_returns, tracked_returns)
                    if value is None or value < minimum_correlation:
                        continue
                    tracker = min(
                        live,
                        key=lambda meta: (meta.list_date, meta.first_trade_date, meta.code),
                    )
                    proxy_candidates.append((value, tracker))
                if proxy_candidates:
                    best_value = max(item[0] for item in proxy_candidates)
                    candidates = [
                        tracker
                        for value, tracker in proxy_candidates
                        if abs(value - best_value) <= 1e-12
                    ]
            if candidates:
                pass
            elif allow_early_broad_proxy:
                candidates = [
                    meta
                    for meta in all_metas
                    if meta.code in EARLY_BROAD_PROXY_CODES
                    and meta.list_date <= snapshot
                    and meta.first_trade_date <= snapshot
                    and (
                        etf_series is None
                        or has_recent_etf_price(etf_series, meta.code, snapshot)
                    )
                ]
            if not candidates:
                raise RuntimeError(f"no point-in-time passive ETF for {index_code} at {snapshot}")
        # The oldest live tracker avoids selecting a fund using future liquidity information.
        selected = min(
            candidates,
            key=lambda meta: (
                EARLY_BROAD_PROXY_CODES.index(meta.code)
                if meta.code in EARLY_BROAD_PROXY_CODES
                else -1,
                meta.list_date,
                meta.first_trade_date,
                meta.code,
            ),
        )
        etf_weights[selected.code] = etf_weights.get(selected.code, 0.0) + float(weight)
    _INDEX_MAPPING_CACHE[cache_key] = dict(etf_weights)
    return etf_weights


def portfolio_turnover(previous: dict[str, float], current: dict[str, float]) -> float:
    codes = set(previous) | set(current)
    risky_l1 = sum(abs(current.get(code, 0.0) - previous.get(code, 0.0)) for code in codes)
    previous_cash = max(0.0, 1.0 - sum(previous.values()))
    current_cash = max(0.0, 1.0 - sum(current.values()))
    return 0.5 * (risky_l1 + abs(current_cash - previous_cash))


def describe_equity_universe(metas_by_index: dict[str, list[EquityEtfMeta]]) -> dict[str, object]:
    metas = [meta for values in metas_by_index.values() for meta in values]
    return {
        "index_count": len(metas_by_index),
        "etf_count": len(metas),
        "first_list_date": min((meta.list_date for meta in metas), default=None).isoformat() if metas else None,
    }

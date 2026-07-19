"""Point-in-time CSI selector for arbitrary calendar-neutral snapshots."""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Sequence

from backtest.phase_features import (
    DatedSeries,
    PHASE_FEATURE_STORE,
    moving_average_distance,
    realized_volatility,
    rolling_drawdown,
    trailing_return,
)
from backtest.phase_schedule import shift_month_end


def geometric_trend_acceleration(
    momentum_3m: float | None,
    momentum_6m: float | None,
) -> float | None:
    """Latest-quarter return minus the compounded 6m quarterly equivalent."""

    if momentum_3m is None or momentum_6m is None or momentum_6m <= -1.0:
        return None
    return momentum_3m - ((1.0 + momentum_6m) ** 0.5 - 1.0)


@dataclass(frozen=True)
class SelectorPolicy:
    name: str
    top_n: int
    annual_score_weight: float
    momentum_12m_weight: float
    momentum_6m_weight: float
    momentum_3m_weight: float
    low_volatility_weight: float
    shallow_drawdown_weight: float
    trend_weight: float
    positive_month_ratio_weight: float = 0.0
    calmar_weight: float = 0.0
    drawdown_12m_weight: float = 0.0
    pe_history_percentile_weight: float = 0.0
    pb_history_percentile_weight: float = 0.0
    turnover_crowding_weight: float = 0.0
    low_turnover_acceleration_weight: float = 0.0
    impute_missing_weighted_features: bool = False
    score_power: float = 1.0
    risk_adjusted_momentum_12m_weight: float = 0.0
    max_weight: float = 1.0


MOMENTUM_12M_TOP10 = SelectorPolicy(
    "momentum12_top10",
    10,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
)
SAVED_REGIME_MOMENTUM_HYBRID = SelectorPolicy(
    "saved_regime_momentum_hybrid",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
)
ANNUAL_SCORE_TOP1 = SelectorPolicy(
    "annual_score_top1",
    1,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
)
ANNUAL_SCORE_TOP3 = SelectorPolicy(
    "annual_score_top3",
    3,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    score_power=2.0,
)
ANNUAL_SCORE_TREND_TOP3 = SelectorPolicy(
    "annual_score_trend_top3",
    3,
    0.50,
    0.0,
    0.15,
    0.10,
    0.05,
    0.0,
    0.20,
    score_power=2.0,
)
ANNUAL25_TREND_TOP3 = SelectorPolicy(
    "annual25_trend_top3",
    3,
    0.25,
    0.25,
    0.15,
    0.10,
    0.05,
    0.05,
    0.15,
    score_power=2.0,
)
BALANCED_TOP10 = SelectorPolicy(
    "annual_momentum_quality_top10",
    10,
    0.15,
    0.30,
    0.20,
    0.10,
    0.10,
    0.05,
    0.10,
)
BALANCED_TOP5 = SelectorPolicy(
    "annual_momentum_quality_top5",
    5,
    0.15,
    0.30,
    0.20,
    0.10,
    0.10,
    0.05,
    0.10,
)
DUAL_MOMENTUM_TOP5 = SelectorPolicy(
    "dual_momentum_top5",
    5,
    0.0,
    0.50,
    0.30,
    0.10,
    0.05,
    0.0,
    0.05,
)
ROBUST_TREND_TOP10 = SelectorPolicy(
    "robust_trend_top10",
    10,
    0.0,
    0.0,
    0.25,
    0.15,
    0.0,
    0.0,
    0.30,
    positive_month_ratio_weight=0.15,
    calmar_weight=0.05,
    drawdown_12m_weight=0.10,
)
ROBUST_TREND_TOP5 = SelectorPolicy(
    "robust_trend_top5",
    5,
    0.0,
    0.0,
    0.25,
    0.15,
    0.0,
    0.0,
    0.30,
    positive_month_ratio_weight=0.15,
    calmar_weight=0.05,
    drawdown_12m_weight=0.10,
)
ROBUST_TREND_TOP3 = SelectorPolicy(
    "robust_trend_top3",
    3,
    0.0,
    0.0,
    0.25,
    0.15,
    0.0,
    0.0,
    0.30,
    positive_month_ratio_weight=0.15,
    calmar_weight=0.05,
    drawdown_12m_weight=0.10,
)
MOMENTUM_TREND_TOP3 = SelectorPolicy(
    "momentum_trend_top3",
    3,
    0.0,
    0.0,
    0.35,
    0.20,
    0.0,
    0.0,
    0.30,
    positive_month_ratio_weight=0.15,
)
MOMENTUM_TREND_TOP1 = SelectorPolicy(
    "momentum_trend_top1",
    1,
    0.0,
    0.0,
    0.35,
    0.20,
    0.0,
    0.0,
    0.30,
    positive_month_ratio_weight=0.15,
)
VALUATION_CROWDING_TREND_TOP5 = SelectorPolicy(
    "valuation_crowding_trend_top5",
    5,
    0.0,
    0.0,
    0.10,
    0.05,
    0.0,
    0.0,
    0.15,
    positive_month_ratio_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.15,
    low_turnover_acceleration_weight=0.10,
)
VALUATION_CROWDING_TREND_TOP10 = SelectorPolicy(
    "valuation_crowding_trend_top10",
    10,
    0.0,
    0.0,
    0.10,
    0.05,
    0.0,
    0.0,
    0.15,
    positive_month_ratio_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.15,
    low_turnover_acceleration_weight=0.10,
)
VALUATION_CROWDING_TREND_TOP5_IMPUTED = SelectorPolicy(
    "valuation_crowding_trend_top5_imputed",
    5,
    0.0,
    0.0,
    0.10,
    0.05,
    0.0,
    0.0,
    0.15,
    positive_month_ratio_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.15,
    low_turnover_acceleration_weight=0.10,
    impute_missing_weighted_features=True,
)
VALUATION_CROWDING_TREND_TOP10_IMPUTED = SelectorPolicy(
    "valuation_crowding_trend_top10_imputed",
    10,
    0.0,
    0.0,
    0.10,
    0.05,
    0.0,
    0.0,
    0.15,
    positive_month_ratio_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.15,
    low_turnover_acceleration_weight=0.10,
    impute_missing_weighted_features=True,
)
EXPANDED_RISK_CONTROL_TOP10 = SelectorPolicy(
    "expanded_risk_control_top10",
    10,
    0.0,
    0.0,
    0.10,
    0.0,
    0.15,
    0.10,
    0.15,
    positive_month_ratio_weight=0.10,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.10,
    pb_history_percentile_weight=0.10,
    turnover_crowding_weight=0.05,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
)
EXPANDED_RISK_CONTROL_TOP5 = SelectorPolicy(
    "expanded_risk_control_top5",
    5,
    0.0,
    0.0,
    0.10,
    0.0,
    0.15,
    0.10,
    0.15,
    positive_month_ratio_weight=0.10,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.10,
    pb_history_percentile_weight=0.10,
    turnover_crowding_weight=0.05,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
)
EXPANDED_LOW_VOL_TOP10 = SelectorPolicy(
    "expanded_low_vol_top10",
    10,
    0.0,
    0.0,
    0.10,
    0.0,
    0.25,
    0.15,
    0.15,
    positive_month_ratio_weight=0.10,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.05,
    pb_history_percentile_weight=0.05,
    turnover_crowding_weight=0.025,
    low_turnover_acceleration_weight=0.025,
    impute_missing_weighted_features=True,
)
EXPANDED_VALUE_RISK_TOP10 = SelectorPolicy(
    "expanded_value_risk_top10",
    10,
    0.0,
    0.0,
    0.05,
    0.0,
    0.10,
    0.05,
    0.15,
    positive_month_ratio_weight=0.05,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.10,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
)
EXPANDED_VALUE_RISK_TOP7 = SelectorPolicy(
    "expanded_value_risk_top7",
    7,
    0.0,
    0.0,
    0.05,
    0.0,
    0.10,
    0.05,
    0.15,
    positive_month_ratio_weight=0.05,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.10,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
)
EXPANDED_VALUE_RISK_TOP5 = SelectorPolicy(
    "expanded_value_risk_top5",
    5,
    0.0,
    0.0,
    0.05,
    0.0,
    0.10,
    0.05,
    0.15,
    positive_month_ratio_weight=0.05,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.10,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
)
EXPANDED_VALUE_RISK_TOP7_POWER2 = SelectorPolicy(
    "expanded_value_risk_top7_power2",
    7,
    0.0,
    0.0,
    0.05,
    0.0,
    0.10,
    0.05,
    0.15,
    positive_month_ratio_weight=0.05,
    calmar_weight=0.10,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.20,
    turnover_crowding_weight=0.10,
    low_turnover_acceleration_weight=0.05,
    impute_missing_weighted_features=True,
    score_power=2.0,
)
EXPANDED_VALUE_RISK_NEIGHBOR_POLICIES = (
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top6_power2",
        top_n=6,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top8_power2",
        top_n=8,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power15",
        score_power=1.5,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power25",
        score_power=2.5,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power225",
        score_power=2.25,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power275",
        score_power=2.75,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power3",
        score_power=3.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power35",
        score_power=3.5,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power4",
        score_power=4.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power5",
        score_power=5.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power6",
        score_power=6.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power8",
        score_power=8.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power7",
        score_power=7.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power9",
        score_power=9.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power10",
        score_power=10.0,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power8_cap40",
        score_power=8.0,
        max_weight=0.40,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power8_cap50",
        score_power=8.0,
        max_weight=0.50,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power8_cap45",
        score_power=8.0,
        max_weight=0.45,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power8_cap60",
        score_power=8.0,
        max_weight=0.60,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power7_cap40",
        score_power=7.0,
        max_weight=0.40,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power7_cap45",
        score_power=7.0,
        max_weight=0.45,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power7_cap50",
        score_power=7.0,
        max_weight=0.50,
    ),
    replace(
        EXPANDED_VALUE_RISK_TOP7_POWER2,
        name="expanded_value_risk_top7_power7_cap55",
        score_power=7.0,
        max_weight=0.55,
    ),
)

VALUATION_CROWDING_CONVICTION_TOP5 = SelectorPolicy(
    "valuation_crowding_conviction_top5",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.10,
    positive_month_ratio_weight=0.10,
    pe_history_percentile_weight=0.20,
    pb_history_percentile_weight=0.30,
    turnover_crowding_weight=0.20,
    low_turnover_acceleration_weight=0.10,
)
TREND_PERSISTENCE_TOP10 = SelectorPolicy(
    "trend_persistence_top10",
    10,
    0.0,
    0.0,
    0.20,
    0.10,
    0.0,
    0.0,
    0.45,
    positive_month_ratio_weight=0.25,
)
MONTHLY_TREND6_TOP7 = SelectorPolicy(
    "monthly_trend6_top7",
    7,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)
MONTHLY_TREND6_TOP10 = SelectorPolicy(
    "monthly_trend6_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)
MONTHLY_TREND6_RISK_TOP10 = SelectorPolicy(
    "monthly_trend6_risk_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.20,
    0.10,
    0.70,
)
MONTHLY_TREND6_RISK80_TOP10 = SelectorPolicy(
    "monthly_trend6_risk80_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.10,
    0.10,
    0.80,
)
MONTHLY_TREND6_RISK75_TOP10 = SelectorPolicy(
    "monthly_trend6_risk75_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.15,
    0.10,
    0.75,
)
MONTHLY_TREND6_RISK60_TOP10 = SelectorPolicy(
    "monthly_trend6_risk60_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.30,
    0.10,
    0.60,
)
MONTHLY_TREND6_VALUE_TOP10 = SelectorPolicy(
    "monthly_trend6_value_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.70,
    pe_history_percentile_weight=0.15,
    pb_history_percentile_weight=0.15,
    impute_missing_weighted_features=True,
)
IC_STABLE_QUARTERLY_TOP5 = SelectorPolicy(
    "ic_stable_quarterly_top5",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=0.15,
    turnover_crowding_weight=0.25,
    low_turnover_acceleration_weight=0.35,
    impute_missing_weighted_features=True,
    score_power=2.0,
    risk_adjusted_momentum_12m_weight=0.25,
)
IC_STABLE_QUARTERLY_TOP10 = SelectorPolicy(
    "ic_stable_quarterly_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=0.15,
    turnover_crowding_weight=0.25,
    low_turnover_acceleration_weight=0.35,
    impute_missing_weighted_features=True,
    score_power=2.0,
    risk_adjusted_momentum_12m_weight=0.25,
)
# Low-degree stable-feature policies selected only after requiring positive
# fixed-rule edges in each of the three historical eras.  Names encode raw
# integer weights for acceleration/crowding/risk-adjusted momentum/Calmar.
CSI_STABLE4_A1C1R3M2_TOP5_P4 = SelectorPolicy(
    "csi_stable4_a1c1r3m2_top5_p4",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=2.0,
    turnover_crowding_weight=1.0,
    low_turnover_acceleration_weight=1.0,
    impute_missing_weighted_features=True,
    score_power=4.0,
    risk_adjusted_momentum_12m_weight=3.0,
)
CSI_STABLE4_A3C2R1M2_TOP5_P4 = SelectorPolicy(
    "csi_stable4_a3c2r1m2_top5_p4",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=2.0,
    turnover_crowding_weight=2.0,
    low_turnover_acceleration_weight=3.0,
    impute_missing_weighted_features=True,
    score_power=4.0,
    risk_adjusted_momentum_12m_weight=1.0,
)
CSI_STABLE4_A4C2R2M1_TOP5_P4 = SelectorPolicy(
    "csi_stable4_a4c2r2m1_top5_p4",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=1.0,
    turnover_crowding_weight=2.0,
    low_turnover_acceleration_weight=4.0,
    impute_missing_weighted_features=True,
    score_power=4.0,
    risk_adjusted_momentum_12m_weight=2.0,
)
CSI_STABLE4_A4C2R1M2_TOP3_P4 = SelectorPolicy(
    "csi_stable4_a4c2r1m2_top3_p4",
    3,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    calmar_weight=2.0,
    turnover_crowding_weight=2.0,
    low_turnover_acceleration_weight=4.0,
    impute_missing_weighted_features=True,
    score_power=4.0,
    risk_adjusted_momentum_12m_weight=1.0,
)
REGIME_IC_ADAPTIVE_TOP5 = SelectorPolicy(
    "regime_ic_adaptive_top5",
    5,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    score_power=2.0,
)
REGIME_IC_ADAPTIVE_TOP10 = SelectorPolicy(
    "regime_ic_adaptive_top10",
    10,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    score_power=2.0,
)
REGIME_ADAPTIVE_FIELDS = {
    "bull": (
        ("pe_ttm_history_percentile_3y", 0.25, True),
        ("calmar_12m", 0.20, False),
        ("turnover_crowding_percentile_3y", 0.20, False),
        ("stable_trend_6m", 0.15, True),
        ("drawdown_12m", 0.10, True),
        ("turnover_acceleration_1m_6m", 0.10, False),
    ),
    "neutral": (
        ("pb_history_percentile_3y", 0.25, True),
        ("turnover_crowding_percentile_3y", 0.20, True),
        ("pe_ttm_history_percentile_3y", 0.15, True),
        ("momentum_12m", 0.15, True),
        ("turnover_acceleration_1m_6m", 0.10, False),
        ("risk_adjusted_momentum_12m", 0.10, True),
        ("calmar_12m", 0.05, True),
    ),
    "bear": (
        ("turnover_acceleration_1m_6m", 0.20, False),
        ("stable_trend_6m", 0.15, False),
        ("positive_month_ratio_12m", 0.15, True),
        ("momentum_12m_skip1m", 0.15, True),
        ("calmar_12m", 0.10, True),
        ("risk_adjusted_trend_6m", 0.10, False),
        ("momentum_3m", 0.10, False),
        ("pe_ttm_history_percentile_3y", 0.05, True),
    ),
}
SELECTOR_POLICIES = [
    SAVED_REGIME_MOMENTUM_HYBRID,
    ANNUAL_SCORE_TOP1,
    ANNUAL_SCORE_TOP3,
    ANNUAL_SCORE_TREND_TOP3,
    ANNUAL25_TREND_TOP3,
    MOMENTUM_12M_TOP10,
    BALANCED_TOP10,
    BALANCED_TOP5,
    DUAL_MOMENTUM_TOP5,
    ROBUST_TREND_TOP10,
    ROBUST_TREND_TOP5,
    ROBUST_TREND_TOP3,
    MOMENTUM_TREND_TOP3,
    MOMENTUM_TREND_TOP1,
    VALUATION_CROWDING_TREND_TOP5,
    VALUATION_CROWDING_TREND_TOP10,
    VALUATION_CROWDING_TREND_TOP5_IMPUTED,
    VALUATION_CROWDING_TREND_TOP10_IMPUTED,
    EXPANDED_RISK_CONTROL_TOP10,
    EXPANDED_RISK_CONTROL_TOP5,
    EXPANDED_LOW_VOL_TOP10,
    EXPANDED_VALUE_RISK_TOP10,
    EXPANDED_VALUE_RISK_TOP7,
    EXPANDED_VALUE_RISK_TOP5,
    EXPANDED_VALUE_RISK_TOP7_POWER2,
    *EXPANDED_VALUE_RISK_NEIGHBOR_POLICIES,
    VALUATION_CROWDING_CONVICTION_TOP5,
    TREND_PERSISTENCE_TOP10,
    MONTHLY_TREND6_TOP7,
    MONTHLY_TREND6_TOP10,
    MONTHLY_TREND6_RISK_TOP10,
    MONTHLY_TREND6_RISK80_TOP10,
    MONTHLY_TREND6_RISK75_TOP10,
    MONTHLY_TREND6_RISK60_TOP10,
    MONTHLY_TREND6_VALUE_TOP10,
    IC_STABLE_QUARTERLY_TOP5,
    IC_STABLE_QUARTERLY_TOP10,
    CSI_STABLE4_A1C1R3M2_TOP5_P4,
    CSI_STABLE4_A3C2R1M2_TOP5_P4,
    CSI_STABLE4_A4C2R2M1_TOP5_P4,
    CSI_STABLE4_A4C2R1M2_TOP3_P4,
    REGIME_IC_ADAPTIVE_TOP5,
    REGIME_IC_ADAPTIVE_TOP10,
]


def percentile_ranks(values: dict[str, float], *, higher_is_better: bool = True) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(ordered) - 1, 1)
    ranks = {code: rank / denominator for rank, (code, _value) in enumerate(ordered)}
    if not higher_is_better:
        ranks = {code: 1.0 - value for code, value in ranks.items()}
    return ranks


def capped_score_power_weights(
    selected: Sequence[dict[str, Any]],
    score_power: float,
    max_weight: float,
) -> dict[str, float]:
    """Power-weight selected scores, then water-fill under a feasible cap."""

    if not selected:
        return {}
    if max_weight <= 0.0:
        raise ValueError("max_weight must be positive")
    raw = {
        str(row["ts_code"]): max(float(row["selector_score"]), 0.01)
        ** score_power
        for row in selected
    }
    feasible_cap = max(min(float(max_weight), 1.0), 1.0 / len(raw))
    remaining_codes = set(raw)
    remaining_weight = 1.0
    output: dict[str, float] = {}
    while remaining_codes:
        denominator = sum(raw[code] for code in remaining_codes)
        tentative = {
            code: remaining_weight * raw[code] / denominator
            for code in remaining_codes
        }
        capped = [
            code
            for code, weight in tentative.items()
            if weight > feasible_cap + 1e-12
        ]
        if not capped:
            output.update(tentative)
            break
        for code in sorted(capped):
            output[code] = feasible_cap
            remaining_weight -= feasible_cap
            remaining_codes.remove(code)
    total = sum(output.values())
    return {code: weight / total for code, weight in output.items()}


def chunks(items: Sequence[str], size: int = 128) -> list[list[str]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


class SnapshotCSISelector:
    def __init__(self) -> None:
        self._recommendations: dict[date, dict[str, dict[str, Any]]] = {}
        self._eligible: dict[date, set[str]] = {}
        self._selection: dict[
            tuple[date, SelectorPolicy, frozenset[str] | None],
            list[dict[str, Any]],
        ] = {}
        self._features: dict[date, list[dict[str, Any]]] = {}
        self._dailybasic: dict[
            str,
            list[
                tuple[
                    date,
                    float | None,
                    float | None,
                    float | None,
                    float | None,
                ]
            ],
        ] = {}
        self._index_prices: dict[str, dict[date, float]] = {}
        self._constituent_fundamentals: dict[
            str,
            list[
                tuple[
                    date,
                    float | None,
                    float | None,
                    float | None,
                    float | None,
                    float | None,
                    float | None,
                ]
            ],
        ] = {}
        self._etf_trackers: dict[str, list[tuple[str, date]]] = {}
        self._etf_daily: dict[str, list[tuple[date, float, float]]] = {}
        self._etf_daily_dates: dict[str, list[date]] = {}
        self._etf_daily_loaded_until: dict[str, date] = {}
        self._dailybasic_dates: dict[str, list[date]] = {}

    def _preload_candidate_inputs(self, cur, index_codes: set[str], snapshot: date) -> None:
        missing_index = sorted(code for code in index_codes if code not in PHASE_FEATURE_STORE._index)
        for batch in chunks(missing_index):
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM index_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                batch,
            )
            grouped: dict[str, list[tuple[date, float]]] = {code: [] for code in batch}
            for code, day, close in cur.fetchall():
                grouped[str(code)].append((day, float(close)))
            for code, rows in grouped.items():
                PHASE_FEATURE_STORE._index[code] = DatedSeries.from_rows(rows)

        missing_dailybasic = sorted(code for code in index_codes if code not in self._dailybasic)
        for batch in chunks(missing_dailybasic):
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, pe_ttm, pb, turnover_rate_f
                FROM index_dailybasic
                WHERE ts_code IN ({placeholders})
                ORDER BY ts_code, trade_date
                """,
                batch,
            )
            basic_grouped: dict[str, list[tuple[date, Any, Any, Any]]] = {
                code: [] for code in batch
            }
            for code, day, pe, pb, turnover in cur.fetchall():
                basic_grouped[str(code)].append((day, pe, pb, turnover))
            for code, basic_rows in basic_grouped.items():
                index_series = PHASE_FEATURE_STORE._index.get(code)
                price_by_date = {
                    day: value
                    for day, value in zip(
                        index_series.dates if index_series is not None else (),
                        index_series.values if index_series is not None else (),
                    )
                }
                self._index_prices[code] = price_by_date
                self._dailybasic[code] = [
                    (
                        day,
                        float(pe) if pe is not None and float(pe) > 0 else None,
                        float(pb) if pb is not None and float(pb) > 0 else None,
                        float(turnover)
                        if turnover is not None and float(turnover) >= 0
                        else None,
                        price_by_date.get(day),
                    )
                    for day, pe, pb, turnover in basic_rows
                ]
                self._dailybasic_dates[code] = [row[0] for row in self._dailybasic[code]]

        missing_trackers = sorted(code for code in index_codes if code not in self._etf_trackers)
        for batch in chunks(missing_trackers):
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"""
                SELECT index_ts_code, ts_code, list_date
                FROM passive_etf
                WHERE index_ts_code IN ({placeholders})
                  AND list_date IS NOT NULL
                  AND (etf_type IS NULL OR etf_type!='QDII')
                  AND (is_enhanced IS NULL OR is_enhanced=0)
                  AND (ts_code LIKE '%%.SH' OR ts_code LIKE '%%.SZ')
                ORDER BY index_ts_code, list_date, ts_code
                """,
                batch,
            )
            grouped_trackers: dict[str, list[tuple[str, date]]] = {code: [] for code in batch}
            for index_code, etf_code, list_date in cur.fetchall():
                grouped_trackers[str(index_code)].append((str(etf_code), list_date))
            self._etf_trackers.update(grouped_trackers)

        etf_codes = sorted(
            {
                etf_code
                for code in index_codes
                for etf_code, _list_date in [
                    item
                    for item in self._etf_trackers.get(code, [])
                    if item[1] <= snapshot
                ][:3]
                if self._etf_daily_loaded_until.get(etf_code, date.min) < snapshot
            }
        )
        for batch in chunks(etf_codes, 64):
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, amount, pct_chg
                FROM fund_daily
                WHERE ts_code IN ({placeholders})
                  AND trade_date <= %s
                  AND amount IS NOT NULL AND amount>0
                ORDER BY ts_code, trade_date
                """,
                [*batch, snapshot],
            )
            grouped_daily: dict[str, list[tuple[date, float, float]]] = {
                code: [] for code in batch
            }
            for code, day, amount, pct_chg in cur.fetchall():
                grouped_daily[str(code)].append((day, float(amount), float(pct_chg or 0.0)))
            for code, rows in grouped_daily.items():
                self._etf_daily[code] = rows
                self._etf_daily_dates[code] = [row[0] for row in rows]
                self._etf_daily_loaded_until[code] = snapshot

    def _etf_flow_features(self, cur, index_code: str, snapshot: date) -> dict[str, float | None]:
        empty = {
            "etf_amount_acceleration_1m_6m": None,
            "etf_amount_crowding_percentile_3y": None,
            "etf_positive_turnover_pressure_1m": None,
        }
        if index_code not in self._etf_trackers:
            cur.execute(
                """
                SELECT ts_code, list_date
                FROM passive_etf
                WHERE index_ts_code=%s
                  AND list_date IS NOT NULL
                  AND (etf_type IS NULL OR etf_type!='QDII')
                  AND (is_enhanced IS NULL OR is_enhanced=0)
                  AND (ts_code LIKE '%%.SH' OR ts_code LIKE '%%.SZ')
                ORDER BY list_date, ts_code
                """,
                (index_code,),
            )
            self._etf_trackers[index_code] = [
                (str(code), list_date) for code, list_date in cur.fetchall()
            ]
        candidates = [
            (code, list_date)
            for code, list_date in self._etf_trackers[index_code]
            if list_date <= snapshot
        ]
        for code, _list_date in candidates:
            if code not in self._etf_daily:
                cur.execute(
                    """
                    SELECT trade_date, amount, pct_chg
                    FROM fund_daily
                    WHERE ts_code=%s AND amount IS NOT NULL AND amount>0
                    ORDER BY trade_date
                    """,
                    (code,),
                )
                self._etf_daily[code] = [
                    (day, float(amount), float(pct_chg or 0.0))
                    for day, amount, pct_chg in cur.fetchall()
                ]
                self._etf_daily_dates[code] = [row[0] for row in self._etf_daily[code]]
                self._etf_daily_loaded_until[code] = date.max
            rows = self._etf_daily[code]
            end = bisect_right(self._etf_daily_dates[code], snapshot)
            window = rows[max(0, end - 756) : end]
            if len(window) < 126:
                continue
            amounts = [row[1] for row in window]
            amount_1m = sum(amounts[-21:]) / 21.0
            amount_6m = sum(amounts[-126:]) / 126.0
            prefix_amounts = [0.0]
            for value in amounts:
                prefix_amounts.append(prefix_amounts[-1] + value)
            rolling_amount = [
                (prefix_amounts[index + 1] - prefix_amounts[index - 20]) / 21.0
                for index in range(20, len(amounts))
            ]
            recent = window[-21:]
            total_amount = sum(row[1] for row in recent)
            signed_amount = sum(
                row[1] * (1.0 if row[2] > 0 else -1.0 if row[2] < 0 else 0.0)
                for row in recent
            )
            return {
                "etf_amount_acceleration_1m_6m": (
                    amount_1m / amount_6m - 1.0 if amount_6m > 0 else None
                ),
                "etf_amount_crowding_percentile_3y": (
                    sum(value <= amount_1m for value in rolling_amount) / len(rolling_amount)
                    if rolling_amount
                    else None
                ),
                "etf_positive_turnover_pressure_1m": (
                    signed_amount / total_amount if total_amount > 0 else None
                ),
            }
        return empty

    def _dailybasic_features(self, cur, code: str, snapshot: date) -> dict[str, float | None]:
        if code not in self._dailybasic:
            cur.execute(
                """
                SELECT trade_date, pe_ttm, pb, turnover_rate_f
                FROM index_dailybasic
                WHERE ts_code=%s
                ORDER BY trade_date
                """,
                (code,),
            )
            basic_rows = cur.fetchall()
            if code not in self._index_prices:
                cur.execute(
                    """
                    SELECT trade_date, close
                    FROM index_daily
                    WHERE ts_code=%s AND close IS NOT NULL
                    ORDER BY trade_date
                    """,
                    (code,),
                )
                self._index_prices[code] = {
                    day: float(close)
                    for day, close in cur.fetchall()
                    if close is not None and float(close) > 0
                }
            price_by_date = self._index_prices[code]
            self._dailybasic[code] = [
                (
                    day,
                    float(pe) if pe is not None and float(pe) > 0 else None,
                    float(pb) if pb is not None and float(pb) > 0 else None,
                    float(turnover) if turnover is not None and float(turnover) >= 0 else None,
                    price_by_date.get(day),
                )
                for day, pe, pb, turnover in basic_rows
            ]
            self._dailybasic_dates[code] = [row[0] for row in self._dailybasic[code]]
        rows = self._dailybasic[code]
        end = bisect_right(self._dailybasic_dates[code], snapshot)
        window = rows[max(0, end - 756) : end]
        if len(window) < 126:
            return {
                "pe_ttm_history_percentile_3y": None,
                "pb_history_percentile_3y": None,
                "turnover_crowding_percentile_3y": None,
                "turnover_acceleration_1m_6m": None,
                "fundamental_earnings_yield": None,
                "fundamental_book_yield": None,
                "fundamental_roe_proxy": None,
                "fundamental_earnings_growth_3m": None,
                "fundamental_earnings_growth_6m": None,
                "fundamental_earnings_growth_12m": None,
                "fundamental_book_growth_6m": None,
                "fundamental_book_growth_12m": None,
                "fundamental_pe_change_3m": None,
                "fundamental_pe_change_6m": None,
                "fundamental_pb_change_3m": None,
                "fundamental_pb_change_6m": None,
            }

        def history_percentile(column: int) -> float | None:
            values = [float(row[column]) for row in window if row[column] is not None]
            if len(values) < 126:
                return None
            current = values[-1]
            return sum(value <= current for value in values) / len(values)

        turnover = [float(row[3]) for row in window if row[3] is not None]
        turnover_1m = sum(turnover[-21:]) / 21.0 if len(turnover) >= 21 else None
        turnover_6m = sum(turnover[-126:]) / 126.0 if len(turnover) >= 126 else None
        prefix_turnover = [0.0]
        for value in turnover:
            prefix_turnover.append(prefix_turnover[-1] + value)
        rolling_turnover = [
            (prefix_turnover[index + 1] - prefix_turnover[index - 20]) / 21.0
            for index in range(20, len(turnover))
        ]
        current = window[-1]

        def lagged_change(transform, observations: int) -> float | None:
            if len(window) <= observations:
                return None
            latest = transform(current)
            previous = transform(window[-1 - observations])
            return (
                latest / previous - 1.0
                if latest is not None and previous is not None and previous > 0
                else None
            )

        def earnings_proxy(row) -> float | None:
            return row[4] / row[1] if row[4] is not None and row[1] is not None else None

        def book_proxy(row) -> float | None:
            return row[4] / row[2] if row[4] is not None and row[2] is not None else None

        def pe_value(row) -> float | None:
            return row[1]

        def pb_value(row) -> float | None:
            return row[2]

        pe_current = current[1]
        pb_current = current[2]
        return {
            "pe_ttm_history_percentile_3y": history_percentile(1),
            "pb_history_percentile_3y": history_percentile(2),
            "turnover_crowding_percentile_3y": (
                sum(value <= turnover_1m for value in rolling_turnover) / len(rolling_turnover)
                if turnover_1m is not None and rolling_turnover
                else None
            ),
            "turnover_acceleration_1m_6m": (
                turnover_1m / turnover_6m - 1.0
                if turnover_1m is not None and turnover_6m is not None and turnover_6m > 0
                else None
            ),
            "fundamental_earnings_yield": (
                1.0 / pe_current if pe_current is not None else None
            ),
            "fundamental_book_yield": (
                1.0 / pb_current if pb_current is not None else None
            ),
            "fundamental_roe_proxy": (
                pb_current / pe_current
                if pb_current is not None and pe_current is not None
                else None
            ),
            "fundamental_earnings_growth_3m": lagged_change(earnings_proxy, 63),
            "fundamental_earnings_growth_6m": lagged_change(earnings_proxy, 126),
            "fundamental_earnings_growth_12m": lagged_change(earnings_proxy, 252),
            "fundamental_book_growth_6m": lagged_change(book_proxy, 126),
            "fundamental_book_growth_12m": lagged_change(book_proxy, 252),
            "fundamental_pe_change_3m": lagged_change(pe_value, 63),
            "fundamental_pe_change_6m": lagged_change(pe_value, 126),
            "fundamental_pb_change_3m": lagged_change(pb_value, 63),
            "fundamental_pb_change_6m": lagged_change(pb_value, 126),
        }

    def _constituent_fundamental_features(
        self, cur, code: str, snapshot: date
    ) -> dict[str, float | None]:
        names = (
            "constituent_earnings_yield",
            "constituent_book_yield",
            "constituent_roe_proxy",
            "constituent_dividend_yield",
            "constituent_positive_earnings_weight",
            "constituent_weight_hhi",
            "constituent_earnings_yield_change_12m",
            "constituent_roe_change_12m",
            "constituent_dividend_yield_change_12m",
            "constituent_positive_earnings_change_12m",
        )
        empty = {name: None for name in names}
        if code not in self._constituent_fundamentals:
            cur.execute(
                """
                SELECT trade_date, earnings_yield, book_yield, roe_proxy,
                       dividend_yield, positive_earnings_weight, weight_hhi
                FROM index_constituent_fundamental
                WHERE index_code=%s
                ORDER BY trade_date
                """,
                (code,),
            )
            self._constituent_fundamentals[code] = [
                (
                    day,
                    *(float(value) if value is not None else None for value in values),
                )
                for day, *values in cur.fetchall()
            ]
        rows = self._constituent_fundamentals[code]
        dates = [row[0] for row in rows]
        end = bisect_right(dates, snapshot)
        if end == 0:
            return empty
        current = rows[end - 1]
        prior_end = bisect_right(dates, shift_month_end(snapshot, -12))
        prior = rows[prior_end - 1] if prior_end > 0 else None

        def change(column: int) -> float | None:
            if prior is None or current[column] is None or prior[column] is None:
                return None
            return (
                current[column] / prior[column] - 1.0
                if prior[column] > 0
                else None
            )

        return {
            "constituent_earnings_yield": current[1],
            "constituent_book_yield": current[2],
            "constituent_roe_proxy": current[3],
            "constituent_dividend_yield": current[4],
            "constituent_positive_earnings_weight": current[5],
            "constituent_weight_hhi": current[6],
            "constituent_earnings_yield_change_12m": change(1),
            "constituent_roe_change_12m": change(3),
            "constituent_dividend_yield_change_12m": change(4),
            "constituent_positive_earnings_change_12m": change(5),
        }

    def recommendations(self, cur, snapshot: date) -> dict[str, dict[str, Any]]:
        if snapshot in self._recommendations:
            return self._recommendations[snapshot]
        cur.execute(
            "SELECT MAX(as_of_date) FROM csi_annual_recommendation WHERE as_of_date <= %s",
            (snapshot,),
        )
        row = cur.fetchone()
        as_of = row[0] if row else None
        if as_of is None:
            self._recommendations[snapshot] = {}
            return {}
        cur.execute(
            """
            SELECT ts_code, index_name, final_score, policy_score, news_score
            FROM csi_annual_recommendation
            WHERE as_of_date=%s
            """,
            (as_of,),
        )
        output = {
            str(code): {
                "ts_code": str(code),
                "index_name": str(name or code),
                "recommendation_as_of": as_of,
                "annual_score": float(final_score or 0.0),
                "policy_score": float(policy_score or 0.0),
                "news_score": float(news_score or 0.0),
            }
            for code, name, final_score, policy_score, news_score in cur.fetchall()
        }
        self._recommendations[snapshot] = output
        return output

    def eligible_index_codes(self, cur, snapshot: date) -> set[str]:
        if snapshot not in self._eligible:
            cur.execute(
                """
                SELECT DISTINCT e.index_ts_code
                FROM passive_etf e
                WHERE e.index_ts_code IS NOT NULL
                  AND e.list_date IS NOT NULL
                  AND e.list_date <= %s
                  AND (e.etf_type IS NULL OR e.etf_type != 'QDII')
                  AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
                  AND (e.ts_code LIKE '%%.SH' OR e.ts_code LIKE '%%.SZ')
                  AND e.ts_code NOT LIKE '513%%'
                  AND e.ts_code NOT LIKE '517%%'
                  AND e.ts_code NOT LIKE '520%%'
                  AND COALESCE(e.extname, '') NOT REGEXP
                      '港股|沪港深|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特'
                  AND COALESCE(e.index_name, '') NOT REGEXP
                      '港股|沪港深|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特'
                  AND EXISTS (
                      SELECT 1
                      FROM fund_daily f
                      WHERE f.ts_code=e.ts_code
                        AND f.trade_date <= %s
                        AND f.close IS NOT NULL
                  )
                """,
                (snapshot, snapshot),
            )
            self._eligible[snapshot] = {str(row[0]) for row in cur.fetchall() if row[0]}
        return self._eligible[snapshot]

    def _feature_row(self, cur, code: str, meta: dict[str, Any], snapshot: date) -> dict[str, Any] | None:
        values = PHASE_FEATURE_STORE.index_series(cur, code).trailing(
            snapshot,
            270,
            include_snapshot=True,
        )
        if len(values) < 253:
            return None
        momentum_12m = trailing_return(values, 252)
        momentum_6m = trailing_return(values, 126)
        momentum_3m = trailing_return(values, 63)
        volatility_3m = realized_volatility(values, 63)
        volatility_6m = realized_volatility(values, 126)
        drawdown_12m = rolling_drawdown(values, 252)
        trend_6m = moving_average_distance(values, 126)
        monthly_points = [values[-1 - offset] for offset in range(0, 253, 21)][::-1]
        monthly_returns = [cur / prev - 1.0 for prev, cur in zip(monthly_points, monthly_points[1:]) if prev > 0]
        positive_month_ratio = (
            sum(value > 0 for value in monthly_returns) / len(monthly_returns)
            if monthly_returns
            else None
        )
        return {
            **meta,
            **self._dailybasic_features(cur, code, snapshot),
            **self._constituent_fundamental_features(cur, code, snapshot),
            **self._etf_flow_features(cur, code, snapshot),
            "momentum_12m": momentum_12m,
            "momentum_6m": momentum_6m,
            "momentum_3m": momentum_3m,
            "momentum_1m": trailing_return(values, 21),
            "momentum_12m_skip1m": values[-22] / values[-253] - 1.0,
            "volatility_3m": volatility_3m,
            "volatility_6m": volatility_6m,
            "drawdown_6m": rolling_drawdown(values, 126),
            "drawdown_12m": drawdown_12m,
            "trend_6m": trend_6m,
            "trend_12m": moving_average_distance(values, 252),
            "positive_month_ratio_12m": positive_month_ratio,
            "risk_adjusted_trend_6m": (
                trend_6m / volatility_3m
                if trend_6m is not None and volatility_3m is not None and volatility_3m > 0
                else None
            ),
            "trend_consistency_3m_6m": (
                min(momentum_3m, momentum_6m)
                if momentum_3m is not None and momentum_6m is not None
                else None
            ),
            "trend_acceleration_3m_vs_6m": (
                momentum_3m - momentum_6m / 2.0
                if momentum_3m is not None and momentum_6m is not None
                else None
            ),
            # Compare the latest three-month return with the compounded
            # three-month equivalent of the six-month return.  The legacy
            # field above divided a simple six-month return by two, which
            # embeds a magnitude-dependent compounding bias even when the two
            # quarters have the same return.  Keep it for reproducibility and
            # expose the corrected point-in-time feature separately.
            "trend_acceleration_geometric_3m_vs_6m": (
                geometric_trend_acceleration(momentum_3m, momentum_6m)
            ),
            "stable_trend_6m": (
                trend_6m * positive_month_ratio / volatility_3m
                if trend_6m is not None
                and positive_month_ratio is not None
                and volatility_3m is not None
                and volatility_3m > 0
                else None
            ),
            "risk_adjusted_momentum_12m": (
                momentum_12m / volatility_6m
                if momentum_12m is not None and volatility_6m is not None and volatility_6m > 0
                else None
            ),
            "calmar_12m": (
                momentum_12m / abs(drawdown_12m)
                if momentum_12m is not None and drawdown_12m is not None and drawdown_12m < 0
                else momentum_12m
            ),
        }

    def candidate_rows(self, cur, snapshot: date) -> list[dict[str, Any]]:
        if snapshot in self._features:
            return self._features[snapshot]
        recommendations = self.recommendations(cur, snapshot)
        eligible = self.eligible_index_codes(cur, snapshot)
        self._preload_candidate_inputs(cur, eligible, snapshot)
        rows = []
        for code in sorted(eligible):
            meta = recommendations.get(
                code,
                {
                    "ts_code": code,
                    "index_name": code,
                    "recommendation_as_of": None,
                    "annual_score": 0.0,
                    "policy_score": 0.0,
                    "news_score": 0.0,
                },
            )
            row = self._feature_row(cur, code, meta, snapshot)
            if row is not None:
                rows.append(row)
        self._features[snapshot] = rows
        return rows

    def select(
        self,
        cur,
        snapshot: date,
        policy: SelectorPolicy,
        *,
        eligible_codes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        eligible_key = frozenset(eligible_codes) if eligible_codes is not None else None
        key = (snapshot, policy, eligible_key)
        if key in self._selection:
            return self._selection[key]
        if policy.name.startswith("regime_ic_adaptive_"):
            market_values = PHASE_FEATURE_STORE.index_series(cur, "000300.SH").trailing(
                snapshot,
                140,
                include_snapshot=True,
            )
            market_return_6m = (
                market_values[-1] / market_values[-127] - 1.0
                if len(market_values) >= 127 and market_values[-127] > 0
                else 0.0
            )
            regime = (
                "bull"
                if market_return_6m >= 0.10
                else "bear"
                if market_return_6m <= -0.10
                else "neutral"
            )
            adaptive_fields = REGIME_ADAPTIVE_FIELDS[regime]
            rows = [
                row.copy()
                for row in self.candidate_rows(cur, snapshot)
                if eligible_codes is None or row["ts_code"] in eligible_codes
            ]
            rank_maps = {
                field: percentile_ranks(
                    {
                        str(row["ts_code"]): float(row[field])
                        for row in rows
                        if row.get(field) is not None
                    },
                    higher_is_better=higher,
                )
                for field, _weight, higher in adaptive_fields
            }
            for row in rows:
                row["selector_score"] = sum(
                    weight * rank_maps[field].get(str(row["ts_code"]), 0.5)
                    for field, weight, _higher in adaptive_fields
                )
                row["selector_regime"] = regime
            selected = sorted(
                rows,
                key=lambda row: (-float(row["selector_score"]), str(row["ts_code"])),
            )[: policy.top_n]
            weights = capped_score_power_weights(
                selected, policy.score_power, policy.max_weight
            )
            for row in selected:
                row["weight"] = weights[str(row["ts_code"])]
            self._selection[key] = selected
            return selected
        fields = {
            "annual_score": (policy.annual_score_weight, True),
            "momentum_12m": (policy.momentum_12m_weight, True),
            "momentum_6m": (policy.momentum_6m_weight, True),
            "momentum_3m": (policy.momentum_3m_weight, True),
            "volatility_3m": (policy.low_volatility_weight, False),
            "drawdown_6m": (policy.shallow_drawdown_weight, True),
            "trend_6m": (policy.trend_weight, True),
            "positive_month_ratio_12m": (policy.positive_month_ratio_weight, True),
            "calmar_12m": (policy.calmar_weight, True),
            "drawdown_12m": (policy.drawdown_12m_weight, True),
            "pe_ttm_history_percentile_3y": (policy.pe_history_percentile_weight, True),
            "pb_history_percentile_3y": (policy.pb_history_percentile_weight, True),
            "turnover_crowding_percentile_3y": (policy.turnover_crowding_weight, True),
            "turnover_acceleration_1m_6m": (policy.low_turnover_acceleration_weight, False),
            "risk_adjusted_momentum_12m": (
                policy.risk_adjusted_momentum_12m_weight,
                True,
            ),
        }
        required = [field for field, (weight, _higher) in fields.items() if weight > 0]
        rows = [
            row.copy()
            for row in self.candidate_rows(cur, snapshot)
            if eligible_codes is None or row["ts_code"] in eligible_codes
            if all(
                row[field] is not None
                for field in (
                    "momentum_12m",
                    "momentum_6m",
                    "momentum_3m",
                    "volatility_3m",
                    "drawdown_6m",
                    "trend_6m",
                    *(() if policy.impute_missing_weighted_features else required),
                )
            )
        ]
        if not rows:
            self._selection[key] = []
            return []
        rank_maps = {
            field: percentile_ranks(
                {
                    row["ts_code"]: float(row[field])
                    for row in rows
                    if row[field] is not None
                },
                higher_is_better=higher,
            )
            for field, (weight, higher) in fields.items()
            if weight > 0
        }
        for row in rows:
            row["selector_score"] = sum(
                weight * rank_maps[field].get(row["ts_code"], 0.5)
                for field, (weight, _higher) in fields.items()
                if weight > 0
            )
        selected = sorted(rows, key=lambda row: (-row["selector_score"], row["ts_code"]))[: policy.top_n]
        weights = capped_score_power_weights(
            selected, policy.score_power, policy.max_weight
        )
        for row in selected:
            row["weight"] = weights[str(row["ts_code"])]
        self._selection[key] = selected
        return selected


SNAPSHOT_CSI_SELECTOR = SnapshotCSISelector()

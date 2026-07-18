"""Walk-forward monthly direction model using only completed prior months."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Any

import numpy as np


MONTHLY_DIRECTION_FEATURES = (
    "external_dxy_return_1m",
    "external_us_curve_10y2y",
    "external_dxy_drawdown_1m",
    "cs300_drawdown_3m",
    "breadth_return_3m_positive",
    "external_dxy_drawdown_6m",
    "market_overheat_flag",
    "crisis_continuation_flag",
)

RIDGE_DIRECTION_FEATURES = (
    "external_dxy_return_1m",
    "external_anfci_change_3m",
    "cs300_return_3m",
    "cs300_return_6m",
    "cs300_drawdown_3m",
    "cs300_ma_6m_distance",
    "cs300_vol_3m",
    "basket_return_1m",
    "basket_return_3m",
    "basket_ma_6m_distance",
    "basket_vol_3m",
    "breadth_return_3m_positive",
    "domestic_shibor_on_change_1m",
)

RIDGE_CORE_FEATURES = (
    "external_dxy_return_1m",
    "cs300_return_3m",
    "cs300_drawdown_3m",
    "cs300_ma_6m_distance",
    "basket_return_3m",
    "breadth_return_3m_positive",
)


@dataclass(frozen=True)
class MonthlyDirectionPolicy:
    name: str
    negative_score_lte: float
    negative_exposure_cap: float
    overheat_exposure_cap: float
    rebound_overheat_exposure_cap: float
    crisis_exposure_cap: float
    min_history: int = 36
    min_abs_correlation: float = 0.03
    features: tuple[str, ...] = MONTHLY_DIRECTION_FEATURES
    minimum_vote_count_for_cap: int = 1
    short_cycle_exposure_cap: float = 99.0
    nonnegative_exposure_multiplier: float = 1.0
    minimum_vote_count_for_boost: int = 2
    liquidity_stress_exposure_cap: float = 99.0
    boost_allowed_drawdown_gte: float = -1.0
    local_negative_dxy_positive_multiplier: float | None = None
    local_positive_dxy_negative_multiplier: float | None = None
    basket_volatility_target: float | None = None
    positive_score_exposure_floor: float = 0.0
    positive_floor_vote_feature: str | None = None
    model_type: str = "rank_vote"
    history_months: int | None = None
    ridge_alpha: float = 10.0
    target_clip: float = 0.15
    ridge_target_mode: str = "return"
    oracle_tail_threshold: float | None = None
    medium_cycle_exposure_cap: float = 99.0
    tightening_rebound_exposure_cap: float = 99.0
    mature_reversal_exposure_cap: float = 99.0
    rally_distribution_exposure_cap: float = 99.0
    financed_surge_exposure_cap: float = 99.0
    option_panic_exposure_cap: float = 99.0
    turnover_overheat_exposure_cap: float = 99.0
    daily_margin_rally_exposure_cap: float = 99.0
    low_vol_flat_exposure_cap: float = 99.0
    breadth_reversal_exposure_cap: float = 99.0
    leadership_collapse_exposure_cap: float = 99.0
    leverage_macro_exposure_cap: float = 99.0
    fund_distribution_exposure_cap: float = 99.0
    fund_saturation_exposure_cap: float = 99.0
    theme_divergence_3m_exposure_cap: float = 99.0
    theme_divergence_1m_exposure_cap: float = 99.0
    theme_divergence_1m_crowded_exposure_cap: float = 99.0
    credit_contraction_exposure_cap: float = 99.0
    macro_weak_rebound_exposure_cap: float = 99.0
    weak_credit_rebound_exposure_cap: float = 99.0
    fund_moderate_distribution_exposure_cap: float = 99.0
    binned_bins: int = 3
    binned_shrink_count: float = 8.0
    positive_score_gt: float = 0.0


TREND_ONLY = MonthlyDirectionPolicy("trend_only", -2.0, 99.0, 99.0, 99.0, 99.0)
WALKFORWARD_CAP0 = MonthlyDirectionPolicy("walkforward_cap0", -0.20, 0.0, 0.0, 99.0, 0.0)
WALKFORWARD_CAP15 = MonthlyDirectionPolicy("walkforward_cap15", -0.20, 0.15, 0.0, 99.0, 0.0)
WALKFORWARD_STRICT_CAP0 = MonthlyDirectionPolicy("walkforward_strict_cap0", -0.40, 0.0, 0.0, 99.0, 0.0)
WALKFORWARD_CAP0_OVERHEAT15 = MonthlyDirectionPolicy(
    "walkforward_cap0_overheat15",
    -0.20,
    0.0,
    0.15,
    0.15,
    0.0,
)
WALKFORWARD_CAP0_REBOUND25 = MonthlyDirectionPolicy(
    "walkforward_cap0_rebound25",
    -0.20,
    0.0,
    0.0,
    0.25,
    0.0,
)
DXY_ONLY_CAP0 = MonthlyDirectionPolicy(
    "dxy_only_cap0",
    -0.20,
    0.0,
    0.0,
    99.0,
    0.0,
    features=("external_dxy_return_1m",),
)
DXY_ONLY_CAP0_REBOUND25 = MonthlyDirectionPolicy(
    "dxy_only_cap0_rebound25",
    -0.20,
    0.0,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m",),
    short_cycle_exposure_cap=0.25,
)
DXY_ONLY_CAP15_REBOUND25 = MonthlyDirectionPolicy(
    "dxy_only_cap15_rebound25",
    -0.20,
    0.15,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m",),
    short_cycle_exposure_cap=0.25,
)
DXY_ONLY_CAP25_REBOUND25 = MonthlyDirectionPolicy(
    "dxy_only_cap25_rebound25",
    -0.20,
    0.25,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m",),
    short_cycle_exposure_cap=0.25,
)
DXY_LOCAL_TREND_CONFIRMED_CAP0 = MonthlyDirectionPolicy(
    "dxy_local_trend_confirmed_cap0",
    -0.20,
    0.0,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.25,
)
DXY_LOCAL_TREND_TAIL25_REBOUND50 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail25_rebound50",
    -0.20,
    0.0,
    0.25,
    0.50,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
)
DXY_LOCAL_TREND_TAIL0_SHORT50 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail0_short50",
    -0.20,
    0.0,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
)
DXY_LOCAL_TREND_TAIL15_SHORT50 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST125 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost125",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.25,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST125_LIQ25 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost125_liq25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.25,
    liquidity_stress_exposure_cap=0.25,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST140_LIQ25 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost140_liq25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.40,
    liquidity_stress_exposure_cap=0.25,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150_LIQ25 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost150_liq25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150_LIQ25_DD5 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost150_liq25_dd5",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_LIQ25_DD5 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost150_liq25_dd5",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_LOCAL_MEDIUM40_BOOST150 = MonthlyDirectionPolicy(
    "dxy_local_medium40_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    medium_cycle_exposure_cap=0.40,
)
DXY_LOCAL_MEDIUM25_BOOST150 = MonthlyDirectionPolicy(
    "dxy_local_medium25_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    medium_cycle_exposure_cap=0.25,
)
DXY_LOCAL_TIGHTENING25_BOOST150 = MonthlyDirectionPolicy(
    "dxy_local_tightening25_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
)
DXY_LOCAL_TIGHTENING40_BOOST150 = MonthlyDirectionPolicy(
    "dxy_local_tightening40_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.40,
)
DXY_LOCAL_TIGHTENING25_MATURE40 = MonthlyDirectionPolicy(
    "dxy_local_tightening25_mature40",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.40,
)
DXY_LOCAL_TIGHTENING25_MATURE25 = MonthlyDirectionPolicy(
    "dxy_local_tightening25_mature25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
)
DXY_LOCAL_MATURE25_DISTRIBUTION25 = MonthlyDirectionPolicy(
    "dxy_local_mature25_distribution25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
    rally_distribution_exposure_cap=0.25,
)
DXY_LOCAL_MATURE25_DISTRIBUTION25_FINANCED25 = MonthlyDirectionPolicy(
    "dxy_local_mature25_distribution25_financed25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
    rally_distribution_exposure_cap=0.25,
    financed_surge_exposure_cap=0.25,
)
DXY_LOCAL_MATURE25_DISTRIBUTION25_FINANCED25_OPTION25 = MonthlyDirectionPolicy(
    "dxy_local_mature25_distribution25_financed25_option25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
    rally_distribution_exposure_cap=0.25,
    financed_surge_exposure_cap=0.25,
    option_panic_exposure_cap=0.25,
)
DXY_LOCAL_MATURE25_OPTION25 = MonthlyDirectionPolicy(
    "dxy_local_mature25_option25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
    option_panic_exposure_cap=0.25,
)
DXY_LOCAL_MATURE25_FINANCED25_OPTION25 = MonthlyDirectionPolicy(
    "dxy_local_mature25_financed25_option25",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    tightening_rebound_exposure_cap=0.25,
    mature_reversal_exposure_cap=0.25,
    financed_surge_exposure_cap=0.25,
    option_panic_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_TURNOVER25 = replace(
    DXY_LOCAL_MATURE25_FINANCED25_OPTION25,
    name="dxy_local_option25_turnover25",
    turnover_overheat_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25 = replace(
    DXY_LOCAL_OPTION25_TURNOVER25,
    name="dxy_local_option25_turnover25_margin25",
    daily_margin_rally_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25_LOWVOL40 = replace(
    DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25,
    name="dxy_local_option25_turnover25_margin25_lowvol40",
    low_vol_flat_exposure_cap=0.40,
)
DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25_LOWVOL40_BREADTH25 = replace(
    DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25_LOWVOL40,
    name="dxy_local_option25_turnover25_margin25_lowvol40_breadth25",
    breadth_reversal_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_LOWVOL40 = replace(
    DXY_LOCAL_MATURE25_FINANCED25_OPTION25,
    name="dxy_local_option25_lowvol40",
    low_vol_flat_exposure_cap=0.40,
)
DXY_LOCAL_OPTION25_BREADTH25 = replace(
    DXY_LOCAL_MATURE25_FINANCED25_OPTION25,
    name="dxy_local_option25_breadth25",
    breadth_reversal_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25 = replace(
    DXY_LOCAL_OPTION25_LOWVOL40,
    name="dxy_local_option25_lowvol40_breadth25",
    breadth_reversal_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25 = replace(
    DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25,
    name="dxy_local_option25_short25_lowvol40_breadth25",
    short_cycle_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_SHORT25_LOWVOL25_BREADTH25 = replace(
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25,
    name="dxy_local_option25_short25_lowvol25_breadth25",
    low_vol_flat_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_SHORT25_LOWVOL25_BREADTH25_LEADERSHIP25 = replace(
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL25_BREADTH25,
    name="dxy_local_option25_short25_lowvol25_breadth25_leadership25",
    leadership_collapse_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25_LEADERSHIP25 = replace(
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25,
    name="dxy_local_option25_short25_lowvol40_breadth25_leadership25",
    leadership_collapse_exposure_cap=0.25,
)
DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25_LEADERSHIP25 = replace(
    DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25,
    name="dxy_local_option25_lowvol40_breadth25_leadership25",
    leadership_collapse_exposure_cap=0.25,
)
DXY_LOCAL_LEADERSHIP25_LEVERAGE25 = replace(
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25_LEADERSHIP25,
    name="dxy_local_leadership25_leverage25",
    leverage_macro_exposure_cap=0.25,
)
DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25 = replace(
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25,
    name="dxy_local_leadership25_leverage25_funddist25",
    fund_distribution_exposure_cap=0.25,
)
DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25_SAT25 = replace(
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25,
    name="dxy_local_leadership25_leverage25_funddist25_sat25",
    fund_saturation_exposure_cap=0.25,
)
DXY_LOCAL_SAT25_THEME3M25 = replace(
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25_SAT25,
    name="dxy_local_sat25_theme3m25",
    theme_divergence_3m_exposure_cap=0.25,
)
DXY_LOCAL_SAT25_THEME3M25_THEME1M40 = replace(
    DXY_LOCAL_SAT25_THEME3M25,
    name="dxy_local_sat25_theme3m25_theme1m40",
    theme_divergence_1m_exposure_cap=0.40,
)
DXY_LOCAL_SAT25_THEME3M25_THEME1M40_CREDIT25 = replace(
    DXY_LOCAL_SAT25_THEME3M25_THEME1M40,
    name="dxy_local_sat25_theme3m25_theme1m40_credit25",
    credit_contraction_exposure_cap=0.25,
)
DXY_LOCAL_SAT25_THEME3M25_THEME1M40_CREDIT25_MACRO40 = replace(
    DXY_LOCAL_SAT25_THEME3M25_THEME1M40_CREDIT25,
    name="dxy_local_sat25_theme3m25_theme1m40_credit25_macro40",
    macro_weak_rebound_exposure_cap=0.40,
)
DXY_LOCAL_SAT25_THEME3M25_CREDIT25 = replace(
    DXY_LOCAL_SAT25_THEME3M25,
    name="dxy_local_sat25_theme3m25_credit25",
    credit_contraction_exposure_cap=0.25,
)
DXY_LOCAL_SAT25_THEME3M25_MACRO40 = replace(
    DXY_LOCAL_SAT25_THEME3M25,
    name="dxy_local_sat25_theme3m25_macro40",
    macro_weak_rebound_exposure_cap=0.40,
)
DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40 = replace(
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25,
    name="dxy_local_sat25_theme3m25_credit25_macro40",
    macro_weak_rebound_exposure_cap=0.40,
)
DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40_THEME1C25 = replace(
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40,
    name="dxy_local_sat25_theme3m25_credit25_macro40_theme1c25",
    theme_divergence_1m_crowded_exposure_cap=0.25,
)
DXY_LOCAL_THEME1C25_WEAKCREDIT25 = replace(
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40_THEME1C25,
    name="dxy_local_theme1c25_weakcredit25",
    weak_credit_rebound_exposure_cap=0.25,
)
DXY_LOCAL_THEME1C25_FUND_DIST_MAX100 = replace(
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40_THEME1C25,
    name="dxy_local_theme1c25_fund_dist_max100",
    fund_moderate_distribution_exposure_cap=1.0,
)
RIDGE120_A10_BOOST150 = MonthlyDirectionPolicy(
    "ridge120_a10_boost150",
    -0.002,
    0.0,
    0.15,
    0.25,
    0.0,
    min_history=60,
    features=RIDGE_DIRECTION_FEATURES,
    minimum_vote_count_for_cap=5,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=5,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="ridge",
    history_months=120,
    ridge_alpha=10.0,
)
RIDGE120_A30_BOOST150 = MonthlyDirectionPolicy(
    "ridge120_a30_boost150",
    -0.002,
    0.0,
    0.15,
    0.25,
    0.0,
    min_history=60,
    features=RIDGE_DIRECTION_FEATURES,
    minimum_vote_count_for_cap=5,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=5,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="ridge",
    history_months=120,
    ridge_alpha=30.0,
)
RIDGE_CORE_A30_BOOST150 = MonthlyDirectionPolicy(
    "ridge_core_a30_boost150",
    -0.002,
    0.0,
    0.15,
    0.25,
    0.0,
    min_history=48,
    features=RIDGE_CORE_FEATURES,
    minimum_vote_count_for_cap=5,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=5,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="ridge",
    history_months=120,
    ridge_alpha=30.0,
)
RIDGE_CORE_SIGN_A30_BOOST150 = MonthlyDirectionPolicy(
    "ridge_core_sign_a30_boost150",
    -0.05,
    0.0,
    0.15,
    0.25,
    0.0,
    min_history=48,
    features=RIDGE_CORE_FEATURES,
    minimum_vote_count_for_cap=5,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=5,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="ridge",
    history_months=120,
    ridge_alpha=30.0,
    ridge_target_mode="sign",
)
DIAGNOSTIC_ORACLE_DIRECTION = MonthlyDirectionPolicy(
    "diagnostic_oracle_direction",
    -0.001,
    0.0,
    0.15,
    0.25,
    0.0,
    min_history=0,
    features=(),
    minimum_vote_count_for_cap=1,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=1,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="diagnostic_oracle",
)
DIAGNOSTIC_TAIL_ORACLE_3PCT = MonthlyDirectionPolicy(
    "diagnostic_tail_oracle_3pct",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="diagnostic_tail_oracle",
    oracle_tail_threshold=0.03,
)
DIAGNOSTIC_TAIL_ORACLE_5PCT = MonthlyDirectionPolicy(
    "diagnostic_tail_oracle_5pct",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="diagnostic_tail_oracle",
    oracle_tail_threshold=0.05,
)
DIAGNOSTIC_TAIL_ORACLE_8PCT = MonthlyDirectionPolicy(
    "diagnostic_tail_oracle_8pct",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    model_type="diagnostic_tail_oracle",
    oracle_tail_threshold=0.08,
)
DXY_LOCAL_TREND_POSITIVE_FLOOR50 = MonthlyDirectionPolicy(
    "dxy_local_trend_positive_floor50",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    positive_score_exposure_floor=0.50,
)
DXY_LOCAL_TREND_POSITIVE_FLOOR65 = MonthlyDirectionPolicy(
    "dxy_local_trend_positive_floor65",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    positive_score_exposure_floor=0.65,
)
DXY_LOCAL_TREND_POSITIVE_FLOOR72 = MonthlyDirectionPolicy(
    "dxy_local_trend_positive_floor72",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    positive_score_exposure_floor=0.72,
)
DXY_LOCAL_VOTE_POSITIVE_FLOOR50 = MonthlyDirectionPolicy(
    "dxy_local_vote_positive_floor50",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    positive_score_exposure_floor=0.50,
    positive_floor_vote_feature="cs300_ma_6m_distance",
)
DXY_LOCAL_VOTE_POSITIVE_FLOOR65 = MonthlyDirectionPolicy(
    "dxy_local_vote_positive_floor65",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    positive_score_exposure_floor=0.65,
    positive_floor_vote_feature="cs300_ma_6m_distance",
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_VOL20 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost150_vol20",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    basket_volatility_target=0.20,
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_VOL18 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost150_vol18",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    basket_volatility_target=0.18,
)
DXY_CURVE_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_curve_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "external_us_curve_10y2y",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=3,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=3,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_MONEY_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_money_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_m1_m2_scissors_change_3m",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=3,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=3,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_CREDIT_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_credit_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_sf_rolling_12m_growth",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_CREDIT3_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_credit3_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_sf_rolling_3m_yoy",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_ANFCI_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_anfci_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "external_anfci_change_3m",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_MARGIN_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_margin_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_margin_balance_return_3m",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_OPTION_VOLUME_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_option_volume_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_option_put_call_volume_change_1m",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_OPTION_OI_LOCAL_MAJORITY_BOOST150 = MonthlyDirectionPolicy(
    "dxy_option_oi_local_majority_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=(
        "external_dxy_return_1m",
        "domestic_option_put_call_oi_change_1m",
        "cs300_ma_6m_distance",
    ),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST175_LIQ25_DD5 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost175_liq25_dd5",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.75,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST175_DXY_GUARD = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost175_dxy_guard",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=1.75,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    local_positive_dxy_negative_multiplier=1.0,
)
DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST200_LIQ25_DD5 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short40_boost200_liq25_dd5",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.40,
    nonnegative_exposure_multiplier=2.00,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
)
DXY_LOCAL_TREND_ASYMMETRIC_REBOUND200 = MonthlyDirectionPolicy(
    "dxy_local_trend_asymmetric_rebound200",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.25,
    liquidity_stress_exposure_cap=0.25,
    boost_allowed_drawdown_gte=-0.05,
    local_negative_dxy_positive_multiplier=2.0,
    local_positive_dxy_negative_multiplier=1.0,
)
DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150 = MonthlyDirectionPolicy(
    "dxy_local_trend_tail15_short50_boost150",
    -0.20,
    0.0,
    0.15,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    short_cycle_exposure_cap=0.50,
    nonnegative_exposure_multiplier=1.50,
)
DXY_BREADTH_CONFIRMED_CAP0 = MonthlyDirectionPolicy(
    "dxy_breadth_confirmed_cap0",
    -0.20,
    0.0,
    0.0,
    0.25,
    0.0,
    features=("external_dxy_return_1m", "breadth_return_3m_positive"),
    minimum_vote_count_for_cap=2,
)
DXY_CURVE_CAP0 = MonthlyDirectionPolicy(
    "dxy_curve_cap0",
    -0.20,
    0.0,
    0.0,
    99.0,
    0.0,
    features=("external_dxy_return_1m", "external_us_curve_10y2y"),
)
LOCAL_DRAWDOWN_BREADTH_CAP0 = MonthlyDirectionPolicy(
    "local_drawdown_breadth_cap0",
    -0.20,
    0.0,
    0.0,
    99.0,
    0.0,
    features=("cs300_drawdown_3m", "breadth_return_3m_positive"),
)
MONTHLY_DIRECTION_POLICIES = (
    TREND_ONLY,
    WALKFORWARD_CAP0,
    WALKFORWARD_CAP15,
    WALKFORWARD_STRICT_CAP0,
    WALKFORWARD_CAP0_OVERHEAT15,
    WALKFORWARD_CAP0_REBOUND25,
    DXY_ONLY_CAP0,
    DXY_ONLY_CAP0_REBOUND25,
    DXY_ONLY_CAP15_REBOUND25,
    DXY_ONLY_CAP25_REBOUND25,
    DXY_LOCAL_TREND_CONFIRMED_CAP0,
    DXY_LOCAL_TREND_TAIL25_REBOUND50,
    DXY_LOCAL_TREND_TAIL0_SHORT50,
    DXY_LOCAL_TREND_TAIL15_SHORT50,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST125,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST125_LIQ25,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST140_LIQ25,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150_LIQ25,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150_LIQ25_DD5,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_LIQ25_DD5,
    DXY_LOCAL_MEDIUM40_BOOST150,
    DXY_LOCAL_MEDIUM25_BOOST150,
    DXY_LOCAL_TIGHTENING25_BOOST150,
    DXY_LOCAL_TIGHTENING40_BOOST150,
    DXY_LOCAL_TIGHTENING25_MATURE40,
    DXY_LOCAL_TIGHTENING25_MATURE25,
    DXY_LOCAL_MATURE25_DISTRIBUTION25,
    DXY_LOCAL_MATURE25_DISTRIBUTION25_FINANCED25,
    DXY_LOCAL_MATURE25_DISTRIBUTION25_FINANCED25_OPTION25,
    DXY_LOCAL_MATURE25_OPTION25,
    DXY_LOCAL_MATURE25_FINANCED25_OPTION25,
    DXY_LOCAL_OPTION25_TURNOVER25,
    DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25,
    DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25_LOWVOL40,
    DXY_LOCAL_OPTION25_TURNOVER25_MARGIN25_LOWVOL40_BREADTH25,
    DXY_LOCAL_OPTION25_LOWVOL40,
    DXY_LOCAL_OPTION25_BREADTH25,
    DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25,
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25,
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL25_BREADTH25,
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL25_BREADTH25_LEADERSHIP25,
    DXY_LOCAL_OPTION25_SHORT25_LOWVOL40_BREADTH25_LEADERSHIP25,
    DXY_LOCAL_OPTION25_LOWVOL40_BREADTH25_LEADERSHIP25,
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25,
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25,
    DXY_LOCAL_LEADERSHIP25_LEVERAGE25_FUNDDIST25_SAT25,
    DXY_LOCAL_SAT25_THEME3M25,
    DXY_LOCAL_SAT25_THEME3M25_THEME1M40,
    DXY_LOCAL_SAT25_THEME3M25_THEME1M40_CREDIT25,
    DXY_LOCAL_SAT25_THEME3M25_THEME1M40_CREDIT25_MACRO40,
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25,
    DXY_LOCAL_SAT25_THEME3M25_MACRO40,
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40,
    DXY_LOCAL_SAT25_THEME3M25_CREDIT25_MACRO40_THEME1C25,
    DXY_LOCAL_THEME1C25_WEAKCREDIT25,
    DXY_LOCAL_THEME1C25_FUND_DIST_MAX100,
    RIDGE120_A10_BOOST150,
    RIDGE120_A30_BOOST150,
    RIDGE_CORE_A30_BOOST150,
    RIDGE_CORE_SIGN_A30_BOOST150,
    DIAGNOSTIC_ORACLE_DIRECTION,
    DIAGNOSTIC_TAIL_ORACLE_3PCT,
    DIAGNOSTIC_TAIL_ORACLE_5PCT,
    DIAGNOSTIC_TAIL_ORACLE_8PCT,
    DXY_LOCAL_TREND_POSITIVE_FLOOR50,
    DXY_LOCAL_TREND_POSITIVE_FLOOR65,
    DXY_LOCAL_TREND_POSITIVE_FLOOR72,
    DXY_LOCAL_VOTE_POSITIVE_FLOOR50,
    DXY_LOCAL_VOTE_POSITIVE_FLOOR65,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_VOL20,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST150_VOL18,
    DXY_CURVE_LOCAL_MAJORITY_BOOST150,
    DXY_MONEY_LOCAL_MAJORITY_BOOST150,
    DXY_CREDIT_LOCAL_MAJORITY_BOOST150,
    DXY_CREDIT3_LOCAL_MAJORITY_BOOST150,
    DXY_ANFCI_LOCAL_MAJORITY_BOOST150,
    DXY_MARGIN_LOCAL_MAJORITY_BOOST150,
    DXY_OPTION_VOLUME_LOCAL_MAJORITY_BOOST150,
    DXY_OPTION_OI_LOCAL_MAJORITY_BOOST150,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST175_LIQ25_DD5,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST175_DXY_GUARD,
    DXY_LOCAL_TREND_TAIL15_SHORT40_BOOST200_LIQ25_DD5,
    DXY_LOCAL_TREND_ASYMMETRIC_REBOUND200,
    DXY_LOCAL_TREND_TAIL15_SHORT50_BOOST150,
    DXY_BREADTH_CONFIRMED_CAP0,
    DXY_CURVE_CAP0,
    LOCAL_DRAWDOWN_BREADTH_CAP0,
)


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = rank
        cursor = end
    return ranks


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    ranked_x = _average_ranks(xs)
    ranked_y = _average_ranks(ys)
    mean_x = statistics.mean(ranked_x)
    mean_y = statistics.mean(ranked_y)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(ranked_x, ranked_y))
    denominator_x = sum((x - mean_x) ** 2 for x in ranked_x)
    denominator_y = sum((y - mean_y) ** 2 for y in ranked_y)
    denominator = (denominator_x * denominator_y) ** 0.5
    return numerator / denominator if denominator > 0 else None


def predict_direction(
    history: list[dict[str, Any]],
    current_features: dict[str, float | None],
    policy: MonthlyDirectionPolicy,
) -> dict[str, Any]:
    votes: dict[str, int] = {}
    correlations: dict[str, float] = {}
    thresholds: dict[str, float] = {}
    for feature in policy.features:
        usable = [
            row
            for row in history
            if row["features"].get(feature) is not None and row.get("forward_return") is not None
        ]
        current = current_features.get(feature)
        if current is None or len(usable) < policy.min_history:
            continue
        xs = [float(row["features"][feature]) for row in usable]
        ys = [float(row["forward_return"]) for row in usable]
        correlation = _correlation(xs, ys)
        if correlation is None or abs(correlation) < policy.min_abs_correlation:
            continue
        threshold = statistics.median(xs)
        if float(current) == threshold:
            continue
        orientation = 1 if correlation > 0 else -1
        votes[feature] = orientation * (1 if float(current) > threshold else -1)
        correlations[feature] = correlation
        thresholds[feature] = threshold
    score = statistics.mean(votes.values()) if votes else None
    return {
        "score": score,
        "predicted_direction": 1 if score is not None and score > 0 else (-1 if score is not None and score < 0 else 0),
        "vote_count": len(votes),
        "votes": votes,
        "correlations": correlations,
        "thresholds": thresholds,
    }


def predict_ridge_direction(
    history: list[dict[str, Any]],
    current_features: dict[str, float | None],
    policy: MonthlyDirectionPolicy,
) -> dict[str, Any]:
    window = history[-policy.history_months :] if policy.history_months else history
    usable_history = [row for row in window if row.get("forward_return") is not None]
    if len(usable_history) < policy.min_history:
        return {
            "score": None,
            "predicted_direction": 0,
            "vote_count": 0,
            "votes": {},
            "correlations": {},
            "thresholds": {},
        }
    minimum_available = max(int(len(usable_history) * 0.80), policy.min_history)
    features = [
        feature
        for feature in policy.features
        if sum(
            row["features"].get(feature) is not None
            and math.isfinite(float(row["features"][feature]))
            for row in usable_history
        )
        >= minimum_available
    ]
    if len(features) < policy.minimum_vote_count_for_cap:
        return {
            "score": None,
            "predicted_direction": 0,
            "vote_count": 0,
            "votes": {},
            "correlations": {},
            "thresholds": {},
        }

    columns = []
    centers = []
    scales = []
    current = []
    active_features = []
    for feature in features:
        observed = [
            float(row["features"][feature])
            for row in usable_history
            if row["features"].get(feature) is not None
            and math.isfinite(float(row["features"][feature]))
        ]
        center = statistics.median(observed)
        deviations = [abs(value - center) for value in observed]
        scale = statistics.median(deviations) * 1.4826
        if scale <= 1e-12:
            scale = statistics.pstdev(observed) if len(observed) > 1 else 0.0
        if scale <= 1e-12:
            continue
        column = [
            (float(row["features"][feature]) - center) / scale
            if row["features"].get(feature) is not None
            and math.isfinite(float(row["features"][feature]))
            else 0.0
            for row in usable_history
        ]
        columns.append(column)
        active_features.append(feature)
        centers.append(center)
        scales.append(scale)
        value = current_features.get(feature)
        current.append(
            (float(value) - center) / scale
            if value is not None and math.isfinite(float(value))
            else 0.0
        )
    if len(columns) < policy.minimum_vote_count_for_cap:
        return {
            "score": None,
            "predicted_direction": 0,
            "vote_count": 0,
            "votes": {},
            "correlations": {},
            "thresholds": {},
        }

    x = np.asarray(columns, dtype=float).T
    x = np.clip(x, -5.0, 5.0)
    design = np.column_stack([np.ones(len(x)), x])
    y = np.asarray(
        [
            (1.0 if float(row["forward_return"]) > 0 else -1.0)
            if policy.ridge_target_mode == "sign"
            else max(
                -policy.target_clip,
                min(policy.target_clip, float(row["forward_return"])),
            )
            for row in usable_history
        ],
        dtype=float,
    )
    penalty = np.eye(design.shape[1], dtype=float) * policy.ridge_alpha
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    current_vector = np.asarray([1.0, *np.clip(current, -5.0, 5.0)], dtype=float)
    prediction = float(current_vector @ coefficients)
    contributions = current_vector[1:] * coefficients[1:]
    votes = {
        feature: 1 if contribution > 0 else -1
        for feature, contribution in zip(active_features, contributions)
        if abs(float(contribution)) > 1e-12
    }
    return {
        "score": prediction,
        "predicted_direction": 1 if prediction > 0 else (-1 if prediction < 0 else 0),
        "vote_count": len(columns),
        "votes": votes,
        "correlations": {
            feature: float(coefficient)
            for feature, coefficient in zip(active_features, coefficients[1:])
        },
        "thresholds": {
            feature: center for feature, center in zip(active_features, centers)
        },
    }


def _quantile_edges(values: list[float], bins: int) -> list[float]:
    """Return deterministic prior-sample edges for equal-frequency bins."""

    ordered = sorted(values)
    if bins < 2 or len(ordered) < bins:
        return []
    return [
        ordered[min(len(ordered) - 1, max(0, round(len(ordered) * step / bins) - 1))]
        for step in range(1, bins)
    ]


def predict_binned_direction(
    history: list[dict[str, Any]],
    current_features: dict[str, float | None],
    policy: MonthlyDirectionPolicy,
) -> dict[str, Any]:
    """Predict next-period return from shrunk point-in-time feature bins.

    Every threshold and bucket mean is fitted only on completed prior periods
    from the same path.  The additive model deliberately has no interactions
    or feature selection, keeping the small quarterly sample auditable.
    """

    window = history[-policy.history_months :] if policy.history_months else history
    usable_history = [
        row
        for row in window
        if isinstance(row.get("forward_return"), (int, float))
        and math.isfinite(float(row["forward_return"]))
    ]
    if len(usable_history) < policy.min_history:
        return {
            "score": None,
            "predicted_direction": 0,
            "vote_count": 0,
            "votes": {},
            "correlations": {},
            "thresholds": {},
            "bucket_counts": {},
            "model_type": "binned",
        }

    clipped_targets = [
        max(-policy.target_clip, min(policy.target_clip, float(row["forward_return"])))
        for row in usable_history
    ]
    global_mean = statistics.mean(clipped_targets)
    predictions: dict[str, float] = {}
    thresholds: dict[str, list[float]] = {}
    bucket_counts: dict[str, int] = {}
    shrink = max(0.0, float(policy.binned_shrink_count))

    for feature in policy.features:
        current = current_features.get(feature)
        paired = [
            (float(row["features"][feature]), target)
            for row, target in zip(usable_history, clipped_targets)
            if isinstance(row.get("features"), dict)
            and isinstance(row["features"].get(feature), (int, float))
            and math.isfinite(float(row["features"][feature]))
        ]
        if (
            not isinstance(current, (int, float))
            or not math.isfinite(float(current))
            or len(paired) < policy.min_history
        ):
            continue
        edges = _quantile_edges(
            [value for value, _target in paired],
            policy.binned_bins,
        )
        if len(edges) != policy.binned_bins - 1:
            continue
        current_bucket = bisect_right(edges, float(current))
        bucket_targets = [
            target
            for value, target in paired
            if bisect_right(edges, value) == current_bucket
        ]
        if not bucket_targets:
            continue
        bucket_mean = (
            sum(bucket_targets) + shrink * global_mean
        ) / (len(bucket_targets) + shrink)
        predictions[feature] = bucket_mean
        thresholds[feature] = edges
        bucket_counts[feature] = len(bucket_targets)

    required = min(policy.minimum_vote_count_for_boost, len(policy.features))
    score = (
        statistics.mean(predictions.values())
        if len(predictions) >= required and required > 0
        else None
    )
    votes = {
        feature: 1 if prediction > policy.positive_score_gt else -1
        for feature, prediction in predictions.items()
    }
    return {
        "score": score,
        "predicted_direction": (
            1
            if score is not None and score > policy.positive_score_gt
            else -1
            if score is not None and score < policy.positive_score_gt
            else 0
        ),
        "vote_count": len(predictions),
        "votes": votes,
        "correlations": predictions,
        "thresholds": thresholds,
        "bucket_counts": bucket_counts,
        "global_mean": global_mean,
        "model_type": "binned",
    }


def attach_walkforward_predictions(
    months: list[dict[str, Any]],
    policy: MonthlyDirectionPolicy,
    initial_history: list[dict[str, Any]] | None = None,
) -> None:
    history: list[dict[str, Any]] = list(initial_history or [])
    for month in months:
        if policy.model_type == "diagnostic_oracle":
            outcome = float(month["risk_return"])
            month["direction_model"] = {
                "score": outcome,
                "predicted_direction": 1 if outcome > 0 else (-1 if outcome < 0 else 0),
                "vote_count": 1,
                "votes": {"diagnostic_oracle": 1 if outcome > 0 else -1},
                "correlations": {},
                "thresholds": {},
            }
        elif policy.model_type == "diagnostic_tail_oracle":
            prediction = predict_direction(history, month["features"], policy)
            outcome = float(month["risk_return"])
            if abs(outcome) >= float(policy.oracle_tail_threshold or 0.0):
                prediction = {
                    "score": 1.0 if outcome > 0 else -1.0,
                    "predicted_direction": 1 if outcome > 0 else -1,
                    "vote_count": max(policy.minimum_vote_count_for_cap, 2),
                    "votes": {"diagnostic_tail_oracle": 1 if outcome > 0 else -1},
                    "correlations": {},
                    "thresholds": {},
                }
            month["direction_model"] = prediction
        else:
            month["direction_model"] = (
                predict_ridge_direction(history, month["features"], policy)
                if policy.model_type == "ridge"
                else predict_binned_direction(history, month["features"], policy)
                if policy.model_type == "binned"
                else predict_direction(history, month["features"], policy)
            )
        history.append({"features": month["features"], "forward_return": month["risk_return"]})

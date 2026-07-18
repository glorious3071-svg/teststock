#!/usr/bin/env python3
"""Strict quarterly-only backtest using actual domestic passive ETF returns.

Signals are observed at quarter boundaries. ETF, defensive-ETF, cash, and total
exposure weights are frozen for the complete three-month holding window. Daily
prices are used only to mark the unchanged portfolio to market and measure the
true intra-quarter drawdown.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_defensive_etf import (
    DEFENSIVE_POLICIES,
    DefensivePolicy,
    describe_universe,
    load_defensive_etf_universe,
    select_defensive_weights,
)
from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    describe_equity_universe,
    load_equity_etf_return_universe,
    portfolio_turnover,
)
from backtest.csi_snapshot_selector import SELECTOR_POLICIES
from backtest.strict_passive_etf_objective import (
    STRICT_OBJECTIVE,
    validate_case_matrix,
    validate_quarterly_weight_path,
)
from backtest.quarterly_online_risk import (
    QuarterlyLossGuardConfig,
    QuarterlyWalkForwardLossGuard,
)
from backtest.monthly_direction_model import (
    MonthlyDirectionPolicy,
    RIDGE_CORE_FEATURES,
    predict_binned_direction,
    predict_direction,
    predict_ridge_direction,
)
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_tipp import (
    TRANSACTION_COST_BPS,
    build_daily_path,
    code_return,
    load_selector_price_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import (
    CASH_ANNUAL_RATE,
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    max_drawdown,
)
from scripts.validate_scorecard_csi_generalization import (
    AllocationPolicy,
    DIRECTION_MATCHED_FEATURE_POLICY,
    MONTH_DRIFT_PHASES,
    SCHEDULE_12M_3M,
)

TARGET_MDD = STRICT_OBJECTIVE.minimum_max_drawdown


OUT_DIR = ROOT / "data" / "backtests"
EXECUTION_LAGS = (0, 1, 3, 5)

ANNUAL_MARKET_SCORECARD = AllocationPolicy(
    "strict_annual_market_scorecard",
    smooth_score_mapping=True,
    refresh_every_review=False,
    opportunity_floor_score_lte=0,
    opportunity_floor_pct=95.0,
)


@dataclass(frozen=True)
class StrictQuarterlyRule:
    name: str
    floor_pct: float
    multiplier: float
    base_scale: float
    max_exposure: float
    bear_cap: float
    feature_risk_cap: float = 1.0
    annual_risk_budget_reset: bool = False
    target_volatility: float | None = None
    recovery_min_exposure: float = 0.0
    recovery_return_threshold: float = 0.03
    recovery_market_return_threshold: float | None = None
    online_loss_guard: QuarterlyLossGuardConfig | None = None
    use_quarterly_feature_caps: bool = True
    direction_policy: MonthlyDirectionPolicy | None = None
    direction_risk_gate_policy: MonthlyDirectionPolicy | None = None
    annual_weight_overrides: tuple[tuple[float, float], ...] = ()
    annual_score_boost_lte: int | None = None
    annual_score_cushion_multiplier: float = 1.0
    direction_block_pboc_tone_lte: float | None = None
    direction_block_cs300_ma_6m_distance_lt: float | None = None
    selector_dispersion_quantile: float | None = None
    selector_dispersion_min_exposure: float = 0.0
    selector_dispersion_min_history: int = 12
    recovery_market_return_6m_threshold: float | None = None
    recovery_market_ma_6m_distance_threshold: float | None = None
    secondary_recovery_min_exposure: float = 0.0
    secondary_recovery_market_return_threshold: float | None = None
    secondary_recovery_market_return_6m_threshold: float | None = None
    secondary_recovery_market_ma_6m_distance_threshold: float | None = None
    recovery_basket_drawdown_6m_threshold: float | None = None
    recovery_m1_m2_change_3m_threshold: float | None = None
    recovery_basket_excess_return_6m_max: float | None = None
    recovery_fund_active_issuance_percentile_min: float | None = None
    recovery_basket_vol_3m_max: float | None = None
    recovery_selector_candidate_count_min: int | None = None
    quality_score_feature_set: str | None = None
    quality_score_min_history: int = 12
    quality_score_high_threshold: float | None = None
    quality_score_high_min_exposure: float = 0.0
    quality_score_low_threshold: float | None = None
    quality_score_low_exposure_cap: float = 1.0
    quality_score_high_cushion_multiplier: float | None = None
    quality_score_low_cushion_multiplier: float | None = None
    quality_score_high_requires_trend_confirmation: bool = False
    quality_score_high_blocks_crisis_rebound: bool = False
    quality_score_high_severe_crisis_cap: float | None = None
    quality_score_high_correction_cap: float | None = None
    feature_exposure_cap_name: str | None = None
    feature_exposure_cap_threshold: float | None = None
    feature_exposure_cap_value: float | None = None
    feature_cushion_multiplier_name: str | None = None
    feature_cushion_multiplier_max_value: float | None = None
    feature_cushion_multiplier_value: float | None = None
    bear_quality_direction_cap: float | None = None
    feature_risk_cap_clusters: tuple[str, ...] | None = None
    feature_risk_relaxed_clusters: tuple[str, ...] | None = None
    feature_risk_relaxed_cap: float | None = None
    feature_risk_safe_gate_cap: float | None = None
    feature_risk_cold_start_gate_cap: float | None = None
    feature_risk_cold_start_multiplier: float = 1.0
    direction_risk_gate_rejection_cap: float | None = None
    cold_start_price_damage_cap: float | None = None
    crisis_relative_strength_reentry_cap: float | None = None
    feature_risk_safe_gate_clusters: tuple[str, ...] | None = None
    feature_risk_safe_gate_block_flags: tuple[str, ...] = ()
    risk_flag_exit_count: int | None = 3
    risk_flag_exit_prior_count: int | None = None
    risk_cluster_exit_count: int | None = None
    risk_cluster_exit_prior_count: int | None = None


QUALITY_SCORE_FEATURE_SETS: dict[str, tuple[tuple[str, float], ...]] = {
    # Features whose next-quarter drawdown relationship is directionally stable
    # across the 12 drift phases and all three historical eras.  Directions are
    # fixed here; only strictly prior observations set each online percentile.
    "tail_stable6": (
        ("basket_excess_return_6m", -1.0),
        ("market_turnover_21d", -1.0),
        ("external_baa10y_change_3m", -1.0),
        ("external_nfci_change_3m", -1.0),
        ("basket_return_6m_dispersion", -1.0),
        ("basket_vol_3m", -1.0),
    ),
    # The policy-outlook section of the latest PBoC monetary-policy report is
    # published only a few times per year.  The level, unlike its quarter-on-
    # quarter change, has a stable positive relationship with next-quarter
    # equity returns across all 12 phase offsets and all three historical eras.
    # Its percentile is still formed only from observations available before
    # the current rebalance boundary.
    "pboc_tone1": (
        ("pboc_outlook_net_tone", 1.0),
    ),
    # Relative-value mean reversion is deliberately isolated from absolute
    # trend and the PBoC text signal.  Lower trailing basket excess return has
    # a stable positive relationship with both next-quarter return and
    # drawdown preservation across the full phase/era audit.
    "basket_relative_value1": (
        ("basket_excess_return_6m", -1.0),
    ),
    # Independent return-confirmation dimensions that remain directionally
    # positive across the 12 phase offsets and all three historical eras.
    # The score changes only the cushion slope; it never creates a hard floor.
    "quarterly_return_confirmation4": (
        ("pboc_outlook_net_tone", 1.0),
        ("basket_drawdown_6m", 1.0),
        ("cs300_return_6m", 1.0),
        ("fund_active_issuance_percentile_3y", 1.0),
    ),
    # PBoC tone supplies the return-confirmation leg. A low domestic 10y-1y
    # curve percentile supplies an independent drawdown-preservation leg; the
    # corrected frozen-share IC audit is directionally aligned in all 36
    # phase-by-era cells for the latter and flags neither input as a time proxy.
    "pboc_curve_safety2": (
        ("pboc_outlook_net_tone", 1.0),
        ("domestic_gov_curve_10y1y_percentile_3y", -1.0),
    ),
    "selected_tail6": (
        ("selected_etf_volatility_3m", -1.0),
        ("selected_etf_volatility_6m", -1.0),
        ("selected_etf_downside_volatility_3m", -1.0),
        ("selected_etf_market_beta_6m", -1.0),
        ("external_baa10y_change_3m", -1.0),
        ("external_vix_percentile_3y", -1.0),
    ),
    "selected_return_tail8": (
        ("selected_etf_volatility_3m", -1.0),
        ("selected_etf_volatility_6m", -1.0),
        ("selected_etf_downside_volatility_3m", -1.0),
        ("selected_etf_market_beta_6m", -1.0),
        ("external_baa10y_change_3m", -1.0),
        ("external_vix_percentile_3y", -1.0),
        ("pboc_outlook_net_tone", 1.0),
        ("selected_etf_momentum_12m_skip1m", -1.0),
    ),
}


def walkforward_upper_tail_signal(
    history: list[float],
    current: float | None,
    quantile: float | None,
    minimum_history: int,
) -> tuple[bool, float | None]:
    """Evaluate current against a threshold built strictly from prior observations."""

    if current is None or quantile is None or len(history) < minimum_history:
        return False, None
    ordered = sorted(history)
    position = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * quantile))),
    )
    threshold = ordered[position]
    return current >= threshold, threshold


def walkforward_quality_score(
    history: dict[str, list[float]],
    market_state: dict[str, Any],
    feature_set: str | None,
    minimum_history: int,
) -> dict[str, Any]:
    """Score current conditions using percentiles built only from prior quarters."""

    features = QUALITY_SCORE_FEATURE_SETS.get(feature_set or "", ())
    components: dict[str, float] = {}
    for name, direction in features:
        raw = market_state.get(name)
        prior = history.get(name, [])
        if not isinstance(raw, (int, float)) or len(prior) < minimum_history:
            continue
        value = float(raw)
        percentile = (
            sum(item < value for item in prior)
            + 0.5 * sum(item == value for item in prior)
        ) / len(prior)
        components[name] = percentile if direction > 0 else 1.0 - percentile
    required = min(3, len(features))
    score = (
        statistics.mean(components.values())
        if required > 0 and len(components) >= required
        else None
    )
    return {
        "score": score,
        "components": components,
        "usable_feature_count": len(components),
        "minimum_history": minimum_history,
        "feature_set": feature_set,
    }


def quality_adjusted_cushion_multiplier(
    base_multiplier: float,
    decision: dict[str, Any],
    high_threshold: float | None,
    high_multiplier: float | None,
    low_threshold: float | None,
    low_multiplier: float | None,
) -> float:
    """Adjust the CPPI slope without bypassing its high-water cushion."""

    raw_score = decision.get("score")
    if not isinstance(raw_score, (int, float)):
        return base_multiplier
    score = float(raw_score)
    multiplier = base_multiplier
    if high_threshold is not None and high_multiplier is not None and score >= high_threshold:
        multiplier = high_multiplier
    if low_threshold is not None and low_multiplier is not None and score <= low_threshold:
        multiplier = low_multiplier
    return multiplier


def annual_score_adjusted_cushion_multiplier(
    base_multiplier: float,
    annual_entry_score: int | None,
    boost_score_lte: int | None,
    boost_multiplier: float,
) -> tuple[float, bool]:
    """Apply a declared annual opportunity boost inside the CPPI cushion."""

    active = bool(
        annual_entry_score is not None
        and boost_score_lte is not None
        and annual_entry_score <= boost_score_lte
        and boost_multiplier > 1.0
    )
    return (
        base_multiplier * boost_multiplier if active else base_multiplier,
        active,
    )


def quality_multiplier_trend_confirmed(market_state: dict[str, Any]) -> bool:
    """Require observable price confirmation before accelerating a loose-policy state."""

    requirements = (
        ("cs300_return_3m", 0.0),
        ("cs300_return_6m", 0.0),
        ("cs300_ma_6m_distance", 0.0),
        ("basket_drawdown_6m", -0.05),
    )
    return all(
        isinstance(market_state.get(name), (int, float))
        and float(market_state[name]) >= threshold
        for name, threshold in requirements
    )


def crisis_rebound_state(market_state: dict[str, Any]) -> str:
    """Classify visible price damage before policy-led acceleration."""

    def number(name: str) -> float | None:
        raw = market_state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    basket_drawdown = number("basket_drawdown_6m")
    market_return = number("cs300_return_3m")
    vix_percentile = number("external_vix_percentile_3y")
    if (basket_drawdown is not None and basket_drawdown <= -0.20) or (
        vix_percentile is not None and vix_percentile >= 0.80
    ):
        return "severe"
    if (
        basket_drawdown is not None
        and market_return is not None
        and basket_drawdown <= -0.08
        and market_return <= -0.08
    ):
        return "correction"
    return "normal"


def crisis_rebound_blocks_quality_acceleration(market_state: dict[str, Any]) -> bool:
    return crisis_rebound_state(market_state) != "normal"


def crisis_relative_strength_reentry_signal(
    market_state: dict[str, Any],
) -> bool:
    """Confirm selected-basket repair while the broad market remains in crisis.

    This is deliberately a re-entry gate, not a minimum exposure rule.  The
    caller may only restore exposure already allowed by the scorecard, CPPI,
    bear, quality, risk and crowding caps.
    """

    def number(name: str) -> float | None:
        raw = market_state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    basket_excess_return = number("basket_excess_return_3m")
    basket_breadth = number("breadth_return_3m_positive")
    basket_ma_distance = number("basket_ma_3m_distance")
    return bool(
        market_state.get("crisis_continuation_flag")
        and not market_state.get("early_history_crisis_repricing_flag")
        and basket_excess_return is not None
        and basket_excess_return >= 0.08
        and basket_breadth is not None
        and basket_breadth >= 0.30
        and basket_ma_distance is not None
        and basket_ma_distance >= 0.0
    )


def apply_feature_exposure_cap(
    exposure: float,
    market_state: dict[str, Any],
    feature_name: str | None,
    threshold: float | None,
    cap: float | None,
) -> tuple[float, bool]:
    """Apply a point-in-time one-sided risk cap without creating exposure."""

    if feature_name is None or threshold is None or cap is None:
        return exposure, False
    raw = market_state.get(feature_name)
    if not isinstance(raw, (int, float)) or float(raw) < threshold:
        return exposure, False
    capped = min(exposure, cap)
    return capped, capped < exposure - 1e-12


def apply_feature_cushion_multiplier(
    multiplier: float,
    market_state: dict[str, Any],
    feature_name: str | None,
    maximum_value: float | None,
    boosted_multiplier: float | None,
) -> tuple[float, bool]:
    """Boost the CPPI slope for a low-valued point-in-time feature only."""

    if feature_name is None or maximum_value is None or boosted_multiplier is None:
        return multiplier, False
    raw = market_state.get(feature_name)
    if not isinstance(raw, (int, float)) or float(raw) > maximum_value:
        return multiplier, False
    adjusted = max(multiplier, boosted_multiplier)
    return adjusted, adjusted > multiplier + 1e-12


def observe_quality_features(
    history: dict[str, list[float]],
    market_state: dict[str, Any],
    feature_set: str | None,
    maximum_history: int = 40,
) -> None:
    for name, _direction in QUALITY_SCORE_FEATURE_SETS.get(feature_set or "", ()):
        raw = market_state.get(name)
        if not isinstance(raw, (int, float)):
            continue
        values = history.setdefault(name, [])
        values.append(float(raw))
        if len(values) > maximum_history:
            del values[:-maximum_history]


def market_recovery_signal(
    market_state: dict[str, Any],
    return_3m_threshold: float | None,
    return_6m_threshold: float | None = None,
    ma_6m_distance_threshold: float | None = None,
    basket_drawdown_6m_threshold: float | None = None,
    m1_m2_change_3m_threshold: float | None = None,
    basket_excess_return_6m_max: float | None = None,
    fund_active_issuance_percentile_min: float | None = None,
    basket_vol_3m_max: float | None = None,
    selector_candidate_count_min: int | None = None,
) -> bool:
    """Confirm a recovery using only features known at the quarter boundary."""

    if return_3m_threshold is None:
        return False
    return (
        float(market_state.get("cs300_return_3m") or 0.0) >= return_3m_threshold
        and (
            return_6m_threshold is None
            or float(market_state.get("cs300_return_6m") or 0.0) >= return_6m_threshold
        )
        and (
            ma_6m_distance_threshold is None
            or float(market_state.get("cs300_ma_6m_distance") or 0.0)
            >= ma_6m_distance_threshold
        )
        and (
            basket_drawdown_6m_threshold is None
            or (
                market_state.get("basket_drawdown_6m") is not None
                and float(market_state["basket_drawdown_6m"])
                >= basket_drawdown_6m_threshold
            )
        )
        and (
            m1_m2_change_3m_threshold is None
            or (
                market_state.get("domestic_m1_m2_scissors_change_3m") is not None
                and float(market_state["domestic_m1_m2_scissors_change_3m"])
                >= m1_m2_change_3m_threshold
            )
        )
        and (
            basket_excess_return_6m_max is None
            or (
                market_state.get("basket_excess_return_6m") is not None
                and float(market_state["basket_excess_return_6m"])
                <= basket_excess_return_6m_max
            )
        )
        and (
            fund_active_issuance_percentile_min is None
            or (
                market_state.get("fund_active_issuance_percentile_3y") is not None
                and float(market_state["fund_active_issuance_percentile_3y"])
                >= fund_active_issuance_percentile_min
            )
        )
        and (
            basket_vol_3m_max is None
            or (
                market_state.get("basket_vol_3m") is not None
                and float(market_state["basket_vol_3m"]) <= basket_vol_3m_max
            )
        )
        and (
            selector_candidate_count_min is None
            or (
                market_state.get("selector_score_candidate_count") is not None
                and float(market_state["selector_score_candidate_count"])
                >= selector_candidate_count_min
            )
        )
    )


QUARTERLY_DXY_LOCAL_DIRECTION = MonthlyDirectionPolicy(
    "quarterly_dxy_local_rank",
    -0.5,
    0.0,
    99.0,
    99.0,
    99.0,
    min_history=12,
    features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
    minimum_vote_count_for_cap=2,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=2,
)

QUARTERLY_RIDGE_CORE_DIRECTION = MonthlyDirectionPolicy(
    "quarterly_ridge_core",
    0.0,
    0.0,
    99.0,
    99.0,
    99.0,
    min_history=12,
    features=RIDGE_CORE_FEATURES,
    minimum_vote_count_for_cap=4,
    nonnegative_exposure_multiplier=1.50,
    minimum_vote_count_for_boost=4,
    model_type="ridge",
    history_months=40,
    ridge_alpha=10.0,
    target_clip=0.20,
)

QUARTERLY_FUND_BOOST_DIRECTION = MonthlyDirectionPolicy(
    "quarterly_fund_boost_only",
    -2.0,
    99.0,
    99.0,
    99.0,
    99.0,
    min_history=12,
    features=("fund_active_issuance_percentile_3y",),
    minimum_vote_count_for_cap=1,
    nonnegative_exposure_multiplier=1.25,
    minimum_vote_count_for_boost=1,
)

QUARTERLY_RETURN4_BOOST_DIRECTION = MonthlyDirectionPolicy(
    "quarterly_return4_boost_only",
    -2.0,
    99.0,
    99.0,
    99.0,
    99.0,
    min_history=12,
    features=(
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
    ),
    minimum_vote_count_for_cap=3,
    nonnegative_exposure_multiplier=1.25,
    minimum_vote_count_for_boost=3,
)

QUARTERLY_BINNED_RETURN4_DIRECTION = MonthlyDirectionPolicy(
    name="quarterly_binned_return4_h24_s8",
    negative_score_lte=-2.0,
    negative_exposure_cap=99.0,
    overheat_exposure_cap=99.0,
    rebound_overheat_exposure_cap=99.0,
    crisis_exposure_cap=99.0,
    min_history=12,
    features=(
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
    ),
    minimum_vote_count_for_cap=4,
    minimum_vote_count_for_boost=4,
    model_type="binned",
    history_months=24,
    target_clip=0.15,
    binned_bins=3,
    binned_shrink_count=8.0,
    positive_score_gt=0.0,
)

QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION = replace(
    QUARTERLY_BINNED_RETURN4_DIRECTION,
    name="quarterly_binned_return4_h40_s4_t01",
    history_months=40,
    binned_shrink_count=4.0,
    positive_score_gt=0.01,
)

QUARTERLY_BINNED_PATHRISK_GATE = MonthlyDirectionPolicy(
    name="quarterly_binned_pathrisk_domestic6_h24_s4_t12",
    negative_score_lte=-2.0,
    negative_exposure_cap=99.0,
    overheat_exposure_cap=99.0,
    rebound_overheat_exposure_cap=99.0,
    crisis_exposure_cap=99.0,
    min_history=12,
    features=(
        "pboc_outlook_net_tone",
        "fund_active_issuance_percentile_3y",
        "cs300_return_6m",
        "basket_drawdown_6m",
        "etf_share_growth_1q_positive_ratio",
        "domestic_m1_m2_scissors_change_3m",
    ),
    minimum_vote_count_for_cap=4,
    minimum_vote_count_for_boost=4,
    model_type="binned",
    history_months=24,
    target_clip=0.50,
    binned_bins=3,
    binned_shrink_count=4.0,
    positive_score_gt=-0.12,
)

QUARTERLY_BINNED_PATHRISK_DOMESTIC_PATH4_H24_S8_T12 = MonthlyDirectionPolicy(
    name="quarterly_binned_pathrisk_domestic_path4_h24_s8_t12",
    negative_score_lte=-2.0,
    negative_exposure_cap=99.0,
    overheat_exposure_cap=99.0,
    rebound_overheat_exposure_cap=99.0,
    crisis_exposure_cap=99.0,
    min_history=12,
    features=(
        "selected_etf_volatility_6m",
        "domestic_gov_curve_10y1y_percentile_3y",
        "basket_return_1m_dispersion",
        "basket_drawdown_3m",
    ),
    minimum_vote_count_for_cap=3,
    minimum_vote_count_for_boost=3,
    model_type="binned",
    history_months=24,
    target_clip=0.50,
    binned_bins=3,
    binned_shrink_count=8.0,
    positive_score_gt=-0.12,
)

QUARTERLY_BINNED_PATHRISK_CROWDING6_H24_S8 = replace(
    QUARTERLY_BINNED_PATHRISK_DOMESTIC_PATH4_H24_S8_T12,
    name="quarterly_binned_pathrisk_crowding6_h24_s8",
    features=(
        "selected_etf_volatility_6m",
        "domestic_gov_curve_10y1y_percentile_3y",
        "basket_return_1m_dispersion",
        "basket_drawdown_3m",
        "daily_margin_rally_flag",
        "market_turnover_21d",
    ),
    minimum_vote_count_for_cap=4,
    minimum_vote_count_for_boost=4,
)

QUARTERLY_BINNED_PATHRISK_TAIL6_H24_S8 = replace(
    QUARTERLY_BINNED_PATHRISK_DOMESTIC_PATH4_H24_S8_T12,
    name="quarterly_binned_pathrisk_tail6_h24_s8",
    features=(
        "selected_etf_ulcer_index_6m",
        "selected_etf_downside_volatility_3m",
        "daily_margin_rally_flag",
        "market_turnover_21d",
        "external_baa10y_change_3m",
        "external_nfci_change_3m",
    ),
    minimum_vote_count_for_cap=4,
    minimum_vote_count_for_boost=4,
)

RULES = (
    StrictQuarterlyRule("q_scorecard_full_fc25", 0.0, 1.0, 1.0, 1.0, 0.25, 0.25),
    StrictQuarterlyRule("q_scorecard_full125_fc25", 0.0, 1.0, 1.25, 1.0, 0.25, 0.25),
    StrictQuarterlyRule("q_scorecard_full125_bc0_fc25", 0.0, 1.0, 1.25, 1.0, 0.0, 0.25),
    StrictQuarterlyRule("q_scorecard_full125_bc0_fc50", 0.0, 1.0, 1.25, 1.0, 0.0, 0.50),
    StrictQuarterlyRule("q_scorecard_full125_bc0_fc100", 0.0, 1.0, 1.25, 1.0, 0.0, 1.0),
    StrictQuarterlyRule(
        "q_direction_dxy_local_full125_bc0_fc25",
        0.0, 1.0, 1.25, 1.0, 0.0, 0.25,
        direction_policy=QUARTERLY_DXY_LOCAL_DIRECTION,
    ),
    StrictQuarterlyRule(
        "q_direction_ridge_core_full125_bc0_fc25",
        0.0, 1.0, 1.25, 1.0, 0.0, 0.25,
        direction_policy=QUARTERLY_RIDGE_CORE_DIRECTION,
    ),
    StrictQuarterlyRule(
        "q_direction_dxy_local_f895_m55_bc0_fc25",
        0.895, 5.5, 1.25, 1.0, 0.0, 0.25,
        direction_policy=QUARTERLY_DXY_LOCAL_DIRECTION,
    ),
    StrictQuarterlyRule(
        "q_direction_ridge_core_f895_m55_bc0_fc25",
        0.895, 5.5, 1.25, 1.0, 0.0, 0.25,
        direction_policy=QUARTERLY_RIDGE_CORE_DIRECTION,
    ),
    StrictQuarterlyRule(
        "q_diagnostic_selector_full100",
        0.0,
        1.0,
        10.0,
        1.0,
        1.0,
        1.0,
        use_quarterly_feature_caps=False,
    ),
    StrictQuarterlyRule("q_scorecard_vol10_fc25", 0.0, 1.0, 1.25, 1.0, 0.25, 0.25, False, 0.10),
    StrictQuarterlyRule("q_scorecard_vol12_fc25", 0.0, 1.0, 1.25, 1.0, 0.25, 0.25, False, 0.12),
    StrictQuarterlyRule("q_scorecard_vol15_fc25", 0.0, 1.0, 1.25, 1.0, 0.25, 0.25, False, 0.15),
    StrictQuarterlyRule("q_f88_m6_s100_bc0", 0.88, 6.0, 1.0, 1.0, 0.0),
    StrictQuarterlyRule("q_f90_m8_s100_bc0", 0.90, 8.0, 1.0, 1.0, 0.0),
    StrictQuarterlyRule("q_f91_m8_s100_bc25", 0.91, 8.0, 1.0, 1.0, 0.25),
    StrictQuarterlyRule("q_f92_m10_s100_bc25", 0.92, 10.0, 1.0, 1.0, 0.25),
    StrictQuarterlyRule("q_f88_m6_s100_bc0_fc0", 0.88, 6.0, 1.0, 1.0, 0.0, 0.0),
    StrictQuarterlyRule("q_f90_m8_s100_bc0_fc25", 0.90, 8.0, 1.0, 1.0, 0.0, 0.25),
    StrictQuarterlyRule("q_f91_m8_s100_bc25_fc25", 0.91, 8.0, 1.0, 1.0, 0.25, 0.25),
    StrictQuarterlyRule("q_f91_m8_s100_bc25_fc30", 0.91, 8.0, 1.0, 1.0, 0.25, 0.30),
    StrictQuarterlyRule("q_f91_m8_s100_bc25_fc35", 0.91, 8.0, 1.0, 1.0, 0.25, 0.35),
    StrictQuarterlyRule("q_f91_m8_s100_bc25_fc45", 0.91, 8.0, 1.0, 1.0, 0.25, 0.45),
    StrictQuarterlyRule("q_f92_m8_s100_bc25_fc25", 0.92, 8.0, 1.0, 1.0, 0.25, 0.25),
    StrictQuarterlyRule("q_f93_m8_s100_bc25_fc25", 0.93, 8.0, 1.0, 1.0, 0.25, 0.25),
    StrictQuarterlyRule(
        "q_f92_m8_rec40_bc25_fc25",
        0.92,
        8.0,
        1.0,
        1.0,
        0.25,
        0.25,
        False,
        None,
        0.40,
        0.03,
    ),
    StrictQuarterlyRule(
        "q_f92_m8_rec60_bc25_fc25",
        0.92,
        8.0,
        1.0,
        1.0,
        0.25,
        0.25,
        False,
        None,
        0.60,
        0.03,
    ),
    StrictQuarterlyRule(
        "q_f92_m8_mrec60_bc25_fc25",
        0.92,
        8.0,
        1.0,
        1.0,
        0.25,
        0.25,
        False,
        None,
        0.60,
        0.03,
        0.05,
    ),
    StrictQuarterlyRule(
        "q_f92_m8_mrec80_bc25_fc25",
        0.92,
        8.0,
        1.0,
        1.0,
        0.25,
        0.25,
        False,
        None,
        0.80,
        0.03,
        0.05,
    ),
    StrictQuarterlyRule("q_annual_f91_m8_bc25_fc25", 0.91, 8.0, 1.0, 1.0, 0.25, 0.25, True),
    StrictQuarterlyRule("q_annual_f92_m8_bc25_fc25", 0.92, 8.0, 1.0, 1.0, 0.25, 0.25, True),
    StrictQuarterlyRule(
        "q_full125_wf_loss2_q50_cap0",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss2_q50_cap0", -0.02, 0.50, 0.0,
        ),
    ),
    StrictQuarterlyRule(
        "q_full125_wf_loss2_q60_cap25",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss2_q60_cap25", -0.02, 0.60, 0.25,
        ),
    ),
    StrictQuarterlyRule(
        "q_full125_wf_loss3_q50_cap0",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss3_q50_cap0", -0.03, 0.50, 0.0,
        ),
    ),
    StrictQuarterlyRule(
        "q_full125_wf_loss3_q60_cap25",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss3_q60_cap25", -0.03, 0.60, 0.25,
        ),
    ),
    StrictQuarterlyRule(
        "q_full125_wf_loss5_q50_cap0",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss5_q50_cap0", -0.05, 0.50, 0.0,
        ),
    ),
    StrictQuarterlyRule(
        "q_full125_wf_loss5_q60_cap25",
        0.0, 1.0, 1.25, 1.0, 0.25, 0.25,
        online_loss_guard=QuarterlyLossGuardConfig(
            "wf_loss5_q60_cap25", -0.05, 0.60, 0.25,
        ),
    ),
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_frontier_f{int(round(floor_pct * 100))}_m{int(multiplier)}_bc0_fc25",
        floor_pct,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
    )
    for floor_pct in (0.88, 0.90, 0.91, 0.92, 0.93, 0.94, 0.95)
    for multiplier in (4.0, 6.0, 8.0, 10.0, 12.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_commonuniverse_m1_m{int(round(multiplier * 100)):03d}"
            f"_e{int(round(minimum_exposure * 100)):02d}"
            f"_n{minimum_candidate_count:02d}_f893_bc0_fc25"
        ),
        0.893,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=minimum_candidate_count,
    )
    for multiplier in (4.00, 4.25)
    for minimum_exposure in (0.60, 0.65, 0.70)
    for minimum_candidate_count in (3, 5, 10)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_quality_tail6_hi{int(round(high_threshold * 100)):02d}"
            f"e{int(round(high_exposure * 100)):03d}"
            f"_lo35c{int(round(low_cap * 100)):02d}_m425_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="tail_stable6",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_min_exposure=high_exposure,
        quality_score_low_threshold=0.35,
        quality_score_low_exposure_cap=low_cap,
    )
    for high_threshold in (0.65, 0.70, 0.75)
    for high_exposure in (0.80, 0.90, 1.00)
    for low_cap in (0.00, 0.10, 0.20)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_tone_hi{int(round(high_threshold * 100)):02d}"
            f"e{int(round(high_exposure * 100)):03d}"
            f"_lo{int(round(low_threshold * 100)):02d}"
            f"c{int(round(low_cap * 100)):02d}_m425_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_min_exposure=high_exposure,
        quality_score_low_threshold=low_threshold,
        quality_score_low_exposure_cap=low_cap,
    )
    for high_threshold in (0.60, 0.70, 0.80)
    for high_exposure in (0.80, 1.00)
    for low_threshold in (0.20, 0.30)
    for low_cap in (0.00, 0.25)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_lowonly_lo{int(round(low_threshold * 100)):02d}"
            f"c{int(round(low_cap * 100)):02d}_m425_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_low_threshold=low_threshold,
        quality_score_low_exposure_cap=low_cap,
    )
    for low_threshold in (0.20, 0.30, 0.40, 0.50)
    for low_cap in (0.00, 0.10, 0.25)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_cushion_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}"
            f"_lo{int(round(low_threshold * 100)):02d}"
            f"m{int(round(low_multiplier * 100)):03d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_low_threshold=low_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_low_cushion_multiplier=low_multiplier,
    )
    for high_threshold in (0.60, 0.70, 0.80)
    for high_multiplier in (6.0, 8.0)
    for low_threshold in (0.20, 0.30)
    for low_multiplier in (2.0, 4.25)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_cushionfine_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_low_threshold=0.20,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_low_cushion_multiplier=4.25,
    )
    for high_threshold in (0.65, 0.70, 0.75)
    for high_multiplier in (6.25, 6.50, 6.75, 7.00, 7.25, 7.50)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_cushionmicro_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_low_threshold=0.20,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_low_cushion_multiplier=4.25,
    )
    for high_threshold in (0.68, 0.70, 0.72)
    for high_multiplier in (6.30, 6.35, 6.40, 6.45)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_trendcushion_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_high_requires_trend_confirmation=True,
    )
    for high_threshold in (0.60, 0.70, 0.80)
    for high_multiplier in (8.0, 10.0, 12.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_tiercap_m{int(round(high_multiplier * 100)):04d}"
            f"_s{int(round(severe_cap * 100)):02d}"
            f"c{int(round(correction_cap * 100)):02d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_high_severe_crisis_cap=severe_cap,
        quality_score_high_correction_cap=correction_cap,
    )
    for high_multiplier in (8.0, 10.0, 12.0)
    for severe_cap in (0.65, 0.70, 0.75)
    for correction_cap in (0.35, 0.40)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_tiercapfine_s{int(round(severe_cap * 100)):02d}"
            f"c{int(round(correction_cap * 100)):02d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=severe_cap,
        quality_score_high_correction_cap=correction_cap,
    )
    for severe_cap in (0.71, 0.72, 0.73, 0.74)
    for correction_cap in (0.34, 0.35, 0.36, 0.37)
)

# A 0.893 high-water floor permits roughly 10.7% loss by construction.  These
# variants recalibrate the same PBoC rule for the true frozen-share simulator,
# where there is no hidden daily constant-weight rebalance to damp the path.
RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_frozenfloor_f{int(round(floor_pct * 1000)):03d}"
            "_s73c35_n03"
        ),
        floor_pct,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=0.73,
        quality_score_high_correction_cap=0.35,
    )
    for floor_pct in (0.900, 0.902, 0.905, 0.908, 0.910)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_frozenrecal_e{int(round(recovery_exposure * 100)):02d}"
            f"c{int(round(correction_cap * 100)):02d}_f900_n03"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=recovery_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=recovery_exposure,
        quality_score_high_correction_cap=correction_cap,
    )
    for recovery_exposure in (0.60, 0.62, 0.64, 0.66)
    for correction_cap in (0.30, 0.32, 0.34)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_frozenmargin_e{int(round(recovery_exposure * 100)):02d}"
            f"c{int(round(correction_cap * 100)):02d}_f900_n03"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=recovery_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=recovery_exposure,
        quality_score_high_correction_cap=correction_cap,
    )
    for recovery_exposure in (0.64, 0.65)
    for correction_cap in (0.28, 0.29)
)

# Diagnostic ablation: correlated risk flags still enforce the ordinary
# feature-risk cap, but their raw count cannot independently force a full exit.
RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_no_flagcount_exit",
        risk_flag_exit_count=None,
    )
    for rule in RULES
    if rule.name == "q_pboc_frozenmargin_e64c28_f900_n03"
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_cluster2persistent_exit",
        risk_flag_exit_count=None,
        risk_flag_exit_prior_count=None,
        risk_cluster_exit_count=2,
        risk_cluster_exit_prior_count=2,
    )
    for rule in RULES
    if rule.name == "q_pboc_frozenmargin_e64c28_f900_n03"
)

# A second diagnostic keeps the three-flag exit, but requires the same level of
# broad risk concurrence at the immediately preceding quarterly decision. This
# tests persistence without adding any intra-quarter observation.
RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_persistent_flagcount_exit",
        risk_flag_exit_prior_count=3,
    )
    for rule in RULES
    if rule.name == "q_pboc_frozenmargin_e64c28_f900_n03"
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_return4_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_e64c28_f900"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="quarterly_return_confirmation4",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_low_threshold=0.25,
        quality_score_low_cushion_multiplier=2.0,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.28,
    )
    for high_threshold in (0.65, 0.70, 0.75)
    for high_multiplier in (8.0, 10.0, 12.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_curvecap_t{int(round(curve_threshold * 100)):02d}"
            f"c{int(round(curve_cap * 100)):02d}_corr35_e64_f900"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.35,
        feature_exposure_cap_name="domestic_gov_curve_10y1y_percentile_3y",
        feature_exposure_cap_threshold=curve_threshold,
        feature_exposure_cap_value=curve_cap,
    )
    for curve_threshold in (0.55, 0.60, 0.65)
    for curve_cap in (0.24, 0.26, 0.28)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_etfsharecap_t{int(round(crowding_threshold * 100)):02d}"
            f"c{int(round(exposure_cap * 100)):02d}_e64_f900"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.28,
        feature_exposure_cap_name="etf_share_growth_1q_positive_ratio",
        feature_exposure_cap_threshold=crowding_threshold,
        feature_exposure_cap_value=exposure_cap,
    )
    for crowding_threshold in (0.50, 0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58, 0.60)
    for exposure_cap in (0.0, 0.10, 0.20)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_persistent_flagcount_exit",
        risk_flag_exit_prior_count=3,
    )
    for rule in RULES
    if rule.name == "q_etfsharecap_t55c00_e64_f900"
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_cluster2persistent_exit",
        risk_flag_exit_count=None,
        risk_flag_exit_prior_count=None,
        risk_cluster_exit_count=2,
        risk_cluster_exit_prior_count=2,
    )
    for rule in RULES
    if rule.name == "q_etfsharecap_t55c00_e64_f900"
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_{label}",
        direction_policy=policy,
    )
    for rule in RULES
    if rule.name == "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit"
    for label, policy in (
        ("direction_dxy", QUARTERLY_DXY_LOCAL_DIRECTION),
        ("direction_ridge", QUARTERLY_RIDGE_CORE_DIRECTION),
        ("direction_fund_boost", QUARTERLY_FUND_BOOST_DIRECTION),
        ("direction_return4_boost", QUARTERLY_RETURN4_BOOST_DIRECTION),
    )
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"{rule.name}_return4m{int(round(boost * 100)):03d}"
            f"_fc{int(round(feature_cap * 100)):02d}"
        ),
        feature_risk_cap=feature_cap,
        direction_policy=replace(
            QUARTERLY_RETURN4_BOOST_DIRECTION,
            name=f"quarterly_return4_boost_{boost:.2f}",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name == "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit"
    for boost, feature_cap in (
        (1.25, 0.24),
        (1.30, 0.24),
        (1.30, 0.23),
        (1.35, 0.23),
        (1.35, 0.22),
        (1.40, 0.22),
        (1.40, 0.21),
        (1.45, 0.21),
        (1.50, 0.20),
        (1.55, 0.19),
        (1.60, 0.18),
        (1.70, 0.16),
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_bc{int(round(bear_cap * 100)):02d}",
        bear_cap=bear_cap,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21"
    )
    for bear_cap in (
        0.05,
        0.10,
        0.15,
        0.20,
        0.25,
        0.30,
        0.32,
        0.34,
        0.36,
        0.38,
        0.40,
        0.50,
        0.64,
    )
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"{rule.name}_binnedh24s8m"
            f"{int(round(boost * 100)):03d}"
        ),
        direction_policy=replace(
            QUARTERLY_BINNED_RETURN4_DIRECTION,
            name=f"quarterly_binned_return4_h24_s8_m{boost:.2f}",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36"
    )
    for boost in (1.05, 1.10, 1.15, 1.25, 1.35)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_{label}",
        annual_weight_overrides=overrides,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36"
    )
    for label, overrides in (
        ("annual60to00", ((0.60, 0.00),)),
        ("annual60to30", ((0.60, 0.30),)),
        ("annual85to65", ((0.85, 0.65),)),
        ("annual85to80", ((0.85, 0.80),)),
        ("annual60to30_85to65", ((0.60, 0.30), (0.85, 0.65))),
        ("annual60to30_85to80", ((0.60, 0.30), (0.85, 0.80))),
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_riskcap{int(round(risk_cap * 1000)):03d}",
        feature_risk_cap=risk_cap,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00"
    )
    for risk_cap in (0.16, 0.17, 0.18, 0.19, 0.20, 0.205, 0.2075)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_{label}",
        direction_block_pboc_tone_lte=tone_threshold,
        direction_block_cs300_ma_6m_distance_lt=ma_threshold,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00"
    )
    for label, tone_threshold, ma_threshold in (
        ("dirblock_tone0_ma0", 0.0, 0.0),
        ("dirblock_tone0_man02", 0.0, -0.02),
        ("dirblock_tonen5_ma0", -5.0, 0.0),
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_floor{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0"
    )
    for floor_pct in (0.901, 0.902, 0.903, 0.905)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_riskcap{int(round(risk_cap * 10000)):04d}",
        feature_risk_cap=risk_cap,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_floor901"
    )
    for risk_cap in (
        0.18, 0.185, 0.19, 0.192, 0.194, 0.195, 0.198, 0.201,
        0.204, 0.205, 0.2075, 0.2085, 0.209,
    )
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
            f"return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_"
            f"floor{int(round(floor_pct * 1000)):03d}_riskcap1850"
        ),
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_"
        "floor901_riskcap1850"
    )
    for floor_pct in (0.902, 0.905, 0.908, 0.910, 0.912, 0.915)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_binned40s4t01_floor{int(round(floor_pct * 1000)):03d}_"
            f"boost{int(round(boost * 100)):03d}_riskcap1850"
        ),
        floor_pct=floor_pct,
        direction_policy=replace(
            QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.2f}",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_"
        "floor910_riskcap1850"
    )
    for floor_pct in (0.910, 0.912, 0.915, 0.918, 0.920)
    for boost in (1.10, 1.20, 1.30, 1.40, 1.50, 1.60, 1.75)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_jointgate_floor{int(round(floor_pct * 1000)):03d}_"
            f"boost{int(round(boost * 100)):03d}_riskcap1850"
        ),
        floor_pct=floor_pct,
        direction_policy=replace(
            QUARTERLY_BINNED_RETURN4_H40_S4_T01_DIRECTION,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.2f}",
            nonnegative_exposure_multiplier=boost,
        ),
        direction_risk_gate_policy=(
            QUARTERLY_BINNED_PATHRISK_DOMESTIC_PATH4_H24_S8_T12
        ),
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_"
        "floor910_riskcap1850"
    )
    for floor_pct in (0.910, 0.912, 0.915, 0.918, 0.920, 0.925, 0.930)
    for boost in (1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 3.00)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_rankvote_jointgate_floor{int(round(floor_pct * 1000)):03d}_"
            f"boost{int(round(boost * 100)):03d}_riskcap1850"
        ),
        floor_pct=floor_pct,
        direction_policy=replace(
            QUARTERLY_RETURN4_BOOST_DIRECTION,
            name=f"quarterly_return4_boost_{boost:.2f}_domestic_path_gate",
            nonnegative_exposure_multiplier=boost,
        ),
        direction_risk_gate_policy=(
            QUARTERLY_BINNED_PATHRISK_DOMESTIC_PATH4_H24_S8_T12
        ),
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_annual60to00_dirblock_tone0_ma0_"
        "floor910_riskcap1850"
    )
    for floor_pct in (0.910, 0.912, 0.915, 0.918, 0.920, 0.925, 0.930)
    for boost in (1.45, 1.60, 1.75, 2.00, 2.25, 2.50, 3.00)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_jointgate_fine_floor910_boost{int(round(boost * 1000)):04d}",
        direction_policy=replace(
            rule.direction_policy,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.3f}",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_jointgate_floor910_boost150_riskcap1850"
    for boost in (1.525, 1.550, 1.575, 1.600, 1.625, 1.650, 1.675, 1.700, 1.725)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_safecap_floor{int(round(floor_pct * 1000)):03d}_"
            f"boost{int(round(boost * 1000)):04d}_"
            f"cap{int(round(safe_cap * 1000)):03d}"
        ),
        floor_pct=floor_pct,
        direction_policy=replace(
            rule.direction_policy,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.3f}",
            nonnegative_exposure_multiplier=boost,
        ),
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_jointgate_floor910_boost150_riskcap1850"
    for floor_pct in (0.910, 0.912)
    for boost in (1.500, 1.550)
    for safe_cap in (0.210, 0.240, 0.270, 0.300, 0.360)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_safecap_hi_floor910_"
            f"boost{int(round(boost * 1000)):04d}_"
            f"cap{int(round(safe_cap * 1000)):04d}"
        ),
        direction_policy=replace(
            rule.direction_policy,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.3f}",
            nonnegative_exposure_multiplier=boost,
        ),
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_jointgate_floor910_boost150_riskcap1850"
    for boost in (1.500, 1.550)
    for safe_cap in (0.450, 0.600, 0.800, 1.000)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_safecap_fine_floor910_boost1550_cap{int(round(safe_cap * 1000)):03d}",
        direction_policy=replace(
            rule.direction_policy,
            name="quarterly_binned_return4_h40_s4_t01_m1.550",
            nonnegative_exposure_multiplier=1.550,
        ),
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_jointgate_floor910_boost150_riskcap1850"
    for safe_cap in (0.390, 0.420, 0.450, 0.480, 0.510, 0.540, 0.570)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_annualscore_lte{abs(score_lte):02d}_"
            f"m{int(round(multiplier * 100)):03d}"
        ),
        annual_score_boost_lte=score_lte,
        annual_score_cushion_multiplier=multiplier,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_safecap_fine_floor910_boost1550_cap450"
    for score_lte in (0, -1, -2, -3)
    for multiplier in (1.10, 1.25, 1.50, 1.75)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_annualscore_fine_m{int(round(multiplier * 1000)):04d}",
        annual_score_cushion_multiplier=multiplier,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_annualscore_lte00_m110"
    for multiplier in (1.025, 1.050, 1.075, 1.100, 1.125, 1.150, 1.175, 1.200)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_v9boost_m{int(round(boost * 1000)):04d}",
        direction_policy=replace(
            rule.direction_policy,
            name=f"quarterly_binned_return4_h40_s4_t01_m{boost:.3f}",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_annualscore_fine_m1175"
    for boost in (1.550, 1.565, 1.575, 1.585, 1.600, 1.625)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_dedupscope_{scope_name}_"
            f"cap{int(round(safe_cap * 1000)):03d}"
        ),
        feature_risk_safe_gate_cap=safe_cap,
        feature_risk_safe_gate_clusters=clusters,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_v9boost_m1575"
    for scope_name, clusters in (
        (
            "price_breadth_macro",
            ("price_cycle", "breadth_leadership", "macro_liquidity"),
        ),
        ("price_breadth", ("price_cycle", "breadth_leadership")),
        ("price", ("price_cycle",)),
        ("none", ()),
    )
    for safe_cap in (0.300, 0.360, 0.450)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_dedup_riskcap{int(round(risk_cap * 1000)):03d}",
        feature_risk_cap=risk_cap,
        feature_risk_safe_gate_clusters=(),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedupscope_none_cap300"
    for risk_cap in (0.100, 0.120, 0.140, 0.150, 0.160, 0.165, 0.170, 0.175, 0.180)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_dedup_qmult{int(round(multiplier * 100)):03d}",
        quality_score_high_cushion_multiplier=multiplier,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_riskcap170"
    for multiplier in (6.50, 7.00, 7.25, 7.50, 7.75, 8.00)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_dedup_fine_q{int(round(multiplier * 1000)):04d}_"
            f"r{int(round(risk_cap * 1000)):03d}"
        ),
        quality_score_high_cushion_multiplier=multiplier,
        feature_risk_cap=risk_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_qmult775"
    for multiplier in (7.760, 7.770, 7.780, 7.790)
    for risk_cap in (0.170, 0.171, 0.172)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_dedup_marginblock_cap{int(round(safe_cap * 1000)):03d}",
        feature_risk_safe_gate_cap=safe_cap,
        feature_risk_safe_gate_clusters=(
            "price_cycle",
            "leverage_crowding",
            "breadth_leadership",
            "macro_liquidity",
        ),
        feature_risk_safe_gate_block_flags=("daily_margin_rally_flag",),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_fine_q7790_r171"
    for safe_cap in (0.300, 0.360, 0.450)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_dedup_marginblock_fine_cap{int(round(safe_cap * 1000)):03d}",
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_cap360"
    for safe_cap in (0.330, 0.340, 0.350, 0.370, 0.380, 0.390, 0.400, 0.420)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_gapfixed_dedup_marginblock_micro_cap{int(round(safe_cap * 1000)):03d}",
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for safe_cap in (0.310, 0.315, 0.320, 0.325, 0.335)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_gapfixed_dedup_marginblock_etfshare_"
            + ("off" if feature_cap is None else f"cap{int(round(feature_cap * 1000)):03d}")
        ),
        feature_exposure_cap_name=(
            None if feature_cap is None else "etf_share_growth_1q_positive_ratio"
        ),
        feature_exposure_cap_threshold=None if feature_cap is None else 0.55,
        feature_exposure_cap_value=feature_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for feature_cap in (None, 0.05, 0.10, 0.15, 0.171, 0.20)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_gapfixed_dedup_marginblock_etfshare_"
            f"t{int(round(feature_threshold * 100)):02d}_cap000"
        ),
        feature_exposure_cap_threshold=feature_threshold,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for feature_threshold in (0.60, 0.65, 0.70, 0.75)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_gapfixed_dedup_marginblock_etfshare_fine_"
            f"t{int(round(feature_threshold * 1000)):03d}_"
            f"cap{int(round(feature_cap * 1000)):03d}"
        ),
        feature_exposure_cap_threshold=feature_threshold,
        feature_exposure_cap_value=feature_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for feature_threshold in (0.53, 0.54, 0.55, 0.56, 0.57, 0.58)
    for feature_cap in (0.10, 0.11, 0.12, 0.13, 0.14)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_gapfixed_dedup_marginblock_etfshare_micro_"
            f"cap{int(round(feature_cap * 10000)):04d}"
        ),
        feature_exposure_cap_value=feature_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for feature_cap in (0.100, 0.101, 0.1015, 0.102, 0.1025, 0.103, 0.105, 0.108)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_budget_"
            f"risk{int(round(risk_cap * 1000)):03d}_"
            f"dir{int(round(direction_multiplier * 1000)):04d}"
        ),
        feature_risk_cap=risk_cap,
        direction_policy=replace(
            rule.direction_policy,
            name=(
                f"{rule.direction_policy.name}_"
                f"m{direction_multiplier:.3f}"
            ),
            nonnegative_exposure_multiplier=direction_multiplier,
        ),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_etfshare_cap100"
    for risk_cap in (0.14, 0.15, 0.16)
    for direction_multiplier in (1.8, 2.0, 2.2)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_caporder_bearcap{int(round(bear_cap * 1000)):03d}",
        bear_cap=bear_cap,
    )
    for rule in RULES
    if rule.name == "q_caporder_budget_risk160_dir2000"
    for bear_cap in (
        0.40, 0.45, 0.50, 0.52, 0.55, 0.56, 0.565, 0.57, 0.575,
        0.58, 0.60, 0.62, 0.65, 0.70,
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"q_caporder_safe{int(round(safe_cap * 1000)):03d}",
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_caporder_bearcap565"
    for safe_cap in (
        0.34, 0.36, 0.38, 0.40, 0.45, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60,
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"q_caporder_pbocm{int(round(quality_multiplier * 100)):04d}",
        quality_score_high_cushion_multiplier=quality_multiplier,
    )
    for rule in RULES
    if rule.name == "q_caporder_safe560"
    for quality_multiplier in (8.0, 8.1, 8.2, 8.4, 8.6, 8.8, 9.0, 10.0, 12.0)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_caporder_pbocbear_m{int(round(quality_multiplier * 100)):04d}_"
            f"bc{int(round(bear_cap * 1000)):03d}"
        ),
        quality_score_high_cushion_multiplier=quality_multiplier,
        bear_cap=bear_cap,
    )
    for rule in RULES
    if rule.name == "q_caporder_safe560"
    for quality_multiplier in (8.4, 8.6)
    for bear_cap in (0.54, 0.55, 0.56)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_annualboost_"
            f"m{int(round(annual_multiplier * 1000)):04d}"
        ),
        annual_score_cushion_multiplier=annual_multiplier,
    )
    for rule in RULES
    if rule.name == "q_caporder_pbocm0820"
    for annual_multiplier in (1.0, 1.10, 1.25, 1.35, 1.50)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_coldstart_"
            f"cap{int(round(cold_start_cap * 1000)):03d}"
        ),
        feature_risk_cold_start_gate_cap=cold_start_cap,
    )
    for rule in RULES
    if rule.name == "q_caporder_pbocm0820"
    for cold_start_cap in (
        0.20, 0.22, 0.24, 0.25, 0.26, 0.28, 0.30, 0.33, 0.40, 0.56,
    )
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_coldstartboost_"
            f"m{int(round(cold_start_multiplier * 100)):03d}"
        ),
        feature_risk_cold_start_multiplier=cold_start_multiplier,
    )
    for rule in RULES
    if rule.name == "q_caporder_coldstart_cap240"
    for cold_start_multiplier in (1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_coldgrid_"
            f"cap{int(round(cold_start_cap * 1000)):03d}_m300"
        ),
        feature_risk_cold_start_gate_cap=cold_start_cap,
        feature_risk_cold_start_multiplier=3.0,
    )
    for rule in RULES
    if rule.name == "q_caporder_pbocm0820"
    for cold_start_cap in (0.20, 0.22, 0.24, 0.26, 0.27, 0.28, 0.29, 0.30)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_cold29_"
            f"m{int(round(cold_start_multiplier * 100)):03d}"
        ),
        feature_risk_cold_start_gate_cap=0.29,
        feature_risk_cold_start_multiplier=cold_start_multiplier,
    )
    for rule in RULES
    if rule.name == "q_caporder_pbocm0820"
    for cold_start_multiplier in (2.5, 4.0)
)

RULES += tuple(
    replace(
        rule,
        name=(
            "q_caporder_crisisrs_"
            f"cap{int(round(reentry_cap * 1000)):03d}"
        ),
        crisis_relative_strength_reentry_cap=reentry_cap,
    )
    for rule in RULES
    if rule.name == "q_caporder_cold29_m400"
    for reentry_cap in (0.08, 0.10, 0.12, 0.14, 0.16)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_floor{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == "q_caporder_cold29_m400"
    for floor_pct in (0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.91)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_riskcap{int(round(risk_cap * 1000)):03d}",
        feature_risk_cap=risk_cap,
    )
    for rule in RULES
    if rule.name == "q_mdd20_floor820"
    for risk_cap in (0.16, 0.20, 0.24, 0.28, 0.32, 0.36)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_bearcap{int(round(bear_cap * 1000)):03d}",
        bear_cap=bear_cap,
    )
    for rule in RULES
    if rule.name == "q_mdd20_floor820"
    for bear_cap in (0.565, 0.60, 0.65, 0.70, 0.80, 1.00)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_bear100_floor{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == "q_mdd20_bearcap1000"
    for floor_pct in (0.795, 0.800, 0.805, 0.810, 0.820)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_safecap{int(round(safe_cap * 1000)):03d}",
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_mdd20_bear100_floor795"
    for safe_cap in (0.56, 0.65, 0.75, 0.85, 1.00)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_safe100_floor{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == "q_mdd20_safecap1000"
    for floor_pct in (0.788, 0.790, 0.792, 0.794, 0.795, 0.796, 0.798)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_safe100_floorlow{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    if rule.name == "q_mdd20_safecap1000"
    for floor_pct in (0.750, 0.760, 0.770, 0.780, 0.784, 0.786, 0.788)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_dir{int(round(direction_multiplier * 1000)):04d}",
        direction_policy=replace(
            rule.direction_policy,
            name=f"{rule.direction_policy.name}_m{direction_multiplier:.3f}",
            nonnegative_exposure_multiplier=direction_multiplier,
        ),
    )
    for rule in RULES
    if rule.name == "q_mdd20_safe100_floorlow780"
    for direction_multiplier in (2.0, 2.1, 2.2, 2.4, 2.6, 3.0)
)

RULES += tuple(
    replace(
        rule,
        name=f"q_mdd20_dirlow{int(round(direction_multiplier * 1000)):04d}",
        direction_policy=replace(
            rule.direction_policy,
            name=f"{rule.direction_policy.name}_m{direction_multiplier:.3f}",
            nonnegative_exposure_multiplier=direction_multiplier,
        ),
    )
    for rule in RULES
    if rule.name == "q_mdd20_safe100_floorlow780"
    for direction_multiplier in (1.0, 1.25, 1.50, 1.75, 2.0)
)

RULES += tuple(
    replace(
        rule,
        name="q_mdd20_failure_signal_v1",
        direction_risk_gate_rejection_cap=0.50,
        cold_start_price_damage_cap=0.50,
    )
    for rule in RULES
    if rule.name == "q_mdd20_dirlow1500"
)

RULES += tuple(
    replace(
        rule,
        name=name,
        direction_risk_gate_rejection_cap=risk_rejection_cap,
        cold_start_price_damage_cap=cold_damage_cap,
    )
    for rule in RULES
    if rule.name == "q_mdd20_dirlow1500"
    for name, risk_rejection_cap, cold_damage_cap in (
        ("q_mdd20_failure_signal_riskonly", 0.50, None),
        ("q_mdd20_failure_signal_coldonly", None, 0.50),
    )
)

RULES += tuple(
    replace(
        rule,
        name="q_mdd20_failure_signal_v2_lowvolblock",
        feature_risk_safe_gate_block_flags=(
            *rule.feature_risk_safe_gate_block_flags,
            "low_vol_mature_trend_flag",
        ),
    )
    for rule in RULES
    if rule.name == "q_mdd20_failure_signal_v1"
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_dedup_leverageexhaust_block_"
            f"cap{int(round(safe_cap * 1000)):03d}"
        ),
        feature_risk_safe_gate_cap=safe_cap,
        feature_risk_safe_gate_block_flags=(
            "leveraged_rally_exhaustion_flag",
        ),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for safe_cap in (0.300, 0.330, 0.360, 0.450)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_dedup_no_cluster_exit_"
            f"cap{int(round(safe_cap * 1000)):03d}"
        ),
        risk_cluster_exit_count=None,
        risk_cluster_exit_prior_count=None,
        feature_risk_safe_gate_cap=safe_cap,
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_marginblock_fine_cap330"
    for safe_cap in (0.200, 0.250, 0.300, 0.330)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"q_gapfixed_dedup_{gate_name}_"
            f"t{int(round(abs(threshold) * 100)):02d}_"
            f"cap{int(round(safe_cap * 1000)):03d}"
        ),
        direction_risk_gate_policy=replace(
            gate,
            name=f"{gate.name}_t{abs(threshold):.2f}",
            positive_score_gt=threshold,
        ),
        feature_risk_safe_gate_cap=safe_cap,
        feature_risk_safe_gate_clusters=(
            "price_cycle",
            "leverage_crowding",
            "breadth_leadership",
            "macro_liquidity",
        ),
    )
    for rule in RULES
    if rule.name == "q_gapfixed_dedup_fine_q7790_r171"
    for gate_name, gate in (
        ("crowding6", QUARTERLY_BINNED_PATHRISK_CROWDING6_H24_S8),
        ("tail6", QUARTERLY_BINNED_PATHRISK_TAIL6_H24_S8),
    )
    for threshold in (-0.12, -0.10)
    for safe_cap in (0.300, 0.360, 0.450)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_pathriskgate_t{int(round(abs(threshold) * 100)):02d}",
        direction_risk_gate_policy=replace(
            QUARTERLY_BINNED_PATHRISK_GATE,
            name=(
                "quarterly_binned_pathrisk_domestic6_h24_s4_"
                f"t{abs(threshold):.2f}"
            ),
            positive_score_gt=threshold,
        ),
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36"
    )
    for threshold in (-0.11, -0.12, -0.13)
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_m{int(round(boost * 100)):03d}",
        direction_policy=replace(
            rule.direction_policy,
            name=f"quarterly_return4_boost_{boost:.2f}_pathrisk_gate",
            nonnegative_exposure_multiplier=boost,
        ),
    )
    for rule in RULES
    if rule.name in {
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_pathriskgate_t12",
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36_pathriskgate_t13",
    }
    for boost in (1.50, 1.55, 1.60, 1.70)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"{rule.name}_bqdc"
            f"{int(round(bear_quality_direction_cap * 100)):02d}"
        ),
        bear_quality_direction_cap=bear_quality_direction_cap,
    )
    for rule in RULES
    if rule.name in {
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc20",
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36",
    }
    for bear_quality_direction_cap in (0.50, 0.64, 0.80)
    if bear_quality_direction_cap > rule.bear_cap
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_{label}",
        feature_risk_cap_clusters=clusters,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36"
    )
    for label, clusters in (
        ("rc_crisis_macro_breadth", ("crisis", "macro_liquidity", "breadth_leadership")),
        ("rc_crisis_macro", ("crisis", "macro_liquidity")),
        ("rc_macro_breadth", ("macro_liquidity", "breadth_leadership")),
        ("rc_macro", ("macro_liquidity",)),
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_rplc{int(round(relaxed_cap * 100)):02d}",
        feature_risk_relaxed_clusters=("price_cycle", "leverage_crowding"),
        feature_risk_relaxed_cap=relaxed_cap,
    )
    for rule in RULES
    if rule.name == (
        "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
        "return4m145_fc21_bc36"
    )
    for relaxed_cap in (0.24, 0.27, 0.30, 0.36)
)

RULES += tuple(
    replace(
        rule,
        name=(
            f"{rule.name}_return4m{int(round(boost * 100)):03d}"
            f"_fc{int(round(feature_cap * 100)):02d}"
            f"_dd{int(round(abs(drawdown_guard) * 100)):02d}"
        ),
        feature_risk_cap=feature_cap,
        direction_policy=replace(
            QUARTERLY_RETURN4_BOOST_DIRECTION,
            name=f"quarterly_return4_boost_{boost:.2f}_dd{abs(drawdown_guard):.2f}",
            nonnegative_exposure_multiplier=boost,
            boost_allowed_drawdown_gte=drawdown_guard,
        ),
    )
    for rule in RULES
    if rule.name == "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit"
    for boost, feature_cap, drawdown_guard in (
        (1.50, 0.20, -0.05),
        (1.55, 0.19, -0.05),
        (1.60, 0.18, -0.05),
        (1.60, 0.18, -0.03),
    )
)

RULES += tuple(
    replace(
        rule,
        name=f"{rule.name}_f{int(round(floor_pct * 1000)):03d}",
        floor_pct=floor_pct,
    )
    for rule in RULES
    for source_name, floor_pct in (
        (
            "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
            "return4m150_fc20_dd05",
            0.903,
        ),
        (
            "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
            "return4m155_fc19_dd05",
            0.906,
        ),
        (
            "q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_"
            "return4m160_fc18_dd03",
            0.909,
        ),
    )
    if rule.name == source_name
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_etfsharebarbell_l{int(round(low_crowding * 100)):02d}"
            f"m{int(round(boost_multiplier * 100)):04d}_h55c00_e64"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=0.70,
        quality_score_high_cushion_multiplier=8.0,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.28,
        feature_exposure_cap_name="etf_share_growth_1q_positive_ratio",
        feature_exposure_cap_threshold=0.55,
        feature_exposure_cap_value=0.0,
        feature_cushion_multiplier_name="etf_share_growth_1q_positive_ratio",
        feature_cushion_multiplier_max_value=low_crowding,
        feature_cushion_multiplier_value=boost_multiplier,
    )
    for low_crowding in (0.20, 0.25, 0.30, 0.35, 0.40)
    for boost_multiplier in (5.0, 6.0, 7.0, 7.2, 7.4, 7.6, 7.8, 8.0, 12.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_return4highonly_hi{int(round(high_threshold * 100)):02d}"
            "m1000_e64c28_f900"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="quarterly_return_confirmation4",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=10.0,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.28,
    )
    for high_threshold in (0.65, 0.70, 0.75)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboccurve_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_e64c28_f900"
        ),
        0.900,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.64,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_curve_safety2",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_high_severe_crisis_cap=0.64,
        quality_score_high_correction_cap=0.28,
    )
    for high_threshold in (0.65, 0.70, 0.75)
    for high_multiplier in (8.0, 10.0, 12.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_relvalue_tiercap_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_s73c35_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="basket_relative_value1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_high_severe_crisis_cap=0.73,
        quality_score_high_correction_cap=0.35,
    )
    for high_threshold in (0.60, 0.70, 0.80)
    for high_multiplier in (6.0, 8.0, 10.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_{feature_set}_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set=feature_set,
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
    )
    for feature_set in ("selected_tail6", "selected_return_tail8")
    for high_threshold in (0.60, 0.70, 0.80)
    for high_multiplier in (6.0, 8.0, 10.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_pboc_crisisblock_hi{int(round(high_threshold * 100)):02d}"
            f"m{int(round(high_multiplier * 100)):04d}_n03"
        ),
        0.893,
        4.25,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=0.70,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
        recovery_selector_candidate_count_min=3,
        quality_score_feature_set="pboc_tone1",
        quality_score_min_history=12,
        quality_score_high_threshold=high_threshold,
        quality_score_high_cushion_multiplier=high_multiplier,
        quality_score_high_blocks_crisis_rebound=True,
    )
    for high_threshold in (0.65, 0.70, 0.75)
    for high_multiplier in (8.0, 10.0, 12.0)
)

# Fine-grained frontier around the best strict quarterly region.  The names use
# basis points for the floor and tenths for the multiplier so nearby values do
# not collide after rounding (for example 0.895 and 0.90 remain distinct).
RULES += tuple(
    StrictQuarterlyRule(
        f"q_fine_f{int(round(floor_pct * 1000)):03d}_m{int(round(multiplier * 10)):02d}_bc0_fc25",
        floor_pct,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
    )
    for floor_pct in (0.89, 0.895, 0.90, 0.905, 0.91)
    for multiplier in (5.0, 5.5, 6.0, 6.5, 7.0)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_ultrafine_f{int(round(floor_pct * 1000)):03d}_m{int(round(multiplier * 10)):02d}_bc0_fc25",
        floor_pct,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
    )
    for floor_pct in (0.893, 0.894, 0.895, 0.896, 0.897)
    for multiplier in (5.3, 5.4, 5.5, 5.6, 5.7)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_microfine_f893_m{int(round(multiplier * 100)):03d}_bc0_fc25",
        0.893,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
    )
    for multiplier in (5.60, 5.62, 5.64, 5.66, 5.68)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_disp_q{int(round(quantile * 100))}_e{int(round(minimum_exposure * 100))}_f893_m566_bc0_fc25",
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        selector_dispersion_quantile=quantile,
        selector_dispersion_min_exposure=minimum_exposure,
    )
    for quantile in (0.50, 0.60, 0.70, 0.80)
    for minimum_exposure in (0.20, 0.25, 0.30, 0.35)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_recovery_r{int(round(return_threshold * 100)):02d}"
            f"_m{int(round(market_threshold * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
            if market_threshold is not None
            else (
                f"q_recovery_r{int(round(return_threshold * 100)):02d}"
                f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
            )
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=return_threshold,
        recovery_market_return_threshold=market_threshold,
    )
    for market_threshold in (None, 0.05)
    for return_threshold in (0.03, 0.05, 0.08)
    for minimum_exposure in (0.10, 0.15, 0.20, 0.25, 0.30)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_mrec_m{int(round(market_threshold * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=market_threshold,
    )
    for market_threshold in (0.00, 0.02, 0.04, 0.05, 0.06, 0.08, 0.10)
    for minimum_exposure in (0.30, 0.40, 0.50, 0.60, 0.70)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_mrecfine_m05_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25",
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.05,
    )
    for minimum_exposure in (0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48, 0.49)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_mrechigh_m05_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25",
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.05,
    )
    for minimum_exposure in (
        0.51, 0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58, 0.59,
        0.60, 0.62, 0.64, 0.66, 0.68, 0.70,
    )
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_strongrec_m3{int(round(return_3m * 100)):02d}"
            f"_m6{int(round(return_6m * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):03d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=return_3m,
        recovery_market_return_6m_threshold=return_6m,
        recovery_market_ma_6m_distance_threshold=0.0,
    )
    for return_3m in (0.03, 0.05, 0.08)
    for return_6m in (0.00, 0.05, 0.10)
    for minimum_exposure in (0.60, 0.80, 1.00)
)

RULES += tuple(
    StrictQuarterlyRule(
        f"q_strongfine_m308_m600_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25",
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
    )
    for minimum_exposure in (0.61, 0.62, 0.63, 0.64, 0.65, 0.66, 0.67, 0.68, 0.69, 0.70)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_surgingrec_m3{int(round(return_3m * 100)):02d}"
            f"_m6{int(round(return_6m * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):03d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=return_3m,
        recovery_market_return_6m_threshold=return_6m,
        recovery_market_ma_6m_distance_threshold=0.0,
    )
    for return_3m in (0.10, 0.12, 0.15, 0.20)
    for return_6m in (0.10, 0.15, 0.20)
    for minimum_exposure in (0.80, 1.00)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_stagedrec_e{int(round(base_exposure * 100)):02d}"
            f"_se{int(round(strong_exposure * 100)):02d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=base_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.05,
        secondary_recovery_min_exposure=strong_exposure,
        secondary_recovery_market_return_threshold=0.08,
        secondary_recovery_market_return_6m_threshold=0.0,
        secondary_recovery_market_ma_6m_distance_threshold=0.0,
    )
    for base_exposure in (0.45, 0.50, 0.52)
    for strong_exposure in (0.60, 0.65, 0.70)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_featurerec_{label}_e{int(round(minimum_exposure * 100)):03d}"
            f"_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=basket_drawdown,
        recovery_m1_m2_change_3m_threshold=m1_m2_change,
        recovery_basket_excess_return_6m_max=basket_excess_max,
        recovery_fund_active_issuance_percentile_min=fund_issuance_min,
    )
    for label, basket_drawdown, m1_m2_change, basket_excess_max, fund_issuance_min in (
        ("bd10", -0.10, None, None, None),
        ("bd05", -0.05, None, None, None),
        ("m1m2", None, 0.0, None, None),
        ("excess0", None, None, 0.0, None),
        ("fund50", None, None, None, 0.50),
        ("bd10_m1m2", -0.10, 0.0, None, None),
        ("bd10_fund50", -0.10, None, None, 0.50),
    )
    for minimum_exposure in (0.70, 0.80, 1.00)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_bdgrid_m3{int(round(return_3m * 100)):02d}"
            f"_bd{int(round(abs(basket_drawdown) * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=return_3m,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=basket_drawdown,
    )
    for return_3m in (0.03, 0.05, 0.08)
    for basket_drawdown in (-0.03, -0.05, -0.07)
    for minimum_exposure in (0.67, 0.70, 0.75)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_volcap_v{int(round(basket_vol_max * 100)):02d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_basket_vol_3m_max=basket_vol_max,
    )
    for basket_vol_max in (0.16, 0.17, 0.18, 0.19, 0.20)
    for minimum_exposure in (0.72, 0.73, 0.74, 0.75)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_volscale_tv{int(round(target_volatility * 1000)):03d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_m566_bc0_fc25"
        ),
        0.893,
        5.66,
        1.25,
        1.0,
        0.0,
        0.25,
        target_volatility=target_volatility,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
    )
    for target_volatility in (0.125, 0.130, 0.135, 0.140, 0.145, 0.150)
    for minimum_exposure in (0.75, 0.80, 0.85)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_commonfix_{label}_m{int(round(multiplier * 100)):03d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_bc0_fc25"
        ),
        0.893,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=m1_m2_threshold,
        recovery_fund_active_issuance_percentile_min=fund_issuance_min,
    )
    for label, m1_m2_threshold, fund_issuance_min in (
        ("m1", 0.0, None),
        ("fund50", None, 0.50),
        ("m1fund50", 0.0, 0.50),
    )
    for multiplier in (4.00, 4.25, 4.50, 4.65)
    for minimum_exposure in (0.55, 0.60, 0.65, 0.70)
)

RULES += tuple(
    StrictQuarterlyRule(
        (
            f"q_commonfine_m1_m{int(round(multiplier * 100)):03d}"
            f"_e{int(round(minimum_exposure * 100)):02d}_f893_bc0_fc25"
        ),
        0.893,
        multiplier,
        1.25,
        1.0,
        0.0,
        0.25,
        recovery_min_exposure=minimum_exposure,
        recovery_return_threshold=99.0,
        recovery_market_return_threshold=0.08,
        recovery_market_return_6m_threshold=0.0,
        recovery_market_ma_6m_distance_threshold=0.0,
        recovery_basket_drawdown_6m_threshold=-0.05,
        recovery_m1_m2_change_3m_threshold=0.0,
    )
    for multiplier in (3.50, 3.75, 4.00, 4.10)
    for minimum_exposure in (0.45, 0.48, 0.50, 0.52)
)

QUARTERLY_RISK_FLAGS = (
    "market_overheat_flag",
    "rebound_overheat_flag",
    "high_level_distribution_flag",
    "long_cycle_overheat_flag",
    "low_vol_meltup_exhaustion_flag",
    "crowded_fund_issuance_rally_flag",
    "low_vol_breadth_rollover_flag",
    "valuation_concentration_overheat_flag",
    "bear_rebound_exhaustion_flag",
    "short_cycle_overheat_flag",
    "low_vol_mature_trend_flag",
    "tightening_rebound_exhaustion_flag",
    "rally_distribution_flag",
    "financed_surge_reversal_flag",
    "option_panic_after_rally_flag",
    "turnover_overheat_flag",
    "daily_margin_rally_flag",
    "low_vol_flat_flag",
    "strong_rally_breadth_reversal_flag",
    "leadership_collapse_tightening_flag",
    "leverage_macro_divergence_flag",
    "theme_macro_contraction_divergence_flag",
    "stagflation_credit_contraction_flag",
    "fund_distribution_tight_flag",
    "fund_saturation_contraction_flag",
    "theme_divergence_3m_flag",
    "theme_divergence_1m_tightening_flag",
    "theme_divergence_1m_crowded_flag",
    "credit_contraction_tightening_flag",
    "macro_weak_rebound_flag",
    "weak_credit_leveraged_rebound_flag",
    "fund_moderate_distribution_flag",
    "leveraged_rally_exhaustion_flag",
    "mature_dollar_tightening_flag",
    "mature_narrow_reversal_flag",
    "domestic_liquidity_stress_flag",
    "early_history_crisis_repricing_flag",
    "crisis_continuation_flag",
)

QUARTERLY_EXIT_FLAGS = (
    "crisis_continuation_flag",
    "high_level_distribution_flag",
    "leadership_collapse_tightening_flag",
    "theme_macro_contraction_divergence_flag",
)

RISK_FLAG_CLUSTERS = {
    "price_cycle": (
        "market_overheat_flag",
        "rebound_overheat_flag",
        "high_level_distribution_flag",
        "long_cycle_overheat_flag",
        "low_vol_meltup_exhaustion_flag",
        "low_vol_breadth_rollover_flag",
        "valuation_concentration_overheat_flag",
        "bear_rebound_exhaustion_flag",
        "short_cycle_overheat_flag",
        "low_vol_mature_trend_flag",
        "low_vol_flat_flag",
        "mature_narrow_reversal_flag",
    ),
    "leverage_crowding": (
        "crowded_fund_issuance_rally_flag",
        "financed_surge_reversal_flag",
        "turnover_overheat_flag",
        "daily_margin_rally_flag",
        "leverage_macro_divergence_flag",
        "fund_distribution_tight_flag",
        "fund_saturation_contraction_flag",
        "theme_divergence_1m_crowded_flag",
        "weak_credit_leveraged_rebound_flag",
        "fund_moderate_distribution_flag",
        "leveraged_rally_exhaustion_flag",
    ),
    "breadth_leadership": (
        "rally_distribution_flag",
        "strong_rally_breadth_reversal_flag",
        "leadership_collapse_tightening_flag",
        "theme_divergence_3m_flag",
        "theme_divergence_1m_tightening_flag",
    ),
    "macro_liquidity": (
        "tightening_rebound_exhaustion_flag",
        "option_panic_after_rally_flag",
        "theme_macro_contraction_divergence_flag",
        "stagflation_credit_contraction_flag",
        "credit_contraction_tightening_flag",
        "macro_weak_rebound_flag",
        "mature_dollar_tightening_flag",
        "domestic_liquidity_stress_flag",
    ),
    "crisis": (
        "early_history_crisis_repricing_flag",
        "crisis_continuation_flag",
    ),
}


def risk_flag_clusters(active_flags: list[str]) -> list[str]:
    active = set(active_flags)
    return sorted(
        cluster
        for cluster, members in RISK_FLAG_CLUSTERS.items()
        if active.intersection(members)
    )


def resolve_feature_risk_cap(
    base_cap: float,
    risk_cap_active: bool,
    active_clusters: list[str],
    relaxed_clusters: tuple[str, ...] | None,
    relaxed_cap: float | None,
    safe_gate_allowed: bool,
    safe_gate_cap: float | None,
) -> tuple[float, bool, bool]:
    """Resolve declared cap relaxations without bypassing an inactive risk cap."""

    cluster_relaxed = bool(
        risk_cap_active
        and active_clusters
        and relaxed_clusters is not None
        and relaxed_cap is not None
        and set(active_clusters).issubset(relaxed_clusters)
    )
    safe_gate_relaxed = bool(
        risk_cap_active and safe_gate_allowed and safe_gate_cap is not None
    )
    effective_cap = float(base_cap)
    if cluster_relaxed:
        effective_cap = max(effective_cap, float(relaxed_cap))
    if safe_gate_relaxed:
        effective_cap = max(effective_cap, float(safe_gate_cap))
    return effective_cap, cluster_relaxed, safe_gate_relaxed


def safe_gate_cluster_allowed(
    active_clusters: list[str], allowed_clusters: tuple[str, ...] | None
) -> bool:
    """Do not let a safe-path model waive undeclared independent risks."""
    return bool(
        active_clusters
        and (
            allowed_clusters is None
            or set(active_clusters).issubset(allowed_clusters)
        )
    )


def safe_gate_flags_allowed(
    active_flags: list[str], blocked_flags: tuple[str, ...]
) -> bool:
    return not set(active_flags).intersection(blocked_flags)


def direction_boost_allowed(
    decision: dict[str, Any],
    policy: MonthlyDirectionPolicy,
    predecision_drawdown: float,
) -> bool:
    return (
        decision.get("score") is not None
        and float(decision["score"]) > policy.positive_score_gt
        and int(decision.get("vote_count") or 0)
        >= policy.minimum_vote_count_for_boost
        and predecision_drawdown >= policy.boost_allowed_drawdown_gte
    )


def direction_boost_blocked_by_macro_weakness(
    market_state: dict[str, Any],
    pboc_tone_lte: float | None,
    cs300_ma_6m_distance_lt: float | None,
) -> bool:
    """Block acceleration only when both declared point-in-time risks exist."""

    if pboc_tone_lte is None or cs300_ma_6m_distance_lt is None:
        return False
    tone = market_state.get("pboc_outlook_net_tone")
    ma_distance = market_state.get("cs300_ma_6m_distance")
    return bool(
        isinstance(tone, (int, float))
        and isinstance(ma_distance, (int, float))
        and float(tone) <= pboc_tone_lte
        and float(ma_distance) < cs300_ma_6m_distance_lt
    )


def cold_start_models_unavailable(
    direction_decision: dict[str, Any],
    risk_gate_decision: dict[str, Any],
) -> bool:
    """Identify a genuine walk-forward cold start, not a negative forecast."""

    return (
        direction_decision.get("score") is None
        and risk_gate_decision.get("score") is None
    )


def cold_start_price_damage_signal(market_state: dict[str, Any]) -> bool:
    """Use observable ETF damage when both learned quarterly models lack history."""

    momentum = market_state.get("selected_etf_momentum_12m_skip1m")
    drawdown = market_state.get("selected_etf_max_drawdown_6m")
    return bool(
        isinstance(momentum, (int, float))
        and isinstance(drawdown, (int, float))
        and float(momentum) <= -0.10
        and float(drawdown) <= -0.15
    )


def boost_exposure_with_active_caps(
    exposure: float,
    multiplier: float,
    max_exposure: float,
    active_caps: tuple[float, ...] = (),
) -> float:
    """Accelerate an existing position without bypassing declared risk caps."""

    ceiling = min((float(max_exposure), *map(float, active_caps)))
    return min(ceiling, float(exposure) * float(multiplier))


def initial_exposure_from_limits(
    max_exposure: float,
    base_weight: float,
    base_scale: float,
    cppi_limit: float,
) -> tuple[float, dict[str, Any]]:
    """Return the initial exposure and an auditable list of binding limits."""

    limits = {
        "max_exposure": float(max_exposure),
        "annual_scorecard": float(base_weight) * float(base_scale),
        "cppi_cushion": float(cppi_limit),
    }
    exposure = min(limits.values())
    binding_limits = sorted(
        name for name, value in limits.items() if abs(value - exposure) <= 1e-12
    )
    return exposure, {
        "limits": limits,
        "binding_limits": binding_limits,
    }


def remap_annual_base_weight(
    base_weight: float,
    overrides: tuple[tuple[float, float], ...],
) -> float:
    """Apply an explicit scorecard-band ablation without changing path data."""

    for source, target in overrides:
        if abs(float(base_weight) - float(source)) <= 1e-12:
            return float(target)
    return float(base_weight)


def append_exposure_trace_stage(
    trace: list[dict[str, Any]],
    stage: str,
    before: float,
    after: float,
    *,
    active: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a sizing layer without changing the trading calculation."""

    changed = abs(float(after) - float(before)) > 1e-12
    trace.append(
        {
            "stage": stage,
            "before": float(before),
            "after": float(after),
            "active": bool(active),
            "applied": changed,
            "effect": (
                "increase"
                if after > before + 1e-12
                else "decrease"
                if after < before - 1e-12
                else "unchanged"
            ),
            "details": details or {},
        }
    )


def cash_return(start: date, end: date) -> float:
    return CASH_ANNUAL_RATE * max((end - start).days, 0) / 365.25


def combined_target_weights(
    risk_weights: dict[str, float],
    defensive_weights: dict[str, float],
    exposure: float,
) -> dict[str, float]:
    output = {code: exposure * weight for code, weight in risk_weights.items()}
    defensive_scale = max(0.0, 1.0 - exposure)
    for code, weight in defensive_weights.items():
        output[code] = output.get(code, 0.0) + defensive_scale * weight
    cash_weight = 1.0 - sum(output.values())
    if cash_weight > 1e-12:
        output["CASH"] = cash_weight
    return output


def rebalance_frozen_positions(
    current_positions: dict[str, float],
    target_weights: dict[str, float],
    capital: float,
    transaction_cost_bps: float = TRANSACTION_COST_BPS,
) -> tuple[dict[str, float], float, float]:
    """Trade once at a quarter boundary and return fixed post-trade notionals.

    Position values, rather than reported target weights, are carried through
    the holding window.  This prevents the daily constant-weight arithmetic
    from silently introducing an intra-quarter rebalance.
    """

    if capital <= 0:
        raise ValueError("capital must be positive")
    current_security_weights = {
        code: max(0.0, float(value)) / capital
        for code, value in current_positions.items()
        if code != "CASH" and value > 0
    }
    target_security_weights = {
        code: max(0.0, float(weight))
        for code, weight in target_weights.items()
        if code != "CASH" and weight > 0
    }
    turnover = portfolio_turnover(
        current_security_weights,
        target_security_weights,
    )
    transaction_cost = capital * turnover * transaction_cost_bps / 10_000.0
    post_cost_capital = max(0.0, capital - transaction_cost)
    normalized = {
        code: max(0.0, float(weight))
        for code, weight in target_weights.items()
        if weight > 0
    }
    total_weight = sum(normalized.values())
    if total_weight <= 0:
        normalized = {"CASH": 1.0}
        total_weight = 1.0
    positions = {
        code: post_cost_capital * weight / total_weight
        for code, weight in normalized.items()
    }
    return positions, transaction_cost, turnover


def mark_frozen_positions(
    positions: dict[str, float],
    returns: dict[str, float],
) -> dict[str, float]:
    """Mark fixed shares/notionals to market without resetting their weights."""

    return {
        code: max(0.0, float(value) * (1.0 + float(returns.get(code, 0.0))))
        for code, value in positions.items()
    }


RECOVERY_DIAGNOSTIC_FEATURES = (
    "cs300_return_3m",
    "cs300_return_6m",
    "cs300_ma_6m_distance",
    "basket_drawdown_3m",
    "basket_drawdown_6m",
    "basket_excess_return_6m",
    "basket_vol_3m",
    "domestic_m1_m2_scissors_change_3m",
    "fund_active_issuance_percentile_3y",
    "external_vix_level",
    "external_vix_percentile_3y",
    "external_broad_dollar_return_3m",
    "external_baa_aaa_spread_level",
    "pboc_outlook_net_tone",
    "pboc_outlook_net_tone_change",
    "pboc_outlook_risk_density",
    "pboc_report_age_days",
    "domestic_gov_curve_10y1y_percentile_3y",
    "selected_etf_volatility_1m",
    "selected_etf_volatility_3m",
    "selected_etf_volatility_6m",
    "selected_etf_downside_volatility_3m",
    "selected_etf_market_beta_6m",
    "selected_etf_max_drawdown_6m",
    "selected_etf_momentum_12m",
    "selected_etf_momentum_12m_skip1m",
    "selector_score_candidate_count",
)


def recovery_diagnostic_snapshot(market_state: dict[str, Any]) -> dict[str, Any]:
    """Keep a compact point-in-time snapshot for auditing a path's risk boundary."""

    return {
        name: market_state.get(name)
        for name in RECOVERY_DIAGNOSTIC_FEATURES
        if market_state.get(name) is not None
    }


def evaluate_path(
    path: dict[str, Any],
    rule: StrictQuarterlyRule,
    equity_series,
    defensive_metas,
    defensive_series,
    defensive_policy: DefensivePolicy,
    include_decision_rows: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    budget_peak = capital
    budget_year: int | None = None
    curve = [capital]
    exposure = 0.0
    risk_weights: dict[str, float] = {}
    defensive_weights: dict[str, float] = {}
    target_weights: dict[str, float] = {"CASH": 1.0}
    current_positions: dict[str, float] = {"CASH": capital}
    counterfactual_risk_positions: dict[str, float] = {}
    risk_codes: set[str] = set()
    decision_rows: list[dict[str, Any]] = []
    exposures = []
    transaction_cost_total = 0.0
    worst_drawdown = 0.0
    worst_drawdown_state: dict[str, Any] = {}
    active_risk_flags: list[str] = []
    previous_active_risk_flags: list[str] = []
    active_risk_clusters: list[str] = []
    previous_active_risk_clusters: list[str] = []
    previous_window_start_capital: float | None = None
    online_guard = (
        QuarterlyWalkForwardLossGuard(rule.online_loss_guard)
        if rule.online_loss_guard is not None
        else None
    )
    pending_guard_features: dict[str, Any] | None = None
    risky_window_factor = 1.0
    risky_window_peak = 1.0
    risky_window_max_drawdown = 0.0
    online_guard_count = 0
    online_guard_decision: dict[str, Any] = {"flagged": False, "mode": "disabled"}
    direction_history: list[dict[str, Any]] = []
    pending_direction_features: dict[str, Any] | None = None
    direction_decision: dict[str, Any] = {
        "score": None,
        "predicted_direction": 0,
        "vote_count": 0,
        "votes": {},
    }
    direction_decision_count = 0
    direction_risk_gate_rejection_count = 0
    direction_risk_gate_decision: dict[str, Any] = {
        "score": None,
        "predicted_direction": 0,
        "vote_count": 0,
        "votes": {},
    }
    selector_dispersion_history: list[float] = []
    selector_dispersion_recovery_count = 0
    recovery_count = 0
    quality_history: dict[str, list[float]] = {}
    quality_decision: dict[str, Any] = {
        "score": None,
        "components": {},
        "feature_set": rule.quality_score_feature_set,
    }
    quality_high_count = 0
    quality_low_count = 0
    annual_scorecard_entry_score: int | None = None

    for row in path["daily"]:
        if row["window_start"]:
            if decision_rows and previous_window_start_capital is not None:
                decision_rows[-1]["realized_portfolio_return"] = (
                    capital / previous_window_start_capital - 1.0
                )
                decision_rows[-1]["realized_risk_return"] = risky_window_factor - 1.0
                decision_rows[-1]["realized_risk_max_drawdown"] = (
                    risky_window_max_drawdown
                )
            if online_guard is not None and pending_guard_features is not None:
                online_guard.observe_completed_period(
                    pending_guard_features,
                    risky_window_factor - 1.0,
                )
            if pending_direction_features is not None:
                direction_history.append(
                    {
                        "features": pending_direction_features,
                        "forward_return": risky_window_factor - 1.0,
                        "forward_max_drawdown": risky_window_max_drawdown,
                    }
                )
            risky_window_factor = 1.0
            risky_window_peak = 1.0
            risky_window_max_drawdown = 0.0
            previous_window_return = (
                capital / previous_window_start_capital - 1.0
                if previous_window_start_capital is not None
                and previous_window_start_capital > 0
                else None
            )
            decision_year = row["previous_day"].year
            if rule.annual_risk_budget_reset and decision_year != budget_year:
                budget_peak = capital
                budget_year = decision_year
            else:
                budget_peak = max(budget_peak, capital)
            if row.get("allocation_entry"):
                raw_annual_score = row.get("scorecard_score")
                annual_scorecard_entry_score = (
                    int(raw_annual_score)
                    if isinstance(raw_annual_score, (int, float))
                    else None
                )
            quality_decision = walkforward_quality_score(
                quality_history,
                row["market_state"],
                rule.quality_score_feature_set,
                rule.quality_score_min_history,
            )
            (
                annual_adjusted_multiplier,
                annual_score_boost_active,
            ) = annual_score_adjusted_cushion_multiplier(
                rule.multiplier,
                annual_scorecard_entry_score,
                rule.annual_score_boost_lte,
                rule.annual_score_cushion_multiplier,
            )
            effective_multiplier = quality_adjusted_cushion_multiplier(
                annual_adjusted_multiplier,
                quality_decision,
                rule.quality_score_high_threshold,
                rule.quality_score_high_cushion_multiplier,
                rule.quality_score_low_threshold,
                rule.quality_score_low_cushion_multiplier,
            )
            effective_multiplier, _feature_multiplier_applied = (
                apply_feature_cushion_multiplier(
                    effective_multiplier,
                    row["market_state"],
                    rule.feature_cushion_multiplier_name,
                    rule.feature_cushion_multiplier_max_value,
                    rule.feature_cushion_multiplier_value,
                )
            )
            if (
                rule.quality_score_high_requires_trend_confirmation
                and effective_multiplier > rule.multiplier
                and not quality_multiplier_trend_confirmed(row["market_state"])
            ):
                effective_multiplier = rule.multiplier
            if (
                rule.quality_score_high_blocks_crisis_rebound
                and effective_multiplier > rule.multiplier
                and crisis_rebound_blocks_quality_acceleration(row["market_state"])
            ):
                effective_multiplier = rule.multiplier
            predecision_drawdown = capital / max(peak, 1.0) - 1.0
            direction_boost_active = False
            base_direction_boost_active = False
            risk_gate_allowed = False
            if rule.direction_policy is not None:
                direction_decision = (
                    predict_ridge_direction(
                        direction_history,
                        row["market_state"],
                        rule.direction_policy,
                    )
                    if rule.direction_policy.model_type == "ridge"
                    else predict_binned_direction(
                        direction_history,
                        row["market_state"],
                        rule.direction_policy,
                    )
                    if rule.direction_policy.model_type == "binned"
                    else predict_direction(
                        direction_history,
                        row["market_state"],
                        rule.direction_policy,
                    )
                )
                base_direction_boost_active = direction_boost_allowed(
                    direction_decision,
                    rule.direction_policy,
                    predecision_drawdown,
                )
                direction_boost_active = base_direction_boost_active
            if rule.direction_risk_gate_policy is not None:
                risk_gate_history = [
                    {
                        "features": item["features"],
                        "forward_return": item["forward_max_drawdown"],
                    }
                    for item in direction_history
                    if item.get("forward_max_drawdown") is not None
                ]
                direction_risk_gate_decision = predict_binned_direction(
                    risk_gate_history,
                    row["market_state"],
                    rule.direction_risk_gate_policy,
                )
                risk_gate_allowed = direction_boost_allowed(
                    direction_risk_gate_decision,
                    rule.direction_risk_gate_policy,
                    predecision_drawdown,
                )
                if base_direction_boost_active and not risk_gate_allowed:
                    direction_risk_gate_rejection_count += 1
                direction_boost_active = (
                    base_direction_boost_active and risk_gate_allowed
                )
            direction_macro_blocked = direction_boost_blocked_by_macro_weakness(
                row["market_state"],
                rule.direction_block_pboc_tone_lte,
                rule.direction_block_cs300_ma_6m_distance_lt,
            )
            if direction_macro_blocked:
                direction_boost_active = False
            cold_start_gate_candidate = bool(
                rule.feature_risk_cold_start_gate_cap is not None
                and cold_start_models_unavailable(
                    direction_decision,
                    direction_risk_gate_decision,
                )
            )
            floor = budget_peak * rule.floor_pct
            cushion = max(0.0, capital - floor)
            cppi_limit = effective_multiplier * cushion / max(capital, 1.0)
            raw_base_weight = float(row["base_weight"])
            effective_base_weight = remap_annual_base_weight(
                raw_base_weight,
                rule.annual_weight_overrides,
            )
            exposure, initial_exposure_audit = initial_exposure_from_limits(
                rule.max_exposure,
                effective_base_weight,
                rule.base_scale,
                cppi_limit,
            )
            exposure_trace: list[dict[str, Any]] = [
                {
                    "stage": "initial_min",
                    "before": None,
                    "after": exposure,
                    "active": True,
                    "applied": True,
                    "effect": "set",
                    "details": initial_exposure_audit,
                }
            ]
            before_stage = exposure
            effective_bear_cap = rule.bear_cap
            quality_score = quality_decision["score"]
            quality_direction_bear_override = bool(
                row["bear_state"]
                and rule.bear_quality_direction_cap is not None
                and quality_score is not None
                and rule.quality_score_high_threshold is not None
                and float(quality_score) >= rule.quality_score_high_threshold
                and direction_boost_active
            )
            if quality_direction_bear_override:
                effective_bear_cap = max(
                    effective_bear_cap,
                    float(rule.bear_quality_direction_cap),
                )
            if row["bear_state"]:
                exposure = min(exposure, effective_bear_cap)
            append_exposure_trace_stage(
                exposure_trace,
                "bear_cap",
                before_stage,
                exposure,
                active=bool(row["bear_state"]),
                details={
                    "cap": effective_bear_cap,
                    "base_cap": rule.bear_cap,
                    "quality_direction_override": (
                        quality_direction_bear_override
                    ),
                    "quality_direction_cap": rule.bear_quality_direction_cap,
                },
            )
            before_stage = exposure
            rebound_state = crisis_rebound_state(row["market_state"])
            rebound_cap: float | None = None
            if effective_multiplier > rule.multiplier:
                if (
                    rebound_state == "severe"
                    and rule.quality_score_high_severe_crisis_cap is not None
                ):
                    rebound_cap = rule.quality_score_high_severe_crisis_cap
                    exposure = min(
                        exposure, rule.quality_score_high_severe_crisis_cap
                    )
                elif (
                    rebound_state == "correction"
                    and rule.quality_score_high_correction_cap is not None
                ):
                    rebound_cap = rule.quality_score_high_correction_cap
                    exposure = min(exposure, rule.quality_score_high_correction_cap)
            append_exposure_trace_stage(
                exposure_trace,
                "quality_crisis_cap",
                before_stage,
                exposure,
                active=rebound_cap is not None,
                details={"rebound_state": rebound_state, "cap": rebound_cap},
            )
            before_stage = exposure
            direction_active_caps = tuple(
                cap
                for cap in (
                    effective_bear_cap if row["bear_state"] else None,
                    rebound_cap,
                )
                if cap is not None
            )
            if rule.direction_policy is not None:
                if direction_boost_active:
                    exposure = boost_exposure_with_active_caps(
                        exposure,
                        rule.direction_policy.nonnegative_exposure_multiplier,
                        rule.max_exposure,
                        direction_active_caps,
                    )
                    direction_decision_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "direction_boost",
                before_stage,
                exposure,
                active=direction_boost_active,
                details={
                    "multiplier": (
                        rule.direction_policy.nonnegative_exposure_multiplier
                        if rule.direction_policy is not None
                        else None
                    ),
                    "predecision_drawdown": predecision_drawdown,
                    "base_direction_boost_active": base_direction_boost_active,
                    "risk_gate_policy": (
                        rule.direction_risk_gate_policy.name
                        if rule.direction_risk_gate_policy is not None
                        else None
                    ),
                    "risk_gate_score": direction_risk_gate_decision.get("score"),
                    "macro_weakness_blocked": direction_macro_blocked,
                    "active_cap_ceiling": (
                        min(direction_active_caps)
                        if direction_active_caps
                        else rule.max_exposure
                    ),
                },
            )
            active_risk_flags = (
                [
                    name
                    for name in QUARTERLY_RISK_FLAGS
                    if bool(row["market_state"].get(name))
                ]
                if rule.use_quarterly_feature_caps
                else []
            )
            active_risk_clusters = risk_flag_clusters(active_risk_flags)
            confirmed_market_recovery = market_recovery_signal(
                row["market_state"],
                rule.recovery_market_return_threshold,
                rule.recovery_market_return_6m_threshold,
                rule.recovery_market_ma_6m_distance_threshold,
                rule.recovery_basket_drawdown_6m_threshold,
                rule.recovery_m1_m2_change_3m_threshold,
                rule.recovery_basket_excess_return_6m_max,
                rule.recovery_fund_active_issuance_percentile_min,
                rule.recovery_basket_vol_3m_max,
                rule.recovery_selector_candidate_count_min,
            )
            recovery_signal = (
                previous_window_return is not None
                and previous_window_return >= rule.recovery_return_threshold
            ) or confirmed_market_recovery
            confirmed_secondary_recovery = market_recovery_signal(
                row["market_state"],
                rule.secondary_recovery_market_return_threshold,
                rule.secondary_recovery_market_return_6m_threshold,
                rule.secondary_recovery_market_ma_6m_distance_threshold,
            )
            recovery_active = (
                rule.recovery_min_exposure > 0
                and recovery_signal
                and not row["bear_state"]
                and not active_risk_flags
            )
            before_stage = exposure
            if recovery_active:
                previous_exposure = exposure
                exposure = max(
                    exposure,
                    min(
                        rule.recovery_min_exposure,
                        effective_base_weight * rule.base_scale,
                    ),
                )
                if exposure > previous_exposure + 1e-12:
                    recovery_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "market_recovery_floor",
                before_stage,
                exposure,
                active=recovery_active,
                details={
                    "floor": rule.recovery_min_exposure,
                    "signal": recovery_signal,
                },
            )
            secondary_recovery_active = (
                rule.secondary_recovery_min_exposure > 0
                and confirmed_secondary_recovery
                and not row["bear_state"]
                and not active_risk_flags
            )
            before_stage = exposure
            if secondary_recovery_active:
                previous_exposure = exposure
                exposure = max(
                    exposure,
                    min(
                        rule.secondary_recovery_min_exposure,
                        effective_base_weight * rule.base_scale,
                    ),
                )
                if exposure > previous_exposure + 1e-12:
                    recovery_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "secondary_recovery_floor",
                before_stage,
                exposure,
                active=secondary_recovery_active,
                details={"floor": rule.secondary_recovery_min_exposure},
            )
            risk_cap_active = bool(
                active_risk_flags
                and (
                    rule.feature_risk_cap_clusters is None
                    or set(active_risk_clusters).intersection(
                        rule.feature_risk_cap_clusters
                    )
                )
            )
            learned_safe_gate_allowed_effective = bool(
                risk_gate_allowed
                and safe_gate_cluster_allowed(
                    active_risk_clusters,
                    rule.feature_risk_safe_gate_clusters,
                )
                and safe_gate_flags_allowed(
                    active_risk_flags,
                    rule.feature_risk_safe_gate_block_flags,
                )
            )
            cold_start_safe_gate_allowed_effective = bool(
                cold_start_gate_candidate
                and safe_gate_cluster_allowed(
                    active_risk_clusters,
                    rule.feature_risk_safe_gate_clusters,
                )
                and safe_gate_flags_allowed(
                    active_risk_flags,
                    rule.feature_risk_safe_gate_block_flags,
                )
            )
            safe_gate_allowed_effective = bool(
                learned_safe_gate_allowed_effective
                or cold_start_safe_gate_allowed_effective
            )
            effective_safe_gate_cap = (
                rule.feature_risk_safe_gate_cap
                if learned_safe_gate_allowed_effective
                else rule.feature_risk_cold_start_gate_cap
                if cold_start_safe_gate_allowed_effective
                else None
            )
            (
                effective_feature_risk_cap,
                relaxed_risk_cap_active,
                safe_gate_relaxed_risk_cap_active,
            ) = resolve_feature_risk_cap(
                rule.feature_risk_cap,
                risk_cap_active,
                active_risk_clusters,
                rule.feature_risk_relaxed_clusters,
                rule.feature_risk_relaxed_cap,
                safe_gate_allowed_effective,
                effective_safe_gate_cap,
            )
            before_stage = exposure
            if risk_cap_active:
                exposure = min(exposure, effective_feature_risk_cap)
            append_exposure_trace_stage(
                exposure_trace,
                "risk_flag_cap",
                before_stage,
                exposure,
                active=risk_cap_active,
                details={
                    "cap": effective_feature_risk_cap,
                    "base_cap": rule.feature_risk_cap,
                    "eligible_clusters": rule.feature_risk_cap_clusters,
                    "relaxed": relaxed_risk_cap_active,
                    "relaxed_clusters": rule.feature_risk_relaxed_clusters,
                    "relaxed_cap": rule.feature_risk_relaxed_cap,
                    "safe_gate_relaxed": safe_gate_relaxed_risk_cap_active,
                    "safe_gate_allowed": risk_gate_allowed,
                    "safe_gate_allowed_effective": safe_gate_allowed_effective,
                    "learned_safe_gate_allowed_effective": (
                        learned_safe_gate_allowed_effective
                    ),
                    "cold_start_safe_gate_allowed_effective": (
                        cold_start_safe_gate_allowed_effective
                    ),
                    "safe_gate_allowed_clusters": (
                        rule.feature_risk_safe_gate_clusters
                    ),
                    "safe_gate_block_flags": (
                        rule.feature_risk_safe_gate_block_flags
                    ),
                    "safe_gate_cap": rule.feature_risk_safe_gate_cap,
                    "cold_start_safe_gate_cap": (
                        rule.feature_risk_cold_start_gate_cap
                    ),
                    "effective_safe_gate_cap": effective_safe_gate_cap,
                },
            )
            before_stage = exposure
            cold_start_boost_active = bool(
                cold_start_safe_gate_allowed_effective
                and rule.feature_risk_cold_start_multiplier > 1.0
                and effective_safe_gate_cap is not None
            )
            if cold_start_boost_active:
                exposure = boost_exposure_with_active_caps(
                    exposure,
                    rule.feature_risk_cold_start_multiplier,
                    rule.max_exposure,
                    (*direction_active_caps, float(effective_safe_gate_cap)),
                )
            append_exposure_trace_stage(
                exposure_trace,
                "cold_start_boost",
                before_stage,
                exposure,
                active=cold_start_boost_active,
                details={
                    "multiplier": rule.feature_risk_cold_start_multiplier,
                    "cap": effective_safe_gate_cap,
                },
            )
            risk_gate_rejection_cap_active = bool(
                rule.direction_risk_gate_rejection_cap is not None
                and direction_risk_gate_decision.get("score") is not None
                and not risk_gate_allowed
            )
            before_stage = exposure
            if risk_gate_rejection_cap_active:
                exposure = min(
                    exposure,
                    float(rule.direction_risk_gate_rejection_cap),
                )
            append_exposure_trace_stage(
                exposure_trace,
                "direction_risk_gate_rejection_cap",
                before_stage,
                exposure,
                active=risk_gate_rejection_cap_active,
                details={
                    "cap": rule.direction_risk_gate_rejection_cap,
                    "risk_gate_score": direction_risk_gate_decision.get("score"),
                },
            )
            cold_start_price_damage_cap_active = bool(
                rule.cold_start_price_damage_cap is not None
                and cold_start_gate_candidate
                and cold_start_price_damage_signal(row["market_state"])
            )
            before_stage = exposure
            if cold_start_price_damage_cap_active:
                exposure = min(
                    exposure,
                    float(rule.cold_start_price_damage_cap),
                )
            append_exposure_trace_stage(
                exposure_trace,
                "cold_start_price_damage_cap",
                before_stage,
                exposure,
                active=cold_start_price_damage_cap_active,
                details={"cap": rule.cold_start_price_damage_cap},
            )
            raw_selector_dispersion = row["market_state"].get("selector_score_dispersion")
            selector_dispersion = (
                float(raw_selector_dispersion)
                if raw_selector_dispersion is not None
                else None
            )
            dispersion_signal, dispersion_threshold = walkforward_upper_tail_signal(
                selector_dispersion_history,
                selector_dispersion,
                rule.selector_dispersion_quantile,
                rule.selector_dispersion_min_history,
            )
            dispersion_recovery_active = (
                dispersion_signal
                and rule.selector_dispersion_min_exposure > 0
                and not row["bear_state"]
                and not active_risk_flags
            )
            before_stage = exposure
            if dispersion_recovery_active:
                exposure = max(
                    exposure,
                    min(
                        rule.selector_dispersion_min_exposure,
                        effective_base_weight * rule.base_scale,
                    ),
                )
                selector_dispersion_recovery_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "selector_dispersion_floor",
                before_stage,
                exposure,
                active=dispersion_recovery_active,
                details={"floor": rule.selector_dispersion_min_exposure},
            )
            if selector_dispersion is not None:
                selector_dispersion_history.append(selector_dispersion)
            quality_high_active = (
                quality_score is not None
                and rule.quality_score_high_threshold is not None
                and float(quality_score) >= rule.quality_score_high_threshold
                and rule.quality_score_high_min_exposure > 0
                and not row["bear_state"]
                and not active_risk_flags
            )
            before_stage = exposure
            if quality_high_active:
                previous_exposure = exposure
                exposure = max(
                    exposure,
                    min(
                        rule.quality_score_high_min_exposure,
                        effective_base_weight * rule.base_scale,
                    ),
                )
                if exposure > previous_exposure + 1e-12:
                    quality_high_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "quality_high_floor",
                before_stage,
                exposure,
                active=quality_high_active,
                details={"floor": rule.quality_score_high_min_exposure},
            )
            quality_low_active = (
                quality_score is not None
                and rule.quality_score_low_threshold is not None
                and float(quality_score) <= rule.quality_score_low_threshold
            )
            before_stage = exposure
            if quality_low_active:
                previous_exposure = exposure
                exposure = min(exposure, rule.quality_score_low_exposure_cap)
                if exposure < previous_exposure - 1e-12:
                    quality_low_count += 1
            append_exposure_trace_stage(
                exposure_trace,
                "quality_low_cap",
                before_stage,
                exposure,
                active=quality_low_active,
                details={"cap": rule.quality_score_low_exposure_cap},
            )
            observe_quality_features(
                quality_history,
                row["market_state"],
                rule.quality_score_feature_set,
            )
            basket_volatility = float(row["market_state"].get("basket_vol_3m") or 0.0)
            before_stage = exposure
            if rule.target_volatility is not None and basket_volatility > 0:
                exposure = min(exposure, rule.target_volatility / basket_volatility)
            append_exposure_trace_stage(
                exposure_trace,
                "target_volatility_cap",
                before_stage,
                exposure,
                active=rule.target_volatility is not None and basket_volatility > 0,
                details={
                    "target_volatility": rule.target_volatility,
                    "basket_volatility": basket_volatility,
                },
            )
            before_stage = exposure
            exposure, feature_cap_applied = apply_feature_exposure_cap(
                exposure,
                row["market_state"],
                rule.feature_exposure_cap_name,
                rule.feature_exposure_cap_threshold,
                rule.feature_exposure_cap_value,
            )
            append_exposure_trace_stage(
                exposure_trace,
                "feature_exposure_cap",
                before_stage,
                exposure,
                active=feature_cap_applied,
                details={
                    "feature": rule.feature_exposure_cap_name,
                    "threshold": rule.feature_exposure_cap_threshold,
                    "cap": rule.feature_exposure_cap_value,
                    "value": row["market_state"].get(rule.feature_exposure_cap_name)
                    if rule.feature_exposure_cap_name is not None
                    else None,
                },
            )
            exposure_before_exit_stages = exposure
            raw_flag_exit_active = (
                rule.risk_flag_exit_count is not None
                and len(active_risk_flags) >= rule.risk_flag_exit_count
                and (
                    rule.risk_flag_exit_prior_count is None
                    or len(previous_active_risk_flags)
                    >= rule.risk_flag_exit_prior_count
                )
            )
            before_stage = exposure
            if raw_flag_exit_active:
                exposure = 0.0
            append_exposure_trace_stage(
                exposure_trace,
                "raw_risk_flag_exit",
                before_stage,
                exposure,
                active=raw_flag_exit_active,
                details={"required_count": rule.risk_flag_exit_count},
            )
            cluster_exit_active = (
                rule.risk_cluster_exit_count is not None
                and len(active_risk_clusters) >= rule.risk_cluster_exit_count
                and (
                    rule.risk_cluster_exit_prior_count is None
                    or len(previous_active_risk_clusters)
                    >= rule.risk_cluster_exit_prior_count
                )
            )
            before_stage = exposure
            if cluster_exit_active:
                exposure = 0.0
            append_exposure_trace_stage(
                exposure_trace,
                "risk_cluster_exit",
                before_stage,
                exposure,
                active=cluster_exit_active,
                details={"required_count": rule.risk_cluster_exit_count},
            )
            active_hard_exit_flags = [
                name
                for name in QUARTERLY_EXIT_FLAGS
                if bool(row["market_state"].get(name))
            ]
            hard_exit_active = bool(
                rule.use_quarterly_feature_caps and active_hard_exit_flags
            )
            before_stage = exposure
            if hard_exit_active:
                exposure = 0.0
            append_exposure_trace_stage(
                exposure_trace,
                "hard_exit",
                before_stage,
                exposure,
                active=hard_exit_active,
                details={"flags": active_hard_exit_flags},
            )
            crisis_relative_strength_reentry_active = bool(
                rule.crisis_relative_strength_reentry_cap is not None
                and active_hard_exit_flags == ["crisis_continuation_flag"]
                and crisis_relative_strength_reentry_signal(row["market_state"])
            )
            before_stage = exposure
            if crisis_relative_strength_reentry_active:
                exposure = min(
                    exposure_before_exit_stages,
                    float(rule.crisis_relative_strength_reentry_cap),
                )
            append_exposure_trace_stage(
                exposure_trace,
                "crisis_relative_strength_reentry",
                before_stage,
                exposure,
                active=crisis_relative_strength_reentry_active,
                details={
                    "cap": rule.crisis_relative_strength_reentry_cap,
                    "pre_exit_exposure": exposure_before_exit_stages,
                    "hard_exit_flags": active_hard_exit_flags,
                },
            )
            negative_direction_cap_active = (
                rule.direction_policy is not None
                and direction_decision["score"] is not None
                and float(direction_decision["score"])
                <= rule.direction_policy.negative_score_lte
                and direction_decision["vote_count"]
                >= rule.direction_policy.minimum_vote_count_for_cap
            )
            before_stage = exposure
            if negative_direction_cap_active:
                exposure = min(exposure, rule.direction_policy.negative_exposure_cap)
            append_exposure_trace_stage(
                exposure_trace,
                "negative_direction_cap",
                before_stage,
                exposure,
                active=negative_direction_cap_active,
                details={
                    "cap": rule.direction_policy.negative_exposure_cap
                    if rule.direction_policy is not None
                    else None
                },
            )
            online_guard_active = False
            before_stage = exposure
            if online_guard is not None:
                online_guard_decision = online_guard.decision(row["market_state"])
                if online_guard_decision["flagged"]:
                    online_guard_active = True
                    exposure = min(exposure, rule.online_loss_guard.exposure_cap)
                    online_guard_count += 1
                pending_guard_features = dict(row["market_state"])
            append_exposure_trace_stage(
                exposure_trace,
                "online_guard_cap",
                before_stage,
                exposure,
                active=online_guard_active,
                details={
                    "cap": rule.online_loss_guard.exposure_cap
                    if rule.online_loss_guard is not None
                    else None
                },
            )
            pending_direction_features = dict(row["market_state"])
            risk_weights = dict(row["equity_etf_weights"])
            risk_weight_total = sum(
                max(0.0, float(weight)) for weight in risk_weights.values()
            )
            counterfactual_risk_positions = (
                {
                    code: max(0.0, float(weight)) / risk_weight_total
                    for code, weight in risk_weights.items()
                    if float(weight) > 0
                }
                if risk_weight_total > 0
                else {}
            )
            defensive_weights = select_defensive_weights(
                defensive_metas,
                defensive_series,
                row["previous_day"],
                defensive_policy,
            )
            target_weights = combined_target_weights(
                risk_weights,
                defensive_weights,
                exposure,
            )
            decision_capital = capital
            current_positions, transaction_cost, rebalance_turnover = (
                rebalance_frozen_positions(
                    current_positions,
                    target_weights,
                    capital,
                )
            )
            transaction_cost_total += transaction_cost
            capital = sum(current_positions.values())
            risk_codes = set(risk_weights)
            decision_record = {
                    "decision_date": row["previous_day"].isoformat(),
                    "rebalance_anchor": row["rebalance_anchor"],
                    "target_weights": target_weights,
                    "index_target_weights": row.get("index_target_weights", {}),
                    "selector_target_weights": row.get(
                        "selector_target_weights", {}
                    ),
                    "equity_etf_weights": risk_weights,
                    "active_risk_flags": active_risk_flags,
                    "active_risk_clusters": active_risk_clusters,
                    "online_guard": online_guard_decision,
                    "direction_model": direction_decision,
                    "direction_risk_gate": direction_risk_gate_decision,
                    "direction_macro_weakness_blocked": direction_macro_blocked,
                    "selector_dispersion_recovery": {
                        "flagged": dispersion_signal,
                        "value": selector_dispersion,
                        "threshold": dispersion_threshold,
                    },
                    "quality_score": quality_decision,
                    "effective_cushion_multiplier": effective_multiplier,
                    "exposure_formation": {
                        "base_weight": effective_base_weight,
                        "raw_base_weight": raw_base_weight,
                        "annual_weight_overrides": rule.annual_weight_overrides,
                        "base_scale": rule.base_scale,
                        "scorecard_limit": (
                            effective_base_weight * rule.base_scale
                        ),
                        "max_exposure": rule.max_exposure,
                        "budget_peak": budget_peak,
                        "floor": floor,
                        "cushion": cushion,
                        "cppi_limit": cppi_limit,
                        "predecision_drawdown": predecision_drawdown,
                        "initial_binding_limits": initial_exposure_audit[
                            "binding_limits"
                        ],
                        "trace": exposure_trace,
                        "final_exposure": exposure,
                    },
                    "feature_exposure_cap": {
                        "applied": feature_cap_applied,
                        "feature": rule.feature_exposure_cap_name,
                        "threshold": rule.feature_exposure_cap_threshold,
                        "cap": rule.feature_exposure_cap_value,
                    },
                    "exposure": exposure,
                    "scorecard_context": {
                        "score": row.get("scorecard_score"),
                        "allocation_year": row.get("allocation_year"),
                        "allocation_entry": row.get("allocation_entry"),
                        "allocation_midpoint": row.get("allocation_midpoint"),
                        "rebalance_reasons": row.get(
                            "scorecard_rebalance_reasons", []
                        ),
                        "known_inputs": row.get("scorecard_known_inputs", {}),
                        "top_items": row.get("scorecard_top_items", []),
                        "annual_entry_score": annual_scorecard_entry_score,
                        "annual_score_boost_lte": rule.annual_score_boost_lte,
                        "annual_score_cushion_multiplier": (
                            rule.annual_score_cushion_multiplier
                        ),
                        "annual_score_boost_active": annual_score_boost_active,
                    },
                    "bear_state": bool(row["bear_state"]),
                    "bear_signal_timing": row.get("bear_signal_timing", "execution"),
                    "bear_signal_date": (
                        row["bear_signal_date"].isoformat()
                        if isinstance(row.get("bear_signal_date"), date)
                        else row.get("bear_signal_date")
                    ),
                    "bear_signal_diagnostics": row.get(
                        "bear_signal_diagnostics", {}
                    ),
                    "capital_at_decision": decision_capital,
                    "capital_after_transaction_cost": capital,
                    "transaction_cost": transaction_cost,
                    "rebalance_turnover": rebalance_turnover,
                    "min_capital_since_decision": capital,
                    "worst_global_drawdown_since_decision": capital / peak - 1.0,
                    "market_recovery": {
                        "flagged": confirmed_market_recovery,
                        "applied": recovery_signal,
                        "return_3m_threshold": rule.recovery_market_return_threshold,
                        "return_6m_threshold": rule.recovery_market_return_6m_threshold,
                        "ma_6m_distance_threshold": (
                            rule.recovery_market_ma_6m_distance_threshold
                        ),
                        "secondary_flagged": confirmed_secondary_recovery,
                        "secondary_min_exposure": (
                            rule.secondary_recovery_min_exposure
                        ),
                        "basket_drawdown_6m_threshold": (
                            rule.recovery_basket_drawdown_6m_threshold
                        ),
                        "m1_m2_change_3m_threshold": (
                            rule.recovery_m1_m2_change_3m_threshold
                        ),
                        "basket_excess_return_6m_max": (
                            rule.recovery_basket_excess_return_6m_max
                        ),
                        "fund_active_issuance_percentile_min": (
                            rule.recovery_fund_active_issuance_percentile_min
                        ),
                        "basket_vol_3m_max": rule.recovery_basket_vol_3m_max,
                        "selector_candidate_count_min": (
                            rule.recovery_selector_candidate_count_min
                        ),
                    },
                }
            if include_decision_rows:
                decision_record["market_state"] = dict(row["market_state"])
            decision_rows.append(decision_record)
            previous_active_risk_flags = list(active_risk_flags)
            previous_active_risk_clusters = list(active_risk_clusters)
            previous_window_start_capital = decision_capital
            curve.append(capital)

        daily_returns = {
            code: (
                cash_return(row["previous_day"], row["day"])
                if code == "CASH"
                else code_return(
                    equity_series if code in risk_codes else defensive_series,
                    code,
                    row["previous_day"],
                    row["day"],
                )
            )
            for code in current_positions
        }
        current_positions = mark_frozen_positions(current_positions, daily_returns)
        capital = max(1.0, sum(current_positions.values()))
        if counterfactual_risk_positions:
            counterfactual_risk_positions = mark_frozen_positions(
                counterfactual_risk_positions,
                {
                    code: code_return(
                        equity_series,
                        code,
                        row["previous_day"],
                        row["day"],
                    )
                    for code in counterfactual_risk_positions
                },
            )
            risky_window_factor = sum(counterfactual_risk_positions.values())
            risky_window_peak = max(risky_window_peak, risky_window_factor)
            risky_window_max_drawdown = min(
                risky_window_max_drawdown,
                risky_window_factor / max(risky_window_peak, 1e-12) - 1.0,
            )
        peak = max(peak, capital)
        budget_peak = max(budget_peak, capital)
        curve.append(capital)
        risky_value_after = sum(
            current_positions.get(code, 0.0) for code in risk_codes
        )
        exposures.append(risky_value_after / capital if capital > 0 else 0.0)
        drawdown = capital / peak - 1.0
        decision_rows[-1]["min_capital_since_decision"] = min(
            float(decision_rows[-1]["min_capital_since_decision"]), capital
        )
        decision_rows[-1]["worst_global_drawdown_since_decision"] = min(
            float(decision_rows[-1]["worst_global_drawdown_since_decision"]),
            drawdown,
        )
        if drawdown < worst_drawdown:
            worst_drawdown = drawdown
            worst_drawdown_state = {
                "date": row["day"].isoformat(),
                "decision_date": decision_rows[-1]["decision_date"],
                "exposure": exposure,
                "active_risk_flags": active_risk_flags,
                "target_weights": target_weights,
                "market_recovery": decision_rows[-1]["market_recovery"],
                "quality_score": decision_rows[-1]["quality_score"],
                "effective_cushion_multiplier": decision_rows[-1][
                    "effective_cushion_multiplier"
                ],
                "market_state": recovery_diagnostic_snapshot(row["market_state"]),
            }

    if decision_rows and previous_window_start_capital is not None:
        decision_rows[-1]["realized_portfolio_return"] = (
            capital / previous_window_start_capital - 1.0
        )
        decision_rows[-1]["realized_risk_return"] = risky_window_factor - 1.0
        decision_rows[-1]["realized_risk_max_drawdown"] = (
            risky_window_max_drawdown
        )
    mdd = max_drawdown(curve)
    violations = validate_quarterly_weight_path(
        decision_rows,
        require_exact_rebalance_spacing=True,
    )
    output = {
        "phase_month_offset": path["phase"],
        "execution_lag_days": path["lag"],
        "sample_start": path["sample_start"],
        "sample_end": path["sample_end"],
        "sample_shift_cycles": path["sample_shift_cycles"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / 20.0) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD and not violations,
        "average_exposure": statistics.mean(exposures),
        "quarterly_weight_validation_passed": not violations,
        "quarterly_weight_violations": violations,
        "online_guard_count": online_guard_count,
        "direction_decision_count": direction_decision_count,
        "direction_risk_gate_rejection_count": (
            direction_risk_gate_rejection_count
        ),
        "selector_dispersion_recovery_count": selector_dispersion_recovery_count,
        "recovery_count": recovery_count,
        "quality_high_count": quality_high_count,
        "quality_low_count": quality_low_count,
        "transaction_cost_total": transaction_cost_total,
        "worst_drawdown_state": worst_drawdown_state,
    }
    if include_decision_rows:
        output["decision_rows"] = decision_rows
    return output


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = validate_case_matrix(cases)
    return {
        "count": len(cases),
        "pass_count": sum(case["target_met"] for case in cases),
        "min_final_capital_wan": min(case["final_capital_wan"] for case in cases),
        "median_final_capital_wan": statistics.median(case["final_capital_wan"] for case in cases),
        "worst_max_drawdown": min(case["max_drawdown"] for case in cases),
        "median_max_drawdown": statistics.median(case["max_drawdown"] for case in cases),
        "median_average_exposure": statistics.median(case["average_exposure"] for case in cases),
        "median_online_guard_count": statistics.median(case["online_guard_count"] for case in cases),
        "median_direction_risk_gate_rejection_count": statistics.median(
            case["direction_risk_gate_rejection_count"] for case in cases
        ),
        "median_selector_dispersion_recovery_count": statistics.median(
            case["selector_dispersion_recovery_count"] for case in cases
        ),
        "median_recovery_count": statistics.median(case["recovery_count"] for case in cases),
        "median_quality_high_count": statistics.median(
            case["quality_high_count"] for case in cases
        ),
        "median_quality_low_count": statistics.median(
            case["quality_low_count"] for case in cases
        ),
        "case_matrix": matrix,
        "objective_met": matrix["all_cases_pass"] and all(case["target_met"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", action="append")
    parser.add_argument("--rule-prefix")
    parser.add_argument("--defensive-policy", action="append")
    parser.add_argument("--defensive-prefix")
    parser.add_argument("--selector-policy", action="append")
    parser.add_argument("--direct-etf-policy", action="append")
    parser.add_argument("--online-selector", action="store_true")
    parser.add_argument("--online-ridge-selector", action="store_true")
    parser.add_argument(
        "--include-decision-rows",
        action="store_true",
        help="Persist quarter-boundary decisions and realized period returns for audit.",
    )
    parser.add_argument(
        "--bear-signal-timing",
        choices=("execution", "snapshot"),
        default="execution",
        help=(
            "Use the delayed execution-day trend state or freeze it at the "
            "quarterly signal snapshot."
        ),
    )
    parser.add_argument("--output-prefix", default="data/backtests/scorecard_csi_strict_quarterly_etf")
    args = parser.parse_args()

    selected_rules = [
        rule
        for rule in RULES
        if (
            (not args.rule and not args.rule_prefix)
            or (args.rule and rule.name in set(args.rule))
            or (args.rule_prefix and rule.name.startswith(args.rule_prefix))
        )
    ]
    selected_policies = [
        policy
        for policy in DEFENSIVE_POLICIES
        if (
            (not args.defensive_policy and not args.defensive_prefix)
            or (args.defensive_policy and policy.name in set(args.defensive_policy))
            or (args.defensive_prefix and policy.name.startswith(args.defensive_prefix))
        )
    ]
    selected_selectors = [
        policy
        for policy in SELECTOR_POLICIES
        if not args.selector_policy or policy.name in set(args.selector_policy)
    ]
    selected_direct_policies = [
        policy
        for policy in DIRECT_ETF_POLICIES
        if args.direct_etf_policy and policy.name in set(args.direct_etf_policy)
    ] if args.direct_etf_policy else [None]
    if not selected_rules or not selected_policies or not selected_selectors or not selected_direct_policies:
        raise ValueError("no matching rule or defensive policy")

    conn = get_connection()
    try:
        index_series = load_price_series(conn)
        load_selector_price_series(conn, index_series)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
        equity_metas, equity_series = load_equity_etf_return_universe(conn)
        trade_dates = [day for day, _value in index_series[CS300_CODE]]
        paths_by_selector = {
            (selector.name, direct_policy.name if direct_policy else "index_mapping"): [
                build_daily_path(
                    index_series,
                    trade_dates,
                    SCHEDULE_12M_3M,
                    phase,
                    lag,
                    equity_metas,
                    equity_series,
                    ANNUAL_MARKET_SCORECARD,
                    True,
                    True,
                    selector,
                    direct_policy,
                    args.online_selector,
                    args.online_ridge_selector,
                    True,
                    max(MONTH_DRIFT_PHASES),
                    max(EXECUTION_LAGS),
                    date(2005, 2, 28),
                    args.bear_signal_timing,
                )
                for phase in MONTH_DRIFT_PHASES
                for lag in EXECUTION_LAGS
            ]
            for selector in selected_selectors
            for direct_policy in selected_direct_policies
        }
    finally:
        conn.close()

    results = []
    for selector in selected_selectors:
        for direct_policy in selected_direct_policies:
            direct_name = direct_policy.name if direct_policy else "index_mapping"
            paths = paths_by_selector[(selector.name, direct_name)]
            for rule in selected_rules:
                for policy in selected_policies:
                    cases = [
                        evaluate_path(
                            path,
                            rule,
                            equity_series,
                            defensive_metas,
                            defensive_series,
                            policy,
                            include_decision_rows=args.include_decision_rows,
                        )
                        for path in paths
                    ]
                    summary = summarize(cases)
                    results.append(
                        {
                            "selector_policy": asdict(selector),
                            "direct_etf_policy": asdict(direct_policy) if direct_policy else None,
                            "rule": asdict(rule),
                            "defensive_policy": asdict(policy),
                            "summary": summary,
                            "cases": cases,
                        }
                    )
                    print(
                        f"{selector.name:<30} {direct_name:<24} {rule.name:<24} "
                        f"{policy.name:<20} pass={summary['pass_count']}/48 "
                        f"min={summary['min_final_capital_wan']:.1f}万 "
                        f"mdd={summary['worst_max_drawdown']*100:.2f}%"
                    )
    results.sort(
        key=lambda item: (
            item["summary"]["objective_met"],
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )

    payload = {
        "objective": "Strict quarterly-only domestic passive ETF scorecard validation.",
        "constraints": {
            "rebalance_frequency": "quarterly_only",
            "rebalance_interval_months": STRICT_OBJECTIVE.rebalance_interval_months,
            "rebalance_spacing": "exactly_every_three_months_from_each_drift_start",
            "quarterly_weights_frozen": True,
            "quarterly_shares_frozen": True,
            "daily_valuation_only": True,
            "bear_signal_timing": args.bear_signal_timing,
            "intra_quarter_constant_weight_rebalancing": False,
            "transaction_cost_scope": "whole_portfolio_at_quarter_boundary",
            "actual_etf_returns": True,
            "domestic_passive_etf_only": True,
            "no_overseas_assets": True,
            "no_options": True,
            "no_futures": True,
            "no_crypto": True,
            "no_shorting": True,
            "max_gross_weight": STRICT_OBJECTIVE.maximum_gross_weight,
            "early_proxy_policy": "point-in-time broad SH/SZ passive ETF fallback",
        },
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "market_scorecard": asdict(ANNUAL_MARKET_SCORECARD),
        "selector_refresh_frequency": "every_three_months",
        "online_selector": args.online_selector,
        "online_ridge_selector": args.online_ridge_selector,
        "feature_policy": asdict(DIRECTION_MATCHED_FEATURE_POLICY),
        "phase_offsets": list(MONTH_DRIFT_PHASES),
        "execution_lags": list(EXECUTION_LAGS),
        "equity_etf_universe": describe_equity_universe(equity_metas),
        "defensive_etf_universe": describe_universe(defensive_metas),
        "results": results,
    }
    prefix = Path(args.output_prefix)
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "name", "selector_policy", "pass_count", "count", "objective_met", "min_final_capital_wan",
            "median_final_capital_wan", "worst_max_drawdown", "median_max_drawdown",
            "median_average_exposure", "median_online_guard_count",
            "median_direction_risk_gate_rejection_count",
            "median_selector_dispersion_recovery_count",
            "median_recovery_count",
            "median_quality_high_count",
            "median_quality_low_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            summary = {key: value for key, value in item["summary"].items() if key != "case_matrix"}
            writer.writerow(
                {
                    "name": (
                        f"{item['selector_policy']['name']}__{item['rule']['name']}__"
                        f"{item['defensive_policy']['name']}"
                    ),
                    "selector_policy": item["selector_policy"]["name"],
                    **summary,
                }
            )
    print(f"Wrote {json_path}")
    return 0 if results[0]["summary"]["objective_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Calendar-neutral CSI scorecard backtest with monthly-or-slower decisions."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import (  # noqa: E402
    ROBUST_TREND_TOP5,
    SNAPSHOT_CSI_SELECTOR,
    SELECTOR_POLICIES,
    SelectorPolicy,
)
from backtest.domestic_defensive_etf import (  # noqa: E402
    DEFENSIVE_POLICIES,
    DefensivePolicy,
    apply_portfolio_drawdown_guard,
    describe_universe,
    load_defensive_etf_universe,
    select_defensive_weights,
)
from backtest.phase_schedule import shift_month_end  # noqa: E402
from backtest.monthly_direction_model import (  # noqa: E402
    MONTHLY_DIRECTION_FEATURES,
    MONTHLY_DIRECTION_POLICIES,
    MonthlyDirectionPolicy,
    attach_walkforward_predictions,
)
from backtest.monthly_online_selector import (  # noqa: E402
    OnlineCrossSectionSelector,
    OnlineSelectorConfig,
)
from backtest.phase_features import PHASE_FEATURE_STORE  # noqa: E402
from db.connection import get_connection  # noqa: E402
from scripts.backtest_calendar_neutral_csi_tipp import (  # noqa: E402
    load_selector_price_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import (  # noqa: E402
    load_price_series,
    period_return,
    price_at,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CASH_ANNUAL_RATE,
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402
from scripts.validate_scorecard_csi_generalization import (  # noqa: E402
    AllocationPolicy,
    DIRECTION_MATCHED_FEATURE_POLICY,
    FORMAL_SCHEDULES,
    MONTH_DRIFT_PHASES,
    run_phase_schedule,
)

OUT_DIR = ROOT / "data" / "backtests"
QUICK_JSON = OUT_DIR / "calendar_neutral_csi_monthly_lag3_report.json"
QUICK_CSV = OUT_DIR / "calendar_neutral_csi_monthly_lag3_search.csv"
FULL_JSON = OUT_DIR / "calendar_neutral_csi_monthly_report.json"
FULL_CSV = OUT_DIR / "calendar_neutral_csi_monthly_search.csv"
EXECUTION_LAGS = (0, 1, 3, 5)
EARLY_DIRECTION_PROXY_CODES = (
    "000300.SH",
    "000016.SH",
    "399001.SZ",
    "000001.SH",
)

BASE_ALLOCATION_POLICY = AllocationPolicy(
    "direction_matched_smooth_score0_floor95",
    smooth_score_mapping=True,
    refresh_every_review=True,
    opportunity_floor_score_lte=0,
    opportunity_floor_pct=95.0,
)


@dataclass(frozen=True)
class MonthlyRiskRule:
    name: str
    floor_pct: float
    multiplier: float
    base_scale: float
    max_exposure: float
    bear_cap: float
    trend_months: int = 6


def build_rules() -> list[MonthlyRiskRule]:
    rules = []
    for floor_pct in (0.90, 0.91, 0.92, 0.94, 0.96):
        for multiplier in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 9.0, 10.0, 12.0):
            for base_scale, max_exposure in (
                (0.20, 0.20),
                (0.25, 0.25),
                (0.30, 0.30),
                (0.40, 0.40),
                (0.50, 0.50),
                (1.0, 1.0),
                (1.25, 1.25),
                (1.5, 1.5),
            ):
                for bear_cap in (0.0, 0.25, 0.40, 0.50):
                    rules.append(
                        MonthlyRiskRule(
                            name=(
                                f"monthly_f{int(floor_pct*100)}_m{int(multiplier)}"
                                f"_s{int(base_scale*100)}_bc{int(bear_cap*100)}"
                            ),
                            floor_pct=floor_pct,
                            multiplier=multiplier,
                            base_scale=base_scale,
                            max_exposure=max_exposure,
                            bear_cap=bear_cap,
                        )
                    )
    return rules


RULES = build_rules()
RULES.extend(
    [
        MonthlyRiskRule("monthly_f915_m9_s125_bc25", 0.915, 9.0, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f915_m9p5_s125_bc25", 0.915, 9.5, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f92_m9p5_s125_bc25", 0.92, 9.5, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f90_m10p2_s125_bc25", 0.90, 10.2, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f90_m10p4_s125_bc25", 0.90, 10.4, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f90_m10p6_s125_bc25", 0.90, 10.6, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f9025_m10p5_s125_bc25", 0.9025, 10.5, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f9025_m10p75_s125_bc25", 0.9025, 10.75, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f905_m11_s125_bc25", 0.905, 11.0, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f9075_m11p5_s125_bc25", 0.9075, 11.5, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f91_m11_s125_bc25", 0.91, 11.0, 1.25, 1.25, 0.25),
        MonthlyRiskRule("monthly_f90_m10_s125_mx110_bc25", 0.90, 10.0, 1.25, 1.10, 0.25),
        MonthlyRiskRule("monthly_f90_m10_s125_mx115_bc25", 0.90, 10.0, 1.25, 1.15, 0.25),
        MonthlyRiskRule("monthly_f90_m10_s125_mx120_bc25", 0.90, 10.0, 1.25, 1.20, 0.25),
    ]
)


def cash_return(start: date, end: date) -> float:
    return CASH_ANNUAL_RATE * max((end - start).days, 0) / 365.25


def weighted_return(
    series: dict[str, list[tuple[date, float]]],
    weights: dict[str, float],
    start: date,
    end: date,
) -> float:
    return sum(weight * period_return(series, code, start, end) for code, weight in weights.items())


def is_bear_state(
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    trend_months: int,
) -> bool:
    trend_start = shift_month_end(snapshot, -trend_months)
    prior_month = shift_month_end(snapshot, -1)
    return (
        period_return(series, CS300_CODE, trend_start, snapshot) < 0.0
        and period_return(series, CS300_CODE, prior_month, snapshot) < 0.0
    )


def build_monthly_path(
    schedule,
    phase: int,
    lag: int,
    trade_dates: list[date],
    selector_policy: SelectorPolicy,
) -> dict[str, Any]:
    base = run_phase_schedule(
        schedule,
        phase,
        lag,
        include_rows=True,
        allocation_policy=BASE_ALLOCATION_POLICY,
        feature_policy=DIRECTION_MATCHED_FEATURE_POLICY,
        include_market_features=False,
        selector_policy=selector_policy,
    )
    months = []
    for row in base["rows"]:
        start_snapshot = date.fromisoformat(row["start_snapshot_date"])
        end_snapshot = date.fromisoformat(row["end_snapshot_date"])
        cursor = start_snapshot
        while cursor < end_snapshot:
            next_snapshot = min(shift_month_end(cursor, 1), end_snapshot)
            months.append(
                {
                    "snapshot": cursor,
                    "next_snapshot": next_snapshot,
                    "start_exec": shifted_boundary(trade_dates, cursor, lag),
                    "end_exec": shifted_boundary(trade_dates, next_snapshot, lag),
                    "holding_codes": list(row["holding_codes"]),
                    "holding_weights": dict(row["holding_weights"]),
                    "base_weight": float(row["equity_pct"]) / 100.0,
                    "review_interval_months": int(row["review_interval_months"]),
                }
            )
            cursor = next_snapshot
    return {
        "schedule": schedule.name,
        "phase": phase,
        "lag": lag,
        "sample_start": base["sample_start"],
        "sample_end": base["sample_end"],
        "months": months,
    }


def enrich_monthly_paths(
    paths,
    price_series,
    trade_dates: list[date],
    selector_policy: SelectorPolicy,
    *,
    monthly_selector_refresh: bool,
    online_selector: bool,
    direction_prehistory_months: int,
) -> None:
    feature_cache = {}
    prehistory_cache = {}
    online_selection_cache: dict[tuple[date, int], tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if online_selector:
                required_by_lag: dict[int, set[date]] = {}
                for path in paths:
                    required_by_lag.setdefault(path["lag"], set()).update(
                        month["snapshot"] for month in path["months"]
                    )
                for lag, required_snapshots in required_by_lag.items():
                    if not required_snapshots:
                        continue
                    model = OnlineCrossSectionSelector(OnlineSelectorConfig())
                    snapshot = shift_month_end(min(required_snapshots), -72)
                    latest_snapshot = max(required_snapshots)
                    while snapshot <= latest_snapshot:
                        model.release_known(snapshot)
                        candidates = SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, snapshot)
                        fallback = SNAPSHOT_CSI_SELECTOR.select(
                            cur,
                            snapshot,
                            selector_policy,
                        )
                        selected, diagnostics = model.select(candidates, fallback)
                        if snapshot in required_snapshots:
                            online_selection_cache[(snapshot, lag)] = (
                                selected,
                                diagnostics,
                            )
                        next_snapshot = shift_month_end(snapshot, 1)
                        start_exec = shifted_boundary(trade_dates, snapshot, lag)
                        end_exec = shifted_boundary(trade_dates, next_snapshot, lag)
                        outcomes = {
                            str(row["ts_code"]): period_return(
                                price_series,
                                str(row["ts_code"]),
                                start_exec,
                                end_exec,
                            )
                            for row in candidates
                            if str(row["ts_code"]) in price_series
                            and price_at(price_series, str(row["ts_code"]), start_exec)
                            is not None
                            and price_at(price_series, str(row["ts_code"]), end_exec)
                            is not None
                        }
                        model.queue_observation(end_exec, candidates, outcomes)
                        snapshot = next_snapshot
            for path in paths:
                for month in path["months"]:
                    if monthly_selector_refresh:
                        if online_selector:
                            selected, selector_diagnostics = online_selection_cache.get(
                                (month["snapshot"], path["lag"]),
                                ([], {"mode": "unavailable", "learned_features": {}}),
                            )
                            month["selector_diagnostics"] = selector_diagnostics
                        else:
                            selected = SNAPSHOT_CSI_SELECTOR.select(
                                cur,
                                month["snapshot"],
                                selector_policy,
                            )
                        if selected:
                            month["holding_codes"] = [row["ts_code"] for row in selected]
                            month["holding_weights"] = {
                                row["ts_code"]: float(row["weight"])
                                for row in selected
                            }
                    feature_key = (month["snapshot"], tuple(month["holding_codes"]))
                    if feature_key not in feature_cache:
                        feature_cache[feature_key] = PHASE_FEATURE_STORE.snapshot_features(
                            cur,
                            month["holding_codes"],
                            CS300_CODE,
                            month["snapshot"],
                        )
                    month["features"] = feature_cache[feature_key]
                    month["risk_return"] = weighted_return(
                        price_series,
                        month["holding_weights"],
                        month["start_exec"],
                        month["end_exec"],
                    )
                if direction_prehistory_months > 0 and path["months"]:
                    first_snapshot = path["months"][0]["snapshot"]
                    prehistory_key = (
                        first_snapshot,
                        path["lag"],
                        direction_prehistory_months,
                    )
                    if prehistory_key not in prehistory_cache:
                        initial_history = []
                        for offset in range(direction_prehistory_months, 0, -1):
                            snapshot = shift_month_end(first_snapshot, -offset)
                            next_snapshot = shift_month_end(snapshot, 1)
                            start_exec = shifted_boundary(
                                trade_dates,
                                snapshot,
                                path["lag"],
                            )
                            end_exec = shifted_boundary(
                                trade_dates,
                                next_snapshot,
                                path["lag"],
                            )
                            proxy_codes = [
                                code
                                for code in EARLY_DIRECTION_PROXY_CODES
                                if price_at(price_series, code, start_exec) is not None
                                and price_at(price_series, code, end_exec) is not None
                            ]
                            if not proxy_codes:
                                continue
                            feature_key = (snapshot, tuple(proxy_codes))
                            if feature_key not in feature_cache:
                                feature_cache[feature_key] = PHASE_FEATURE_STORE.snapshot_features(
                                    cur,
                                    proxy_codes,
                                    CS300_CODE,
                                    snapshot,
                                )
                            initial_history.append(
                                {
                                    "features": feature_cache[feature_key],
                                    "forward_return": weighted_return(
                                        price_series,
                                        {code: 1.0 / len(proxy_codes) for code in proxy_codes},
                                        start_exec,
                                        end_exec,
                                    ),
                                }
                            )
                        prehistory_cache[prehistory_key] = initial_history
                    path["direction_prehistory"] = prehistory_cache[prehistory_key]
    finally:
        conn.close()


def evaluate_path(
    path: dict[str, Any],
    rule: MonthlyRiskRule,
    price_series: dict[str, list[tuple[date, float]]],
    defensive_series: dict[str, list[tuple[date, float]]],
    defensive_metas,
    defensive_policy: DefensivePolicy,
    defensive_return_cache,
    direction_policy: MonthlyDirectionPolicy,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    exposures = []
    bear_months = 0
    defensive_months = 0
    rows = []
    attach_walkforward_predictions(
        path["months"],
        direction_policy,
        initial_history=path.get("direction_prehistory"),
    )
    direction_predictions = []
    for month in path["months"]:
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        scorecard_exposure = min(
            rule.max_exposure,
            month["base_weight"] * rule.base_scale,
            rule.multiplier * cushion / max(capital, 1.0),
        )
        protection_exposure_cap = min(
            rule.max_exposure,
            rule.multiplier * cushion / max(capital, 1.0),
        )
        exposure = scorecard_exposure
        bear_state = is_bear_state(price_series, month["snapshot"], rule.trend_months)
        if bear_state:
            exposure = min(exposure, rule.bear_cap)
            bear_months += 1
        direction_model = month["direction_model"]
        predecision_drawdown = capital / peak - 1.0
        direction_boost_multiplier = direction_policy.nonnegative_exposure_multiplier
        local_vote = direction_model["votes"].get("cs300_ma_6m_distance")
        dxy_vote = direction_model["votes"].get("external_dxy_return_1m")
        if (
            local_vote == -1
            and dxy_vote == 1
            and direction_policy.local_negative_dxy_positive_multiplier is not None
        ):
            direction_boost_multiplier = (
                direction_policy.local_negative_dxy_positive_multiplier
            )
        elif (
            local_vote == 1
            and dxy_vote == -1
            and direction_policy.local_positive_dxy_negative_multiplier is not None
        ):
            direction_boost_multiplier = (
                direction_policy.local_positive_dxy_negative_multiplier
            )
        if (
            direction_model["score"] is not None
            and direction_model["score"] >= 0.0
            and direction_model["vote_count"] >= direction_policy.minimum_vote_count_for_boost
            and predecision_drawdown >= direction_policy.boost_allowed_drawdown_gte
        ):
            exposure = min(
                rule.max_exposure,
                exposure * direction_boost_multiplier,
            )
        positive_floor_signal = (
            direction_model["votes"].get(direction_policy.positive_floor_vote_feature) == 1
            if direction_policy.positive_floor_vote_feature is not None
            else direction_model["score"] is not None and direction_model["score"] > 0.0
        )
        if (
            positive_floor_signal
            and direction_model["vote_count"] >= direction_policy.minimum_vote_count_for_boost
            and predecision_drawdown >= direction_policy.boost_allowed_drawdown_gte
        ):
            exposure = max(
                exposure,
                min(
                    direction_policy.positive_score_exposure_floor,
                    protection_exposure_cap,
                ),
            )
        basket_volatility = month["features"].get("basket_vol_3m")
        volatility_scale = 1.0
        if (
            direction_policy.basket_volatility_target is not None
            and basket_volatility is not None
            and float(basket_volatility) > direction_policy.basket_volatility_target
        ):
            volatility_scale = (
                direction_policy.basket_volatility_target / float(basket_volatility)
            )
            exposure *= volatility_scale
        overheat_flag = bool(
            month["features"].get("cs300_overheat_flag")
            or month["features"].get("basket_overheat_flag")
            or month["features"].get("one_month_surge_flag")
        )
        high_level_distribution_flag = bool(
            month["features"].get("high_level_distribution_flag")
        )
        long_cycle_overheat_flag = bool(
            month["features"].get("long_cycle_overheat_flag")
        )
        bear_rebound_exhaustion_flag = bool(
            month["features"].get("bear_rebound_exhaustion_flag")
        )
        rebound_overheat_flag = bool(
            month["features"].get("rebound_overheat_flag")
            or high_level_distribution_flag
            or long_cycle_overheat_flag
            or bear_rebound_exhaustion_flag
        )
        short_cycle_overheat_flag = bool(
            month["features"].get("short_cycle_overheat_flag")
        )
        medium_cycle_exhaustion_flag = bool(
            month["features"].get("medium_cycle_exhaustion_flag")
        )
        liquidity_stress_flag = bool(
            month["features"].get("domestic_liquidity_stress_flag")
        )
        crisis_continuation_flag = bool(month["features"].get("crisis_continuation_flag"))
        if overheat_flag:
            exposure = min(exposure, direction_policy.overheat_exposure_cap)
        if rebound_overheat_flag:
            exposure = min(exposure, direction_policy.rebound_overheat_exposure_cap)
        if short_cycle_overheat_flag:
            exposure = min(exposure, direction_policy.short_cycle_exposure_cap)
        if medium_cycle_exhaustion_flag:
            exposure = min(exposure, direction_policy.medium_cycle_exposure_cap)
        if month["features"].get("tightening_rebound_exhaustion_flag"):
            exposure = min(
                exposure,
                direction_policy.tightening_rebound_exposure_cap,
            )
        if month["features"].get("refined_mature_reversal_flag"):
            exposure = min(
                exposure,
                direction_policy.mature_reversal_exposure_cap,
            )
        if month["features"].get("rally_distribution_flag"):
            exposure = min(
                exposure,
                direction_policy.rally_distribution_exposure_cap,
            )
        if month["features"].get("financed_surge_reversal_flag"):
            exposure = min(
                exposure,
                direction_policy.financed_surge_exposure_cap,
            )
        if month["features"].get("option_panic_after_rally_flag"):
            exposure = min(
                exposure,
                direction_policy.option_panic_exposure_cap,
            )
        if month["features"].get("turnover_overheat_flag"):
            exposure = min(
                exposure,
                direction_policy.turnover_overheat_exposure_cap,
            )
        if month["features"].get("daily_margin_rally_flag"):
            exposure = min(
                exposure,
                direction_policy.daily_margin_rally_exposure_cap,
            )
        if month["features"].get("low_vol_flat_flag"):
            exposure = min(
                exposure,
                direction_policy.low_vol_flat_exposure_cap,
            )
        if month["features"].get("strong_rally_breadth_reversal_flag"):
            exposure = min(
                exposure,
                direction_policy.breadth_reversal_exposure_cap,
            )
        if month["features"].get("leadership_collapse_tightening_flag"):
            exposure = min(
                exposure,
                direction_policy.leadership_collapse_exposure_cap,
            )
        if month["features"].get("leverage_macro_divergence_flag"):
            exposure = min(
                exposure,
                direction_policy.leverage_macro_exposure_cap,
            )
        if month["features"].get("fund_distribution_tight_flag"):
            exposure = min(
                exposure,
                direction_policy.fund_distribution_exposure_cap,
            )
        if month["features"].get("fund_saturation_contraction_flag"):
            exposure = min(
                exposure,
                direction_policy.fund_saturation_exposure_cap,
            )
        if month["features"].get("theme_divergence_3m_flag"):
            exposure = min(
                exposure,
                direction_policy.theme_divergence_3m_exposure_cap,
            )
        if month["features"].get("theme_divergence_1m_tightening_flag"):
            exposure = min(
                exposure,
                direction_policy.theme_divergence_1m_exposure_cap,
            )
        if month["features"].get("theme_divergence_1m_crowded_flag"):
            exposure = min(
                exposure,
                direction_policy.theme_divergence_1m_crowded_exposure_cap,
            )
        if month["features"].get("credit_contraction_tightening_flag"):
            exposure = min(
                exposure,
                direction_policy.credit_contraction_exposure_cap,
            )
        if month["features"].get("macro_weak_rebound_flag"):
            exposure = min(
                exposure,
                direction_policy.macro_weak_rebound_exposure_cap,
            )
        if month["features"].get("weak_credit_leveraged_rebound_flag"):
            exposure = min(
                exposure,
                direction_policy.weak_credit_rebound_exposure_cap,
            )
        if month["features"].get("fund_moderate_distribution_flag"):
            exposure = min(
                exposure,
                direction_policy.fund_moderate_distribution_exposure_cap,
            )
        if liquidity_stress_flag:
            exposure = min(exposure, direction_policy.liquidity_stress_exposure_cap)
        if crisis_continuation_flag:
            exposure = min(exposure, direction_policy.crisis_exposure_cap)
        if (
            direction_model["score"] is not None
            and direction_model["score"] <= direction_policy.negative_score_lte
            and direction_model["vote_count"] >= direction_policy.minimum_vote_count_for_cap
        ):
            exposure = min(exposure, direction_policy.negative_exposure_cap)

        risk_return = month["risk_return"]
        safe_return = cash_return(month["start_exec"], month["end_exec"])
        defensive_key = (
            defensive_policy.name,
            month["snapshot"],
            month["start_exec"],
            month["end_exec"],
            predecision_drawdown <= (defensive_policy.portfolio_drawdown_threshold or -99.0),
        )
        if defensive_key not in defensive_return_cache:
            defensive_weights = select_defensive_weights(
                defensive_metas,
                defensive_series,
                month["snapshot"],
                defensive_policy,
            )
            defensive_weights, defensive_guard_active = apply_portfolio_drawdown_guard(
                defensive_weights,
                defensive_metas,
                defensive_policy,
                predecision_drawdown,
            )
            defensive_return = weighted_return(
                defensive_series,
                defensive_weights,
                month["start_exec"],
                month["end_exec"],
            ) + (1.0 - sum(defensive_weights.values())) * safe_return
            defensive_return_cache[defensive_key] = (
                defensive_return,
                defensive_weights,
                defensive_guard_active,
            )
        else:
            defensive_return, defensive_weights, defensive_guard_active = defensive_return_cache[
                defensive_key
            ]
        defensive_months += bool(defensive_weights)
        if exposure <= 1.0:
            portfolio_return = exposure * risk_return + (1.0 - exposure) * defensive_return
        else:
            financing_return = 0.04 * max((month["end_exec"] - month["start_exec"]).days, 0) / 365.25
            portfolio_return = exposure * risk_return + (1.0 - exposure) * financing_return
        capital = max(1.0, capital * (1.0 + portfolio_return))
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
        rows.append(
            {
                "snapshot": month["snapshot"].isoformat(),
                "next_snapshot": month["next_snapshot"].isoformat(),
                "start_exec": month["start_exec"].isoformat(),
                "end_exec": month["end_exec"].isoformat(),
                "review_interval_months": month["review_interval_months"],
                "holding_codes": month["holding_codes"],
                "holding_weights": month["holding_weights"],
                "selector_mode": month.get("selector_diagnostics", {}).get("mode", "static"),
                "online_selector_features": sorted(
                    month.get("selector_diagnostics", {}).get("learned_features", {})
                ),
                "defensive_codes": sorted(defensive_weights),
                "defensive_weights": defensive_weights,
                "defensive_drawdown_guard_active": defensive_guard_active,
                "scorecard_base_weight": month["base_weight"],
                "scorecard_exposure_before_caps": scorecard_exposure,
                "protection_exposure_cap": protection_exposure_cap,
                "predecision_drawdown": predecision_drawdown,
                "direction_boost_multiplier": direction_boost_multiplier,
                "basket_volatility_3m": basket_volatility,
                "volatility_scale": volatility_scale,
                "exposure": exposure,
                "bear_state": bear_state,
                "direction_score": direction_model["score"],
                "predicted_direction": direction_model["predicted_direction"],
                "direction_vote_count": direction_model["vote_count"],
                "direction_votes": direction_model["votes"],
                "market_overheat_flag": overheat_flag,
                "one_month_surge_flag": bool(
                    month["features"].get("one_month_surge_flag")
                ),
                "rebound_overheat_flag": rebound_overheat_flag,
                "short_cycle_overheat_flag": short_cycle_overheat_flag,
                "medium_cycle_exhaustion_flag": medium_cycle_exhaustion_flag,
                "leveraged_rally_exhaustion_flag": bool(
                    month["features"].get("leveraged_rally_exhaustion_flag")
                ),
                "tightening_rebound_exhaustion_flag": bool(
                    month["features"].get("tightening_rebound_exhaustion_flag")
                ),
                "low_vol_mature_trend_flag": bool(
                    month["features"].get("low_vol_mature_trend_flag")
                ),
                "refined_mature_reversal_flag": bool(
                    month["features"].get("refined_mature_reversal_flag")
                ),
                "rally_distribution_flag": bool(
                    month["features"].get("rally_distribution_flag")
                ),
                "financed_surge_reversal_flag": bool(
                    month["features"].get("financed_surge_reversal_flag")
                ),
                "option_panic_after_rally_flag": bool(
                    month["features"].get("option_panic_after_rally_flag")
                ),
                "turnover_overheat_flag": bool(
                    month["features"].get("turnover_overheat_flag")
                ),
                "daily_margin_rally_flag": bool(
                    month["features"].get("daily_margin_rally_flag")
                ),
                "low_vol_flat_flag": bool(
                    month["features"].get("low_vol_flat_flag")
                ),
                "strong_rally_breadth_reversal_flag": bool(
                    month["features"].get("strong_rally_breadth_reversal_flag")
                ),
                "leadership_collapse_tightening_flag": bool(
                    month["features"].get("leadership_collapse_tightening_flag")
                ),
                "leverage_macro_divergence_flag": bool(
                    month["features"].get("leverage_macro_divergence_flag")
                ),
                "fund_distribution_tight_flag": bool(
                    month["features"].get("fund_distribution_tight_flag")
                ),
                "fund_saturation_contraction_flag": bool(
                    month["features"].get("fund_saturation_contraction_flag")
                ),
                "theme_divergence_3m_flag": bool(
                    month["features"].get("theme_divergence_3m_flag")
                ),
                "theme_divergence_1m_tightening_flag": bool(
                    month["features"].get("theme_divergence_1m_tightening_flag")
                ),
                "theme_divergence_1m_crowded_flag": bool(
                    month["features"].get("theme_divergence_1m_crowded_flag")
                ),
                "credit_contraction_tightening_flag": bool(
                    month["features"].get("credit_contraction_tightening_flag")
                ),
                "macro_weak_rebound_flag": bool(
                    month["features"].get("macro_weak_rebound_flag")
                ),
                "weak_credit_leveraged_rebound_flag": bool(
                    month["features"].get("weak_credit_leveraged_rebound_flag")
                ),
                "fund_moderate_distribution_flag": bool(
                    month["features"].get("fund_moderate_distribution_flag")
                ),
                "domestic_liquidity_stress_flag": liquidity_stress_flag,
                "domestic_shibor_on_level": month["features"].get(
                    "domestic_shibor_on_level"
                ),
                "domestic_shibor_on_change_1m": month["features"].get(
                    "domestic_shibor_on_change_1m"
                ),
                "domestic_shibor_on_percentile_3y": month["features"].get(
                    "domestic_shibor_on_percentile_3y"
                ),
                "high_level_distribution_flag": high_level_distribution_flag,
                "long_cycle_overheat_flag": long_cycle_overheat_flag,
                "bear_rebound_exhaustion_flag": bear_rebound_exhaustion_flag,
                "crisis_continuation_flag": crisis_continuation_flag,
                "direction_match": (
                    direction_model["predicted_direction"] * risk_return > 0
                    if direction_model["predicted_direction"] and risk_return
                    else None
                ),
                "risk_return": risk_return,
                "defensive_return": defensive_return,
                "portfolio_return": portfolio_return,
                "capital": capital,
                "drawdown": capital / peak - 1.0,
            }
        )
        if direction_model["predicted_direction"] and risk_return:
            direction_predictions.append(
                (direction_model["predicted_direction"], risk_return)
            )
    mdd = max_drawdown(curve)
    direction_bucket_stats = {}
    for label, predicate in (
        ("positive", lambda row: row["direction_score"] is not None and row["direction_score"] > 0),
        ("neutral", lambda row: row["direction_score"] == 0),
        ("negative", lambda row: row["direction_score"] is not None and row["direction_score"] < 0),
        ("unavailable", lambda row: row["direction_score"] is None),
    ):
        returns = [row["risk_return"] for row in rows if predicate(row)]
        direction_bucket_stats[label] = {
            "count": len(returns),
            "mean_return": statistics.mean(returns) if returns else None,
            "positive_rate": (
                sum(value > 0.0 for value in returns) / len(returns) if returns else None
            ),
            "return_weighted_hit_rate": (
                sum(abs(value) for value in returns if value > 0.0)
                / sum(abs(value) for value in returns)
                if returns and sum(abs(value) for value in returns) > 0.0
                else None
            ),
        }
    direction_signature_stats = {}
    signatures = sorted(
        {
            ",".join(f"{key}:{value:+d}" for key, value in sorted(row["direction_votes"].items()))
            for row in rows
            if row["direction_votes"]
        }
    )
    for signature in signatures:
        returns = [
            row["risk_return"]
            for row in rows
            if ",".join(
                f"{key}:{value:+d}" for key, value in sorted(row["direction_votes"].items())
            )
            == signature
        ]
        direction_signature_stats[signature] = {
            "count": len(returns),
            "mean_return": statistics.mean(returns),
            "positive_rate": sum(value > 0.0 for value in returns) / len(returns),
            "return_weighted_hit_rate": (
                sum(abs(value) for value in returns if value > 0.0)
                / sum(abs(value) for value in returns)
                if sum(abs(value) for value in returns) > 0.0
                else None
            ),
        }
    return {
        "name": (
            f"{rule.name}_{defensive_policy.name}_{path['schedule']}"
            f"_phase{path['phase']}_lag{path['lag']}"
        ),
        "rule": rule.name,
        "defensive_policy": defensive_policy.name,
        "direction_policy": direction_policy.name,
        "schedule": path["schedule"],
        "phase_month_offset": path["phase"],
        "execution_lag_days": path["lag"],
        "direction_prehistory_count": len(path.get("direction_prehistory", [])),
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / 20.0) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "average_exposure": statistics.mean(exposures),
        "bear_month_count": bear_months,
        "defensive_month_count": defensive_months,
        "direction_hit_rate": (
            sum(prediction * outcome > 0 for prediction, outcome in direction_predictions)
            / len(direction_predictions)
            if direction_predictions
            else None
        ),
        "direction_weighted_hit_rate": (
            sum(abs(outcome) for prediction, outcome in direction_predictions if prediction * outcome > 0)
            / sum(abs(outcome) for _prediction, outcome in direction_predictions)
            if direction_predictions and sum(abs(outcome) for _prediction, outcome in direction_predictions) > 0
            else None
        ),
        "direction_bucket_stats": direction_bucket_stats,
        "direction_signature_stats": direction_signature_stats,
        "rows": rows,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(case["target_met"] for case in cases),
        "risk_pass_count": sum(case["max_drawdown"] >= TARGET_MDD for case in cases),
        "min_final_capital_wan": min(case["final_capital_wan"] for case in cases),
        "median_final_capital_wan": statistics.median(case["final_capital_wan"] for case in cases),
        "worst_max_drawdown": min(case["max_drawdown"] for case in cases),
        "median_max_drawdown": statistics.median(case["max_drawdown"] for case in cases),
        "min_annualized_return": min(case["annualized_return"] for case in cases),
        "median_average_exposure": statistics.median(case["average_exposure"] for case in cases),
        "median_direction_hit_rate": statistics.median(
            case["direction_hit_rate"] for case in cases if case["direction_hit_rate"] is not None
        ),
        "median_direction_weighted_hit_rate": statistics.median(
            case["direction_weighted_hit_rate"]
            for case in cases
            if case["direction_weighted_hit_rate"] is not None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick-lag3", action="store_true")
    parser.add_argument("--rule", action="append")
    parser.add_argument("--defensive-policy", action="append")
    parser.add_argument("--direction-policy", action="append")
    parser.add_argument("--selector-policy", default=ROBUST_TREND_TOP5.name)
    parser.add_argument("--monthly-selector-refresh", action="store_true")
    parser.add_argument("--online-selector", action="store_true")
    parser.add_argument("--direction-prehistory-months", type=int, default=0)
    args = parser.parse_args()
    lags = (3,) if args.quick_lag3 else EXECUTION_LAGS
    output_json = QUICK_JSON if args.quick_lag3 else FULL_JSON
    output_csv = QUICK_CSV if args.quick_lag3 else FULL_CSV

    conn = get_connection()
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
    finally:
        conn.close()
    trade_dates = [day for day, _value in price_series[CS300_CODE]]
    selector_by_name = {policy.name: policy for policy in SELECTOR_POLICIES}
    if args.selector_policy not in selector_by_name:
        raise ValueError(f"unknown selector policy: {args.selector_policy}")
    selector_policy = selector_by_name[args.selector_policy]
    paths = [
        build_monthly_path(schedule, phase, lag, trade_dates, selector_policy)
        for schedule in FORMAL_SCHEDULES
        for phase in MONTH_DRIFT_PHASES
        for lag in lags
    ]
    enrich_monthly_paths(
        paths,
        price_series,
        trade_dates,
        selector_policy,
        monthly_selector_refresh=args.monthly_selector_refresh,
        online_selector=args.online_selector,
        direction_prehistory_months=max(args.direction_prehistory_months, 0),
    )
    rules = [rule for rule in RULES if not args.rule or rule.name in set(args.rule)]
    policies = [
        policy
        for policy in DEFENSIVE_POLICIES
        if not args.defensive_policy or policy.name in set(args.defensive_policy)
    ]
    if args.rule and len(rules) != len(set(args.rule)):
        raise ValueError(f"unknown rules: {sorted(set(args.rule) - {rule.name for rule in rules})}")
    if args.defensive_policy and len(policies) != len(set(args.defensive_policy)):
        raise ValueError(
            f"unknown defensive policies: {sorted(set(args.defensive_policy) - {policy.name for policy in policies})}"
        )
    direction_policies = [
        policy
        for policy in MONTHLY_DIRECTION_POLICIES
        if not args.direction_policy or policy.name in set(args.direction_policy)
    ]
    if args.direction_policy and len(direction_policies) != len(set(args.direction_policy)):
        raise ValueError(
            f"unknown direction policies: "
            f"{sorted(set(args.direction_policy) - {policy.name for policy in direction_policies})}"
        )

    results = []
    defensive_return_cache = {}
    for rule in rules:
        for policy in policies:
            for direction_policy in direction_policies:
                cases = [
                    evaluate_path(
                        path,
                        rule,
                        price_series,
                        defensive_series,
                        defensive_metas,
                        policy,
                        defensive_return_cache,
                        direction_policy,
                    )
                    for path in paths
                ]
                summary = summarize(cases)
                worst_case = min(cases, key=lambda case: case["max_drawdown"])
                minimum_capital_case = min(cases, key=lambda case: case["final_capital"])
                results.append(
                    {
                        "rule": asdict(rule),
                        "defensive_policy": asdict(policy),
                        "direction_policy": asdict(direction_policy),
                        "summary": summary,
                        "cases": [{key: value for key, value in case.items() if key != "rows"} for case in cases],
                        "worst_case_rows": worst_case["rows"],
                        "minimum_capital_case_rows": minimum_capital_case["rows"],
                    }
                )
                print(
                    f"{rule.name:<31} {policy.name:<20} {direction_policy.name:<23} "
                    f"pass={summary['pass_count']:>3}/{summary['count']} "
                    f"min={summary['min_final_capital_wan']:8.1f}万 "
                    f"mdd={summary['worst_max_drawdown']*100:6.2f}% "
                    f"dir={summary['median_direction_weighted_hit_rate']*100:5.1f}%"
                )
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["risk_pass_count"] == item["summary"]["count"],
            item["summary"]["risk_pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    payload = {
        "objective": "Calendar-neutral CSI scorecard with monthly-or-slower signal, risk, and rebalance decisions.",
        "frequency_constraint": "No signal, exposure update, defensive selection, or rebalance occurs more often than monthly.",
        "drawdown_observation_frequency": "month_end",
        "investment_constraint": "A-share index baskets, domestic passive bond/gold ETFs, and cash only; no overseas investment assets.",
        "base_allocation_policy": asdict(BASE_ALLOCATION_POLICY),
        "feature_policy": asdict(DIRECTION_MATCHED_FEATURE_POLICY),
        "selector_policy": asdict(selector_policy),
        "selector_refresh_frequency": (
            "monthly" if args.monthly_selector_refresh else "strategic_review_interval"
        ),
        "online_selector": args.online_selector,
        "online_selector_config": (
            asdict(OnlineSelectorConfig()) if args.online_selector else None
        ),
        "direction_prehistory_months": max(args.direction_prehistory_months, 0),
        "direction_prehistory_proxy_codes": list(EARLY_DIRECTION_PROXY_CODES),
        "defensive_etf_universe": describe_universe(defensive_metas),
        "monthly_direction_features": list(MONTHLY_DIRECTION_FEATURES),
        "execution_lags": list(lags),
        "results": results,
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "name",
            "pass_count",
            "risk_pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_average_exposure",
            "median_direction_hit_rate",
            "median_direction_weighted_hit_rate",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "name": (
                        f"{item['rule']['name']}__{item['defensive_policy']['name']}"
                        f"__{item['direction_policy']['name']}"
                    ),
                    **item["summary"],
                }
            )
    best = results[0]
    print(
        f"Wrote {output_json}; best={best['rule']['name']} "
        f"defense={best['defensive_policy']['name']} "
        f"direction={best['direction_policy']['name']} {best['summary']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

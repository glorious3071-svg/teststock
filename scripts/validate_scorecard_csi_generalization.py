#!/usr/bin/env python3
"""Validate scorecard + CSI robustness with calendar-neutral schedules.

The generalization engine describes time only as cycle length, review interval,
phase offset, and execution lag. Natural-year and calendar-quarter labels are
confined to legacy diagnostics and strategy-specific data adapters.
"""

from __future__ import annotations

import json
import sys
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from backtest.phase_schedule import (
    ScheduleSpec,
    ScheduleWindow,
    build_windows as build_schedule_windows,
    shift_month_end as month_end_shift,
)
from backtest.phase_features import PHASE_FEATURE_STORE
from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR, SelectorPolicy
from backtest.monthly_online_selector import (
    OnlineCrossSectionSelector,
    OnlineRidgeCrossSectionSelector,
    OnlineRidgeSelectorConfig,
    OnlineSelectorConfig,
)
from scripts.backtest_scorecard_csi_midyear_risk import (
    CASH_ANNUAL_RATE,
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    apply_rule,
    load_hybrid_holdings,
    max_drawdown,
)
from backtest.scorecard import score_to_target_equity
from scripts.backtest_scorecard_csi_quarterly_risk import (
    DEFAULT_OVERLAY,
    DEFAULT_RULE,
    apply_cycle_overlay,
    apply_current_risk_caps,
    TARGET_MDD,
    apply_quarterly_overlay,
    boundary_return as raw_boundary_return,
    quarter_bounds,
    scorecard_detail as raw_scorecard_detail,
)

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_generalization_report.json"

LEGACY_REVIEW_FREQUENCIES = {
    "annual": {"Q1"},
    "semiannual": {"Q1", "Q3"},
    "quarterly": {"Q1", "Q2", "Q3", "Q4"},
}
EXECUTION_LAGS = [0, 1, 3, 5]
ROLLING_WINDOWS = [5, 10]
MONTH_DRIFT_PHASES = list(range(12))
MONTH_DRIFT_EXECUTION_LAGS = [0, 1, 3, 5]
MONTHLY_PRESSURE_EXECUTION_LAGS = [0, 1, 3, 5]
MONTHLY_PRESSURE_EXTREME_MOMENTUM_RETURN_GT = 60.0
MONTHLY_PRESSURE_EXTREME_MOMENTUM_CAP_PCT = 60.0
LEGACY_DIAGNOSTIC_PHASES = [0, 4, 5]
LEGACY_DIAGNOSTIC_LAGS = [0, 3]


SCHEDULE_12M_3M = ScheduleSpec("cycle12m_review3m", 12, 3)
SCHEDULE_12M_6M = ScheduleSpec("cycle12m_review6m", 12, 6)
SCHEDULE_12M_12M = ScheduleSpec("cycle12m_review12m", 12, 12)
SCHEDULE_12M_1M = ScheduleSpec("cycle12m_review1m", 12, 1)
FORMAL_SCHEDULES = [SCHEDULE_12M_12M, SCHEDULE_12M_6M, SCHEDULE_12M_3M, SCHEDULE_12M_1M]


@dataclass(frozen=True)
class AllocationPolicy:
    name: str
    smooth_score_mapping: bool
    refresh_every_review: bool
    opportunity_floor_score_lte: int = -3
    opportunity_floor_pct: float = 95.0
    trend_cap_return_lte: float | None = None
    trend_cap_pct: float = 100.0
    trend_cap_max_review_interval_months: int | None = None
    hard_risk_items: tuple[str, ...] = ()
    hard_risk_score_gte: int | None = None
    hard_risk_cap_pct: float = 100.0
    basket_trend_3m_lte: float | None = None
    basket_trend_6m_lte: float | None = None
    basket_trend_cap_pct: float = 100.0
    require_cs300_bear_confirmation: bool = False
    basket_bull_3m_gte: float | None = None
    basket_bull_6m_gte: float | None = None
    bull_floor_pct: float = 0.0
    require_cs300_bull_confirmation: bool = False
    processed_regime_confirmation: bool = False
    processed_bear_cap_pct: float = 100.0
    processed_weak_trend_cap_pct: float = 100.0
    processed_bull_floor_pct: float = 0.0


@dataclass(frozen=True)
class FeaturePolicy:
    name: str
    core_items: tuple[str, ...] | None = None
    short_medium_items: tuple[str, ...] = ()
    one_month_items: tuple[str, ...] = ()


LEGACY_ALLOCATION_POLICY = AllocationPolicy(
    "legacy_binary_midpoint_refresh",
    smooth_score_mapping=False,
    refresh_every_review=False,
)
SMOOTH_REVIEW_POLICY = AllocationPolicy(
    "smooth_score_every_review",
    smooth_score_mapping=True,
    refresh_every_review=True,
)
SMOOTH_REVIEW_TREND_POLICY = AllocationPolicy(
    "smooth_score_every_review_trend_cap",
    smooth_score_mapping=True,
    refresh_every_review=True,
    trend_cap_return_lte=-10.0,
    trend_cap_pct=35.0,
    trend_cap_max_review_interval_months=6,
)
SLOW_FAST_CONFIRMATION_POLICY = AllocationPolicy(
    "slow_features_fast_market_confirmation",
    smooth_score_mapping=True,
    refresh_every_review=True,
    hard_risk_items=(
        "央行口径从紧",
        "1Y定存>3.5%",
        "累计加准>3pp",
        "累计加息>150bp",
    ),
    hard_risk_score_gte=2,
    hard_risk_cap_pct=10.0,
    basket_trend_3m_lte=-0.05,
    basket_trend_6m_lte=0.0,
    basket_trend_cap_pct=35.0,
)
DUAL_MOMENTUM_CONFIRMATION_POLICY = AllocationPolicy(
    "slow_features_dual_momentum_confirmation",
    smooth_score_mapping=True,
    refresh_every_review=True,
    hard_risk_items=(
        "央行口径从紧",
        "1Y定存>3.5%",
        "累计加准>3pp",
        "累计加息>150bp",
    ),
    hard_risk_score_gte=2,
    hard_risk_cap_pct=10.0,
    basket_trend_3m_lte=0.0,
    basket_trend_6m_lte=0.0,
    basket_trend_cap_pct=10.0,
    require_cs300_bear_confirmation=True,
    basket_bull_3m_gte=0.0,
    basket_bull_6m_gte=0.0,
    bull_floor_pct=95.0,
    require_cs300_bull_confirmation=True,
)
PROCESSED_REGIME_CONFIRMATION_POLICY = AllocationPolicy(
    "processed_regime_confirmation",
    smooth_score_mapping=True,
    refresh_every_review=True,
    processed_regime_confirmation=True,
    processed_bear_cap_pct=10.0,
    processed_weak_trend_cap_pct=60.0,
    processed_bull_floor_pct=95.0,
)

FULL_FEATURE_POLICY = FeaturePolicy("full_scorecard")
DIRECTION_MATCHED_FEATURE_POLICY = FeaturePolicy(
    "direction_matched_core",
    core_items=(
        "全球同步刺激",
        "央行口径宽松",
        "PE<15+ROE上升(真底部)",
        "企业景气<110(低信心底部)",
        "1Y定存>3.5%",
        "累计加准>3pp",
        "国家队入场",
        "累计降息>100bp",
    ),
    short_medium_items=(
        "央行口径从紧",
        "累计加息>150bp",
    ),
    one_month_items=(
        "生产>订单≥3(被动累库)",
    ),
)

_SHIFTED_BOUNDARY_CACHE: dict[tuple[date, int], date] = {}
_BOUNDARY_RETURN_CACHE: dict[tuple[str, date, date], float] = {}
_SCORECARD_DETAIL_CACHE: dict[tuple[int, date], dict[str, Any]] = {}
_LATEST_TRADE_DATE_CACHE: date | None = None
_SCHEDULE_EXECUTION_CACHE: dict[tuple[date, int], date] = {}


def month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def previous_month_end(year: int, month: int) -> date:
    if month == 1:
        return date(year - 1, 12, 31)
    return month_end(year, month - 1)


def quarter_for_month(month: int) -> str:
    if month <= 3:
        return "Q1"
    if month <= 6:
        return "Q2"
    if month <= 9:
        return "Q3"
    return "Q4"


def apply_year_for_snapshot(snapshot: date) -> int:
    if snapshot.month == 12 and snapshot.day == 31:
        return snapshot.year + 1
    return snapshot.year


def holding_codes_for_snapshot(holdings: dict[int, list[str]], snapshot: date) -> list[str]:
    apply_year = apply_year_for_snapshot(snapshot)
    if apply_year >= 2014:
        return holdings.get(apply_year, []) or [CS300_CODE]
    return [CS300_CODE]


def cycle_holdings(holdings: dict[int, list[str]], selection_key: int) -> list[str]:
    """Use one precomputed natural-year basket for the full drift cycle.

    This isolates allocation/review timing drift. Re-ranking CSI constituents
    at arbitrary month phases is a separate selector-drift test and must not be
    approximated by rolling the saved annual basket in the middle of a cycle.
    """
    if selection_key >= 2014:
        return holdings.get(selection_key, []) or [CS300_CODE]
    return [CS300_CODE]


def latest_trade_date(cur) -> date:
    global _LATEST_TRADE_DATE_CACHE
    if _LATEST_TRADE_DATE_CACHE is None:
        cur.execute("SELECT MAX(trade_date) FROM index_daily WHERE ts_code=%s", (CS300_CODE,))
        row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError(f"No index_daily coverage for {CS300_CODE}")
        _LATEST_TRADE_DATE_CACHE = row[0]
    return _LATEST_TRADE_DATE_CACHE


def has_complete_execution_boundary(cur, boundary: date, lag_days: int) -> bool:
    cur.execute(
        """
        SELECT trade_date FROM index_daily
        WHERE ts_code=%s AND trade_date > %s
        ORDER BY trade_date ASC LIMIT 1 OFFSET %s
        """,
        (CS300_CODE, boundary, lag_days),
    )
    return cur.fetchone() is not None


def schedule_execution_boundary(cur, snapshot: date, lag_days: int) -> date:
    """Execute after the snapshot; lag zero means the first later trading day."""
    key = (snapshot, lag_days)
    if key in _SCHEDULE_EXECUTION_CACHE:
        return _SCHEDULE_EXECUTION_CACHE[key]
    cur.execute(
        """
        SELECT trade_date FROM index_daily
        WHERE ts_code=%s AND trade_date > %s
        ORDER BY trade_date ASC LIMIT 1 OFFSET %s
        """,
        (CS300_CODE, snapshot, lag_days),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(
            f"Incomplete execution boundary for snapshot={snapshot} lag_days={lag_days}"
        )
    _SCHEDULE_EXECUTION_CACHE[key] = row[0]
    return row[0]


def complete_schedule_anchor(
    cur,
    base_anchor: date,
    spec: ScheduleSpec,
    phase_month_offset: int,
    execution_lag_days: int,
    sample_cycles: int,
) -> tuple[date, int]:
    """Shift a full sample back by whole cycles until its last trade is known."""
    effective_anchor = base_anchor
    shifted_cycles = 0
    while True:
        final_boundary = month_end_shift(
            effective_anchor,
            phase_month_offset + sample_cycles * spec.cycle_months,
        )
        if has_complete_execution_boundary(cur, final_boundary, execution_lag_days):
            return effective_anchor, shifted_cycles
        effective_anchor = month_end_shift(effective_anchor, -spec.cycle_months)
        shifted_cycles += 1


def calendar_allocation_review_state(
    execution_day: date,
    previous_year: int | None,
    previous_review_index: int,
) -> tuple[int, int, bool, bool]:
    """Anchor annual scorecard resets to the investment year, not drift phase."""

    current_year = execution_day.year
    entry = current_year != previous_year
    review_index = 0 if entry else previous_review_index + 1
    return current_year, review_index, entry, review_index == 2


def shifted_boundary(cur, boundary: date, lag_days: int) -> date:
    key = (boundary, lag_days)
    if key in _SHIFTED_BOUNDARY_CACHE:
        return _SHIFTED_BOUNDARY_CACHE[key]
    if lag_days == 0:
        cur.execute(
            """
            SELECT trade_date FROM index_daily
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 1
            """,
            (CS300_CODE, boundary),
        )
    else:
        cur.execute(
            """
            SELECT trade_date FROM index_daily
            WHERE ts_code=%s AND trade_date > %s
            ORDER BY trade_date ASC LIMIT 1 OFFSET %s
            """,
            (CS300_CODE, boundary, lag_days - 1),
        )
    row = cur.fetchone()
    if not row:
        _SHIFTED_BOUNDARY_CACHE[key] = boundary
        return boundary
    _SHIFTED_BOUNDARY_CACHE[key] = row[0]
    return row[0]


def boundary_return(cur, code: str, start_boundary: date, end_boundary: date) -> float:
    key = (code, start_boundary, end_boundary)
    if key not in _BOUNDARY_RETURN_CACHE:
        _BOUNDARY_RETURN_CACHE[key] = raw_boundary_return(cur, code, start_boundary, end_boundary)
    return _BOUNDARY_RETURN_CACHE[key]


def scorecard_detail(conn, year: int, snapshot: date, rule) -> dict[str, Any]:
    key = (year, snapshot)
    if key not in _SCORECARD_DETAIL_CACHE:
        _SCORECARD_DETAIL_CACHE[key] = raw_scorecard_detail(conn, year, snapshot, rule)
    return _SCORECARD_DETAIL_CACHE[key]


def allocation_target(
    detail: dict[str, Any],
    policy: AllocationPolicy,
    review_interval_months: int,
    market_state: dict[str, float],
) -> tuple[float, list[str]]:
    score = int(detail["score"])
    target_pct = float(
        detail["base_equity_pct"]
        if policy.smooth_score_mapping
        else detail["rule_target_equity_pct"]
    )
    reasons: list[str] = []
    if score <= policy.opportunity_floor_score_lte:
        target_pct = max(target_pct, policy.opportunity_floor_pct)
        reasons.append("opportunity_floor")
    cs300_6m = detail["known_inputs"].get("cs300_6m_return")
    if (
        policy.trend_cap_return_lte is not None
        and (
            policy.trend_cap_max_review_interval_months is None
            or review_interval_months <= policy.trend_cap_max_review_interval_months
        )
        and cs300_6m is not None
        and float(cs300_6m) <= policy.trend_cap_return_lte
    ):
        target_pct = min(target_pct, policy.trend_cap_pct)
        reasons.append("trailing6m_trend_cap")
    if policy.hard_risk_score_gte is not None:
        hard_risk_score = sum(
            max(int(item["score"]), 0)
            for item in detail["score_items"]
            if item["name"] in policy.hard_risk_items
        )
        if hard_risk_score >= policy.hard_risk_score_gte:
            target_pct = min(target_pct, policy.hard_risk_cap_pct)
            reasons.append("hard_risk_feature_confirmation")
    bull_confirmed = (
        policy.basket_bull_3m_gte is not None
        and policy.basket_bull_6m_gte is not None
        and market_state["basket_return_3m"] >= policy.basket_bull_3m_gte
        and market_state["basket_return_6m"] >= policy.basket_bull_6m_gte
        and (
            not policy.require_cs300_bull_confirmation
            or market_state["cs300_return_3m"] >= 0.0
        )
    )
    if bull_confirmed:
        target_pct = max(target_pct, policy.bull_floor_pct)
        reasons.append("dual_momentum_bull_confirmation")
        if policy.hard_risk_score_gte is not None and "hard_risk_feature_confirmation" in reasons:
            target_pct = min(target_pct, policy.hard_risk_cap_pct)
    if (
        policy.basket_trend_3m_lte is not None
        and policy.basket_trend_6m_lte is not None
        and market_state["basket_return_3m"] <= policy.basket_trend_3m_lte
        and market_state["basket_return_6m"] <= policy.basket_trend_6m_lte
        and (
            not policy.require_cs300_bear_confirmation
            or market_state["cs300_return_3m"] <= 0.0
        )
    ):
        target_pct = min(target_pct, policy.basket_trend_cap_pct)
        reasons.append("basket_trend_confirmation")
    if policy.processed_regime_confirmation:
        trend_checks = [
            (market_state.get("basket_return_6m"), 0.0),
            (market_state.get("cs300_ma_3m_distance"), 0.0),
            (market_state.get("breadth_ma_6m_distance_positive"), 0.5),
        ]
        usable_trend = [(float(value), threshold) for value, threshold in trend_checks if value is not None]
        positive_trend_votes = sum(value > threshold for value, threshold in usable_trend)
        stress_checks = [
            (market_state.get("external_vix_percentile_3y"), 0.80),
            (market_state.get("external_us_curve_percentile_3y"), 0.75),
            (market_state.get("cs300_vol_1m_percentile_3y"), 0.80),
            (market_state.get("external_dxy_return_1m"), 0.0),
        ]
        stress_votes = sum(
            float(value) > threshold
            for value, threshold in stress_checks
            if value is not None
        )
        if len(usable_trend) >= 2 and positive_trend_votes <= 1:
            target_pct = min(target_pct, policy.processed_weak_trend_cap_pct)
            reasons.append("processed_weak_trend")
            if stress_votes >= 1:
                target_pct = min(target_pct, policy.processed_bear_cap_pct)
                reasons.append("processed_stress_confirmation")
        elif len(usable_trend) >= 2 and positive_trend_votes >= 2 and stress_votes <= 1:
            target_pct = max(target_pct, policy.processed_bull_floor_pct)
            reasons.append("processed_bull_confirmation")
    return target_pct, reasons


def apply_feature_policy(
    detail: dict[str, Any],
    spec: ScheduleSpec,
    policy: FeaturePolicy,
) -> dict[str, Any]:
    if policy.core_items is None:
        return detail
    allowed = set(policy.core_items)
    if spec.review_interval_months <= 6:
        allowed.update(policy.short_medium_items)
    if spec.review_interval_months == 1:
        allowed.update(policy.one_month_items)
    score_items = [item for item in detail["score_items"] if item["name"] in allowed]
    score = sum(int(item["score"]) for item in score_items)
    base_equity_pct, band = score_to_target_equity(score)
    adjusted = dict(detail)
    adjusted.update(
        {
            "score": score,
            "band": band,
            "base_equity_pct": base_equity_pct,
            "rule_target_equity_pct": apply_rule(DEFAULT_RULE, score, base_equity_pct),
            "score_items": score_items,
            "top_score_items": sorted(score_items, key=lambda item: -abs(int(item["score"])))[:8],
            "feature_policy": policy.name,
            "excluded_score_items": [
                item["name"] for item in detail["score_items"] if item["name"] not in allowed
            ],
        }
    )
    return adjusted


def annualized_return(final_capital: float, years: int) -> float:
    return (final_capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0


def summarize(name: str, capital: float, curve: list[float], rows: list[dict[str, Any]], years: int) -> dict[str, Any]:
    mdd = max_drawdown(curve)
    return {
        "name": name,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": annualized_return(capital, years),
        "max_drawdown": mdd,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "capital_target_met": capital >= TARGET_CAPITAL,
        "mdd_target_met": mdd >= TARGET_MDD,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def run_variant(
    frequency: str,
    execution_lag_days: int,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    include_rows: bool = False,
) -> dict[str, Any]:
    review_quarters = LEGACY_REVIEW_FREQUENCIES[frequency]
    years = end_year - start_year + 1
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            values: dict[str, float] = {}
            cash = capital
            current_equity_pct = 0.0
            previous_quarter_return: float | None = None
            annual_rule_target = 0.0
            for year in range(start_year, end_year + 1):
                codes = holdings.get(year, []) if year >= 2014 else [CS300_CODE]
                h1_return: float | None = None
                for quarter in ["Q1", "Q2", "Q3", "Q4"]:
                    start_boundary, end_boundary, snapshot = quarter_bounds(year, quarter)
                    start_exec = shifted_boundary(cur, start_boundary, execution_lag_days)
                    end_exec = shifted_boundary(cur, end_boundary, execution_lag_days)
                    detail = scorecard_detail(conn, year, snapshot, DEFAULT_RULE)
                    reasons: list[str] = []
                    if quarter == "Q3":
                        h1_return = sum(
                            boundary_return(cur, code, date(year - 1, 12, 31), date(year, 6, 30))
                            for code in codes
                        ) / len(codes)

                    should_review = quarter in review_quarters
                    if quarter == "Q1":
                        target_pct = float(detail["rule_target_equity_pct"])
                        annual_rule_target = target_pct
                        target_pct, reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return or 0.0,
                            DEFAULT_OVERLAY,
                            annual_entry=True,
                        )
                        current_equity_pct = target_pct
                        cash = capital * (1.0 - current_equity_pct / 100.0)
                        values = {
                            code: capital * current_equity_pct / 100.0 / len(codes)
                            for code in codes
                        }
                    elif should_review:
                        target_pct = current_equity_pct
                        if quarter == "Q3" and float(detail["rule_target_equity_pct"]) < target_pct:
                            target_pct = float(detail["rule_target_equity_pct"])
                            reasons.append("scorecard_midyear_risk_reduce")
                        target_pct, overlay_reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return or 0.0,
                            DEFAULT_OVERLAY,
                            annual_entry=False,
                        )
                        reasons.extend(overlay_reasons)
                        known = detail["known_inputs"]
                        can_recover = (
                            target_pct < annual_rule_target
                            and previous_quarter_return is not None
                            and previous_quarter_return > DEFAULT_OVERLAY.recover_prev_quarter_return_gt
                            and (known.get("pmi_mfg_3m_avg") or 0.0) >= DEFAULT_OVERLAY.recover_pmi_3m_gte
                            and not ((known.get("ppi_yoy") or 0.0) < DEFAULT_OVERLAY.weak_repair_ppi_lt)
                        )
                        if can_recover:
                            target_pct = annual_rule_target
                            reasons.append("recover_after_positive_q")
                            target_pct, cap_reasons = apply_current_risk_caps(
                                target_pct,
                                detail,
                                DEFAULT_OVERLAY,
                            )
                            reasons.extend(cap_reasons)
                        if target_pct != current_equity_pct:
                            equity_value = sum(values.values())
                            target_value = capital * target_pct / 100.0
                            if equity_value > 0:
                                scale = target_value / equity_value
                                values = {code: value * scale for code, value in values.items()}
                            else:
                                values = {code: target_value / len(codes) for code in codes}
                            cash = capital - target_value
                            current_equity_pct = target_pct

                    quarter_returns = []
                    for code in list(values):
                        ret = boundary_return(cur, code, start_exec, end_exec)
                        values[code] *= 1.0 + ret
                        quarter_returns.append(ret)
                    cash *= 1.0 + CASH_ANNUAL_RATE / 4.0
                    capital = sum(values.values()) + cash
                    peak = max(peak, capital)
                    curve.append(capital)
                    previous_quarter_return = sum(quarter_returns) / len(quarter_returns) if quarter_returns else 0.0
                    if include_rows:
                        rows.append(
                            {
                                "year": year,
                                "quarter": quarter,
                                "execution_lag_days": execution_lag_days,
                                "start_exec": start_exec.isoformat(),
                                "end_exec": end_exec.isoformat(),
                                "score": detail["score"],
                                "equity_pct": current_equity_pct,
                                "mean_equity_return": previous_quarter_return,
                                "portfolio_drawdown": capital / peak - 1.0,
                                "capital": capital,
                                "rebalance_reasons": reasons,
                            }
                        )
    finally:
        conn.close()
    return summarize(
        f"{frequency}_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        years,
    ) | {
        "frequency": frequency,
        "execution_lag_days": execution_lag_days,
        "start_year": start_year,
        "end_year": end_year,
    }


def run_legacy_label_coupled_3m_review_drift(
    phase_month_offset: int,
    execution_lag_days: int,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    include_rows: bool = False,
) -> dict[str, Any]:
    years = end_year - start_year + 1
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            values: dict[str, float] = {}
            cash = capital
            current_equity_pct = 0.0
            current_codes: list[str] = []
            previous_quarter_return: float | None = None
            annual_rule_target = 0.0

            for year in range(start_year, end_year + 1):
                h1_return: float | None = None
                for quarter in ["Q1", "Q2", "Q3", "Q4"]:
                    start_boundary, end_boundary, snapshot = quarter_bounds(year, quarter)
                    start_snapshot = month_end_shift(start_boundary, phase_month_offset)
                    end_snapshot = month_end_shift(end_boundary, phase_month_offset)
                    start_exec = shifted_boundary(cur, start_snapshot, execution_lag_days)
                    end_exec = shifted_boundary(cur, end_snapshot, execution_lag_days)
                    apply_year = apply_year_for_snapshot(start_snapshot)
                    codes = holding_codes_for_snapshot(holdings, start_snapshot)
                    detail = scorecard_detail(conn, apply_year, start_snapshot, DEFAULT_RULE)
                    reasons: list[str] = []
                    if quarter == "Q3":
                        h1_start = month_end_shift(start_snapshot, -6)
                        h1_return = sum(
                            boundary_return(cur, code, h1_start, start_snapshot)
                            for code in codes
                        ) / len(codes)

                    if quarter == "Q1":
                        target_pct = float(detail["rule_target_equity_pct"])
                        annual_rule_target = target_pct
                        target_pct, reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return or 0.0,
                            DEFAULT_OVERLAY,
                            annual_entry=True,
                        )
                        current_equity_pct = target_pct
                        cash = capital * (1.0 - current_equity_pct / 100.0)
                        values = {
                            code: capital * current_equity_pct / 100.0 / len(codes)
                            for code in codes
                        }
                        current_codes = codes
                    else:
                        target_pct = current_equity_pct
                        if quarter == "Q3" and float(detail["rule_target_equity_pct"]) < target_pct:
                            target_pct = float(detail["rule_target_equity_pct"])
                            reasons.append("scorecard_midyear_risk_reduce")
                        target_pct, overlay_reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return or 0.0,
                            DEFAULT_OVERLAY,
                            annual_entry=False,
                        )
                        reasons.extend(overlay_reasons)
                        known = detail["known_inputs"]
                        can_recover = (
                            target_pct < annual_rule_target
                            and previous_quarter_return is not None
                            and previous_quarter_return > DEFAULT_OVERLAY.recover_prev_quarter_return_gt
                            and (known.get("pmi_mfg_3m_avg") or 0.0) >= DEFAULT_OVERLAY.recover_pmi_3m_gte
                            and not ((known.get("ppi_yoy") or 0.0) < DEFAULT_OVERLAY.weak_repair_ppi_lt)
                        )
                        if can_recover:
                            target_pct = annual_rule_target
                            reasons.append("recover_after_positive_q")
                            target_pct, cap_reasons = apply_current_risk_caps(
                                target_pct,
                                detail,
                                DEFAULT_OVERLAY,
                            )
                            reasons.extend(cap_reasons)
                        codes_changed = codes != current_codes
                        if codes_changed:
                            reasons.append("date_aware_csi_basket_roll")
                        if target_pct != current_equity_pct or codes_changed:
                            equity_value = sum(values.values())
                            target_value = capital * target_pct / 100.0
                            if codes_changed:
                                values = {code: target_value / len(codes) for code in codes}
                            elif equity_value > 0:
                                scale = target_value / equity_value
                                values = {code: value * scale for code, value in values.items()}
                            else:
                                values = {code: target_value / len(codes) for code in codes}
                            cash = capital - target_value
                            current_equity_pct = target_pct
                            current_codes = codes

                    quarter_returns = []
                    for code in list(values):
                        ret = boundary_return(cur, code, start_exec, end_exec)
                        values[code] *= 1.0 + ret
                        quarter_returns.append(ret)
                    holding_days = max((end_exec - start_exec).days, 0)
                    cash *= 1.0 + CASH_ANNUAL_RATE * holding_days / 365.25
                    capital = sum(values.values()) + cash
                    peak = max(peak, capital)
                    curve.append(capital)
                    previous_quarter_return = sum(quarter_returns) / len(quarter_returns) if quarter_returns else 0.0
                    if include_rows:
                        rows.append(
                            {
                                "year": year,
                                "quarter": quarter,
                                "phase_month_offset": phase_month_offset,
                                "execution_lag_days": execution_lag_days,
                                "start_exec": start_exec.isoformat(),
                                "end_exec": end_exec.isoformat(),
                                "start_snapshot_date": start_snapshot.isoformat(),
                                "end_snapshot_date": end_snapshot.isoformat(),
                                "score": detail["score"],
                                "equity_pct": current_equity_pct,
                                "mean_equity_return": previous_quarter_return,
                                "portfolio_drawdown": capital / peak - 1.0,
                                "capital": capital,
                                "rebalance_reasons": reasons,
                            }
                        )
    finally:
        conn.close()
    return summarize(
        f"month_phase_drift_phase{phase_month_offset}_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        years,
    ) | {
        "diagnostic": "legacy_label_coupled_3m_review_drift",
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "start_year": start_year,
        "end_year": end_year,
    }


def run_legacy_label_coupled_12m_review_drift(
    phase_month_offset: int,
    execution_lag_days: int,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    include_rows: bool = False,
) -> dict[str, Any]:
    years = end_year - start_year + 1
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for year in range(start_year, end_year + 1):
                start_snapshot = month_end_shift(date(year - 1, 12, 31), phase_month_offset)
                end_snapshot = month_end_shift(date(year, 12, 31), phase_month_offset)
                start_exec = shifted_boundary(cur, start_snapshot, execution_lag_days)
                end_exec = shifted_boundary(cur, end_snapshot, execution_lag_days)
                apply_year = apply_year_for_snapshot(start_snapshot)
                codes = holding_codes_for_snapshot(holdings, start_snapshot)
                detail = scorecard_detail(conn, apply_year, start_snapshot, DEFAULT_RULE)
                target_pct = float(detail["rule_target_equity_pct"])
                target_pct, reasons = apply_quarterly_overlay(
                    target_pct,
                    detail,
                    "Q1",
                    0.0,
                    DEFAULT_OVERLAY,
                    annual_entry=True,
                )
                returns = [
                    boundary_return(cur, code, start_exec, end_exec)
                    for code in codes
                ]
                equity_return = sum(returns) / len(returns) if returns else 0.0
                holding_days = max((end_exec - start_exec).days, 0)
                cash_return = CASH_ANNUAL_RATE * holding_days / 365.25
                portfolio_return = target_pct / 100.0 * equity_return + (1.0 - target_pct / 100.0) * cash_return
                capital *= 1.0 + portfolio_return
                peak = max(peak, capital)
                curve.append(capital)
                if include_rows:
                    rows.append(
                        {
                            "year": year,
                            "apply_year": apply_year,
                            "phase_month_offset": phase_month_offset,
                            "execution_lag_days": execution_lag_days,
                            "start_exec": start_exec.isoformat(),
                            "end_exec": end_exec.isoformat(),
                            "start_snapshot_date": start_snapshot.isoformat(),
                            "end_snapshot_date": end_snapshot.isoformat(),
                            "score": detail["score"],
                            "equity_pct": target_pct,
                            "mean_equity_return": equity_return,
                            "portfolio_return": portfolio_return,
                            "portfolio_drawdown": capital / peak - 1.0,
                            "capital": capital,
                            "rebalance_reasons": reasons,
                        }
                    )
    finally:
        conn.close()
    return summarize(
        f"annual_month_phase_drift_phase{phase_month_offset}_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        years,
    ) | {
        "diagnostic": "legacy_label_coupled_12m_review_drift",
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "start_year": start_year,
        "end_year": end_year,
    }


def run_phase_schedule(
    spec: ScheduleSpec,
    phase_month_offset: int,
    execution_lag_days: int,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    include_rows: bool = False,
    allocation_policy: AllocationPolicy = LEGACY_ALLOCATION_POLICY,
    feature_policy: FeaturePolicy = FULL_FEATURE_POLICY,
    include_market_features: bool = True,
    selector_policy: SelectorPolicy | None = None,
    selector_refresh_every_review: bool = False,
    online_selector: bool = False,
    online_ridge_selector: bool = False,
    calendar_year_allocation_reset: bool = False,
    common_completion_phase_offset: int | None = None,
    common_completion_lag_days: int | None = None,
    schedule_anchor: date | None = None,
) -> dict[str, Any]:
    """Run a strategy adapter on a calendar-neutral phase schedule.

    The scheduler knows no year/quarter semantics. This adapter deliberately
    freezes one saved CSI basket per 12-month cycle so this test measures
    allocation/review timing only; arbitrary-date CSI re-ranking is validated
    separately once selector features are available at those snapshots.
    """
    if spec.cycle_months != 12:
        raise ValueError("current scorecard/CSI adapter refreshes its saved basket every 12 months")
    sample_cycles = end_year - start_year + 1
    requested_anchor = schedule_anchor or date(start_year - 1, 12, 31)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            effective_anchor, sample_shift_cycles = complete_schedule_anchor(
                cur,
                requested_anchor,
                spec,
                (
                    common_completion_phase_offset
                    if common_completion_phase_offset is not None
                    else phase_month_offset
                ),
                (
                    common_completion_lag_days
                    if common_completion_lag_days is not None
                    else execution_lag_days
                ),
                sample_cycles,
            )
            windows = build_schedule_windows(
                effective_anchor,
                spec,
                phase_month_offset,
                sample_cycles,
            )
            values: dict[str, float] = {}
            cash = capital
            current_equity_pct = 0.0
            current_codes: list[str] = []
            cycle_weights: dict[str, float] = {}
            previous_window_return: float | None = None
            cycle_rule_target = 0.0
            allocation_year: int | None = None
            allocation_review_index = 0
            if online_ridge_selector:
                online_model = OnlineRidgeCrossSectionSelector(
                    OnlineRidgeSelectorConfig()
                )
            elif online_selector:
                online_model = OnlineCrossSectionSelector(
                    OnlineSelectorConfig(
                        name="online_ic20q_top5",
                        top_n=5,
                        min_history_months=12,
                        history_months=20,
                        min_abs_median_ic=0.02,
                        min_direction_consistency=0.55,
                    )
                )
            else:
                online_model = None

            for window in windows:
                start_exec = schedule_execution_boundary(cur, window.start_snapshot, execution_lag_days)
                end_exec = schedule_execution_boundary(cur, window.end_snapshot, execution_lag_days)
                if calendar_year_allocation_reset:
                    (
                        allocation_year,
                        allocation_review_index,
                        allocation_entry,
                        allocation_midpoint,
                    ) = calendar_allocation_review_state(
                        start_exec,
                        allocation_year,
                        allocation_review_index,
                    )
                else:
                    allocation_entry = window.cycle_entry
                    allocation_midpoint = window.cycle_midpoint
                selection_key = start_year - sample_shift_cycles + window.cycle_index
                online_candidates: list[dict[str, Any]] = []
                if (window.cycle_entry or selector_refresh_every_review) and selector_policy is not None:
                    if online_model is not None:
                        online_model.release_known(window.start_snapshot)
                        online_candidates = SNAPSHOT_CSI_SELECTOR.candidate_rows(
                            cur, window.start_snapshot
                        )
                        fallback = SNAPSHOT_CSI_SELECTOR.select(
                            cur, window.start_snapshot, selector_policy
                        )
                        selected, _online_diagnostics = online_model.select(
                            online_candidates, fallback
                        )
                    else:
                        selected = SNAPSHOT_CSI_SELECTOR.select(
                            cur, window.start_snapshot, selector_policy
                        )
                    if selected:
                        codes = [item["ts_code"] for item in selected]
                        cycle_weights = {item["ts_code"]: float(item["weight"]) for item in selected}
                    else:
                        codes = cycle_holdings(holdings, selection_key)
                        cycle_weights = {code: 1.0 / len(codes) for code in codes}
                elif window.cycle_entry:
                    codes = cycle_holdings(holdings, selection_key)
                    cycle_weights = {code: 1.0 / len(codes) for code in codes}
                else:
                    codes = current_codes
                detail = apply_feature_policy(
                    scorecard_detail(
                        conn,
                        start_exec.year,
                        window.start_snapshot,
                        DEFAULT_RULE,
                    ),
                    spec,
                    feature_policy,
                )
                market_state = (
                    PHASE_FEATURE_STORE.snapshot_features(
                        cur,
                        codes,
                        CS300_CODE,
                        window.start_snapshot,
                    )
                    if include_market_features
                    else {}
                )
                trailing_6m_return = 0.0
                if allocation_midpoint:
                    trailing_start = month_end_shift(window.start_snapshot, -6)
                    trailing_6m_return = sum(
                        boundary_return(cur, code, trailing_start, window.start_snapshot)
                        for code in codes
                    ) / len(codes)
                reasons: list[str] = []

                if allocation_entry:
                    target_pct, reasons = allocation_target(
                        detail,
                        allocation_policy,
                        spec.review_interval_months,
                        market_state,
                    )
                    cycle_rule_target = target_pct
                    target_pct, overlay_reasons = apply_cycle_overlay(
                        target_pct,
                        detail,
                        trailing_6m_return,
                        DEFAULT_OVERLAY,
                        cycle_entry=True,
                        cycle_midpoint=False,
                    )
                    reasons.extend(overlay_reasons)
                    current_equity_pct = target_pct
                    current_codes = codes
                    target_value = capital * current_equity_pct / 100.0
                    values = {code: target_value * cycle_weights[code] for code in codes}
                    cash = capital - target_value
                    reasons.append("cycle_basket_refresh")
                else:
                    if allocation_policy.refresh_every_review:
                        target_pct, reasons = allocation_target(
                            detail,
                            allocation_policy,
                            spec.review_interval_months,
                            market_state,
                        )
                        reasons.append("scheduled_scorecard_refresh")
                    else:
                        target_pct = current_equity_pct
                        if allocation_midpoint and float(detail["rule_target_equity_pct"]) < target_pct:
                            target_pct = float(detail["rule_target_equity_pct"])
                            reasons.append("cycle_midpoint_scorecard_risk_reduce")
                    target_pct, overlay_reasons = apply_cycle_overlay(
                        target_pct,
                        detail,
                        trailing_6m_return,
                        DEFAULT_OVERLAY,
                        cycle_entry=False,
                        cycle_midpoint=allocation_midpoint,
                    )
                    reasons.extend(overlay_reasons)
                    known = detail["known_inputs"]
                    can_recover = (
                        not allocation_policy.refresh_every_review
                        and
                        target_pct < cycle_rule_target
                        and previous_window_return is not None
                        and previous_window_return > DEFAULT_OVERLAY.recover_prev_quarter_return_gt
                        and (known.get("pmi_mfg_3m_avg") or 0.0) >= DEFAULT_OVERLAY.recover_pmi_3m_gte
                        and not ((known.get("ppi_yoy") or 0.0) < DEFAULT_OVERLAY.weak_repair_ppi_lt)
                    )
                    if can_recover:
                        target_pct = cycle_rule_target
                        reasons.append("recover_after_positive_review_window")
                        target_pct, cap_reasons = apply_current_risk_caps(
                            target_pct,
                            detail,
                            DEFAULT_OVERLAY,
                        )
                        reasons.extend(cap_reasons)
                    codes_changed = codes != current_codes
                    if codes_changed and not selector_refresh_every_review:
                        raise AssertionError("CSI basket changed inside a schedule cycle")
                    scheduled_selector_rebalance = selector_refresh_every_review
                    if (
                        target_pct != current_equity_pct
                        or codes_changed
                        or scheduled_selector_rebalance
                    ):
                        equity_value = sum(values.values())
                        target_value = capital * target_pct / 100.0
                        if codes_changed or scheduled_selector_rebalance:
                            values = {code: target_value * cycle_weights[code] for code in codes}
                            current_codes = codes
                            reasons.append("scheduled_selector_refresh")
                        elif equity_value > 0:
                            scale = target_value / equity_value
                            values = {code: value * scale for code, value in values.items()}
                        else:
                            values = {code: target_value * cycle_weights[code] for code in codes}
                        cash = capital - target_value
                        current_equity_pct = target_pct

                capital_before = capital
                start_equity_value = sum(values.values())
                start_equity_pct = start_equity_value / capital_before * 100.0 if capital_before else 0.0
                start_holding_weights = {
                    code: value / start_equity_value if start_equity_value else cycle_weights.get(code, 0.0)
                    for code, value in values.items()
                }
                window_returns = []
                for code in list(values):
                    ret = boundary_return(cur, code, start_exec, end_exec)
                    values[code] *= 1.0 + ret
                    window_returns.append(ret)
                holding_days = max((end_exec - start_exec).days, 0)
                cash *= 1.0 + CASH_ANNUAL_RATE * holding_days / 365.25
                capital = sum(values.values()) + cash
                portfolio_return = capital / capital_before - 1.0 if capital_before else 0.0
                peak = max(peak, capital)
                curve.append(capital)
                previous_window_return = (
                    sum(window_returns) / len(window_returns) if window_returns else 0.0
                )
                if online_model is not None and online_candidates:
                    online_model.queue_observation(
                        end_exec,
                        online_candidates,
                        {
                            str(candidate["ts_code"]): boundary_return(
                                cur,
                                str(candidate["ts_code"]),
                                start_exec,
                                end_exec,
                            )
                            for candidate in online_candidates
                        },
                    )
                if include_rows:
                    rows.append(
                        {
                            "schedule": spec.name,
                            "cycle_months": spec.cycle_months,
                            "review_interval_months": spec.review_interval_months,
                            "cycle_index": window.cycle_index,
                            "review_index": window.review_index,
                            "cycle_entry": window.cycle_entry,
                            "cycle_midpoint": window.cycle_midpoint,
                            "allocation_year": start_exec.year,
                            "allocation_entry": allocation_entry,
                            "allocation_midpoint": allocation_midpoint,
                            "phase_month_offset": phase_month_offset,
                            "execution_lag_days": execution_lag_days,
                            "start_snapshot_date": window.start_snapshot.isoformat(),
                            "end_snapshot_date": window.end_snapshot.isoformat(),
                            "start_exec": start_exec.isoformat(),
                            "end_exec": end_exec.isoformat(),
                            "selection_key": selection_key,
                            "basket_policy": (
                                f"snapshot_selector:{selector_policy.name}"
                                if selector_policy is not None
                                else "frozen_saved_selection_per_12m_cycle"
                            ),
                            "holding_codes": codes,
                            "holding_weights": start_holding_weights,
                            "selector_target_weights": dict(cycle_weights),
                            "score": detail["score"],
                            "equity_pct": current_equity_pct,
                            "start_equity_pct": start_equity_pct,
                            "mean_equity_return": previous_window_return,
                            "portfolio_return": portfolio_return,
                            "trailing_6m_return": trailing_6m_return,
                            "portfolio_drawdown": capital / peak - 1.0,
                            "capital": capital,
                            "rebalance_reasons": reasons,
                            "known_inputs": detail["known_inputs"],
                            "market_state": market_state,
                            "feature_inputs": detail["feature_inputs"],
                            "top_score_items": detail["top_score_items"],
                            "score_items": detail["score_items"],
                        }
                    )
    finally:
        conn.close()

    sample_start = windows[0].start_snapshot
    sample_end = windows[-1].end_snapshot
    return summarize(
        f"{spec.name}_phase{phase_month_offset}_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        sample_cycles,
    ) | {
        "schedule": asdict(spec),
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "sample_cycles": sample_cycles,
        "sample_shift_cycles": sample_shift_cycles,
        "common_completion_phase_offset": common_completion_phase_offset,
        "common_completion_lag_days": common_completion_lag_days,
        "requested_schedule_anchor": requested_anchor.isoformat(),
        "sample_start": sample_start.isoformat(),
        "sample_end": sample_end.isoformat(),
        "data_complete": True,
        "basket_policy": (
            f"snapshot_selector:{selector_policy.name}"
            if selector_policy is not None
            else "frozen_saved_selection_per_12m_cycle"
        ),
        "allocation_policy": asdict(allocation_policy),
        "feature_policy": asdict(feature_policy),
        "selector_policy": asdict(selector_policy) if selector_policy is not None else None,
    }


def apply_monthly_pressure_caps(
    target_pct: float,
    detail: dict[str, Any],
) -> tuple[float, list[str]]:
    target_pct, reasons = apply_current_risk_caps(target_pct, detail, DEFAULT_OVERLAY)
    cs300_6m = detail["known_inputs"].get("cs300_6m_return") or 0.0
    if target_pct >= 80.0 and cs300_6m > MONTHLY_PRESSURE_EXTREME_MOMENTUM_RETURN_GT:
        target_pct = min(target_pct, MONTHLY_PRESSURE_EXTREME_MOMENTUM_CAP_PCT)
        reasons.append("monthly_extreme_momentum_cap")
    return target_pct, reasons


def run_monthly_pressure(
    execution_lag_days: int,
    start_year: int = START_YEAR,
    end_year: int = END_YEAR,
    include_rows: bool = False,
) -> dict[str, Any]:
    years = end_year - start_year + 1
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            values: dict[str, float] = {}
            cash = capital
            current_equity_pct = 0.0
            for year in range(start_year, end_year + 1):
                codes = holdings.get(year, []) if year >= 2014 else [CS300_CODE]
                for month in range(1, 13):
                    start_boundary = previous_month_end(year, month)
                    end_boundary = month_end(year, month)
                    start_exec = shifted_boundary(cur, start_boundary, execution_lag_days)
                    end_exec = shifted_boundary(cur, end_boundary, execution_lag_days)
                    quarter = quarter_for_month(month)
                    detail = scorecard_detail(conn, year, start_boundary, DEFAULT_RULE)
                    reasons: list[str] = []
                    h1_return = 0.0
                    if month >= 7:
                        h1_return = sum(
                            boundary_return(cur, code, date(year - 1, 12, 31), date(year, 6, 30))
                            for code in codes
                        ) / len(codes)

                    if month == 1:
                        target_pct = float(detail["rule_target_equity_pct"])
                        target_pct, reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return,
                            DEFAULT_OVERLAY,
                            annual_entry=True,
                        )
                        current_equity_pct = target_pct
                        cash = capital * (1.0 - current_equity_pct / 100.0)
                        values = {
                            code: capital * current_equity_pct / 100.0 / len(codes)
                            for code in codes
                        }
                    else:
                        target_pct = current_equity_pct
                        target_pct, overlay_reasons = apply_quarterly_overlay(
                            target_pct,
                            detail,
                            quarter,
                            h1_return,
                            DEFAULT_OVERLAY,
                            annual_entry=False,
                        )
                        reasons.extend(overlay_reasons)
                        target_pct, pressure_reasons = apply_monthly_pressure_caps(target_pct, detail)
                        reasons.extend(pressure_reasons)
                        if target_pct != current_equity_pct:
                            equity_value = sum(values.values())
                            target_value = capital * target_pct / 100.0
                            if equity_value > 0:
                                scale = target_value / equity_value
                                values = {code: value * scale for code, value in values.items()}
                            else:
                                values = {code: target_value / len(codes) for code in codes}
                            cash = capital - target_value
                            current_equity_pct = target_pct

                    month_returns = []
                    for code in list(values):
                        ret = boundary_return(cur, code, start_exec, end_exec)
                        values[code] *= 1.0 + ret
                        month_returns.append(ret)
                    holding_days = max((end_exec - start_exec).days, 0)
                    cash *= 1.0 + CASH_ANNUAL_RATE * holding_days / 365.25
                    capital = sum(values.values()) + cash
                    peak = max(peak, capital)
                    curve.append(capital)
                    mean_return = sum(month_returns) / len(month_returns) if month_returns else 0.0
                    if include_rows:
                        rows.append(
                            {
                                "year": year,
                                "month": month,
                                "period": f"{year}-{month:02d}",
                                "execution_lag_days": execution_lag_days,
                                "start_exec": start_exec.isoformat(),
                                "end_exec": end_exec.isoformat(),
                                "snapshot_date": start_boundary.isoformat(),
                                "score": detail["score"],
                                "equity_pct": current_equity_pct,
                                "mean_equity_return": mean_return,
                                "portfolio_drawdown": capital / peak - 1.0,
                                "capital": capital,
                                "rebalance_reasons": reasons,
                            }
                        )
    finally:
        conn.close()
    return summarize(
        f"monthly_pressure_lag{execution_lag_days}",
        capital,
        curve,
        rows,
        years,
    ) | {
        "frequency": "monthly_pressure",
        "execution_lag_days": execution_lag_days,
        "start_year": start_year,
        "end_year": end_year,
    }


def rolling_windows(window_years: int) -> list[dict[str, Any]]:
    out = []
    for start_year in range(START_YEAR, END_YEAR - window_years + 2):
        end_year = start_year + window_years - 1
        out.append(
            run_phase_schedule(
                SCHEDULE_12M_3M,
                0,
                0,
                start_year,
                end_year,
                include_rows=False,
            )
        )
    return out


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": median(item["final_capital_wan"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
        "median_annualized_return": median(item["annualized_return"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": median(item["max_drawdown"] for item in items),
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    base = payload["production_reference"]
    drift = payload["execution_drift"]
    phase_schedule_matrix = payload["phase_schedule_matrix"]
    folds_5y = payload["rolling_windows"]["5y"]
    folds_10y = payload["rolling_windows"]["10y"]
    checks = {
        "base_target_met": base["target_met"],
        "base_drawdown_under_10pct": base["max_drawdown"] >= TARGET_MDD,
        "execution_drift_all_target_met": all(item["target_met"] for item in drift),
        "phase_schedule_matrix_all_target_met": all(item["target_met"] for item in phase_schedule_matrix),
        "rolling_5y_all_positive_annualized": all(item["annualized_return"] > 0 for item in folds_5y),
        "rolling_10y_all_positive_annualized": all(item["annualized_return"] > 0 for item in folds_10y),
        "rolling_5y_worst_drawdown_under_10pct": min(item["max_drawdown"] for item in folds_5y) >= TARGET_MDD,
        "rolling_10y_worst_drawdown_under_10pct": min(item["max_drawdown"] for item in folds_10y) >= TARGET_MDD,
    }
    return {
        "checks": checks,
        "stable": all(checks.values()),
    }


def main() -> int:
    production_reference = run_variant("quarterly", 0, include_rows=True)
    drift = [
        run_phase_schedule(SCHEDULE_12M_3M, 0, lag, include_rows=False)
        for lag in EXECUTION_LAGS
    ]
    phase_schedule_matrix = [
        run_phase_schedule(spec, phase, lag, include_rows=False)
        for spec in FORMAL_SCHEDULES
        for phase in MONTH_DRIFT_PHASES
        for lag in MONTH_DRIFT_EXECUTION_LAGS
    ]
    legacy_timing_diagnostics = {
        "label_coupled_12m_review": [
            run_legacy_label_coupled_12m_review_drift(phase, lag, include_rows=False)
            for phase in LEGACY_DIAGNOSTIC_PHASES
            for lag in LEGACY_DIAGNOSTIC_LAGS
        ],
        "label_coupled_3m_review": [
            run_legacy_label_coupled_3m_review_drift(phase, lag, include_rows=False)
            for phase in LEGACY_DIAGNOSTIC_PHASES
            for lag in LEGACY_DIAGNOSTIC_LAGS
        ],
    }
    folds_5y = rolling_windows(5)
    folds_10y = rolling_windows(10)
    summaries_by_schedule = {
        spec.name: matrix_summary(
            [item for item in phase_schedule_matrix if item["schedule"]["name"] == spec.name]
        )
        for spec in FORMAL_SCHEDULES
    }
    payload = {
        "objective": "Validate scorecard + CSI robustness on calendar-neutral phase schedules.",
        "no_lookahead_rule": (
            "Each schedule is defined only by cycle_months, review_interval_months, phase_month_offset, "
            "and execution_lag_days. Signals use the start snapshot and execute no earlier than that snapshot. "
            "All windows are contiguous and the final execution boundary must exist in local market data."
        ),
        "schedule_semantics": (
            "The formal engine contains no annual, quarterly, Q1, Q2, Q3, or Q4 labels. "
            "Strategy logic receives only cycle_entry and cycle_midpoint relative positions."
        ),
        "overfit_guardrail": (
            "This script does not optimize thresholds. It holds the strategy adapter fixed and measures "
            "predeclared schedule perturbations. Legacy label-coupled results are diagnostic-only."
        ),
        "rule": asdict(DEFAULT_RULE),
        "overlay": asdict(DEFAULT_OVERLAY),
        "formal_schedules": [asdict(spec) for spec in FORMAL_SCHEDULES],
        "production_reference": production_reference,
        "execution_drift": drift,
        "phase_schedule_matrix": phase_schedule_matrix,
        "legacy_timing_diagnostics": legacy_timing_diagnostics,
        "rolling_windows": {
            "5y": folds_5y,
            "10y": folds_10y,
            "summary_5y": matrix_summary(folds_5y),
            "summary_10y": matrix_summary(folds_10y),
        },
        "summaries": {
            "execution_drift": matrix_summary(drift),
            "phase_schedule_matrix": matrix_summary(phase_schedule_matrix),
            "by_schedule": summaries_by_schedule,
        },
    }
    payload["validation"] = validate(payload)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Scorecard + CSI generalization validation")
    print(
        f"  production_reference target_met={production_reference['target_met']} "
        f"final={production_reference['final_capital_wan']:.1f}万 "
        f"mdd={production_reference['max_drawdown'] * 100:.1f}%"
    )
    print(
        "  drift "
        f"strict_target_pass={payload['summaries']['execution_drift']['pass_count']}/"
        f"{payload['summaries']['execution_drift']['count']} "
        f"min_ann={payload['summaries']['execution_drift']['min_annualized_return'] * 100:.1f}% "
        f"worst_mdd={payload['summaries']['execution_drift']['worst_max_drawdown'] * 100:.1f}%"
    )
    print(
        "  rolling "
        f"5y_min_ann={payload['rolling_windows']['summary_5y']['min_annualized_return'] * 100:.1f}% "
        f"10y_min_ann={payload['rolling_windows']['summary_10y']['min_annualized_return'] * 100:.1f}%"
    )
    for spec in FORMAL_SCHEDULES:
        summary = payload["summaries"]["by_schedule"][spec.name]
        print(
            f"  {spec.name} strict_target_pass={summary['pass_count']}/{summary['count']} "
            f"min_ann={summary['min_annualized_return'] * 100:.1f}% "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:.1f}%"
        )
    print(f"  stable={payload['validation']['stable']}")
    print(f"Wrote {OUT_JSON}")
    return 0 if payload["validation"]["stable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

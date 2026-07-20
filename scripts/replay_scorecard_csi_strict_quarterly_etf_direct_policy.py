#!/usr/bin/env python3
"""Replay strict quarterly ETF decisions with a different direct ETF policy.

The input must be a strict quarterly ``--include-decision-rows`` report.  The
CSI selector path, market state, exposure, and defensive sleeve are treated as
audited inputs.  Only the equity ETF mapping/direct-policy layer and daily
mark-to-market path are recomputed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
import statistics
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_defensive_etf import (  # noqa: E402
    select_defensive_weights,
)
from backtest.domestic_equity_etf import (  # noqa: E402
    DIRECT_ETF_POLICIES,
    direct_blend_share,
    map_indices_to_etfs,
    select_direct_equity_etfs,
    structural_subtheme_groups_from_metas,
    structural_theme_groups_from_metas,
    wide_structural_opportunity_active,
)
from backtest.strict_passive_etf_objective import validate_case_matrix  # noqa: E402
from scripts.backtest_calendar_neutral_csi_tipp import CodeReturnCache  # noqa: E402
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (  # noqa: E402
    DEFENSIVE_POLICIES,
    TARGET_MDD,
    cash_return,
    combined_target_weights,
    mark_frozen_positions,
    rebalance_frozen_positions,
)
from scripts.strict_quarterly_data_cache import load_strict_quarterly_market_data  # noqa: E402


_WORKER_CONTEXT: dict[str, Any] = {}


def parse_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def named(items, name: str):
    for item in items:
        if item.name == name:
            return item
    raise ValueError(f"unknown policy: {name}")


def report_result(payload: dict[str, Any], result_index: int) -> dict[str, Any]:
    if "results" in payload:
        return payload["results"][result_index]
    if "cases" in payload:
        return payload
    raise ValueError("report must contain either results[] or top-level cases")


def trade_days_for_rows(
    index_series: dict[str, list[tuple[date, float]]],
    rows: list[dict[str, Any]],
    sample_end: date,
) -> list[list[date]]:
    benchmark_days = [day for day, _value in index_series[CS300_CODE]]
    windows = []
    for idx, row in enumerate(rows):
        start = parse_date(row["decision_date"])
        end = parse_date(rows[idx + 1]["decision_date"]) if idx + 1 < len(rows) else sample_end
        days = [day for day in benchmark_days if start <= day <= end]
        if not days or days[0] != start:
            days = [start, *days]
        if days[-1] != end:
            days.append(end)
        windows.append(days)
    return windows


def replay_equity_weights(
    row: dict[str, Any],
    equity_metas,
    equity_series,
    index_series,
    direct_policy,
    groups_by_code,
    subthemes_by_code,
) -> dict[str, float]:
    snapshot = parse_date(row.get("rebalance_anchor", row["decision_date"]))
    index_weights = {
        code: float(weight)
        for code, weight in (row.get("index_target_weights") or {}).items()
        if float(weight) > 0
    }
    etf_weights = map_indices_to_etfs(
        index_weights,
        snapshot,
        equity_metas,
        etf_series=equity_series,
        allow_early_broad_proxy=True,
        allow_correlation_proxy=False,
        index_series=index_series,
    )
    direct_weights = select_direct_equity_etfs(
        equity_metas,
        equity_series,
        snapshot,
        direct_policy,
        benchmark_series=index_series.get("000300.SH"),
        market_state=dict(row.get("market_state") or {}),
    )
    if direct_weights:
        direct_share = direct_blend_share(
            direct_policy,
            dict(row.get("market_state") or {}),
            snapshot=snapshot,
            groups_by_code=groups_by_code,
            subthemes_by_code=subthemes_by_code,
        )
        if direct_share >= 1.0 - 1e-12:
            etf_weights = direct_weights
        else:
            blended = {
                code: (1.0 - direct_share) * weight
                for code, weight in etf_weights.items()
            }
            for code, weight in direct_weights.items():
                blended[code] = blended.get(code, 0.0) + direct_share * weight
            total = sum(blended.values())
            etf_weights = {code: weight / total for code, weight in blended.items()} if total > 0 else {}
    return etf_weights


def equity_weight_cache_key(row: dict[str, Any]) -> tuple[Any, ...]:
    snapshot = str(row.get("rebalance_anchor", row["decision_date"]))
    index_weights = tuple(
        sorted(
            (
                str(code),
                round(float(weight), 12),
            )
            for code, weight in (row.get("index_target_weights") or {}).items()
            if float(weight) > 0
        )
    )
    market_state = json.dumps(
        row.get("market_state") or {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return (snapshot, index_weights, market_state)


PARTICIPATION_REENTRY_BLOCK_FLAGS = {
    "daily_margin_rally_flag",
    "high_level_distribution_flag",
    "leverage_macro_divergence_flag",
    "leveraged_rally_exhaustion_flag",
    "weak_credit_leveraged_rebound_flag",
}


def broad_participation_reentry_active(row: dict[str, Any]) -> bool:
    market_state = row.get("market_state") or {}
    required = (
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
        "basket_vol_3m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    active_flags = set(row.get("active_risk_flags") or [])
    if active_flags.intersection(PARTICIPATION_REENTRY_BLOCK_FLAGS):
        return False
    return (
        float(market_state["basket_return_3m_max"]) >= 0.18
        and float(market_state["breadth_return_3m_positive"]) >= 0.80
        and float(market_state["basket_drawdown_6m"]) > -0.06
        and float(market_state["basket_vol_3m"]) <= 0.32
    )


def macro_market_damage_cap_active(row: dict[str, Any]) -> bool:
    """Replay-only test cap for 2018-style macro and market damage."""

    market_state = row.get("market_state") or {}

    def number(name: str) -> float | None:
        raw = market_state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    cs300_return_3m = number("cs300_return_3m")
    pboc_tone = number("pboc_outlook_net_tone")
    m1_m2_scissors_change = number("domestic_m1_m2_scissors_change_3m")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    cs300_return_6m = number("cs300_return_6m")
    basket_return_3m_max = number("basket_return_3m_max")
    breadth_return_3m_positive = number("breadth_return_3m_positive")
    active_flags = set(row.get("active_risk_flags") or [])
    pre_damage = bool(
        "low_vol_mature_trend_flag" in active_flags
        and cs300_return_3m is not None
        and cs300_return_3m >= 0.03
        and pboc_tone is not None
        and pboc_tone <= -5.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change <= -1.0
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.06
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.20
    )
    initial_damage = bool(
        cs300_return_3m is not None
        and cs300_return_3m <= -0.10
        and pboc_tone is not None
        and pboc_tone <= -5.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change <= -2.0
        and basket_drawdown_6m is not None
        and basket_drawdown_6m <= -0.08
    )
    continuation_damage = bool(
        cs300_return_6m is not None
        and cs300_return_6m <= -0.15
        and pboc_tone is not None
        and pboc_tone <= -5.0
        and basket_drawdown_6m is not None
        and basket_drawdown_6m <= -0.15
        and basket_return_3m_max is not None
        and basket_return_3m_max <= 0.0
        and breadth_return_3m_positive is not None
        and breadth_return_3m_positive <= 0.10
    )
    return bool(
        (pre_damage or initial_damage or continuation_damage)
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.35
    )


def option_panic_after_rally_cap_active(row: dict[str, Any]) -> bool:
    """Replay-only cap for post-rally option panic when gold defense leads."""

    market_state = row.get("market_state") or {}

    def number(name: str) -> float | None:
        raw = market_state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    active_flags = set(row.get("active_risk_flags") or [])
    cs300_return_3m = number("cs300_return_3m")
    cs300_return_6m = number("cs300_return_6m")
    basket_return_1m = number("basket_return_1m")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    pboc_tone = number("pboc_outlook_net_tone")
    m1_m2_scissors_change = number("domestic_m1_m2_scissors_change_3m")
    return bool(
        "option_panic_after_rally_flag" in active_flags
        and cs300_return_3m is not None
        and -0.03 <= cs300_return_3m <= 0.05
        and cs300_return_6m is not None
        and cs300_return_6m >= 0.10
        and basket_return_1m is not None
        and basket_return_1m >= 0.04
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.10
        and basket_vol_3m is not None
        and 0.25 <= basket_vol_3m <= 0.38
        and pboc_tone is not None
        and pboc_tone >= 10.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change >= 0.0
    )


def replay_case(
    case: dict[str, Any],
    equity_metas,
    equity_series,
    index_series,
    defensive_metas,
    defensive_series,
    defensive_policy,
    direct_policy,
    groups_by_code,
    subthemes_by_code,
    structural_reentry_floor: float = 0.0,
    participation_reentry_floor: float = 0.0,
    macro_market_damage_cap: float | None = None,
    option_panic_after_rally_cap: float | None = None,
    use_source_target_weights: bool = False,
    include_decision_rows: bool = True,
    equity_return_cache: CodeReturnCache | None = None,
    defensive_return_cache: CodeReturnCache | None = None,
    equity_weight_cache: dict[tuple[Any, ...], dict[str, float]] | None = None,
    defensive_weight_cache: dict[date, dict[str, float]] | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in case["decision_rows"]]
    sample_end = parse_date(case["sample_end"])
    daily_windows = trade_days_for_rows(index_series, rows, sample_end)
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    current_positions: dict[str, float] = {"CASH": capital}
    decision_rows: list[dict[str, Any]] = []
    previous_window_start_capital: float | None = None
    transaction_cost_total = 0.0
    exposures = []
    macro_market_damage_cap_count = 0
    option_panic_after_rally_cap_count = 0
    worst_drawdown = 0.0
    worst_drawdown_state: dict[str, Any] = {}
    equity_return_cache = equity_return_cache or CodeReturnCache(equity_series)
    defensive_return_cache = defensive_return_cache or CodeReturnCache(defensive_series)

    for row, days in zip(rows, daily_windows):
        if decision_rows and previous_window_start_capital is not None:
            decision_rows[-1]["realized_portfolio_return"] = (
                capital / previous_window_start_capital - 1.0
            )
        if use_source_target_weights:
            risk_weights = {
                code: float(weight)
                for code, weight in (row.get("equity_etf_weights") or {}).items()
                if float(weight) > 0
            }
        else:
            risk_cache_key = equity_weight_cache_key(row)
            cached_risk_weights = (
                equity_weight_cache.get(risk_cache_key)
                if equity_weight_cache is not None
                else None
            )
            if cached_risk_weights is None:
                risk_weights = replay_equity_weights(
                    row,
                    equity_metas,
                    equity_series,
                    index_series,
                    direct_policy,
                    groups_by_code,
                    subthemes_by_code,
                )
                if equity_weight_cache is not None:
                    equity_weight_cache[risk_cache_key] = dict(risk_weights)
            else:
                risk_weights = dict(cached_risk_weights)
        source_exposure = float(row.get("exposure") or 0.0)
        reentry_active = bool(
            structural_reentry_floor > 0.0
            and wide_structural_opportunity_active(row.get("market_state") or {})
            and not row.get("bear_state")
            and not (row.get("active_risk_flags") or [])
        )
        exposure = (
            max(source_exposure, structural_reentry_floor)
            if reentry_active
            else source_exposure
        )
        participation_reentry_active = bool(
            participation_reentry_floor > 0.0
            and broad_participation_reentry_active(row)
            and not row.get("bear_state")
        )
        if participation_reentry_active:
            exposure = max(exposure, participation_reentry_floor)
        macro_market_damage_active = bool(
            macro_market_damage_cap is not None
            and macro_market_damage_cap_active(row)
        )
        if macro_market_damage_active:
            capped_exposure = min(exposure, float(macro_market_damage_cap))
            if capped_exposure < exposure - 1e-12:
                macro_market_damage_cap_count += 1
            exposure = capped_exposure
        option_panic_cap_active = bool(
            option_panic_after_rally_cap is not None
            and option_panic_after_rally_cap_active(row)
        )
        if option_panic_cap_active:
            capped_exposure = min(exposure, float(option_panic_after_rally_cap))
            if capped_exposure < exposure - 1e-12:
                option_panic_after_rally_cap_count += 1
            exposure = capped_exposure
        if use_source_target_weights:
            target_weights = {
                code: float(weight)
                for code, weight in (row.get("target_weights") or {}).items()
                if float(weight) > 0
            }
        else:
            defensive_weights = (
                defensive_weight_cache.get(days[0])
                if defensive_weight_cache is not None
                else None
            )
            if defensive_weights is None:
                defensive_weights = select_defensive_weights(
                    defensive_metas,
                    defensive_series,
                    days[0],
                    defensive_policy,
                )
                if defensive_weight_cache is not None:
                    defensive_weight_cache[days[0]] = dict(defensive_weights)
            else:
                defensive_weights = dict(defensive_weights)
            target_weights = combined_target_weights(
                risk_weights,
                defensive_weights,
                exposure,
            )
        decision_capital = capital
        current_positions, transaction_cost, turnover = rebalance_frozen_positions(
            current_positions,
            target_weights,
            capital,
        )
        transaction_cost_total += transaction_cost
        capital = sum(current_positions.values())
        risk_codes = set(risk_weights)
        counterfactual_risk_positions = (
            {code: weight for code, weight in risk_weights.items() if weight > 0}
            if risk_weights
            else {}
        )
        risky_window_factor = 1.0
        risky_window_peak = 1.0
        risky_window_max_drawdown = 0.0
        decision_record = dict(row)
        decision_record.update(
            {
                "source_exposure": source_exposure,
                "exposure": exposure,
                "structural_reentry_floor_active": reentry_active,
                "participation_reentry_floor_active": participation_reentry_active,
                "macro_market_damage_cap_active": macro_market_damage_active,
                "macro_market_damage_cap": macro_market_damage_cap,
                "option_panic_after_rally_cap_active": option_panic_cap_active,
                "option_panic_after_rally_cap": option_panic_after_rally_cap,
                "target_weights": target_weights,
                "equity_etf_weights": risk_weights,
                "capital_at_decision": decision_capital,
                "capital_after_transaction_cost": capital,
                "transaction_cost": transaction_cost,
                "rebalance_turnover": turnover,
                "min_capital_since_decision": capital,
                "worst_global_drawdown_since_decision": capital / peak - 1.0,
            }
        )
        decision_rows.append(decision_record)
        previous_window_start_capital = decision_capital
        curve.append(capital)

        for previous, current in zip(days, days[1:]):
            daily_returns = {
                code: (
                    cash_return(previous, current)
                    if code == "CASH"
                    else (
                        equity_return_cache if code in risk_codes else defensive_return_cache
                    )(code, previous, current)
                )
                for code in current_positions
            }
            current_positions = mark_frozen_positions(current_positions, daily_returns)
            capital = max(1.0, sum(current_positions.values()))
            if counterfactual_risk_positions:
                counterfactual_risk_positions = mark_frozen_positions(
                    counterfactual_risk_positions,
                    {
                        code: equity_return_cache(code, previous, current)
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
            curve.append(capital)
            risky_value = sum(current_positions.get(code, 0.0) for code in risk_codes)
            exposures.append(risky_value / capital if capital > 0 else 0.0)
            drawdown = capital / peak - 1.0
            decision_rows[-1]["min_capital_since_decision"] = min(
                float(decision_rows[-1]["min_capital_since_decision"]),
                capital,
            )
            decision_rows[-1]["worst_global_drawdown_since_decision"] = min(
                float(decision_rows[-1]["worst_global_drawdown_since_decision"]),
                drawdown,
            )
            if drawdown < worst_drawdown:
                worst_drawdown = drawdown
                worst_drawdown_state = {
                    "date": current.isoformat(),
                    "decision_date": decision_rows[-1]["decision_date"],
                    "exposure": exposure,
                    "target_weights": target_weights,
                }
        decision_rows[-1]["realized_risk_return"] = risky_window_factor - 1.0
        decision_rows[-1]["realized_risk_max_drawdown"] = risky_window_max_drawdown

    if decision_rows and previous_window_start_capital is not None:
        decision_rows[-1]["realized_portfolio_return"] = (
            capital / previous_window_start_capital - 1.0
        )
    mdd = max_drawdown(curve)
    output = {
        "phase_month_offset": case["phase_month_offset"],
        "execution_lag_days": case["execution_lag_days"],
        "sample_start": case["sample_start"],
        "sample_end": case["sample_end"],
        "sample_shift_cycles": case.get("sample_shift_cycles"),
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / 20.0) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "average_exposure": statistics.mean(exposures) if exposures else 0.0,
        "quarterly_weight_validation_passed": bool(case.get("quarterly_weight_validation_passed", True)),
        "quarterly_weight_violations": list(case.get("quarterly_weight_violations") or []),
        "online_guard_count": int(case.get("online_guard_count") or 0),
        "direction_decision_count": int(case.get("direction_decision_count") or 0),
        "direction_risk_gate_rejection_count": int(case.get("direction_risk_gate_rejection_count") or 0),
        "selector_dispersion_recovery_count": int(case.get("selector_dispersion_recovery_count") or 0),
        "recovery_count": int(case.get("recovery_count") or 0),
        "quality_high_count": int(case.get("quality_high_count") or 0),
        "quality_low_count": int(case.get("quality_low_count") or 0),
        "macro_market_damage_cap_count": macro_market_damage_cap_count,
        "option_panic_after_rally_cap_count": option_panic_after_rally_cap_count,
        "transaction_cost_total": transaction_cost_total,
        "worst_drawdown_state": worst_drawdown_state,
    }
    if include_decision_rows:
        output["decision_rows"] = decision_rows
    return output


def init_worker(context: dict[str, Any]) -> None:
    _WORKER_CONTEXT.clear()
    _WORKER_CONTEXT.update(context)
    _WORKER_CONTEXT["equity_return_cache"] = CodeReturnCache(context["equity_series"])
    _WORKER_CONTEXT["defensive_return_cache"] = CodeReturnCache(context["defensive_series"])
    _WORKER_CONTEXT["equity_weight_cache"] = {}
    _WORKER_CONTEXT["defensive_weight_cache"] = {}


def replay_case_worker(payload: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    idx, case = payload
    replayed = replay_case(
        case,
        _WORKER_CONTEXT["equity_metas"],
        _WORKER_CONTEXT["equity_series"],
        _WORKER_CONTEXT["index_series"],
        _WORKER_CONTEXT["defensive_metas"],
        _WORKER_CONTEXT["defensive_series"],
        _WORKER_CONTEXT["defensive_policy"],
        _WORKER_CONTEXT["direct_policy"],
        _WORKER_CONTEXT["groups_by_code"],
        _WORKER_CONTEXT["subthemes_by_code"],
        _WORKER_CONTEXT["structural_reentry_floor"],
        _WORKER_CONTEXT["participation_reentry_floor"],
        _WORKER_CONTEXT["macro_market_damage_cap"],
        _WORKER_CONTEXT["option_panic_after_rally_cap"],
        _WORKER_CONTEXT["use_source_target_weights"],
        _WORKER_CONTEXT["include_decision_rows"],
        _WORKER_CONTEXT.get("equity_return_cache"),
        _WORKER_CONTEXT.get("defensive_return_cache"),
        _WORKER_CONTEXT.get("equity_weight_cache"),
        _WORKER_CONTEXT.get("defensive_weight_cache"),
    )
    return idx, replayed


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    full_matrix = len(cases) == 48
    matrix = validate_case_matrix(cases) if full_matrix else None
    objective_met = (
        bool(matrix)
        and matrix["all_cases_pass"]
        and all(case["target_met"] for case in cases)
    )
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
        "median_quality_high_count": statistics.median(case["quality_high_count"] for case in cases),
        "median_quality_low_count": statistics.median(case["quality_low_count"] for case in cases),
        "median_macro_market_damage_cap_count": statistics.median(
            case.get("macro_market_damage_cap_count", 0) for case in cases
        ),
        "median_option_panic_after_rally_cap_count": statistics.median(
            case.get("option_panic_after_rally_cap_count", 0) for case in cases
        ),
        "case_matrix": matrix,
        "partial_matrix": not full_matrix,
        "objective_met": objective_met,
        "screen_passed": objective_met if full_matrix else all(case["target_met"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--direct-etf-policy", required=True)
    parser.add_argument(
        "--defensive-policy",
        help="Override the audited defensive ETF policy; defaults to the source report policy.",
    )
    parser.add_argument(
        "--structural-reentry-floor",
        type=float,
        default=0.0,
        help=(
            "Replay-only exposure floor for wide structural opportunity "
            "quarters with no active risk flags."
        ),
    )
    parser.add_argument(
        "--participation-reentry-floor",
        type=float,
        default=0.0,
        help=(
            "Replay-only exposure floor for broad ETF participation "
            "quarters after excluding distribution/leverage risk flags."
        ),
    )
    parser.add_argument(
        "--macro-market-damage-cap",
        type=float,
        help=(
            "Replay-only exposure cap when 2018-style CSI300 drawdown, "
            "negative PBoC tone, and weakening M1-M2 scissors are all present."
        ),
    )
    parser.add_argument(
        "--option-panic-after-rally-cap",
        type=float,
        help=(
            "Replay-only exposure cap for post-rally option-panic quarters "
            "with positive policy/liquidity support and elevated ETF volatility."
        ),
    )
    parser.add_argument(
        "--phase",
        type=int,
        action="append",
        help="Only replay the given month-drift phase; may be repeated.",
    )
    parser.add_argument(
        "--lag",
        type=int,
        action="append",
        help="Only replay the given execution lag; may be repeated.",
    )
    parser.add_argument(
        "--use-source-target-weights",
        action="store_true",
        help="Replay audited target weights exactly to validate path valuation.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Do not write replay decision_rows; use for fast first-pass screens.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first replay case that misses the hard gate.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Parallel replay workers for full case sweeps. Values above 1 are "
            "ignored when --fail-fast is set."
        ),
    )
    parser.add_argument(
        "--data-cache-dir",
        type=Path,
        default=Path("data/backtests/cache/strict_quarterly_market_data"),
        help="Local cache for raw market data loaded from MySQL.",
    )
    parser.add_argument(
        "--refresh-data-cache",
        action="store_true",
        help="Reload raw market data from MySQL and overwrite the local cache.",
    )
    parser.add_argument(
        "--no-data-cache",
        action="store_true",
        help="Disable the raw market-data cache for this run.",
    )
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    source_result = report_result(payload, args.result_index)
    source_cases = source_result["cases"]
    if args.phase:
        phase_set = set(args.phase)
        source_cases = [
            case
            for case in source_cases
            if int(case["phase_month_offset"]) in phase_set
        ]
    if args.lag:
        lag_set = set(args.lag)
        source_cases = [
            case
            for case in source_cases
            if int(case["execution_lag_days"]) in lag_set
        ]
    if not source_cases:
        raise ValueError("no source cases matched requested phase/lag filters")
    if not all(case.get("decision_rows") for case in source_cases):
        raise ValueError("source report must include decision_rows")
    direct_policy = named(DIRECT_ETF_POLICIES, args.direct_etf_policy)
    defensive_name = args.defensive_policy or source_result["defensive_policy"]["name"]
    defensive_policy = named(DEFENSIVE_POLICIES, defensive_name)

    market_data = load_strict_quarterly_market_data(
        ROOT,
        args.data_cache_dir,
        refresh=args.refresh_data_cache,
        use_cache=not args.no_data_cache,
    )
    index_series = market_data["index_series"]
    defensive_metas = market_data["defensive_metas"]
    defensive_series = market_data["defensive_series"]
    equity_metas = market_data["equity_metas"]
    equity_series = market_data["equity_series"]
    groups_by_code = structural_theme_groups_from_metas(equity_metas)
    subthemes_by_code = structural_subtheme_groups_from_metas(equity_metas)

    worker_count = max(1, int(args.workers or 1))
    if args.fail_fast and worker_count > 1:
        print("Ignoring --workers with --fail-fast to preserve early-stop semantics.")
        worker_count = 1
    worker_count = min(worker_count, len(source_cases), os.cpu_count() or worker_count)

    def print_case(replayed: dict[str, Any]) -> None:
        print(
            f"phase={replayed['phase_month_offset']:02d} "
            f"lag={replayed['execution_lag_days']} "
            f"pass={int(replayed['target_met'])} "
            f"final={replayed['final_capital_wan']:.1f}万 "
            f"mdd={replayed['max_drawdown'] * 100:.2f}%",
            flush=True,
        )

    cases: list[dict[str, Any]] = []
    if worker_count == 1:
        equity_return_cache = CodeReturnCache(equity_series)
        defensive_return_cache = CodeReturnCache(defensive_series)
        equity_weight_cache: dict[tuple[Any, ...], dict[str, float]] = {}
        defensive_weight_cache: dict[date, dict[str, float]] = {}
        for case in source_cases:
            replayed = replay_case(
                case,
                equity_metas,
                equity_series,
                index_series,
                defensive_metas,
                defensive_series,
                defensive_policy,
                direct_policy,
                groups_by_code,
                subthemes_by_code,
                args.structural_reentry_floor,
                args.participation_reentry_floor,
                args.macro_market_damage_cap,
                args.option_panic_after_rally_cap,
                args.use_source_target_weights,
                not args.summary_only,
                equity_return_cache,
                defensive_return_cache,
                equity_weight_cache,
                defensive_weight_cache,
            )
            cases.append(replayed)
            print_case(replayed)
            if args.fail_fast and not replayed["target_met"]:
                break
    else:
        context = {
            "equity_metas": equity_metas,
            "equity_series": equity_series,
            "index_series": index_series,
            "defensive_metas": defensive_metas,
            "defensive_series": defensive_series,
            "defensive_policy": defensive_policy,
            "direct_policy": direct_policy,
            "groups_by_code": groups_by_code,
            "subthemes_by_code": subthemes_by_code,
            "structural_reentry_floor": args.structural_reentry_floor,
            "participation_reentry_floor": args.participation_reentry_floor,
            "macro_market_damage_cap": args.macro_market_damage_cap,
            "option_panic_after_rally_cap": args.option_panic_after_rally_cap,
            "use_source_target_weights": args.use_source_target_weights,
            "include_decision_rows": not args.summary_only,
        }
        replayed_by_index: dict[int, dict[str, Any]] = {}
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=init_worker,
            initargs=(context,),
        ) as executor:
            futures = {
                executor.submit(replay_case_worker, (idx, case)): idx
                for idx, case in enumerate(source_cases)
            }
            for future in as_completed(futures):
                idx, replayed = future.result()
                replayed_by_index[idx] = replayed
                print_case(replayed)
        cases = [replayed_by_index[idx] for idx in range(len(source_cases))]
    summary = summarize(cases)
    result = {
        "selector_policy": source_result["selector_policy"],
        "direct_etf_policy": asdict(direct_policy),
        "rule": source_result["rule"],
        "defensive_policy": asdict(defensive_policy),
        "source_defensive_policy": source_result["defensive_policy"],
        "summary": summary,
        "cases": cases,
    }
    output = dict(payload)
    output["replay_source_report"] = str(report_path.relative_to(ROOT))
    output["replay_case_count"] = len(cases)
    output["replay_summary_only"] = bool(args.summary_only)
    output["replay_fail_fast"] = bool(args.fail_fast)
    output["replay_workers"] = worker_count
    output["results"] = [result]
    prefix = args.output_prefix if args.output_prefix.is_absolute() else ROOT / args.output_prefix
    out_path = Path(f"{prefix}_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"{direct_policy.name} replay pass={summary['pass_count']}/{summary['count']} "
        f"min={summary['min_final_capital_wan']:.1f}万 "
        f"mdd={summary['worst_max_drawdown']*100:.2f}%"
    )
    print(f"Wrote {out_path}")
    return 0 if summary["screen_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

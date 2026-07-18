#!/usr/bin/env python3
"""Annual-rebalance view of the validated domestic passive-ETF framework."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import MONTHLY_TREND6_RISK_TOP10, SNAPSHOT_CSI_SELECTOR
from backtest.domestic_defensive_etf import (
    DEFENSIVE_POLICIES,
    apply_portfolio_drawdown_guard,
    load_defensive_etf_universe,
    select_defensive_weights,
)
from backtest.domestic_equity_etf import load_equity_etf_return_universe, map_indices_to_etfs
from backtest.monthly_direction_model import MONTHLY_DIRECTION_POLICIES, attach_walkforward_predictions
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_monthly import (
    RULES,
    build_monthly_path,
    cash_return,
    enrich_monthly_paths,
    is_bear_state,
    load_price_series,
    load_selector_price_series,
    weighted_return,
)
from scripts.backtest_scorecard_csi_dynamic_defense import period_return, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL
from scripts.validate_scorecard_csi_generalization import SCHEDULE_12M_12M

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "calendar_neutral_csi_annual_report.json"
OUT_YEARLY = OUT_DIR / "calendar_neutral_csi_annual_yearly.csv"
OUT_HOLDINGS = OUT_DIR / "calendar_neutral_csi_annual_holdings.csv"
RULE_NAME = "monthly_f90_m10_s125_bc25"
DEFENSE_NAME = "bond_gold45_252d_gold42"
DIRECTION_NAME = "dxy_local_theme1c25_fund_dist_max100"


def named(items, name):
    return next(item for item in items if item.name == name)


def capped_exposure(month, capital, peak, price_series, rule, policy):
    floor = peak * rule.floor_pct
    cushion = max(0.0, capital - floor)
    protection_cap = min(rule.max_exposure, rule.multiplier * cushion / max(capital, 1.0))
    exposure = min(rule.max_exposure, month["base_weight"] * rule.base_scale, protection_cap)
    if is_bear_state(price_series, month["snapshot"], rule.trend_months):
        exposure = min(exposure, rule.bear_cap)
    model = month["direction_model"]
    predecision_drawdown = capital / peak - 1.0
    if (
        model["score"] is not None
        and model["score"] >= 0
        and model["vote_count"] >= policy.minimum_vote_count_for_boost
        and predecision_drawdown >= policy.boost_allowed_drawdown_gte
    ):
        exposure = min(rule.max_exposure, exposure * policy.nonnegative_exposure_multiplier)
    flags_and_caps = (
        ("cs300_overheat_flag", policy.overheat_exposure_cap),
        ("basket_overheat_flag", policy.overheat_exposure_cap),
        ("one_month_surge_flag", policy.overheat_exposure_cap),
        ("rebound_overheat_flag", policy.rebound_overheat_exposure_cap),
        ("high_level_distribution_flag", policy.rebound_overheat_exposure_cap),
        ("long_cycle_overheat_flag", policy.rebound_overheat_exposure_cap),
        ("bear_rebound_exhaustion_flag", policy.rebound_overheat_exposure_cap),
        ("short_cycle_overheat_flag", policy.short_cycle_exposure_cap),
        ("tightening_rebound_exhaustion_flag", policy.tightening_rebound_exposure_cap),
        ("refined_mature_reversal_flag", policy.mature_reversal_exposure_cap),
        ("domestic_liquidity_stress_flag", policy.liquidity_stress_exposure_cap),
        ("crisis_continuation_flag", policy.crisis_exposure_cap),
        ("financed_surge_reversal_flag", policy.financed_surge_exposure_cap),
        ("option_panic_after_rally_flag", policy.option_panic_exposure_cap),
        ("strong_rally_breadth_reversal_flag", policy.breadth_reversal_exposure_cap),
        ("leadership_collapse_tightening_flag", policy.leadership_collapse_exposure_cap),
        ("leverage_macro_divergence_flag", policy.leverage_macro_exposure_cap),
        ("fund_distribution_tight_flag", policy.fund_distribution_exposure_cap),
        ("fund_saturation_contraction_flag", policy.fund_saturation_exposure_cap),
        ("theme_divergence_3m_flag", policy.theme_divergence_3m_exposure_cap),
        ("theme_divergence_1m_crowded_flag", policy.theme_divergence_1m_crowded_exposure_cap),
        ("credit_contraction_tightening_flag", policy.credit_contraction_exposure_cap),
        ("macro_weak_rebound_flag", policy.macro_weak_rebound_exposure_cap),
        ("fund_moderate_distribution_flag", policy.fund_moderate_distribution_exposure_cap),
    )
    for flag, cap in flags_and_caps:
        if month["features"].get(flag):
            exposure = min(exposure, cap)
    if (
        model["score"] is not None
        and model["score"] <= policy.negative_score_lte
        and model["vote_count"] >= policy.minimum_vote_count_for_cap
    ):
        exposure = min(exposure, policy.negative_exposure_cap)
    return exposure


def main() -> int:
    rule = named(RULES, RULE_NAME)
    defense = named(DEFENSIVE_POLICIES, DEFENSE_NAME)
    direction = named(MONTHLY_DIRECTION_POLICIES, DIRECTION_NAME)
    conn = get_connection()
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        equity_metas, equity_series = load_equity_etf_return_universe(conn)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
    finally:
        conn.close()
    trade_dates = [day for day, _value in price_series[CS300_CODE]]
    monthly_path = build_monthly_path(SCHEDULE_12M_12M, 0, 3, trade_dates, MONTHLY_TREND6_RISK_TOP10)
    enrich_monthly_paths(
        [monthly_path], price_series, trade_dates, MONTHLY_TREND6_RISK_TOP10,
        monthly_selector_refresh=True, online_selector=False, direction_prehistory_months=0,
    )
    attach_walkforward_predictions(monthly_path["months"], direction)
    by_snapshot = {month["snapshot"]: month for month in monthly_path["months"]}

    capital = INITIAL_CAPITAL
    benchmark_capital = INITIAL_CAPITAL
    peak = capital
    global_mdd = 0.0
    yearly = []
    holdings = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for year in range(2006, 2026):
                snapshot = date(year - 1, 12, 31)
                month = by_snapshot[snapshot]
                live_indices = {
                    index_code
                    for index_code, metas in equity_metas.items()
                    if any(
                        meta.list_date <= snapshot and meta.first_trade_date <= snapshot
                        for meta in metas
                    )
                }
                selected = SNAPSHOT_CSI_SELECTOR.select(
                    cur,
                    snapshot,
                    MONTHLY_TREND6_RISK_TOP10,
                    eligible_codes=live_indices,
                )
                index_weights = {row["ts_code"]: float(row["weight"]) for row in selected}
                etf_weights = map_indices_to_etfs(
                    index_weights,
                    snapshot,
                    equity_metas,
                    etf_series=equity_series,
                )
                exposure = capped_exposure(month, capital, peak, price_series, rule, direction)
                pre_dd = capital / peak - 1.0
                defensive_weights = select_defensive_weights(defensive_metas, defensive_series, snapshot, defense)
                defensive_weights, _guard = apply_portfolio_drawdown_guard(
                    defensive_weights, defensive_metas, defense, pre_dd
                )
                start_exec = shifted_boundary(trade_dates, snapshot, 3)
                end_exec = shifted_boundary(trade_dates, date(year, 12, 31), 3)
                year_start = capital
                year_peak = capital
                year_mdd = 0.0
                days = [day for day in trade_dates if start_exec < day <= end_exec]
                previous = start_exec
                for day in days:
                    risk_return = weighted_return(equity_series, etf_weights, previous, day)
                    safe_return = cash_return(previous, day)
                    defensive_return = weighted_return(
                        defensive_series, defensive_weights, previous, day
                    ) + (1.0 - sum(defensive_weights.values())) * safe_return
                    if exposure <= 1.0:
                        portfolio_return = exposure * risk_return + (1.0 - exposure) * defensive_return
                    else:
                        portfolio_return = exposure * risk_return + (1.0 - exposure) * safe_return
                    capital *= 1.0 + portfolio_return
                    peak = max(peak, capital)
                    year_peak = max(year_peak, capital)
                    year_mdd = min(year_mdd, capital / year_peak - 1.0)
                    global_mdd = min(global_mdd, capital / peak - 1.0)
                    previous = day
                benchmark_return = period_return(price_series, CS300_CODE, start_exec, end_exec)
                benchmark_capital *= 1.0 + benchmark_return
                strategy_return = capital / year_start - 1.0
                yearly.append({
                    "year": year, "snapshot": snapshot.isoformat(), "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(), "equity_exposure": exposure,
                    "strategy_return": strategy_return, "benchmark_return": benchmark_return,
                    "benchmark_end_capital": benchmark_capital,
                    "excess_return": strategy_return - benchmark_return, "year_max_drawdown": year_mdd,
                    "start_capital": year_start, "end_capital": capital,
                })
                for code, inner_weight in etf_weights.items():
                    if exposure * inner_weight <= 1e-9:
                        continue
                    meta = next(meta for metas in equity_metas.values() for meta in metas if meta.code == code)
                    holdings.append({"year": year, "asset_type": "equity_etf", "code": code, "name": meta.name, "weight": exposure * inner_weight})
                if exposure <= 1.0:
                    for code, inner_weight in defensive_weights.items():
                        meta = defensive_metas[code]
                        holdings.append({"year": year, "asset_type": f"defensive_{meta.category}_etf", "code": code, "name": meta.name, "weight": (1.0 - exposure) * inner_weight})
                    residual = (1.0 - exposure) * (1.0 - sum(defensive_weights.values()))
                    if residual > 1e-9:
                        holdings.append({"year": year, "asset_type": "cash", "code": "CASH", "name": "现金", "weight": residual})
                else:
                    holdings.append({"year": year, "asset_type": "financing", "code": "FINANCING", "name": "融资", "weight": 1.0 - exposure})
    finally:
        conn.close()

    payload = {
        "strategy": "validated_framework_annual_rebalance",
        "frequency": "annual_rebalance_only_daily_valuation",
        "period": "2006_start_to_2026_start",
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "benchmark_final_capital": benchmark_capital,
        "total_return": capital / INITIAL_CAPITAL - 1.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / 20.0) - 1.0,
        "max_drawdown": global_mdd,
        "assumptions": {
            "transaction_costs": "not modeled; consistent with the validated framework",
            "availability": "only domestic passive ETFs available at each decision snapshot",
        },
        "policy": {"rule": RULE_NAME, "selector": MONTHLY_TREND6_RISK_TOP10.name, "defense": DEFENSE_NAME, "direction": DIRECTION_NAME},
        "yearly": yearly,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_YEARLY.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=yearly[0].keys()); writer.writeheader(); writer.writerows(yearly)
    with OUT_HOLDINGS.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=holdings[0].keys()); writer.writeheader(); writer.writerows(holdings)
    print(f"Wrote {OUT_JSON}; final={capital:.2f} cagr={payload['annualized_return']:.4%} mdd={global_mdd:.4%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

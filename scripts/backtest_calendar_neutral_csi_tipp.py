#!/usr/bin/env python3
"""Search high-water risk budgets on calendar-neutral CSI phase schedules.

The risky leg contains only the selected A-share index basket. The residual is
cash; exposure above 100% is modeled as financed A-share index exposure rather
than an overseas or synthetic investment asset.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series, price_at  # noqa: E402
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
from backtest.csi_snapshot_selector import ROBUST_TREND_TOP5, SelectorPolicy  # noqa: E402
from backtest.domestic_defensive_etf import (  # noqa: E402
    DEFENSIVE_POLICIES,
    NO_DEFENSIVE_ETF,
    DefensivePolicy,
    DefensiveWeightSchedule,
    describe_universe,
    load_defensive_etf_universe,
)
from backtest.domestic_equity_etf import (  # noqa: E402
    DirectEtfSelectorPolicy,
    blended_etf_diagnostics,
    direct_selector_diagnostics,
    describe_equity_universe,
    load_equity_etf_return_universe,
    map_indices_to_etfs,
    portfolio_turnover,
    select_direct_equity_etfs,
    direct_blend_share,
)

OUT_DIR = ROOT / "data" / "backtests"
FULL_JSON = OUT_DIR / "calendar_neutral_csi_tipp_report.json"
FULL_CSV = OUT_DIR / "calendar_neutral_csi_tipp_search.csv"
QUICK_JSON = OUT_DIR / "calendar_neutral_csi_tipp_lag3_report.json"
QUICK_CSV = OUT_DIR / "calendar_neutral_csi_tipp_lag3_search.csv"
EXECUTION_LAGS = [0, 1, 3, 5]
FINANCING_ANNUAL_RATE = 0.04
TRANSACTION_COST_BPS = 5.0

BASE_ALLOCATION_POLICY = AllocationPolicy(
    "direction_matched_score0_floor95",
    smooth_score_mapping=False,
    refresh_every_review=True,
    opportunity_floor_score_lte=0,
    opportunity_floor_pct=95.0,
)


@dataclass(frozen=True)
class TippRule:
    name: str
    floor_pct: float
    multiplier: float
    base_scale: float
    max_exposure: float
    trend_cap: float
    trend_ma_days: int = 60
    trend_return_days: int = 20


def build_rules() -> list[TippRule]:
    rules = []
    for floor_pct in (0.90, 0.92, 0.94):
        for multiplier in (8.0, 10.0, 12.0, 15.0):
            for base_scale, max_exposure in ((1.0, 1.0), (1.25, 1.25), (1.5, 1.5)):
                for trend_cap in (1.5, 0.25, 0.0):
                    rules.append(
                        TippRule(
                            f"tipp_f{int(floor_pct*100)}_m{int(multiplier)}_s{int(base_scale*100)}_tc{int(trend_cap*100)}",
                            floor_pct,
                            multiplier,
                            base_scale,
                            max_exposure,
                            trend_cap,
                        )
                    )
    for floor_pct in (0.94, 0.95, 0.96, 0.97):
        for multiplier in (20.0, 30.0, 40.0, 50.0):
            for base_scale, max_exposure in ((2.0, 2.0), (3.0, 3.0)):
                rules.append(
                    TippRule(
                        f"tipp_f{int(floor_pct*100)}_m{int(multiplier)}_s{int(base_scale*100)}_tc0",
                        floor_pct,
                        multiplier,
                        base_scale,
                        max_exposure,
                        0.0,
                    )
                )
    return rules


RULES = build_rules()


def load_selector_price_series(conn, series: dict[str, list[tuple[date, float]]]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT index_ts_code
            FROM passive_etf
            WHERE index_ts_code IS NOT NULL
              AND (etf_type IS NULL OR etf_type != 'QDII')
              AND (is_enhanced IS NULL OR is_enhanced=0)
            """
        )
        codes = sorted({str(row[0]) for row in cur.fetchall() if row[0]} - set(series))
        for start in range(0, len(codes), 300):
            chunk = codes[start : start + 300]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM index_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, trade_date, close in cur.fetchall():
                series.setdefault(str(code), []).append((trade_date, float(close)))


def trade_days_between(trade_dates: list[date], start: date, end: date) -> list[date]:
    left = bisect_left(trade_dates, start)
    right = bisect_right(trade_dates, end)
    days = trade_dates[left:right]
    if not days or days[0] != start:
        days = [start, *days]
    return days


def code_return(series: dict[str, list[tuple[date, float]]], code: str, start: date, end: date) -> float:
    start_price = price_at(series, code, start)
    end_price = price_at(series, code, end)
    if not start_price or not end_price or start_price <= 0:
        return 0.0
    return end_price / start_price - 1.0


def benchmark_bear_state(
    series: dict[str, list[tuple[date, float]]],
    benchmark_dates: list[date],
    day: date,
    ma_days: int,
    return_days: int,
) -> bool:
    return bool(
        benchmark_trend_diagnostics(
            series,
            benchmark_dates,
            day,
            ma_days,
            return_days,
        )["bear_state"]
    )


def benchmark_trend_diagnostics(
    series: dict[str, list[tuple[date, float]]],
    benchmark_dates: list[date],
    day: date,
    ma_days: int,
    return_days: int,
) -> dict[str, Any]:
    """Expose the exact point-in-time inputs behind the binary bear signal."""

    index = bisect_right(benchmark_dates, day) - 1
    rows = series[CS300_CODE]
    if index < max(ma_days, return_days):
        return {
            "bear_state": False,
            "available": False,
            "signal_date": None,
            "close": None,
            "moving_average": None,
            "moving_average_days": ma_days,
            "moving_average_distance": None,
            "trailing_return": None,
            "return_days": return_days,
        }
    current = rows[index][1]
    moving_average = statistics.mean(value for _date, value in rows[index - ma_days + 1 : index + 1])
    trailing_return = current / rows[index - return_days][1] - 1.0
    return {
        "bear_state": current < moving_average and trailing_return < 0.0,
        "available": True,
        "signal_date": rows[index][0].isoformat(),
        "close": current,
        "moving_average": moving_average,
        "moving_average_days": ma_days,
        "moving_average_distance": current / moving_average - 1.0,
        "trailing_return": trailing_return,
        "return_days": return_days,
    }


def build_daily_path(
    series: dict[str, list[tuple[date, float]]],
    trade_dates: list[date],
    schedule,
    phase: int,
    lag: int,
    equity_etf_metas=None,
    equity_etf_series=None,
    allocation_policy: AllocationPolicy = BASE_ALLOCATION_POLICY,
    include_market_features: bool = False,
    selector_refresh_every_review: bool = False,
    selector_policy: SelectorPolicy = ROBUST_TREND_TOP5,
    direct_etf_policy: DirectEtfSelectorPolicy | None = None,
    online_selector: bool = False,
    online_ridge_selector: bool = False,
    calendar_year_allocation_reset: bool = False,
    common_completion_phase_offset: int | None = None,
    common_completion_lag_days: int | None = None,
    schedule_anchor: date | None = None,
    bear_signal_timing: str = "execution",
) -> dict[str, Any]:
    if bear_signal_timing not in {"execution", "snapshot"}:
        raise ValueError("bear_signal_timing must be 'execution' or 'snapshot'")
    saved_hybrid = selector_policy.name == "saved_regime_momentum_hybrid"
    base = run_phase_schedule(
        schedule,
        phase,
        lag,
        include_rows=True,
        allocation_policy=allocation_policy,
        feature_policy=DIRECTION_MATCHED_FEATURE_POLICY,
        include_market_features=include_market_features,
        selector_policy=None if saved_hybrid else selector_policy,
        selector_refresh_every_review=selector_refresh_every_review,
        online_selector=online_selector,
        online_ridge_selector=online_ridge_selector,
        calendar_year_allocation_reset=calendar_year_allocation_reset,
        common_completion_phase_offset=common_completion_phase_offset,
        common_completion_lag_days=common_completion_lag_days,
        schedule_anchor=schedule_anchor,
    )
    daily = []
    previous_etf_weights: dict[str, float] = {}
    for row in base["rows"]:
        start = date.fromisoformat(row["start_exec"])
        end = date.fromisoformat(row["end_exec"])
        days = trade_days_between(trade_dates, start, end)
        codes = list(row["holding_codes"])
        holding_weights = row["holding_weights"]
        selector_target_weights = row.get("selector_target_weights") or {}
        if selector_refresh_every_review and selector_target_weights:
            mismatch = max(
                abs(
                    float(holding_weights.get(code, 0.0))
                    - float(selector_target_weights.get(code, 0.0))
                )
                for code in set(holding_weights) | set(selector_target_weights)
            )
            if mismatch > 1e-10:
                raise AssertionError(
                    "quarterly selector weights were not reset at the review boundary"
                )
        etf_weights = (
            map_indices_to_etfs(
                holding_weights,
                date.fromisoformat(row["start_snapshot_date"]),
                equity_etf_metas,
                etf_series=equity_etf_series,
                allow_early_broad_proxy=True,
                allow_correlation_proxy=saved_hybrid,
                index_series=series,
            )
            if equity_etf_metas is not None
            else {}
        )
        if direct_etf_policy is not None and equity_etf_metas is not None:
            direct_weights = select_direct_equity_etfs(
                equity_etf_metas,
                equity_etf_series,
                date.fromisoformat(row["start_snapshot_date"]),
                direct_etf_policy,
                benchmark_series=series.get(CS300_CODE),
            )
            if direct_weights:
                direct_share = direct_blend_share(
                    direct_etf_policy,
                    dict(row.get("market_state") or {}),
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
                    etf_weights = {
                        code: weight / total for code, weight in blended.items()
                    }
        market_state = dict(row.get("market_state") or {})
        if direct_etf_policy is not None and equity_etf_metas is not None:
            market_state.update(
                direct_selector_diagnostics(
                    equity_etf_metas,
                    equity_etf_series,
                    date.fromisoformat(row["start_snapshot_date"]),
                    direct_etf_policy,
                )
            )
        if equity_etf_metas is not None:
            # Risk controls must describe the full frozen basket, not merely
            # the direct selector's top-ranked component in a blended policy.
            for field in tuple(market_state):
                if field.startswith("selected_etf_"):
                    market_state.pop(field)
            market_state.update(
                blended_etf_diagnostics(
                    etf_weights,
                    equity_etf_series,
                    date.fromisoformat(row["start_snapshot_date"]),
                    series.get(CS300_CODE),
                    metas_by_index=equity_etf_metas,
                    index_series=series,
                )
            )
        turnover = portfolio_turnover(previous_etf_weights, etf_weights) if equity_etf_metas is not None else 0.0
        previous_etf_weights = etf_weights
        snapshot_date = date.fromisoformat(row["start_snapshot_date"])
        snapshot_bear_diagnostics = benchmark_trend_diagnostics(
            series,
            trade_dates,
            snapshot_date,
            60,
            20,
        )
        missing = [code for code in codes if not series.get(code)]
        if missing:
            raise RuntimeError(f"missing daily price series for selected indices: {missing}")
        for day_index, (previous, current) in enumerate(zip(days, days[1:])):
            bear_diagnostics = (
                snapshot_bear_diagnostics
                if bear_signal_timing == "snapshot"
                else benchmark_trend_diagnostics(
                    series,
                    trade_dates,
                    previous,
                    60,
                    20,
                )
            )
            returns = [
                float(holding_weights.get(code, 0.0)) * code_return(series, code, previous, current)
                for code in codes
            ]
            daily.append(
                {
                    "previous_day": previous,
                    "day": current,
                    "holding_codes": codes,
                    "index_target_weights": dict(holding_weights),
                    "selector_target_weights": dict(selector_target_weights),
                    "base_weight": float(row["equity_pct"]) / 100.0,
                    "risk_return": sum(returns) if returns else 0.0,
                    "actual_etf_return": (
                        sum(
                            weight * code_return(equity_etf_series, code, previous, current)
                            for code, weight in etf_weights.items()
                        )
                        - (turnover * TRANSACTION_COST_BPS / 10_000.0 if day_index == 0 else 0.0)
                        if equity_etf_metas is not None
                        else None
                    ),
                    "equity_etf_codes": sorted(etf_weights),
                    "equity_etf_weights": dict(etf_weights),
                    "window_start": day_index == 0,
                    "rebalance_anchor": row["start_snapshot_date"],
                    "scorecard_score": row.get("score"),
                    "scorecard_rebalance_reasons": list(
                        row.get("rebalance_reasons") or []
                    ),
                    "scorecard_known_inputs": dict(row.get("known_inputs") or {}),
                    "scorecard_top_items": list(row.get("top_score_items") or []),
                    "allocation_year": row.get("allocation_year"),
                    "allocation_entry": bool(row.get("allocation_entry")),
                    "allocation_midpoint": bool(row.get("allocation_midpoint")),
                    "market_state": market_state,
                    "known_inputs": dict(row.get("known_inputs") or {}),
                    "turnover": turnover if day_index == 0 else 0.0,
                    "bear_state": bool(bear_diagnostics["bear_state"]),
                    "bear_signal_diagnostics": bear_diagnostics,
                    "bear_signal_timing": bear_signal_timing,
                    "bear_signal_date": (
                        snapshot_date if bear_signal_timing == "snapshot" else previous
                    ),
                }
            )
    return {
        "schedule": schedule.name,
        "phase": phase,
        "lag": lag,
        "sample_start": base["sample_start"],
        "sample_end": base["sample_end"],
        "sample_shift_cycles": base["sample_shift_cycles"],
        "daily": daily,
    }


def evaluate_path(
    path: dict[str, Any],
    rule: TippRule,
    series: dict[str, list[tuple[date, float]]],
    defensive_metas,
    defensive_series,
    defensive_policy: DefensivePolicy,
    defensive_selection_cache,
    risk_return_source: str,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    exposure_samples = []
    bear_days = 0
    defensive_schedule = DefensiveWeightSchedule(
        defensive_metas,
        defensive_series,
        defensive_policy,
        defensive_selection_cache,
    )
    defensive_asset_days = 0
    for row in path["daily"]:
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        exposure = min(
            rule.max_exposure,
            float(row["base_weight"]) * rule.base_scale,
            rule.multiplier * cushion / max(capital, 1.0),
        )
        if row["bear_state"]:
            exposure = min(exposure, rule.trend_cap)
            bear_days += 1
        holding_days = max((row["day"] - row["previous_day"]).days, 1)
        cash_return = CASH_ANNUAL_RATE * holding_days / 365.25
        if exposure <= 1.0:
            defensive_weights = defensive_schedule.weights_for(row["previous_day"])
            defensive_return = sum(
                weight * code_return(defensive_series, code, row["previous_day"], row["day"])
                for code, weight in defensive_weights.items()
            )
            defensive_return += (1.0 - sum(defensive_weights.values())) * cash_return
            defensive_asset_days += bool(defensive_weights)
            risky_return = float(
                row["actual_etf_return"] if risk_return_source == "actual_etf" else row["risk_return"]
            )
            portfolio_return = exposure * risky_return + (1.0 - exposure) * defensive_return
        else:
            financing_return = FINANCING_ANNUAL_RATE * holding_days / 365.25
            risky_return = float(
                row["actual_etf_return"] if risk_return_source == "actual_etf" else row["risk_return"]
            )
            portfolio_return = exposure * risky_return + (1.0 - exposure) * financing_return
        capital = max(1.0, capital * (1.0 + portfolio_return))
        peak = max(peak, capital)
        curve.append(capital)
        exposure_samples.append(exposure)
    mdd = max_drawdown(curve)
    return {
        "name": f"{rule.name}_{defensive_policy.name}_{path['schedule']}_phase{path['phase']}_lag{path['lag']}",
        "rule": rule.name,
        "defensive_policy": defensive_policy.name,
        "risk_return_source": risk_return_source,
        "schedule": path["schedule"],
        "phase_month_offset": path["phase"],
        "execution_lag_days": path["lag"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / 20.0) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "average_exposure": statistics.mean(exposure_samples),
        "max_exposure": max(exposure_samples),
        "bear_day_count": bear_days,
        "defensive_asset_days": defensive_asset_days,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(case["target_met"] for case in cases),
        "min_final_capital_wan": min(case["final_capital_wan"] for case in cases),
        "median_final_capital_wan": statistics.median(case["final_capital_wan"] for case in cases),
        "worst_max_drawdown": min(case["max_drawdown"] for case in cases),
        "median_max_drawdown": statistics.median(case["max_drawdown"] for case in cases),
        "min_annualized_return": min(case["annualized_return"] for case in cases),
        "median_average_exposure": statistics.median(case["average_exposure"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick-lag3", action="store_true")
    parser.add_argument("--rule", action="append", help="Evaluate only the named rule; may be repeated.")
    parser.add_argument(
        "--defensive-policy",
        action="append",
        help="Evaluate only the named domestic defensive ETF policy; may be repeated.",
    )
    parser.add_argument(
        "--risk-return-source",
        choices=("index_proxy", "actual_etf"),
        default="index_proxy",
    )
    args = parser.parse_args()
    lags = [3] if args.quick_lag3 else EXECUTION_LAGS
    output_json = QUICK_JSON if args.quick_lag3 else FULL_JSON
    output_csv = QUICK_CSV if args.quick_lag3 else FULL_CSV

    conn = get_connection()
    try:
        series = load_price_series(conn)
        load_selector_price_series(conn, series)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
        equity_etf_metas, equity_etf_series = load_equity_etf_return_universe(conn)
        trade_dates = [day for day, _value in series[CS300_CODE]]
        paths = [
            build_daily_path(
                series,
                trade_dates,
                schedule,
                phase,
                lag,
                equity_etf_metas if args.risk_return_source == "actual_etf" else None,
                equity_etf_series if args.risk_return_source == "actual_etf" else None,
            )
            for schedule in FORMAL_SCHEDULES
            for phase in MONTH_DRIFT_PHASES
            for lag in lags
        ]
    finally:
        conn.close()
    selected_rules = [rule for rule in RULES if not args.rule or rule.name in set(args.rule)]
    if args.rule and len(selected_rules) != len(set(args.rule)):
        known = {rule.name for rule in selected_rules}
        raise ValueError(f"unknown rules: {sorted(set(args.rule) - known)}")
    selected_defensive_policies = [
        policy
        for policy in DEFENSIVE_POLICIES
        if not args.defensive_policy or policy.name in set(args.defensive_policy)
    ]
    if args.defensive_policy and len(selected_defensive_policies) != len(set(args.defensive_policy)):
        known = {policy.name for policy in selected_defensive_policies}
        raise ValueError(f"unknown defensive policies: {sorted(set(args.defensive_policy) - known)}")
    results = []
    defensive_selection_cache = {}
    for rule in selected_rules:
        for defensive_policy in selected_defensive_policies:
            cases = [
                evaluate_path(
                    path,
                    rule,
                    series,
                    defensive_metas,
                    defensive_series,
                    defensive_policy,
                    defensive_selection_cache,
                    args.risk_return_source,
                )
                for path in paths
            ]
            summary = summarize(cases)
            results.append(
                {
                    "rule": asdict(rule),
                    "defensive_policy": asdict(defensive_policy),
                    "summary": summary,
                    "cases": cases,
                }
            )
            print(
                f"{rule.name:<34} {defensive_policy.name:<22} "
                f"pass={summary['pass_count']:>3}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"mdd={summary['worst_max_drawdown']*100:6.2f}%"
            )
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    payload = {
        "objective": "Calendar-neutral high-water risk budget on selected A-share index baskets.",
        "investment_constraint": "Risky holdings are A-share index baskets only; residual is cash. No overseas investment asset is held.",
        "execution_model": (
            "Signals use prior close and affect the next daily return; defensive ETF return indices are reconstructed "
            "from fund_daily.pct_chg to neutralize share splits; financed exposure above 100% pays 4% annual financing."
        ),
        "base_allocation_policy": asdict(BASE_ALLOCATION_POLICY),
        "feature_policy": asdict(DIRECTION_MATCHED_FEATURE_POLICY),
        "selector_policy": asdict(ROBUST_TREND_TOP5),
        "defensive_etf_universe": describe_universe(defensive_metas),
        "equity_etf_universe": describe_equity_universe(equity_etf_metas),
        "risk_return_source": args.risk_return_source,
        "early_proxy_policy": (
            "point-in-time domestic broad passive ETF fallback when the selected early index has no exact tracker"
            if args.risk_return_source == "actual_etf"
            else None
        ),
        "transaction_cost_bps": TRANSACTION_COST_BPS if args.risk_return_source == "actual_etf" else 0.0,
        "execution_lags": lags,
        "results": results,
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        fields = ["name", "pass_count", "count", "min_final_capital_wan", "median_final_capital_wan", "worst_max_drawdown", "median_max_drawdown", "min_annualized_return", "median_average_exposure"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "name": f"{item['rule']['name']}__{item['defensive_policy']['name']}",
                    **item["summary"],
                }
            )
    best = results[0]
    print(
        f"Wrote {output_json}; best={best['rule']['name']} "
        f"defense={best['defensive_policy']['name']} {best['summary']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

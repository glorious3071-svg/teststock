#!/usr/bin/env python3
"""Search domestic passive ETF trend-state portfolios with cash exits."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import month_end_shift, monthly_boundaries, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_passive_etf_only import EtfMeta, load_etf_universe, period_return, price_at, return_vol

OUT_DIR = ROOT / "data" / "backtests"
START_YEAR = 2006
END_YEAR = 2025
MONTH_PHASES = list(range(12))
EXECUTION_LAGS = [0, 1, 3, 5]
PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
SCORE_CACHE: dict[tuple[str, dt.date, int, int, int], float | None] = {}
RANK_CACHE: dict[tuple[dt.date, int, int, int, float], list[tuple[float, str]]] = {}


@dataclass(frozen=True)
class TrendStateRule:
    name: str
    interval_months: int
    top_n: int
    momentum_months: int
    skip_recent_months: int
    min_history_months: int
    market_trend_months: int
    market_trend_gt: float
    min_best_score: float
    stop_loss: float
    max_entry_drawdown: float
    recovery_market_trend_gt: float
    recovery_min_best_score: float
    reentry_after_stop: bool = False


def cached_price(rows: list[tuple[dt.date, float]], boundary: dt.date, code: str) -> float | None:
    key = (code, boundary)
    if key in PRICE_CACHE:
        return PRICE_CACHE[key]
    value = price_at(rows, boundary, code)
    PRICE_CACHE[key] = value
    return value


def score_etf(code: str, rows: list[tuple[dt.date, float]], snapshot: dt.date, rule: TrendStateRule) -> float | None:
    key = (code, snapshot, rule.momentum_months, rule.skip_recent_months, rule.min_history_months)
    if key in SCORE_CACHE:
        return SCORE_CACHE[key]
    end = month_end_shift(snapshot, -rule.skip_recent_months)
    start = month_end_shift(end, -rule.momentum_months)
    min_start = month_end_shift(snapshot, -rule.min_history_months)
    if cached_price(rows, min_start, code) is None:
        SCORE_CACHE[key] = None
        return None
    mom = period_return(rows, start, end, code)
    short = period_return(rows, month_end_shift(end, -3), end, code)
    if mom is None or short is None:
        SCORE_CACHE[key] = None
        return None
    vol = return_vol(rows, end, 6)
    value = mom + 0.5 * short - 2.0 * min(vol, 0.12)
    SCORE_CACHE[key] = value
    return value


def rank_equity_etfs(
    metas: dict[str, EtfMeta],
    series: dict[str, list[tuple[dt.date, float]]],
    snapshot: dt.date,
    rule: TrendStateRule,
) -> list[tuple[float, str]]:
    key = (snapshot, rule.top_n, rule.momentum_months, rule.min_history_months, rule.min_best_score)
    if key in RANK_CACHE:
        return RANK_CACHE[key]
    ranked = []
    for code, meta in metas.items():
        if meta.category != "equity":
            continue
        score = score_etf(code, series[code], snapshot, rule)
        if score is not None:
            ranked.append((score, code))
    ranked.sort(reverse=True)
    RANK_CACHE[key] = ranked
    return ranked


def dates_between(trade_dates: list[dt.date], start: dt.date, end: dt.date) -> list[dt.date]:
    left = bisect_left(trade_dates, start)
    right = bisect_right(trade_dates, end)
    return trade_dates[left:right]


def equal_weight_daily_return(
    codes: list[str],
    series: dict[str, list[tuple[dt.date, float]]],
    prev_day: dt.date,
    day: dt.date,
) -> float:
    returns = []
    for code in codes:
        prev_px = cached_price(series[code], prev_day, code)
        px = cached_price(series[code], day, code)
        if prev_px and px and prev_px > 0:
            returns.append(px / prev_px - 1.0)
    return sum(returns) / len(returns) if returns else 0.0


def run_period(
    capital: float,
    global_peak: float,
    codes: list[str],
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    start_exec: dt.date,
    end_exec: dt.date,
    rule: TrendStateRule,
) -> tuple[float, float, float, bool, list[dict[str, Any]], list[float]]:
    if not codes:
        return capital, global_peak, 0.0, False, [], [capital]
    days = dates_between(trade_dates, start_exec, end_exec)
    if len(days) < 2:
        return capital, global_peak, 0.0, False, [], [capital]
    start_capital = capital
    stopped = False
    active = True
    rows = []
    curve = [capital]
    for prev_day, day in zip(days[:-1], days[1:]):
        if active:
            capital *= 1.0 + equal_weight_daily_return(codes, series, prev_day, day)
            if capital <= 0:
                capital = 1.0
            global_peak = max(global_peak, capital)
            if capital / global_peak - 1.0 <= rule.stop_loss:
                active = False
                stopped = True
        rows.append(
            {
                "trade_date": day.isoformat(),
                "capital": capital,
                "drawdown": capital / global_peak - 1.0,
                "active": active,
            }
        )
        curve.append(capital)
    return capital, global_peak, capital / start_capital - 1.0, stopped, rows, curve


def choose_codes(
    metas: dict[str, EtfMeta],
    series: dict[str, list[tuple[dt.date, float]]],
    snapshot: dt.date,
    rule: TrendStateRule,
    capital_drawdown: float,
) -> tuple[list[str], float | None, float]:
    market_trend = period_return(series[CS300_CODE], month_end_shift(snapshot, -rule.market_trend_months), snapshot) or 0.0
    ranked = rank_equity_etfs(metas, series, snapshot, rule)
    best_score = ranked[0][0] if ranked else None
    if (
        capital_drawdown <= rule.max_entry_drawdown
        and (market_trend <= rule.recovery_market_trend_gt or best_score is None or best_score <= rule.recovery_min_best_score)
    ):
        return [], best_score, market_trend
    if market_trend <= rule.market_trend_gt or best_score is None or best_score <= rule.min_best_score:
        return [], best_score, market_trend
    return [code for _score, code in ranked[: rule.top_n]], best_score, market_trend


def run_case(
    metas: dict[str, EtfMeta],
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    rule: TrendStateRule,
    phase: int,
    lag: int,
    include_daily_rows: bool,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    current_codes: list[str] = []
    stopped_this_holding = False
    rows = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
    for idx, (start_snapshot, end_snapshot) in enumerate(periods):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        rebalance = idx % rule.interval_months == 0 or (rule.reentry_after_stop and stopped_this_holding)
        best_score = None
        market_trend = 0.0
        if rebalance:
            capital_drawdown = capital / peak - 1.0 if peak > 0 else 0.0
            current_codes, best_score, market_trend = choose_codes(metas, series, start_snapshot, rule, capital_drawdown)
            stopped_this_holding = False
        capital, peak, period_ret, stopped, daily_rows, daily_curve = run_period(
            capital,
            peak,
            current_codes,
            series,
            trade_dates,
            start_exec,
            end_exec,
            rule,
        )
        stopped_this_holding = stopped_this_holding or stopped
        curve.extend(daily_curve[1:] if len(daily_curve) > 1 else daily_curve)
        row = {
            "start_snapshot": start_snapshot.isoformat(),
            "end_snapshot": end_snapshot.isoformat(),
            "start_exec": start_exec.isoformat(),
            "end_exec": end_exec.isoformat(),
            "capital": capital,
            "period_return": period_ret,
            "drawdown": capital / peak - 1.0,
            "holdings": ",".join(current_codes) if current_codes else "CASH",
            "stopped": stopped,
            "best_score": best_score,
            "market_trend": market_trend,
        }
        if include_daily_rows:
            row["daily_rows"] = daily_rows
        rows.append(row)
    mdd = max_drawdown(curve)
    return {
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / (END_YEAR - START_YEAR + 1)) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def build_rules(quick: bool) -> list[TrendStateRule]:
    intervals = [1, 3] if quick else [1, 2, 3]
    top_ns = [1, 3] if quick else [1, 2, 3, 5]
    momentum_months = [1, 3, 6, 12]
    market_months = [3, 6, 10]
    market_thresholds = [-0.03, 0.0, 0.03]
    min_scores = [-0.10, -0.05, 0.0, 0.05, 0.10]
    stops = [-0.06, -0.08, -0.10]
    max_entry_drawdowns = [-0.06, -0.10]
    if quick:
        momentum_months = [1, 3, 6]
        market_months = [3, 6]
        market_thresholds = [-0.03, 0.0]
        min_scores = [-0.05, 0.0, 0.05]
        stops = [-0.06, -0.08]
        max_entry_drawdowns = [-0.06]
    rules = []
    for interval in intervals:
        for top_n in top_ns:
            for mom in momentum_months:
                for market_month in market_months:
                    for market_threshold in market_thresholds:
                        for min_score in min_scores:
                            for stop in stops:
                                for max_entry_drawdown in max_entry_drawdowns:
                                    rules.append(
                                        TrendStateRule(
                                            name=(
                                                f"trend_i{interval}_top{top_n}_m{mom}_mk{market_month}"
                                                f"_mt{int(market_threshold*100):+03d}".replace("+", "p").replace("-", "n")
                                                + f"_sg{int(min_score*100):+03d}".replace("+", "p").replace("-", "n")
                                                + f"_st{int(abs(stop)*100):02d}"
                                                + f"_eg{int(abs(max_entry_drawdown)*100):02d}"
                                            ),
                                            interval_months=interval,
                                            top_n=top_n,
                                            momentum_months=mom,
                                            skip_recent_months=0,
                                            min_history_months=max(3, mom),
                                            market_trend_months=market_month,
                                            market_trend_gt=market_threshold,
                                            min_best_score=min_score,
                                            stop_loss=stop,
                                            max_entry_drawdown=max_entry_drawdown,
                                            recovery_market_trend_gt=max(0.06, market_threshold + 0.06),
                                            recovery_min_best_score=max(0.08, min_score + 0.05),
                                        )
                                    )
    return rules


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
    }


def strip_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in results:
        cases = [{key: value for key, value in case.items() if key != "rows"} for case in item["cases"]]
        output.append({"rule": item["rule"], "cases": cases, "summary": item["summary"]})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Search domestic passive ETF trend-state portfolios.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    conn = get_connection()
    try:
        metas, series = load_etf_universe(conn, args.min_rows, include_money_etf_defense=False)
    finally:
        conn.close()
    trade_dates = [day for day, _px in series[CS300_CODE]]
    rules = build_rules(args.quick)
    results = []
    for rule in rules:
        cases = [
            run_case(metas, series, trade_dates, rule, phase, lag, include_daily_rows=not args.summary_only)
            for phase in MONTH_PHASES
            for lag in EXECUTION_LAGS
        ]
        results.append({"rule": asdict(rule), "cases": cases, "summary": summarize(cases)})
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )

    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_passive_etf_trend_state"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    payload = {
        "objective": "Domestic passive ETF trend-state search with cash exits; holdings are ETFs or uninvested cash.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "results": strip_rows(results) if args.summary_only else results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["name", *list(results[0]["rule"].keys())[1:], *list(results[0]["summary"].keys())]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow(row)

    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<52} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:8.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"median={s['median_final_capital_wan']:8.1f}w"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    best = results[0]["summary"]
    return 0 if best["pass_count"] == best["count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

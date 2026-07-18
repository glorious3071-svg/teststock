#!/usr/bin/env python3
"""Search daily-risk tactical portfolios using domestic passive ETFs only."""

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
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, month_end_shift, monthly_boundaries, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, END_YEAR, INITIAL_CAPITAL, START_YEAR, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
CASH_ANNUAL_RATE = 0.015
PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
MOMENTUM_CACHE: dict[tuple[str, dt.date, int, int], float | None] = {}
RANK_CACHE: dict[tuple[dt.date, int, int, int, bool], list[str]] = {}


@dataclass(frozen=True)
class DailyTacticalRule:
    name: str
    top_n: int
    momentum_months: int
    skip_recent_months: int
    min_history_months: int
    rebalance_months: int
    max_exposure: float
    stop_loss_pct: float
    trailing_stop_pct: float
    cooldown_days: int
    reentry_ma_days: int
    cs300_trend_months: int
    cs300_trend_lte: float
    floor_pct: float
    multiplier: float
    drawdown_guard_lte: float
    drawdown_guard_exposure: float
    include_defensive_etfs: bool


def classify_etf(code: str, name: str, index_name: str) -> str:
    text = f"{code} {name} {index_name}"
    if any(key in text for key in ["货币", "保证金", "现金"]):
        return "money"
    if code.startswith("511") or any(key in text for key in ["国债", "债ETF", "信用债", "公司债", "政金债", "城投债", "可转债"]):
        return "bond"
    if any(key in text for key in ["黄金", "上海金", "金ETF"]):
        return "gold"
    return "equity"


def load_universe(conn, min_rows: int, include_defensive_etfs: bool) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[dt.date, float]]], list[dt.date]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date,
                   COUNT(f.trade_date)
            FROM passive_etf e
            JOIN fund_daily f ON f.ts_code=e.ts_code
            WHERE e.list_status='L'
              AND (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND e.ts_code NOT LIKE '%%.OF'
              AND f.close IS NOT NULL
            GROUP BY e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
            HAVING COUNT(f.trade_date) >= %s
            ORDER BY e.list_date, e.ts_code
            """,
            (min_rows,),
        )
        metas = {}
        for code, name, index_code, index_name, list_date, count in cur.fetchall():
            code = str(code)
            category = classify_etf(code, str(name or ""), str(index_name or ""))
            if not include_defensive_etfs and category != "equity":
                continue
            metas[code] = {
                "code": code,
                "name": str(name or code),
                "index_code": str(index_code or ""),
                "index_name": str(index_name or ""),
                "list_date": list_date.isoformat() if list_date else None,
                "category": category,
                "rows": int(count),
            }
        codes = sorted(metas)
        series: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
        for start in range(0, len(codes), 400):
            chunk = codes[start : start + 400]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, trade_date, close in cur.fetchall():
                series.setdefault(str(code), []).append((trade_date, float(close)))
        cur.execute(
            """
            SELECT trade_date, close
            FROM index_daily
            WHERE ts_code=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (CS300_CODE,),
        )
        series[CS300_CODE] = [(trade_date, float(close)) for trade_date, close in cur.fetchall()]
    trade_dates = [day for day, _close in series[CS300_CODE]]
    return metas, series, trade_dates


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date, code: str | None = None) -> float | None:
    if code is not None:
        key = (code, boundary)
        if key in PRICE_CACHE:
            return PRICE_CACHE[key]
    idx = bisect_right(rows, (boundary, math.inf)) - 1
    value = rows[idx][1] if idx >= 0 else None
    if code is not None:
        PRICE_CACHE[key] = value
    return value


def period_return(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date, code: str | None = None) -> float | None:
    start_px = price_at(rows, start, code)
    end_px = price_at(rows, end, code)
    if start_px is None or end_px is None or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def moving_average(rows: list[tuple[dt.date, float]], boundary: dt.date, points: int) -> float | None:
    idx = bisect_right(rows, (boundary, math.inf)) - 1
    if idx + 1 < points:
        return None
    window = rows[idx - points + 1 : idx + 1]
    return sum(px for _day, px in window) / len(window)


def score_etf(code: str, rows: list[tuple[dt.date, float]], snapshot: dt.date, rule: DailyTacticalRule) -> float | None:
    key = (code, snapshot, rule.momentum_months, rule.skip_recent_months)
    if key in MOMENTUM_CACHE:
        return MOMENTUM_CACHE[key]
    min_start = month_end_shift(snapshot, -rule.min_history_months)
    if price_at(rows, min_start, code) is None:
        MOMENTUM_CACHE[key] = None
        return None
    end = month_end_shift(snapshot, -rule.skip_recent_months)
    start = month_end_shift(end, -rule.momentum_months)
    ret = period_return(rows, start, end, code)
    short = period_return(rows, month_end_shift(end, -3), end, code)
    if ret is None or short is None:
        MOMENTUM_CACHE[key] = None
        return None
    value = ret + 0.25 * short
    MOMENTUM_CACHE[key] = value
    return value


def choose_etfs(metas: dict[str, dict[str, Any]], series: dict[str, list[tuple[dt.date, float]]], snapshot: dt.date, rule: DailyTacticalRule) -> list[str]:
    key = (snapshot, rule.top_n, rule.momentum_months, rule.skip_recent_months, rule.include_defensive_etfs)
    if key in RANK_CACHE:
        return RANK_CACHE[key][: rule.top_n]
    scored = []
    for code, meta in metas.items():
        if not rule.include_defensive_etfs and meta["category"] != "equity":
            continue
        value = score_etf(code, series[code], snapshot, rule)
        if value is not None:
            scored.append((value, code))
    ranked = [code for _value, code in sorted(scored, reverse=True)]
    RANK_CACHE[key] = ranked
    return ranked[: rule.top_n]


def daily_cash_return(prev_day: dt.date, day: dt.date) -> float:
    days = max((day - prev_day).days, 0)
    return CASH_ANNUAL_RATE * days / 365.25


def code_daily_return(rows: list[tuple[dt.date, float]], prev_day: dt.date, day: dt.date, code: str) -> float | None:
    prev_px = price_at(rows, prev_day, code)
    px = price_at(rows, day, code)
    if prev_px is None or px is None or prev_px <= 0:
        return None
    return px / prev_px - 1.0


def cs300_trend_ok(series: dict[str, list[tuple[dt.date, float]]], day: dt.date, rule: DailyTacticalRule) -> bool:
    if rule.cs300_trend_months <= 0:
        return True
    trend = period_return(series[CS300_CODE], month_end_shift(day, -rule.cs300_trend_months), day, CS300_CODE)
    return (trend if trend is not None else 0.0) > rule.cs300_trend_lte


def reentry_ok(series: dict[str, list[tuple[dt.date, float]]], codes: list[str], day: dt.date, rule: DailyTacticalRule) -> bool:
    if not codes:
        return False
    if rule.reentry_ma_days <= 0:
        return True
    hits = 0
    checked = 0
    for code in codes:
        rows = series[code]
        px = price_at(rows, day, code)
        ma = moving_average(rows, day, rule.reentry_ma_days)
        if px is None or ma is None:
            continue
        checked += 1
        if px >= ma:
            hits += 1
    return checked > 0 and hits / checked >= 0.5


def build_rules(quick: bool, aggressive: bool, max_rules: int = 0) -> list[DailyTacticalRule]:
    top_ns = [1, 3, 5]
    momentums = [3, 6, 12]
    rebalances = [1, 3]
    max_exposures = [1.0, 1.5, 2.0, 3.0]
    stops = [-0.02, -0.04, -0.06]
    trailing_stops = [-0.04, -0.08, -1.0]
    floors = [0.88, 0.90, 0.92]
    multipliers = [4.0, 8.0, 12.0]
    trend_caps = [(0, -1.0), (6, -0.08), (3, -0.05)]
    defensive_flags = [False, True]
    if quick:
        top_ns = [1, 3]
        momentums = [3, 6, 12]
        rebalances = [1]
        max_exposures = [1.5, 2.0]
        stops = [-0.02, -0.04]
        trailing_stops = [-0.06, -1.0]
        floors = [0.88, 0.90]
        multipliers = [8.0, 12.0]
        trend_caps = [(0, -1.0), (6, -0.08)]
        defensive_flags = [False, True]
    if aggressive:
        max_exposures = [2.0, 3.0, 4.0, 5.0]
        floors = [0.84, 0.86, 0.88]
        multipliers = [12.0, 16.0, 20.0]
        stops = [-0.03, -0.05, -0.08]
        trailing_stops = [-0.08, -0.12, -1.0]
        if quick:
            max_exposures = [3.0, 4.0]
            floors = [0.86, 0.88]
            multipliers = [12.0, 16.0]
            stops = [-0.03, -0.05]
            trailing_stops = [-0.08, -1.0]
    rules = []
    for include_defensive in defensive_flags:
        for top_n in top_ns:
            for mom in momentums:
                for rebalance in rebalances:
                    for max_exposure in max_exposures:
                        for stop in stops:
                            for trailing_stop in trailing_stops:
                                for floor in floors:
                                    for multiplier in multipliers:
                                        for trend_months, trend_lte in trend_caps:
                                            name = (
                                                f"daily_top{top_n}_m{mom}_r{rebalance}_x{int(max_exposure*100)}"
                                                f"_st{int(abs(stop)*100):02d}_tr{int(abs(trailing_stop)*100):02d}"
                                                f"_f{int(floor*100)}_k{int(multiplier)}"
                                                f"_cs{trend_months}_{int(abs(trend_lte)*100):02d}"
                                                f"_def{int(include_defensive)}"
                                            )
                                            rules.append(
                                                DailyTacticalRule(
                                                    name=name,
                                                    top_n=top_n,
                                                    momentum_months=mom,
                                                    skip_recent_months=0,
                                                    min_history_months=max(6, mom),
                                                    rebalance_months=rebalance,
                                                    max_exposure=max_exposure,
                                                    stop_loss_pct=stop,
                                                    trailing_stop_pct=trailing_stop,
                                                    cooldown_days=5,
                                                    reentry_ma_days=20,
                                                    cs300_trend_months=trend_months,
                                                    cs300_trend_lte=trend_lte,
                                                    floor_pct=floor,
                                                    multiplier=multiplier,
                                                    drawdown_guard_lte=-0.08,
                                                    drawdown_guard_exposure=0.0,
                                                    include_defensive_etfs=include_defensive,
                                                )
                                            )
                                            if max_rules and len(rules) >= max_rules:
                                                return rules
    return rules


def run_case(
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    rule: DailyTacticalRule,
    phase: int,
    lag: int,
    include_rows: bool,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    current_codes: list[str] = []
    in_market = False
    period_peak = capital
    entry_capital = capital
    cooldown_until: dt.date | None = None
    stop_count = 0
    exposure_samples: list[float] = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
    for period_idx, (start_snapshot, end_snapshot) in enumerate(periods):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        if period_idx % rule.rebalance_months == 0 or not current_codes:
            current_codes = choose_etfs(metas, series, start_snapshot, rule)
            in_market = bool(current_codes) and cs300_trend_ok(series, start_exec, rule) and reentry_ok(series, current_codes, start_exec, rule)
            period_peak = capital
            entry_capital = capital
        left = bisect_left(trade_dates, start_exec)
        right = bisect_right(trade_dates, end_exec)
        days = trade_dates[left:right]
        if not days or days[0] != start_exec:
            days = [start_exec, *days]
        prev_day = days[0]
        for day in days[1:]:
            drawdown = capital / peak - 1.0
            floor = peak * rule.floor_pct
            cushion = max(0.0, capital - floor)
            wrapper_exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
            if drawdown <= rule.drawdown_guard_lte:
                wrapper_exposure = min(wrapper_exposure, rule.drawdown_guard_exposure)
            if not in_market and (cooldown_until is None or day >= cooldown_until):
                in_market = bool(current_codes) and cs300_trend_ok(series, day, rule) and reentry_ok(series, current_codes, day, rule)
                if in_market:
                    period_peak = capital
                    entry_capital = capital
            cash_ret = daily_cash_return(prev_day, day)
            etf_ret = 0.0
            if in_market and current_codes and wrapper_exposure > 0:
                returns = [code_daily_return(series[code], prev_day, day, code) for code in current_codes]
                valid = [ret for ret in returns if ret is not None]
                etf_ret = sum(valid) / len(valid) if valid else 0.0
                ret = wrapper_exposure * etf_ret + (1.0 - wrapper_exposure) * cash_ret
            else:
                ret = cash_ret
                wrapper_exposure = 0.0
            capital *= 1.0 + ret
            if capital <= 0:
                capital = 1.0
            peak = max(peak, capital)
            period_peak = max(period_peak, capital)
            curve.append(capital)
            exposure_samples.append(wrapper_exposure if in_market else 0.0)
            entry_dd = capital / entry_capital - 1.0 if entry_capital > 0 else 0.0
            trailing_dd = capital / period_peak - 1.0 if period_peak > 0 else 0.0
            if in_market and (entry_dd <= rule.stop_loss_pct or trailing_dd <= rule.trailing_stop_pct or not cs300_trend_ok(series, day, rule)):
                in_market = False
                stop_count += 1
                cooldown_until = day + dt.timedelta(days=rule.cooldown_days)
            prev_day = day
        if include_rows:
            rows.append(
                {
                    "start_snapshot": start_snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "holdings": ",".join(current_codes) if current_codes else "CASH",
                    "in_market": in_market,
                    "stop_count": stop_count,
                }
            )
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_exposure": statistics.mean(exposure_samples) if exposure_samples else 0.0,
        "stop_count": stop_count,
        "rows": rows,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in cases),
        "median_stop_count": statistics.median(item["stop_count"] for item in cases),
    }


def strip_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rule": item["rule"],
            "cases": [{key: value for key, value in case.items() if key != "rows"} for case in item["cases"]],
            "summary": item["summary"],
            "target_met": item["target_met"],
        }
        for item in results
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Search domestic passive ETF-only daily tactical portfolios.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--max-rules", type=int, default=0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    conn = get_connection()
    try:
        metas_all, series, trade_dates = load_universe(conn, args.min_rows, include_defensive_etfs=True)
    finally:
        conn.close()
    rules = build_rules(args.quick, args.aggressive, args.max_rules)
    results = []
    for rule in rules:
        metas = metas_all if rule.include_defensive_etfs else {code: meta for code, meta in metas_all.items() if meta["category"] == "equity"}
        cases = [
            run_case(metas, series, trade_dates, rule, phase, lag, include_rows=not args.summary_only)
            for phase in MONTH_PHASES
            for lag in EXECUTION_LAGS
        ]
        summary = summarize(cases)
        results.append({"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]})
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_passive_etf_daily_tactical"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    payload = {
        "objective": "Daily tactical search using domestic passive ETF prices only.",
        "constraints": {
            "invested_assets": "Domestic SH/SZ passive ETFs from passive_etf and fund_daily",
            "no_overseas_assets": True,
            "no_options": True,
            "no_futures": True,
            "no_crypto": True,
            "cash_or_financing_residual_only": True,
        },
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "start_year": START_YEAR,
        "end_year": END_YEAR,
        "month_phases": MONTH_PHASES,
        "execution_lags": EXECUTION_LAGS,
        "universe": {
            "etf_count": len(metas_all),
            "equity_count": sum(1 for meta in metas_all.values() if meta["category"] == "equity"),
            "bond_count": sum(1 for meta in metas_all.values() if meta["category"] == "bond"),
            "gold_count": sum(1 for meta in metas_all.values() if meta["category"] == "gold"),
            "money_count": sum(1 for meta in metas_all.values() if meta["category"] == "money"),
        },
        "results": strip_rows(results) if args.summary_only else results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = ["name", *[key for key in asdict(rules[0]).keys() if key != "name"], *list(results[0]["summary"].keys())]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<92} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:9.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"med={s['median_final_capital_wan']:9.1f}w exp={s['median_avg_exposure']:.2f}"
        )
    best = results[0]["summary"]
    print(
        f"Wrote {json_path}; rules={len(results)} best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {csv_path}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

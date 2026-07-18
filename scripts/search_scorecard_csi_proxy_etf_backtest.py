#!/usr/bin/env python3
"""Backtest scorecard CSI/SI recommendations mapped to domestic ETF proxies."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import apply_year_for_snapshot, month_end_shift, monthly_boundaries, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.map_csi_to_etf_proxy import resolve_etf_proxy, suffix_like
from scripts.search_scorecard_csi_passive_etf_only import period_return

OUT_DIR = ROOT / "data" / "backtests"
START_YEAR = 2006
END_YEAR = 2025
MONTH_PHASES = list(range(12))
EXECUTION_LAGS = [0, 1, 3, 5]
PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
MAPPING_CACHE: dict[tuple[str, dt.date, int, float], dict[str, Any] | None] = {}
RECOMMENDATION_CACHE: dict[tuple[int, str, int], list[dict[str, Any]]] = {}
YEAR_MAPPING_CACHE: dict[tuple[int, str, int, int, float], list[dict[str, Any]]] = {}


@dataclass(frozen=True)
class ProxyRule:
    name: str
    suffix: str
    interval_months: int
    top_n: int
    market_trend_months: int
    market_trend_gt: float
    allow_cash_defense: bool
    lookback_days: int
    min_corr: float


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date, code: str | None = None) -> float | None:
    if code is not None:
        key = (code, boundary)
        if key in PRICE_CACHE:
            return PRICE_CACHE[key]
    i = bisect_right(rows, (boundary, math.inf)) - 1
    value = rows[i][1] if i >= 0 else None
    if code is not None:
        PRICE_CACHE[key] = value
    return value


def load_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    series: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, trade_date, close
            FROM fund_daily
            WHERE close IS NOT NULL
            ORDER BY ts_code, trade_date
            """
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
    return series


def load_recommendations(conn, year: int, suffix: str, top_n: int) -> list[dict[str, Any]]:
    key = (year, suffix, top_n)
    if key in RECOMMENDATION_CACHE:
        return RECOMMENDATION_CACHE[key]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rank_position, ts_code, index_name, final_score, best_theme
            FROM csi_annual_recommendation
            WHERE apply_year=%s AND ts_code LIKE %s
            ORDER BY rank_position
            LIMIT %s
            """,
            (year, suffix_like(suffix), top_n),
        )
        rows = [
            {
                "rank": int(rank),
                "index_code": str(code),
                "index_name": str(name or code),
                "final_score": float(score),
                "best_theme": str(theme or ""),
            }
            for rank, code, name, score, theme in cur.fetchall()
        ]
    RECOMMENDATION_CACHE[key] = rows
    return rows


def proxy_for_index(conn, index_code: str, snapshot: dt.date, rule: ProxyRule) -> dict[str, Any] | None:
    key = (index_code, snapshot, rule.lookback_days, rule.min_corr)
    if key in MAPPING_CACHE:
        return MAPPING_CACHE[key]
    with conn.cursor() as cur:
        proxy = resolve_etf_proxy(cur, index_code, snapshot, rule.lookback_days, rule.min_corr)
    MAPPING_CACHE[key] = proxy
    return proxy


def annual_mapping(conn, year: int, rule: ProxyRule) -> list[dict[str, Any]]:
    key = (year, rule.suffix, rule.top_n, rule.lookback_days, rule.min_corr)
    if key in YEAR_MAPPING_CACHE:
        return YEAR_MAPPING_CACHE[key]
    as_of = dt.date(year, 1, 4)
    recs = load_recommendations(conn, year, rule.suffix, rule.top_n)
    rows = []
    for rec in recs:
        proxy = proxy_for_index(conn, rec["index_code"], as_of, rule)
        if proxy:
            rows.append({**rec, **proxy})
    YEAR_MAPPING_CACHE[key] = rows
    return rows


def choose_etfs(conn, snapshot: dt.date, rule: ProxyRule, series: dict[str, list[tuple[dt.date, float]]]) -> tuple[list[str], list[dict[str, Any]]]:
    market_trend = period_return(series[CS300_CODE], month_end_shift(snapshot, -rule.market_trend_months), snapshot) or 0.0
    if rule.allow_cash_defense and market_trend <= rule.market_trend_gt:
        return [], []
    year = apply_year_for_snapshot(snapshot)
    codes = []
    rows = []
    for row in annual_mapping(conn, year, rule):
        code = row["etf_code"]
        if code not in series:
            continue
        if price_at(series[code], snapshot, code) is None:
            continue
        codes.append(code)
        rows.append(row)
    # Aggregate duplicate ETF proxies while preserving first-ranked metadata.
    seen = set()
    unique_codes = []
    unique_rows = []
    for code, row in zip(codes, rows):
        if code in seen:
            continue
        seen.add(code)
        unique_codes.append(code)
        unique_rows.append(row)
    return unique_codes, unique_rows


def portfolio_return(codes: list[str], series: dict[str, list[tuple[dt.date, float]]], start: dt.date, end: dt.date) -> float:
    if not codes:
        return 0.0
    returns = []
    for code in codes:
        value = period_return(series[code], start, end, code)
        if value is not None:
            returns.append(value)
    return sum(returns) / len(returns) if returns else 0.0


def run_case(
    conn,
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    rule: ProxyRule,
    phase: int,
    lag: int,
    include_rows: bool,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    current_codes: list[str] = []
    current_mapping: list[dict[str, Any]] = []
    rows = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
    for idx, (start_snapshot, end_snapshot) in enumerate(periods):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        if idx % rule.interval_months == 0 or not current_codes:
            current_codes, current_mapping = choose_etfs(conn, start_snapshot, rule, series)
        ret = portfolio_return(current_codes, series, start_exec, end_exec)
        capital *= 1.0 + ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "start_snapshot": start_snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "capital": capital,
                    "period_return": ret,
                    "drawdown": capital / peak - 1.0,
                    "holdings": ",".join(current_codes) if current_codes else "CASH",
                    "mapping": current_mapping,
                }
            )
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


def build_rules(quick: bool) -> list[ProxyRule]:
    suffixes = ["all", "CSI"] if quick else ["all", "CSI", "SI"]
    intervals = [1, 3, 12]
    top_ns = [1, 3, 5, 10]
    market_months = [3, 6]
    market_thresholds = [-1.0, -0.03, 0.0]
    min_corrs = [0.70]
    if quick:
        intervals = [1, 3, 12]
        top_ns = [1, 3, 5]
        market_months = [6]
        market_thresholds = [-1.0, -0.03]
    rules = []
    for suffix in suffixes:
        for interval in intervals:
            for top_n in top_ns:
                for market_month in market_months:
                    for market_threshold in market_thresholds:
                        for min_corr in min_corrs:
                            cash_tag = "cash" if market_threshold > -0.99 else "full"
                            rules.append(
                                ProxyRule(
                                    name=(
                                        f"proxy_{suffix}_i{interval}_top{top_n}_mk{market_month}"
                                        f"_mt{int(market_threshold*100):+03d}".replace("+", "p").replace("-", "n")
                                        + f"_{cash_tag}"
                                    ),
                                    suffix=suffix,
                                    interval_months=interval,
                                    top_n=top_n,
                                    market_trend_months=market_month,
                                    market_trend_gt=market_threshold,
                                    allow_cash_defense=market_threshold > -0.99,
                                    lookback_days=504,
                                    min_corr=min_corr,
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
    return [
        {
            "rule": item["rule"],
            "cases": [{key: value for key, value in case.items() if key != "rows"} for case in item["cases"]],
            "summary": item["summary"],
        }
        for item in results
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest CSI/SI scorecard recommendations mapped to ETF proxies.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    conn = get_connection()
    try:
        series = load_series(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        results = []
        for rule in build_rules(args.quick):
            cases = [
                run_case(conn, series, trade_dates, rule, phase, lag, include_rows=not args.summary_only)
                for phase in MONTH_PHASES
                for lag in EXECUTION_LAGS
            ]
            results.append({"rule": asdict(rule), "cases": cases, "summary": summarize(cases)})
    finally:
        conn.close()

    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_proxy_etf_backtest"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    payload = {
        "objective": "CSI/SI scorecard recommendations mapped to domestic passive ETF proxies.",
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
            writer.writerow({**item["rule"], **item["summary"]})

    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<36} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:8.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"median={s['median_final_capital_wan']:8.1f}w"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    best = results[0]["summary"]
    return 0 if best["pass_count"] == best["count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

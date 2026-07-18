#!/usr/bin/env python3
"""Search scorecard-sized CSI/SI passive ETF proxy portfolios.

This variant treats cash as the market scorecard's uninvested allocation.  The
scorecard controls the ETF exposure percentage, while the CSI/SI annual ranking
selects the domestic passive ETF proxies used for the invested sleeve.
"""

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

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import AdapterOptions, load_scorecard_inputs
from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    apply_year_for_snapshot,
    month_end_shift,
    monthly_boundaries,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    RiskRule,
    apply_rule,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.map_csi_to_etf_proxy import broad_proxy_candidates, exact_etf_candidates, resolve_etf_proxy, suffix_like

OUT_DIR = ROOT / "data" / "backtests"
START_YEAR = 2006
END_YEAR = 2025
MONTH_PHASES = list(range(12))
EXECUTION_LAGS = [0, 1, 3, 5]
CASH_PERIOD_RETURN = 0.0

PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
SCORECARD_CACHE: dict[tuple[int, dt.date], dict[str, Any]] = {}
MAPPING_CACHE: dict[tuple[str, dt.date, int, float, bool], dict[str, Any] | None] = {}
RECOMMENDATION_CACHE: dict[tuple[int, str, int], list[dict[str, Any]]] = {}
HYBRID_HOLDINGS_CACHE: dict[int, list[dict[str, Any]]] | None = None
SLEEVE_CACHE: dict[tuple[int, dt.date, str, str, int, int, float], list[dict[str, Any]]] = {}
HYBRID_HOLDINGS_CSV = ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_holdings.csv"


@dataclass(frozen=True)
class MarketAllocationRule:
    name: str
    selector: str
    suffix: str
    interval_months: int
    top_n: int
    risk_rule: RiskRule
    max_equity_pct: float
    trend_months: int
    trend_lte: float
    trend_cap_pct: float
    lookback_days: int = 504
    min_corr: float = 0.70
    use_correlation_proxy: bool = False


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


def period_return(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date, code: str | None = None) -> float | None:
    start_px = price_at(rows, start, code)
    end_px = price_at(rows, end, code)
    if start_px is None or end_px is None or start_px <= 0:
        return None
    return end_px / start_px - 1.0


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


def scorecard_base(conn, snapshot: dt.date) -> dict[str, Any]:
    apply_year = apply_year_for_snapshot(snapshot)
    key = (apply_year, snapshot)
    if key in SCORECARD_CACHE:
        return SCORECARD_CACHE[key]
    inputs = load_scorecard_inputs(snapshot, options=AdapterOptions(), conn=conn)
    result = evaluate_scorecard(apply_year, inputs)
    detail = {
        "apply_year": apply_year,
        "snapshot_date": snapshot.isoformat(),
        "score": int(result.total_score),
        "band": result.band,
        "base_equity_pct": float(result.target_equity_pct),
        "known_inputs": {
            "cs300_6m_return": inputs.cs300_6m_return,
            "pmi_mfg_3m_avg": inputs.pmi_mfg_3m_avg,
            "pmi_below_52_months": inputs.pmi_below_52_months,
            "us10y_chg_12m_bp": inputs.us10y_chg_12m_bp,
            "enterprise_boom_index": inputs.enterprise_boom_index,
            "ppi_yoy": inputs.ppi_yoy,
            "rate_cum_bp_12m": inputs.rate_cum_bp_12m,
        },
    }
    SCORECARD_CACHE[key] = detail
    return detail


def target_equity_pct(
    conn,
    series: dict[str, list[tuple[dt.date, float]]],
    snapshot: dt.date,
    rule: MarketAllocationRule,
) -> tuple[float, dict[str, Any], list[str]]:
    detail = scorecard_base(conn, snapshot)
    target = apply_rule(rule.risk_rule, int(detail["score"]), float(detail["base_equity_pct"]))
    reasons = [rule.risk_rule.name]
    target = min(target, rule.max_equity_pct)
    if target < float(detail["base_equity_pct"]):
        reasons.append("risk_rule_or_max_cap")
    if rule.trend_months > 0:
        trend_start = month_end_shift(snapshot, -rule.trend_months)
        trend = period_return(series[CS300_CODE], trend_start, snapshot, CS300_CODE)
        trend = trend if trend is not None else 0.0
        if trend <= rule.trend_lte:
            target = min(target, rule.trend_cap_pct)
            reasons.append(f"cs300_{rule.trend_months}m_trend_cap")
    return max(0.0, min(100.0, target)), detail, reasons


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
                "final_score": float(score) if score is not None else None,
                "best_theme": str(theme or ""),
            }
            for rank, code, name, score, theme in cur.fetchall()
        ]
    RECOMMENDATION_CACHE[key] = rows
    return rows


def load_hybrid_holdings() -> dict[int, list[dict[str, Any]]]:
    global HYBRID_HOLDINGS_CACHE
    if HYBRID_HOLDINGS_CACHE is not None:
        return HYBRID_HOLDINGS_CACHE
    out: dict[int, list[dict[str, Any]]] = {}
    with HYBRID_HOLDINGS_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            out.setdefault(int(row["year"]), []).append(
                {
                    "rank": int(row["rank"]),
                    "index_code": str(row["ts_code"]),
                    "index_name": str(row["index_name"]),
                    "final_score": float(row["momentum_12m"]) if row.get("momentum_12m") else None,
                    "best_theme": str(row.get("best_theme") or ""),
                    "selector": "csi_regime_momentum_hybrid",
                    "regime": str(row.get("regime") or ""),
                    "selection_rule": str(row.get("selection_rule") or ""),
                }
            )
    for rows in out.values():
        rows.sort(key=lambda item: item["rank"])
    HYBRID_HOLDINGS_CACHE = out
    return out


def proxy_for_index(conn, index_code: str, snapshot: dt.date, rule: MarketAllocationRule) -> dict[str, Any] | None:
    proxy_as_of = dt.date(apply_year_for_snapshot(snapshot) - 1, 12, 31) if rule.use_correlation_proxy else snapshot
    key = (index_code, proxy_as_of, rule.lookback_days, rule.min_corr, rule.use_correlation_proxy)
    if key in MAPPING_CACHE:
        return MAPPING_CACHE[key]
    with conn.cursor() as cur:
        if rule.use_correlation_proxy:
            proxy = resolve_etf_proxy(cur, index_code, proxy_as_of, rule.lookback_days, rule.min_corr)
        else:
            exact = exact_etf_candidates(cur, index_code, proxy_as_of)
            proxy = exact[0] if exact else None
            if proxy is None:
                broad = broad_proxy_candidates(cur, proxy_as_of)
                proxy = broad[0] if broad else None
    MAPPING_CACHE[key] = proxy
    return proxy


def sleeve_mapping(conn, snapshot: dt.date, rule: MarketAllocationRule) -> list[dict[str, Any]]:
    year = apply_year_for_snapshot(snapshot)
    key = (year, snapshot, rule.selector, rule.suffix, rule.top_n, rule.lookback_days, rule.min_corr)
    if key in SLEEVE_CACHE:
        return SLEEVE_CACHE[key]
    rows = []
    if rule.selector == "hybrid" and year in load_hybrid_holdings():
        recs = load_hybrid_holdings()[year][: rule.top_n]
    else:
        recs = load_recommendations(conn, year, rule.suffix, rule.top_n)
    for rec in recs:
        proxy = proxy_for_index(conn, rec["index_code"], snapshot, rule)
        if proxy:
            rows.append({**rec, **proxy})
    SLEEVE_CACHE[key] = rows
    return rows


def choose_etfs(
    conn,
    snapshot: dt.date,
    rule: MarketAllocationRule,
    series: dict[str, list[tuple[dt.date, float]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    codes = []
    rows = []
    for row in sleeve_mapping(conn, snapshot, rule):
        code = row["etf_code"]
        if code not in series:
            continue
        if price_at(series[code], snapshot, code) is None:
            continue
        codes.append(code)
        rows.append(row)
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


def sleeve_return(codes: list[str], series: dict[str, list[tuple[dt.date, float]]], start: dt.date, end: dt.date) -> float:
    if not codes:
        return 0.0
    values = []
    for code in codes:
        value = period_return(series[code], start, end, code)
        if value is not None:
            values.append(value)
    return sum(values) / len(values) if values else 0.0


def run_case(
    conn,
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    rule: MarketAllocationRule,
    phase: int,
    lag: int,
    include_rows: bool,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows = []
    current_codes: list[str] = []
    current_mapping: list[dict[str, Any]] = []
    current_target = 0.0
    current_scorecard: dict[str, Any] | None = None
    current_reasons: list[str] = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
    for idx, (start_snapshot, end_snapshot) in enumerate(periods):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        if idx % rule.interval_months == 0:
            current_codes, current_mapping = choose_etfs(conn, start_snapshot, rule, series)
            current_target, current_scorecard, current_reasons = target_equity_pct(conn, series, start_snapshot, rule)
            if not current_codes:
                current_target = 0.0
                current_reasons = [*current_reasons, "no_investable_proxy"]
        etf_ret = sleeve_return(current_codes, series, start_exec, end_exec)
        portfolio_ret = current_target / 100.0 * etf_ret + (1.0 - current_target / 100.0) * CASH_PERIOD_RETURN
        capital *= 1.0 + portfolio_ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            scorecard = current_scorecard or {}
            rows.append(
                {
                    "start_snapshot": start_snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "apply_year": scorecard.get("apply_year"),
                    "score": scorecard.get("score"),
                    "band": scorecard.get("band"),
                    "base_equity_pct": scorecard.get("base_equity_pct"),
                    "target_equity_pct": current_target,
                    "target_cash_pct": 100.0 - current_target,
                    "etf_sleeve_return": etf_ret,
                    "portfolio_return": portfolio_ret,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "holdings": ",".join(current_codes) if current_codes else "CASH",
                    "target_reasons": current_reasons,
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


def build_rules(
    quick: bool,
    selector_filter: str,
    use_correlation_proxy: bool,
    core: bool,
) -> list[MarketAllocationRule]:
    risk_rules = [
        RiskRule("base_scorecard", -99, 0.0, 99, 100.0),
        RiskRule("previous_score0_floor95", 0, 95.0, 99, 100.0),
        RiskRule("risk_off_score_positive_floor95", -3, 95.0, 0, 0.0),
        RiskRule("risk10_score_positive_floor95", -3, 95.0, 0, 10.0),
        RiskRule("risk20_score_positive_floor100", -2, 100.0, 0, 20.0),
        RiskRule("risk30_score_gt1_floor100", -2, 100.0, 1, 30.0),
    ]
    if core:
        risk_rules = [
            RiskRule("base_scorecard", -99, 0.0, 99, 100.0),
            RiskRule("previous_score0_floor95", 0, 95.0, 99, 100.0),
            RiskRule("risk30_score_gt1_floor100", -2, 100.0, 1, 30.0),
        ]
    selectors = ["hybrid", "recommendation"] if selector_filter == "all" else [selector_filter]
    suffixes = ["all", "CSI"] if quick else ["all", "CSI", "SI"]
    intervals = [3, 12] if core else [1, 3, 12]
    top_ns = [5] if core else ([1, 3, 5] if quick else [1, 3, 5, 10])
    trend_caps = [
        (0, -1.0, 100.0, "notrend"),
        (6, -0.08, 0.0, "t6n08c0"),
        (6, -0.05, 30.0, "t6n05c30"),
        (3, -0.05, 0.0, "t3n05c0"),
    ]
    if core:
        trend_caps = [
            (6, -0.05, 30.0, "t6n05c30"),
            (3, -0.05, 0.0, "t3n05c0"),
        ]
    max_equities = [100.0] if quick else [80.0, 100.0]
    rules = []
    for selector in selectors:
        for suffix in suffixes:
            for interval in intervals:
                for top_n in top_ns:
                    for risk_rule in risk_rules:
                        for max_equity in max_equities:
                            for trend_months, trend_lte, trend_cap_pct, trend_name in trend_caps:
                                name = (
                                    f"ma_{selector}_{suffix}_i{interval}_top{top_n}_{risk_rule.name}"
                                    f"_max{int(max_equity)}_{trend_name}"
                                )
                                rules.append(
                                    MarketAllocationRule(
                                        name=name,
                                        selector=selector,
                                        suffix=suffix,
                                        interval_months=interval,
                                        top_n=top_n,
                                        risk_rule=risk_rule,
                                        max_equity_pct=max_equity,
                                    trend_months=trend_months,
                                    trend_lte=trend_lte,
                                    trend_cap_pct=trend_cap_pct,
                                    use_correlation_proxy=use_correlation_proxy,
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
    parser = argparse.ArgumentParser(description="Search market-scorecard-sized CSI/SI domestic passive ETF proxies.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--selector", choices=["all", "hybrid", "recommendation"], default="all")
    parser.add_argument("--use-correlation-proxy", action="store_true")
    parser.add_argument("--core", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    conn = get_connection()
    try:
        series = load_series(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        results = []
        for rule in build_rules(args.quick, args.selector, args.use_correlation_proxy, args.core):
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
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_market_allocation_proxy_etf"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    payload = {
        "objective": "Market scorecard controls ETF exposure; CSI/SI scorecard selects domestic passive ETF proxies.",
        "constraints": {
            "invested_assets": "A-share investable domestic passive index ETFs only",
            "cash_treatment": "uninvested allocation from the market scorecard; 0% assumed return",
            "no_overseas": True,
            "no_derivatives": True,
            "no_leverage": True,
        },
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "start_year": START_YEAR,
        "end_year": END_YEAR,
        "month_phases": MONTH_PHASES,
        "execution_lags": EXECUTION_LAGS,
        "results": strip_rows(results) if args.summary_only else results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["name", *list(results[0]["rule"].keys())[1:], *list(results[0]["summary"].keys())]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            row["risk_rule"] = json.dumps(row["risk_rule"], ensure_ascii=False, sort_keys=True)
            writer.writerow(row)

    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<72} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:8.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"median={s['median_final_capital_wan']:8.1f}w"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    best = results[0]["summary"]
    return 0 if best["pass_count"] == best["count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

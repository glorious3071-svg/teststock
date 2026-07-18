#!/usr/bin/env python3
"""Search TIPP wrappers for passive ETF-only CSI scorecard portfolios.

This is the strict mandate variant: invested assets are domestic SH/SZ passive
index ETFs mapped from the CSI phase-ensemble sleeves.  No option, futures,
overseas, crypto, or synthetic package returns are included.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    apply_year_for_snapshot,
    cash_return,
    holding_codes_for_snapshot,
    load_price_series as load_index_price_series,
    month_end_shift,
    monthly_boundaries,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_phase_ensemble import (
    RULES as PHASE_RULES,
    PhaseEnsembleRule,
    base_target_for_detail,
    scorecard_snapshot,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.map_csi_to_etf_proxy import broad_proxy_candidates, exact_etf_candidates, resolve_etf_proxy

OUT_DIR = ROOT / "data" / "backtests"
DOMESTIC_PHASE_RULE_NAMES = [
    "phase12_lever120_cash",
    "phase12_lever150_cash",
    "phase12_guard60_cash",
    "phase12_guard40_cash",
    "phase12_lever120_dd_cash",
]
PHASE_RULE_BY_NAME = {rule.name: rule for rule in PHASE_RULES}
PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
PROXY_CACHE: dict[tuple[str, dt.date, bool, int, float], dict[str, Any] | None] = {}
SLEEVE_CACHE: dict[tuple[Any, ...], tuple[float, float, list[dict[str, Any]], list[str]]] = {}


@dataclass(frozen=True)
class PassiveEtfTippRule:
    name: str
    phase_rule_name: str
    top_per_sleeve: int
    use_correlation_proxy: bool
    lookback_days: int
    min_corr: float
    floor_pct: float
    multiplier: float
    max_wrapper_exposure: float
    min_wrapper_exposure: float
    stop_loss_pct: float
    post_stop_exposure: float
    drawdown_guard_lte: float
    drawdown_guard_exposure: float


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date, code: str | None = None) -> float | None:
    if code is not None:
        key = (code, boundary)
        if key in PRICE_CACHE:
            return PRICE_CACHE[key]
    idx = bisect_right(rows, (boundary, float("inf"))) - 1
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


def daily_path(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date, code: str | None = None) -> list[tuple[dt.date, float]]:
    start_px = price_at(rows, start, code)
    if start_px is None or start_px <= 0:
        return []
    left = bisect_right(rows, (start, float("inf")))
    right = bisect_right(rows, (end, float("inf")))
    return [(day, close / start_px - 1.0) for day, close in rows[left:right] if close and close > 0]


def load_fund_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    series: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.ts_code, f.trade_date, f.close
            FROM fund_daily f
            JOIN passive_etf e ON e.ts_code=f.ts_code
            WHERE f.close IS NOT NULL
              AND e.list_status='L'
              AND (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND e.ts_code NOT LIKE '%%.OF'
            ORDER BY f.ts_code, f.trade_date
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


def proxy_for_index(
    cur,
    index_code: str,
    snapshot: dt.date,
    use_correlation_proxy: bool,
    lookback_days: int,
    min_corr: float,
) -> dict[str, Any] | None:
    proxy_as_of = dt.date(apply_year_for_snapshot(snapshot) - 1, 12, 31) if use_correlation_proxy else snapshot
    key = (index_code, proxy_as_of, use_correlation_proxy, lookback_days, min_corr)
    if key in PROXY_CACHE:
        return PROXY_CACHE[key]
    if use_correlation_proxy:
        proxy = resolve_etf_proxy(cur, index_code, proxy_as_of, lookback_days, min_corr)
    else:
        exact = exact_etf_candidates(cur, index_code, proxy_as_of)
        proxy = exact[0] if exact else None
        if proxy is None:
            broad = broad_proxy_candidates(cur, proxy_as_of)
            proxy = broad[0] if broad else None
    PROXY_CACHE[key] = proxy
    return proxy


def aggregate_target(values: list[float], mode: str) -> float:
    if not values:
        return 0.0
    if mode == "max":
        return max(values)
    if mode == "min":
        return min(values)
    if mode == "median":
        return statistics.median(values)
    return statistics.mean(values)


def apply_phase_caps(
    rule: PhaseEnsembleRule,
    target: float,
    index_series: dict[str, list[tuple[dt.date, float]]],
    snapshot: dt.date,
    portfolio_drawdown: float,
    current_detail: dict[str, Any],
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    known = current_detail["known_inputs"]
    cs300_trend = period_return(index_series[CS300_CODE], month_end_shift(snapshot, -rule.trend_months), snapshot, CS300_CODE) or 0.0
    cs300_3m = period_return(index_series[CS300_CODE], month_end_shift(snapshot, -3), snapshot, CS300_CODE) or 0.0
    cs300_6m = period_return(index_series[CS300_CODE], month_end_shift(snapshot, -6), snapshot, CS300_CODE) or 0.0
    if target >= rule.extreme_rally_cap_pct and cs300_6m >= rule.extreme_rally_6m_gte and cs300_3m >= rule.extreme_rally_3m_gte:
        target = min(target, rule.extreme_rally_cap_pct)
        reasons.append("extreme_rally_cap")
    if (
        target >= rule.weak_repair_cap_pct
        and int(current_detail["score"]) <= rule.weak_repair_score_lte
        and (known.get("pmi_below_52_months") or 0) >= rule.weak_repair_pmi_below_52_months_gte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < rule.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < rule.weak_repair_ppi_lt
    ):
        target = min(target, rule.weak_repair_cap_pct)
        reasons.append("weak_repair_cap")
    if (
        target >= rule.stagflation_cap_pct
        and (known.get("pmi_below_52_months") or 0) >= rule.stagflation_pmi_below_52_months_gte
        and (known.get("ppi_yoy") or 0.0) >= rule.stagflation_ppi_gte
        and cs300_6m <= rule.stagflation_cs300_6m_lte
    ):
        target = min(target, rule.stagflation_cap_pct)
        reasons.append("stagflation_cap")
    if cs300_trend <= rule.trend_lte:
        target = min(target, rule.trend_cap_pct)
        reasons.append("cs300_trend_cap")
    if portfolio_drawdown <= rule.drawdown_lte:
        target = min(target, rule.drawdown_cap_pct)
        reasons.append("portfolio_drawdown_cap")
    return target, reasons


def etf_phase_state(
    conn,
    index_series: dict[str, list[tuple[dt.date, float]]],
    fund_series: dict[str, list[tuple[dt.date, float]]],
    holdings: dict[int, list[str]],
    rule: PassiveEtfTippRule,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    portfolio_drawdown: float,
) -> tuple[float, float, list[dict[str, Any]], list[str]]:
    phase_rule = PHASE_RULE_BY_NAME[rule.phase_rule_name]
    drawdown_affects_target = phase_rule.drawdown_lte > -0.99
    cache_key = (
        rule.phase_rule_name,
        rule.top_per_sleeve,
        rule.use_correlation_proxy,
        rule.lookback_days,
        rule.min_corr,
        snapshot,
        start_exec,
        end_exec,
        round(portfolio_drawdown, 4) if drawdown_affects_target else None,
    )
    if cache_key in SLEEVE_CACHE:
        return SLEEVE_CACHE[cache_key]
    sleeve_targets: list[float] = []
    sleeve_returns: list[float] = []
    sleeves: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for offset in phase_rule.sleeve_offsets:
            sleeve_snapshot = month_end_shift(snapshot, -offset)
            index_codes = holding_codes_for_snapshot(holdings, sleeve_snapshot)
            if rule.top_per_sleeve > 0:
                index_codes = index_codes[: rule.top_per_sleeve]
            detail = scorecard_snapshot(conn, sleeve_snapshot)
            sleeve_targets.append(base_target_for_detail(detail, phase_rule))
            mapped = []
            returns = []
            for index_code in index_codes:
                proxy = proxy_for_index(cur, index_code, sleeve_snapshot, rule.use_correlation_proxy, rule.lookback_days, rule.min_corr)
                if not proxy:
                    continue
                etf_code = proxy["etf_code"]
                if etf_code not in fund_series:
                    continue
                ret = period_return(fund_series[etf_code], start_exec, end_exec, etf_code)
                if ret is None:
                    continue
                returns.append(ret)
                mapped.append({**proxy, "index_code": index_code})
            sleeve_return = statistics.mean(returns) if returns else 0.0
            sleeve_returns.append(sleeve_return)
            sleeves.append(
                {
                    "offset_months": offset,
                    "snapshot": sleeve_snapshot.isoformat(),
                    "apply_year": apply_year_for_snapshot(sleeve_snapshot),
                    "score": detail["score"],
                    "base_target_equity_pct": sleeve_targets[-1],
                    "etf_return": sleeve_return,
                    "mapped_etfs": mapped,
                }
            )
    current_detail = scorecard_snapshot(conn, snapshot)
    target = aggregate_target(sleeve_targets, phase_rule.target_mode) * phase_rule.target_multiplier
    target = min(phase_rule.max_equity_pct, max(phase_rule.min_equity_pct, target))
    target, reasons = apply_phase_caps(phase_rule, target, index_series, snapshot, portfolio_drawdown, current_detail)
    equity_return = statistics.mean(sleeve_returns) if sleeve_returns else 0.0
    result = (target, equity_return, sleeves, reasons)
    SLEEVE_CACHE[cache_key] = result
    return result


def build_rules(quick: bool, aggressive: bool = False, max_rules: int = 0) -> list[PassiveEtfTippRule]:
    phase_names = ["phase12_lever150_cash", "phase12_lever120_cash", "phase12_guard60_cash"]
    top_values = [3, 5, 10]
    floors = [0.84, 0.86, 0.88, 0.90]
    multipliers = [6.0, 8.0, 10.0, 12.0]
    max_exposures = [1.5, 2.0, 2.5]
    stops = [-0.005, -0.01, -0.015, -0.02]
    drawdown_guards = [(-0.065, 0.0), (-0.08, 0.0), (-0.08, 0.02)]
    if quick:
        phase_names = ["phase12_lever150_cash"]
        top_values = [5, 10]
        floors = [0.86, 0.88]
        multipliers = [8.0, 10.0]
        max_exposures = [2.0]
        stops = [-0.005, -0.01]
        drawdown_guards = [(-0.065, 0.0), (-0.08, 0.02)]
    if aggressive:
        phase_names = ["phase12_lever150_cash", "phase12_lever120_cash"]
        top_values = [5, 10]
        floors = [0.88, 0.90, 0.92]
        multipliers = [12.0, 16.0, 20.0]
        max_exposures = [3.0, 4.0, 5.0]
        stops = [-0.015, -0.025, -0.04, -1.0]
        drawdown_guards = [(-0.07, 0.0), (-0.08, 0.0), (-0.09, 0.0), (-0.08, 0.05)]
        if quick:
            phase_names = ["phase12_lever150_cash"]
            floors = [0.88, 0.90]
            multipliers = [12.0, 16.0]
            max_exposures = [3.0, 4.0]
            stops = [-0.025, -0.04, -1.0]
            drawdown_guards = [(-0.08, 0.0), (-0.09, 0.0)]
    rules: list[PassiveEtfTippRule] = []
    for phase_name in phase_names:
        for top in top_values:
            for use_corr in [False, True]:
                for floor in floors:
                    for multiplier in multipliers:
                        for max_exp in max_exposures:
                            for stop in stops:
                                for guard, guard_exp in drawdown_guards:
                                    name = (
                                        f"petftipp_{phase_name.replace('phase12_', 'p12_')}"
                                        f"_top{top}_corr{int(use_corr)}"
                                        f"_f{int(floor * 100)}_m{int(multiplier * 10)}"
                                        f"_x{int(max_exp * 100)}_st{int(abs(stop) * 1000):03d}"
                                        f"_gd{int(abs(guard) * 1000):03d}e{int(guard_exp * 100)}"
                                    )
                                    rules.append(
                                        PassiveEtfTippRule(
                                            name=name,
                                            phase_rule_name=phase_name,
                                            top_per_sleeve=top,
                                            use_correlation_proxy=use_corr,
                                            lookback_days=504,
                                            min_corr=0.70,
                                            floor_pct=floor,
                                            multiplier=multiplier,
                                            max_wrapper_exposure=max_exp,
                                            min_wrapper_exposure=0.0,
                                            stop_loss_pct=stop,
                                            post_stop_exposure=0.0,
                                            drawdown_guard_lte=guard,
                                            drawdown_guard_exposure=guard_exp,
                                        )
                                    )
                                    if max_rules and len(rules) >= max_rules:
                                        return rules
    return rules


def stopped_return(period_return_value: float, stop_path: list[tuple[dt.date, float]], exposure: float, safe_return: float, rule: PassiveEtfTippRule) -> tuple[float, bool]:
    if exposure <= 0.0:
        return safe_return, False
    if not stop_path:
        return exposure * period_return_value + (1.0 - exposure) * safe_return, False
    total = max(len(stop_path), 1)
    for idx, (_day, risky_to_date) in enumerate(stop_path, start=1):
        fraction = min(1.0, idx / total)
        blended_to_date = exposure * risky_to_date + (1.0 - exposure) * safe_return * fraction
        if blended_to_date <= rule.stop_loss_pct:
            remaining = max(0.0, 1.0 - fraction)
            risky_after = period_return_value - risky_to_date
            after_stop = rule.post_stop_exposure * risky_after + (1.0 - rule.post_stop_exposure) * safe_return * remaining
            return blended_to_date + after_stop, True
    return exposure * period_return_value + (1.0 - exposure) * safe_return, False


def run_case(
    conn,
    index_series: dict[str, list[tuple[dt.date, float]]],
    fund_series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    holdings: dict[int, list[str]],
    rule: PassiveEtfTippRule,
    phase: int,
    lag: int,
    include_rows: bool,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    stop_months = 0
    exposures: list[float] = []
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        drawdown = capital / peak - 1.0
        target_pct, equity_return, sleeves, reasons = etf_phase_state(
            conn,
            index_series,
            fund_series,
            holdings,
            rule,
            start_snapshot,
            start_exec,
            end_exec,
            drawdown,
        )
        equity_weight = target_pct / 100.0
        safe_ret = cash_return(start_exec, end_exec)
        base_return = equity_weight * equity_return + (1.0 - equity_weight) * safe_ret
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        wrapper_exposure = min(
            rule.max_wrapper_exposure,
            max(rule.min_wrapper_exposure, rule.multiplier * cushion / max(capital, 1.0)),
        )
        if drawdown <= rule.drawdown_guard_lte:
            wrapper_exposure = min(wrapper_exposure, rule.drawdown_guard_exposure)
        stop_path = daily_path(index_series[CS300_CODE], start_exec, end_exec, CS300_CODE)
        period_ret, stopped = stopped_return(base_return, stop_path, wrapper_exposure, safe_ret, rule)
        if stopped:
            stop_months += 1
        exposures.append(wrapper_exposure * equity_weight)
        capital *= 1.0 + period_ret
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
                    "target_equity_pct": target_pct,
                    "wrapper_exposure": wrapper_exposure,
                    "effective_etf_exposure": wrapper_exposure * equity_weight,
                    "equity_return": equity_return,
                    "base_return": base_return,
                    "period_return": period_ret,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "stopped": stopped,
                    "reasons": reasons,
                    "sleeves": sleeves,
                }
            )
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": f"{rule.name}_phase{phase}_lag{lag}",
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_effective_etf_exposure": statistics.mean(exposures) if exposures else 0.0,
        "stop_months": stop_months,
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
        "median_effective_etf_exposure": statistics.median(item["avg_effective_etf_exposure"] for item in cases),
        "median_stop_months": statistics.median(item["stop_months"] for item in cases),
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
    parser = argparse.ArgumentParser(description="Search passive ETF-only TIPP wrappers.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--max-rules", type=int, default=0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    conn = get_connection()
    try:
        index_series = load_index_price_series(conn)
        fund_series = load_fund_series(conn)
        trade_dates = [day for day, _px in index_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        results = []
        for rule in build_rules(args.quick, args.aggressive, args.max_rules):
            cases = [
                run_case(conn, index_series, fund_series, trade_dates, holdings, rule, phase, lag, include_rows=not args.summary_only)
                for phase in MONTH_PHASES
                for lag in EXECUTION_LAGS
            ]
            summary = summarize(cases)
            results.append({"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]})
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
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_passive_etf_tipp"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    payload = {
        "objective": "Passive ETF-only TIPP wrapper over market scorecard and CSI phase-ensemble selection.",
        "constraints": {
            "invested_assets": "Domestic SH/SZ passive index ETFs from fund_daily only",
            "cash_treatment": "uninvested residual or financing residual from exposure sizing",
            "no_overseas_assets": True,
            "no_options": True,
            "no_futures": True,
            "no_crypto": True,
            "no_synthetic_package_returns": True,
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
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = ["name", *[key for key in asdict(build_rules(True, False, 1)[0]).keys() if key != "name"], *list(results[0]["summary"].keys())]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<90} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:9.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"med={s['median_final_capital_wan']:9.1f}w exp={s['median_effective_etf_exposure']:.2f}"
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

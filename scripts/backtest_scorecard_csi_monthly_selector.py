#!/usr/bin/env python3
"""Backtest monthly CSI selection using ex-ante price features.

The annual CSI selector is fragile to month-start drift because one calendar cut
point can own the entire year.  This experiment tests a lower-latency CSI return
engine: each month ranks available CSI indices using only price features known
at the rebalance snapshot, then compounds through the same 12 month phases and
4 execution lags used by the strict generalization harness.

Before there is enough CSI cross-section history, the equity leg falls back to
CSI300.  This is a return-engine experiment, not a production rule.
"""

from __future__ import annotations

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

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
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
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import DEFAULT_RULE, TARGET_MDD, scorecard_detail
from scripts.backtest_scorecard_csi_vol_target import US10Y_PROXY, load_us10y_yields, us10y_duration_return

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_monthly_selector_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_monthly_selector_search.csv"


@dataclass(frozen=True)
class MonthlySelectorRule:
    name: str
    score_model: str
    top_k: int
    min_universe: int
    min_history_months: int
    target_multiplier: float
    max_equity_pct: float
    opportunity_score_lte: int
    opportunity_floor_pct: float
    cs300_3m_lte: float = -1.0
    cs300_3m_cap_pct: float = 999.0
    cs300_6m_lte: float = -1.0
    cs300_6m_cap_pct: float = 999.0
    portfolio_dd_lte: float = -1.0
    portfolio_dd_cap_pct: float = 999.0
    defensive_asset: str = "cash"


def build_rules() -> list[MonthlySelectorRule]:
    rules: list[MonthlySelectorRule] = []
    models = ["mom12", "mom6_12", "quality", "lowvol_quality", "breakout"]
    for model in models:
        for top_k in [5, 10, 20]:
            for mult, max_equity in [(1.0, 100.0), (1.2, 120.0), (1.5, 150.0)]:
                base = f"{model}_top{top_k}_lev{int(max_equity)}"
                rules.append(
                    MonthlySelectorRule(
                        base,
                        model,
                        top_k,
                        max(top_k * 2, 20),
                        12,
                        mult,
                        max_equity,
                        -3,
                        min(max_equity, 95.0 * mult),
                    )
                )
                rules.append(
                    MonthlySelectorRule(
                        f"{base}_trend_guard",
                        model,
                        top_k,
                        max(top_k * 2, 20),
                        12,
                        mult,
                        max_equity,
                        -3,
                        min(max_equity, 95.0 * mult),
                        cs300_3m_lte=-0.08,
                        cs300_3m_cap_pct=min(50.0, max_equity),
                        cs300_6m_lte=-0.12,
                        cs300_6m_cap_pct=min(65.0, max_equity),
                    )
                )
                rules.append(
                    MonthlySelectorRule(
                        f"{base}_risk_guard_us10y",
                        model,
                        top_k,
                        max(top_k * 2, 20),
                        12,
                        mult,
                        max_equity,
                        -3,
                        min(max_equity, 95.0 * mult),
                        cs300_3m_lte=-0.08,
                        cs300_3m_cap_pct=min(50.0, max_equity),
                        cs300_6m_lte=-0.12,
                        cs300_6m_cap_pct=min(65.0, max_equity),
                        portfolio_dd_lte=-0.08,
                        portfolio_dd_cap_pct=min(35.0, max_equity),
                        defensive_asset=US10Y_PROXY,
                    )
                )
    return rules


RULES = build_rules()


def load_price_series(conn) -> dict[str, list[tuple[date, float]]]:
    series: dict[str, list[tuple[date, float]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, trade_date, close
            FROM index_daily
            WHERE (ts_code LIKE '%%.CSI' OR ts_code=%s)
              AND close IS NOT NULL
            ORDER BY ts_code, trade_date
            """,
            (CS300_CODE,),
        )
        for code, trade_date, close in cur.fetchall():
            series.setdefault(str(code), []).append((trade_date, float(close)))
    return series


def load_names(conn) -> dict[str, str]:
    names: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, MIN(index_name)
            FROM theme_index_map
            WHERE ts_code LIKE '%%.CSI'
            GROUP BY ts_code
            """
        )
        names.update({str(code): str(name or code) for code, name in cur.fetchall()})
    names.setdefault(CS300_CODE, "沪深300")
    return names


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def period_return(rows: list[tuple[date, float]], start: date, end: date) -> float | None:
    start_px = price_at(rows, start)
    end_px = price_at(rows, end)
    if not start_px or not end_px or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def trailing_daily_returns(rows: list[tuple[date, float]], start: date, end: date) -> list[float]:
    left = bisect_left(rows, (start, -math.inf))
    right = bisect_right(rows, (end, math.inf))
    window = rows[left:right]
    out = []
    for idx in range(1, len(window)):
        prev = window[idx - 1][1]
        cur = window[idx][1]
        if prev > 0 and cur > 0:
            out.append(cur / prev - 1.0)
    return out


def annualized_vol(rows: list[tuple[date, float]], start: date, end: date) -> float | None:
    rets = trailing_daily_returns(rows, start, end)
    if len(rets) < 30:
        return None
    return statistics.pstdev(rets) * math.sqrt(252.0)


def max_trailing_drawdown(rows: list[tuple[date, float]], start: date, end: date) -> float | None:
    left = bisect_left(rows, (start, -math.inf))
    right = bisect_right(rows, (end, math.inf))
    prices = [px for _day, px in rows[left:right] if px and px > 0]
    if len(prices) < 30:
        return None
    peak = prices[0]
    worst = 0.0
    for px in prices:
        peak = max(peak, px)
        worst = min(worst, px / peak - 1.0)
    return worst


def csi_universe(series: dict[str, list[tuple[date, float]]], snapshot: date, min_history_months: int) -> list[str]:
    lookback_start = month_end_shift(snapshot, -min_history_months)
    out = []
    for code, rows in series.items():
        if code == CS300_CODE or not code.endswith(".CSI"):
            continue
        if period_return(rows, lookback_start, snapshot) is not None and price_at(rows, snapshot) is not None:
            out.append(code)
    return out


def raw_index_features(series: dict[str, list[tuple[date, float]]], code: str, snapshot: date) -> dict[str, Any] | None:
    rows = series[code]
    r1 = period_return(rows, month_end_shift(snapshot, -1), snapshot)
    r3 = period_return(rows, month_end_shift(snapshot, -3), snapshot)
    r6 = period_return(rows, month_end_shift(snapshot, -6), snapshot)
    r12 = period_return(rows, month_end_shift(snapshot, -12), snapshot)
    vol6 = annualized_vol(rows, month_end_shift(snapshot, -6), snapshot)
    dd12 = max_trailing_drawdown(rows, month_end_shift(snapshot, -12), snapshot)
    if r6 is None or r12 is None or vol6 is None or dd12 is None:
        return None
    r1 = r1 or 0.0
    r3 = r3 or 0.0
    return {
        "momentum_1m": r1,
        "momentum_3m": r3,
        "momentum_6m": r6,
        "momentum_12m": r12,
        "vol_6m": vol6,
        "drawdown_12m": dd12,
    }


def score_features(features: dict[str, Any], model: str) -> float:
    r1 = float(features["momentum_1m"])
    r3 = float(features["momentum_3m"])
    r6 = float(features["momentum_6m"])
    r12 = float(features["momentum_12m"])
    vol6 = float(features["vol_6m"])
    dd12 = float(features["drawdown_12m"])
    if model == "mom12":
        score = r12
    elif model == "mom6_12":
        score = 0.55 * r12 + 0.45 * r6
    elif model == "quality":
        score = 0.45 * r12 + 0.35 * r6 + 0.20 * r3 - 0.35 * vol6 + 0.25 * dd12
    elif model == "lowvol_quality":
        score = 0.35 * r12 + 0.25 * r6 + 0.15 * r3 - 0.75 * vol6 + 0.35 * dd12
    elif model == "breakout":
        score = 0.30 * r12 + 0.45 * r6 + 0.35 * r3 - 0.20 * max(r1, 0.0) - 0.25 * vol6
    else:
        score = r12
    return score


def build_feature_cache(
    series: dict[str, list[tuple[date, float]]],
    snapshots: list[date],
) -> dict[date, list[tuple[str, dict[str, Any]]]]:
    csi_codes = [
        code
        for code, rows in series.items()
        if code.endswith(".CSI")
        and len(rows) >= 1_000
        and rows[-1][0] >= date(END_YEAR, 12, 31)
    ]
    cache: dict[date, list[tuple[str, dict[str, Any]]]] = {}
    for snapshot in sorted(set(snapshots)):
        rows = []
        for code in csi_codes:
            features = raw_index_features(series, code, snapshot)
            if features is not None:
                rows.append((code, features))
        cache[snapshot] = rows
    return cache


def select_codes(
    names: dict[str, str],
    selection_cache: dict[tuple[date, str, int, int], tuple[list[str], list[dict[str, Any]], str]],
    rule: MonthlySelectorRule,
    snapshot: date,
) -> tuple[list[str], list[dict[str, Any]], str]:
    key = (snapshot, rule.score_model, rule.top_k, rule.min_universe)
    return selection_cache.get(
        key,
        ([CS300_CODE], [{"rank": 1, "ts_code": CS300_CODE, "index_name": names.get(CS300_CODE, CS300_CODE)}], "fallback_cs300"),
    )


def build_selection_cache(
    names: dict[str, str],
    feature_cache: dict[date, list[tuple[str, dict[str, Any]]]],
    rules: list[MonthlySelectorRule],
) -> dict[tuple[date, str, int, int], tuple[list[str], list[dict[str, Any]], str]]:
    keys = {(rule.score_model, rule.top_k, rule.min_universe) for rule in rules}
    cache: dict[tuple[date, str, int, int], tuple[list[str], list[dict[str, Any]], str]] = {}
    fallback = ([CS300_CODE], [{"rank": 1, "ts_code": CS300_CODE, "index_name": names.get(CS300_CODE, CS300_CODE)}], "fallback_cs300")
    for snapshot, universe in feature_cache.items():
        for model, top_k, min_universe in keys:
            if len(universe) < min_universe:
                cache[(snapshot, model, top_k, min_universe)] = fallback
                continue
            scored = []
            for code, features in universe:
                score = score_features(features, model)
                scored.append((score, code, features | {"score": score}))
            scored.sort(reverse=True)
            selected = scored[:top_k]
            rows = [
                {
                    "rank": rank,
                    "ts_code": code,
                    "index_name": names.get(code, code),
                    **features,
                }
                for rank, (_score, code, features) in enumerate(selected, 1)
            ]
            cache[(snapshot, model, top_k, min_universe)] = ([code for _score, code, _features in selected], rows, model)
    return cache


_SCORECARD_CACHE: dict[tuple[int, date], dict[str, Any]] = {}


def apply_year_for_snapshot(snapshot: date) -> int:
    return snapshot.year + 1 if snapshot.month == 12 and snapshot.day == 31 else snapshot.year


def scorecard_target(conn, snapshot: date, rule: MonthlySelectorRule) -> tuple[float, dict[str, Any]]:
    apply_year = apply_year_for_snapshot(snapshot)
    key = (apply_year, snapshot)
    if key not in _SCORECARD_CACHE:
        _SCORECARD_CACHE[key] = scorecard_detail(conn, apply_year, snapshot, DEFAULT_RULE)
    detail = _SCORECARD_CACHE[key]
    target = float(detail["rule_target_equity_pct"]) * rule.target_multiplier
    if int(detail["score"]) <= rule.opportunity_score_lte:
        target = max(target, rule.opportunity_floor_pct)
    target = min(target, rule.max_equity_pct)
    return target, detail


def apply_caps(
    series: dict[str, list[tuple[date, float]]],
    rule: MonthlySelectorRule,
    snapshot: date,
    target_pct: float,
    portfolio_drawdown: float,
) -> tuple[float, list[str]]:
    reasons = []
    cs300_rows = series[CS300_CODE]
    cs300_3m = period_return(cs300_rows, month_end_shift(snapshot, -3), snapshot) or 0.0
    cs300_6m = period_return(cs300_rows, month_end_shift(snapshot, -6), snapshot) or 0.0
    if cs300_3m <= rule.cs300_3m_lte:
        target_pct = min(target_pct, rule.cs300_3m_cap_pct)
        reasons.append("cs300_3m_cap")
    if cs300_6m <= rule.cs300_6m_lte:
        target_pct = min(target_pct, rule.cs300_6m_cap_pct)
        reasons.append("cs300_6m_cap")
    if portfolio_drawdown <= rule.portfolio_dd_lte:
        target_pct = min(target_pct, rule.portfolio_dd_cap_pct)
        reasons.append("portfolio_dd_cap")
    return target_pct, reasons


def equal_weight_return(series: dict[str, list[tuple[date, float]]], codes: list[str], start: date, end: date) -> float:
    rets = [period_return(series[code], start, end) for code in codes if code in series]
    vals = [ret for ret in rets if ret is not None]
    return statistics.mean(vals) if vals else 0.0


def defensive_return(yields: list[tuple[date, float]], rule: MonthlySelectorRule, start: date, end: date) -> tuple[float, str]:
    if rule.defensive_asset == US10Y_PROXY:
        return us10y_duration_return(yields, start, end), US10Y_PROXY
    return cash_return(start, end), "cash"


def summarize(name: str, capital: float, curve: list[float], rows: list[dict[str, Any]]) -> dict[str, Any]:
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": name,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def run_case(
    conn,
    series: dict[str, list[tuple[date, float]]],
    names: dict[str, str],
    selection_cache: dict[tuple[date, str, int, int], tuple[list[str], list[dict[str, Any]], str]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    rule: MonthlySelectorRule,
    phase_month_offset: int,
    execution_lag_days: int,
    include_rows: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    fallback_months = 0
    for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase_month_offset):
        start_exec = shifted_boundary(trade_dates, start_snapshot, execution_lag_days)
        end_exec = shifted_boundary(trade_dates, end_snapshot, execution_lag_days)
        target_pct, detail = scorecard_target(conn, start_snapshot, rule)
        target_pct, cap_reasons = apply_caps(series, rule, start_snapshot, target_pct, capital / peak - 1.0)
        codes, selected_rows, selector = select_codes(names, selection_cache, rule, start_snapshot)
        if selector == "fallback_cs300":
            fallback_months += 1
        equity_return = equal_weight_return(series, codes, start_exec, end_exec)
        equity_weight = target_pct / 100.0
        def_ret, defensive_asset = defensive_return(yields, rule, start_exec, end_exec)
        financing_return = cash_return(start_exec, end_exec)
        non_equity_return = financing_return if equity_weight > 1.0 else def_ret
        period_ret = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
        capital *= 1.0 + period_ret
        peak = max(peak, capital)
        curve.append(capital)
        if include_rows:
            rows.append(
                {
                    "period": start_snapshot.isoformat(),
                    "phase_month_offset": phase_month_offset,
                    "execution_lag_days": execution_lag_days,
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "scorecard_score": detail["score"],
                    "target_equity_pct": target_pct,
                    "selector": selector,
                    "selected_codes": codes,
                    "selected": selected_rows,
                    "equity_return": equity_return,
                    "defensive_asset": defensive_asset,
                    "defensive_return": def_ret,
                    "period_return": period_ret,
                    "capital": capital,
                    "drawdown": capital / peak - 1.0,
                    "cap_reasons": cap_reasons,
                }
            )
    return summarize(f"{rule.name}_phase{phase_month_offset}_lag{execution_lag_days}", capital, curve, rows) | {
        "rule": rule.name,
        "phase_month_offset": phase_month_offset,
        "execution_lag_days": execution_lag_days,
        "fallback_months": fallback_months,
    }


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
        "median_fallback_months": statistics.median(item["fallback_months"] for item in items),
    }


def evaluate_rule(
    conn,
    series: dict[str, list[tuple[date, float]]],
    names: dict[str, str],
    selection_cache: dict[tuple[date, str, int, int], tuple[list[str], list[dict[str, Any]], str]],
    yields: list[tuple[date, float]],
    trade_dates: list[date],
    rule: MonthlySelectorRule,
) -> dict[str, Any]:
    cases = [
        run_case(conn, series, names, selection_cache, yields, trade_dates, rule, phase, lag)
        for phase in MONTH_PHASES
        for lag in EXECUTION_LAGS
    ]
    summary = matrix_summary(cases)
    return {
        "rule": asdict(rule),
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test monthly ex-ante CSI selection with scorecard risk budget across all month phases and execution lags.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Before enough CSI cross-section history exists, the strategy falls back to CSI300; selection uses price-only features known at each monthly snapshot.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "name",
            "score_model",
            "top_k",
            "target_multiplier",
            "max_equity_pct",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_fallback_months",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        names = load_names(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        snapshots = [
            start_snapshot
            for phase in MONTH_PHASES
            for start_snapshot, _end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase)
        ]
        feature_cache = build_feature_cache(series, snapshots)
        selection_cache = build_selection_cache(names, feature_cache, RULES)
        results = []
        for rule in RULES:
            result = evaluate_rule(conn, series, names, selection_cache, yields, trade_dates, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name:<44} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}万 "
                f"median={summary['median_final_capital_wan']:8.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"min_ann={summary['min_annualized_return'] * 100:5.1f}% "
                f"fallback={summary['median_fallback_months']}"
            )
    finally:
        conn.close()
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    write_outputs(results)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

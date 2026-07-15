#!/usr/bin/env python3
"""Search A-share-listed domestic ETF rotation sleeves with CSI scorecards."""

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
from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_domestic_only_regime_defense import (
    DomesticOnlyRule,
    build_domestic_cases,
    build_feature_map,
    period_return_with_listed_stop,
    period_return_with_pre_stop,
    pre_option_exposure,
)

OUT_DIR = ROOT / "data" / "backtests"
DOMESTIC_FUND_PREFIXES = ("510", "512", "515")
ETF_SCORE_CACHE: dict[tuple[str, dt.date, str], float | None] = {}
ETF_ROTATION_CACHE: dict[tuple[str, int, float, float, dt.date, dt.date], tuple[float, str]] = {}


@dataclass(frozen=True)
class EtfRotationRule:
    name: str
    base_rule: DomesticOnlyRule
    base_weight: float
    etf_weight: float
    top_n: int
    score_mode: str
    min_score: float
    etf_leverage: float
    floor_pct: float
    multiplier: float
    max_exposure: float


def load_fund_series(conn, min_rows: int = 1000) -> dict[str, list[tuple[dt.date, float]]]:
    series: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM fund_daily
            WHERE (ts_code LIKE '510%%' OR ts_code LIKE '512%%' OR ts_code LIKE '515%%')
              AND close IS NOT NULL
            GROUP BY ts_code
            HAVING COUNT(*) >= %s
            ORDER BY ts_code
            """,
            (min_rows,),
        )
        codes = [row[0] for row in cur.fetchall()]
        for code in codes:
            cur.execute(
                """
                SELECT trade_date, close
                FROM fund_daily
                WHERE ts_code=%s AND close IS NOT NULL
                ORDER BY trade_date
                """,
                (code,),
            )
            rows = [(day, float(close)) for day, close in cur.fetchall()]
            if rows:
                series[code] = rows
    return series


def price_at(rows: list[tuple[dt.date, float]], day: dt.date) -> float | None:
    idx = bisect_right(rows, (day, math.inf)) - 1
    return rows[idx][1] if idx >= 0 else None


def period_return(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> float | None:
    start_px = price_at(rows, start)
    end_px = price_at(rows, end)
    if not start_px or not end_px or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def month_shift(day: dt.date, months: int) -> dt.date:
    month_index = day.year * 12 + day.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, 1)


def volatility(rows: list[tuple[dt.date, float]], day: dt.date, points: int = 63) -> float | None:
    idx = bisect_right(rows, (day, math.inf)) - 1
    if idx - points < 0:
        return None
    values = [rows[i][1] for i in range(idx - points, idx + 1)]
    if any(value <= 0 for value in values):
        return None
    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((ret - mean) ** 2 for ret in rets) / len(rets)) * math.sqrt(252.0)


def etf_score(rows: list[tuple[dt.date, float]], snapshot: dt.date, mode: str) -> float | None:
    r3 = period_return(rows, month_shift(snapshot, -3), snapshot)
    r6 = period_return(rows, month_shift(snapshot, -6), snapshot)
    r12 = period_return(rows, month_shift(snapshot, -12), snapshot)
    vol = volatility(rows, snapshot, 63)
    if r3 is None or r6 is None or r12 is None or vol is None:
        return None
    if mode == "r6":
        return r6
    if mode == "r12":
        return r12
    if mode == "risk_adjusted":
        return 0.4 * r12 + 0.4 * r6 + 0.2 * r3 - 0.35 * vol
    return 0.5 * r12 + 0.35 * r6 + 0.15 * r3


def base_rule(phase: str, listed_exposure: float, pre_risk: float, pre_stop: float) -> DomesticOnlyRule:
    return DomesticOnlyRule(
        name=f"base_{phase}_lx{int(listed_exposure*100)}_pr{int(pre_risk*100)}_pst{int(abs(pre_stop)*100)}",
        phase_rule_name=phase,
        listed_stop_loss_pct=-0.01,
        listed_normal_exposure=listed_exposure,
        listed_post_stop_exposure=0.0,
        pre_normal_exposure=1.0,
        pre_risk_exposure=pre_risk,
        pre_crisis_exposure=0.0,
        pre_stop_loss_pct=pre_stop,
        pre_post_stop_exposure=0.0,
        drawdown_guard_lte=-1.0,
        drawdown_guard_scale=1.0,
        cs300_3m_lte=-0.10,
        cs300_6m_lte=-0.18,
        cs300_12m_lte=-0.40,
        dd60_lte=-0.15,
        dd120_lte=-0.15,
        ma200_lte=0.0,
        min_bad_signals=1,
    )


def build_rules(quick: bool = False) -> list[EtfRotationRule]:
    phases = ["phase12_lever150_cash", "phase12_lever120_cash"]
    base_specs = [(2.0, 0.30, -0.015), (3.0, 0.30, -0.015), (3.0, 0.0, -0.025)]
    mixes = [(0.50, 0.50), (0.35, 0.65), (0.20, 0.80)]
    top_ns = [1, 2, 3, 5]
    modes = ["blend", "risk_adjusted", "r6"]
    min_scores = [0.0, 0.03]
    etf_leverages = [1.0, 1.5, 2.0]
    floors = [0.86, 0.88, 0.90, 0.92]
    multipliers = [4.0, 6.0, 8.0]
    max_exposures = [1.0, 1.5, 2.0]
    if quick:
        mixes = [(0.35, 0.65), (0.20, 0.80)]
        top_ns = [1, 3]
        modes = ["blend", "risk_adjusted"]
        min_scores = [0.0]
        etf_leverages = [1.0, 1.5, 2.0]
        floors = [0.88, 0.90]
        multipliers = [4.0, 6.0]
        max_exposures = [1.0, 1.5]

    rules: list[EtfRotationRule] = []
    for phase in phases:
        for listed_exposure, pre_risk, pre_stop in base_specs:
            base = base_rule(phase, listed_exposure, pre_risk, pre_stop)
            for base_weight, etf_weight in mixes:
                for top_n in top_ns:
                    for mode in modes:
                        for min_score in min_scores:
                            for etf_leverage in etf_leverages:
                                for floor in floors:
                                    for multiplier in multipliers:
                                        for max_exposure in max_exposures:
                                            name = (
                                                f"detf_{base.name}"
                                                f"_b{int(base_weight*100)}_e{int(etf_weight*100)}"
                                                f"_top{top_n}_{mode}_ms{int(min_score*100)}"
                                                f"_lev{int(etf_leverage*100)}"
                                                f"_f{int(floor*100)}_m{int(multiplier*10)}_x{int(max_exposure*100)}"
                                            )
                                            rules.append(
                                                EtfRotationRule(
                                                    name=name,
                                                    base_rule=base,
                                                    base_weight=base_weight,
                                                    etf_weight=etf_weight,
                                                    top_n=top_n,
                                                    score_mode=mode,
                                                    min_score=min_score,
                                                    etf_leverage=etf_leverage,
                                                    floor_pct=floor,
                                                    multiplier=multiplier,
                                                    max_exposure=max_exposure,
                                                )
                                            )
    return rules


def base_period_return(period: dict[str, Any], base: DomesticOnlyRule, feature_map) -> float:
    phase_item = period["phase"][base.phase_rule_name]
    base_without_package = float(phase_item["base_without_package"])
    no_stop_risky = base_without_package + float(period["package_end_return"])
    if period["package_source"] == "listed_contract" and period["daily_points"] > 0:
        ret, _ = period_return_with_listed_stop(period, base_without_package, base)
        return ret
    exposure, _ = pre_option_exposure(base, feature_map[period["start_exec"]])
    ret, _ = period_return_with_pre_stop(period, no_stop_risky, exposure, base)
    return ret


def cached_etf_score(code: str, rows: list[tuple[dt.date, float]], snapshot: dt.date, mode: str) -> float | None:
    key = (code, snapshot, mode)
    if key not in ETF_SCORE_CACHE:
        ETF_SCORE_CACHE[key] = etf_score(rows, snapshot, mode)
    return ETF_SCORE_CACHE[key]


def etf_rotation_return(period: dict[str, Any], rule: EtfRotationRule, funds: dict[str, list[tuple[dt.date, float]]]) -> tuple[float, str]:
    cache_key = (
        rule.score_mode,
        rule.top_n,
        rule.min_score,
        rule.etf_leverage,
        period["start_exec"],
        period["end_exec"],
    )
    if cache_key in ETF_ROTATION_CACHE:
        return ETF_ROTATION_CACHE[cache_key]
    scored: list[tuple[float, str]] = []
    snapshot = period["start_exec"]
    for code, rows in funds.items():
        score = cached_etf_score(code, rows, snapshot, rule.score_mode)
        if score is None or score < rule.min_score:
            continue
        ret = period_return(rows, period["start_exec"], period["end_exec"])
        if ret is None:
            continue
        scored.append((score, code))
    if not scored:
        result = (float(period["safe_return"]), "cash")
        ETF_ROTATION_CACHE[cache_key] = result
        return result
    scored.sort(reverse=True)
    picks = [code for _score, code in scored[: rule.top_n]]
    returns = [period_return(funds[code], period["start_exec"], period["end_exec"]) for code in picks]
    valid = [ret for ret in returns if ret is not None]
    if not valid:
        result = (float(period["safe_return"]), "cash")
        ETF_ROTATION_CACHE[cache_key] = result
        return result
    result = (
        rule.etf_leverage * sum(valid) / len(valid) + (1.0 - rule.etf_leverage) * float(period["safe_return"]),
        ",".join(picks),
    )
    ETF_ROTATION_CACHE[cache_key] = result
    return result


def run_case(domestic_case: dict[str, Any], rule: EtfRotationRule, feature_map, funds) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    exposures: list[float] = []
    cash_months = 0
    for period in domestic_case["periods"]:
        peak = max(peak, capital)
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        base_ret = base_period_return(period, rule.base_rule, feature_map)
        etf_ret, picks = etf_rotation_return(period, rule, funds)
        safe_ret = float(period["safe_return"])
        if picks == "cash":
            cash_months += 1
        raw_ret = rule.base_weight * base_ret + rule.etf_weight * etf_ret + (1.0 - rule.base_weight - rule.etf_weight) * safe_ret
        period_ret = exposure * raw_ret + (1.0 - exposure) * safe_ret
        capital *= 1.0 + period_ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{domestic_case['phase_month_offset']}_lag{domestic_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": domestic_case["phase_month_offset"],
        "execution_lag_days": domestic_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "cash_months": cash_months,
    }


def matrix_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in cases),
        "median_cash_months": statistics.median(item["cash_months"] for item in cases),
    }


def evaluate_rule(domestic_cases, rule, feature_map, funds) -> dict[str, Any]:
    cases = [run_case(case, rule, feature_map, funds) for case in domestic_cases]
    summary = matrix_summary(cases)
    return {
        "rule": {**{key: value for key, value in asdict(rule).items() if key != "base_rule"}, "base_rule": asdict(rule.base_rule)},
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def output_paths(args) -> tuple[Path, Path]:
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_domestic_etf_rotation_tipp"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def write_outputs(results, meta, args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search A-share-listed domestic ETF rotation sleeves with CSI scorecards.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {"no_overseas_assets": True, "domestic_fund_prefixes": DOMESTIC_FUND_PREFIXES, **meta},
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "base_rule_name",
        "base_weight",
        "etf_weight",
        "top_n",
        "score_mode",
        "min_score",
        "etf_leverage",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_avg_exposure",
        "median_cash_months",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {
                **item["rule"],
                "base_rule_name": item["rule"]["base_rule"]["name"],
                **item["summary"],
            }
            row.pop("base_rule", None)
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search domestic ETF rotation plus CSI scorecard TIPP portfolios.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--package-scale", type=float, default=1.0)
    parser.add_argument("--min-fund-rows", type=int, default=1000)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    domestic_cases, meta, series = build_domestic_cases(args)
    feature_map = build_feature_map(domestic_cases, series)
    conn = get_connection()
    try:
        funds = load_fund_series(conn, args.min_fund_rows)
    finally:
        conn.close()
    meta = {
        **meta,
        "fund_universe_count": len(funds),
        "fund_universe": sorted(funds),
    }
    results = [evaluate_rule(domestic_cases, rule, feature_map, funds) for rule in build_rules(quick=args.quick)]
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )
    json_path, csv_path = write_outputs(results, meta, args)
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['rule']['name']:<118} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_exposure']:.2f}"
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

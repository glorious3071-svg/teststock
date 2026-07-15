#!/usr/bin/env python3
"""Search A-share-only CSI + CFFEX futures TIPP portfolios."""

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
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
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
FUTURES_CODES = ["IF.CFX", "IH.CFX", "IC.CFX", "IM.CFX"]


@dataclass(frozen=True)
class DomesticFuturesRule:
    name: str
    base_rule: DomesticOnlyRule
    base_weight: float
    futures_weight: float
    futures_code: str
    signal_months: int
    long_threshold: float
    short_threshold: float
    futures_leverage: float
    short_cost_annual: float
    floor_pct: float
    multiplier: float
    max_exposure: float


def load_futures_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    out: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        for code in FUTURES_CODES:
            cur.execute(
                """
                SELECT trade_date, close
                FROM fut_daily
                WHERE ts_code=%s AND close IS NOT NULL
                ORDER BY trade_date
                """,
                (code,),
            )
            out[code] = [(day, float(close)) for day, close in cur.fetchall()]
    return out


def price_at(rows: list[tuple[dt.date, float]], day: dt.date) -> float | None:
    idx = bisect_right(rows, (day, math.inf)) - 1
    return rows[idx][1] if idx >= 0 else None


def period_return(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> float:
    start_px = price_at(rows, start)
    end_px = price_at(rows, end)
    if not start_px or not end_px or start_px <= 0:
        return 0.0
    return end_px / start_px - 1.0


def month_shift(day: dt.date, months: int) -> dt.date:
    month_index = day.year * 12 + day.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, 1)


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


def build_rules(quick: bool = False) -> list[DomesticFuturesRule]:
    phases = ["phase12_lever150_cash", "phase12_lever120_cash"]
    listed_exposures = [2.0, 3.0]
    pre_risks = [0.0, 0.30]
    pre_stops = [-0.015, -0.025]
    mixes = [(0.70, 0.30), (0.50, 0.50), (0.35, 0.65)]
    futures_codes = ["IF.CFX", "IC.CFX"]
    futures_leverages = [1.0, 1.5, 2.0]
    floors = [0.88, 0.90, 0.92]
    multipliers = [4.0, 6.0, 8.0]
    max_exposures = [1.0, 1.5]
    if quick:
        mixes = [(0.50, 0.50), (0.35, 0.65)]
        futures_leverages = [1.0, 1.5]
        floors = [0.88, 0.90]
        multipliers = [4.0, 6.0]
        max_exposures = [1.0, 1.5]

    rules: list[DomesticFuturesRule] = []
    for phase in phases:
        for listed_exposure in listed_exposures:
            for pre_risk in pre_risks:
                for pre_stop in pre_stops:
                    base = base_rule(phase, listed_exposure, pre_risk, pre_stop)
                    for base_weight, futures_weight in mixes:
                        for futures_code in futures_codes:
                            for fut_lev in futures_leverages:
                                for floor in floors:
                                    for multiplier in multipliers:
                                        for max_exp in max_exposures:
                                            name = (
                                                f"dfut_{base.name}_{futures_code.replace('.', '')}"
                                                f"_b{int(base_weight*100)}_f{int(futures_weight*100)}"
                                                f"_fl{int(floor*100)}_m{int(multiplier*10):02d}"
                                                f"_x{int(max_exp*100)}_lev{int(fut_lev*100)}"
                                            )
                                            rules.append(
                                                DomesticFuturesRule(
                                                    name=name,
                                                    base_rule=base,
                                                    base_weight=base_weight,
                                                    futures_weight=futures_weight,
                                                    futures_code=futures_code,
                                                    signal_months=6,
                                                    long_threshold=0.05,
                                                    short_threshold=-0.05,
                                                    futures_leverage=fut_lev,
                                                    short_cost_annual=0.01,
                                                    floor_pct=floor,
                                                    multiplier=multiplier,
                                                    max_exposure=max_exp,
                                                )
                                            )
    return rules


def base_period_return(period: dict[str, Any], base: DomesticOnlyRule, feature_map) -> float:
    phase_item = period["phase"][base.phase_rule_name]
    base_without_package = float(phase_item["base_without_package"])
    no_stop_risky = base_without_package + float(period["package_end_return"])
    if period["package_source"] == "listed_contract" and period["daily_points"] > 0:
        period_ret, _stopped = period_return_with_listed_stop(period, base_without_package, base)
        return period_ret
    exposure, _reasons = pre_option_exposure(base, feature_map[period["start_exec"]])
    period_ret, _stopped = period_return_with_pre_stop(period, no_stop_risky, exposure, base)
    return period_ret


def futures_return(
    period: dict[str, Any],
    rule: DomesticFuturesRule,
    futures: dict[str, list[tuple[dt.date, float]]],
    csi_rows: list[tuple[dt.date, float]],
) -> tuple[float, int]:
    signal = period_return(csi_rows, month_shift(period["start_exec"], -rule.signal_months), period["start_exec"])
    direction = 0
    if signal >= rule.long_threshold:
        direction = 1
    elif signal <= rule.short_threshold:
        direction = -1
    if direction == 0:
        return 0.0, 0
    raw = period_return(futures[rule.futures_code], period["start_exec"], period["end_exec"])
    signed = raw if direction > 0 else -raw - rule.short_cost_annual / 12.0
    return rule.futures_leverage * signed, direction


def run_case(domestic_case: dict[str, Any], rule: DomesticFuturesRule, feature_map, futures, csi_rows) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    exposures: list[float] = []
    directions: list[int] = []
    for period in domestic_case["periods"]:
        peak = max(peak, capital)
        floor = peak * rule.floor_pct
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        base_ret = base_period_return(period, rule.base_rule, feature_map)
        fut_ret, direction = futures_return(period, rule, futures, csi_rows)
        safe_ret = float(period["safe_return"])
        raw_ret = rule.base_weight * base_ret + rule.futures_weight * fut_ret + (1.0 - rule.base_weight - rule.futures_weight) * safe_ret
        period_ret = exposure * raw_ret + (1.0 - exposure) * safe_ret
        capital *= 1.0 + period_ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
        directions.append(direction)
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
        "median_direction": statistics.median(directions) if directions else 0,
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
        "median_direction": statistics.median(item["median_direction"] for item in cases),
    }


def evaluate_rule(domestic_cases, rule, feature_map, futures, csi_rows) -> dict[str, Any]:
    cases = [run_case(case, rule, feature_map, futures, csi_rows) for case in domestic_cases]
    summary = matrix_summary(cases)
    return {
        "rule": {**{k: v for k, v in asdict(rule).items() if k != "base_rule"}, "base_rule": asdict(rule.base_rule)},
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def output_paths(args) -> tuple[Path, Path]:
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_domestic_futures_tipp"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def write_outputs(results, meta, args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search A-share-only CSI scorecard plus CFFEX index-futures TIPP portfolios.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {"no_overseas_assets": True, **meta},
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "base_rule_name",
        "base_weight",
        "futures_weight",
        "futures_code",
        "futures_leverage",
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
        "median_direction",
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
    parser = argparse.ArgumentParser(description="Search domestic CSI + CFFEX futures TIPP portfolios.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--package-scale", type=float, default=1.0)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    domestic_cases, meta, series = build_domestic_cases(args)
    feature_map = build_feature_map(domestic_cases, series)
    conn = get_connection()
    try:
        futures = load_futures_series(conn)
    finally:
        conn.close()
    meta = {**meta, "futures_coverage": {code: {"rows": len(rows), "min": str(rows[0][0]) if rows else None, "max": str(rows[-1][0]) if rows else None} for code, rows in futures.items()}}
    results = [evaluate_rule(domestic_cases, rule, feature_map, futures, series[CS300_CODE]) for rule in build_rules(quick=args.quick)]
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

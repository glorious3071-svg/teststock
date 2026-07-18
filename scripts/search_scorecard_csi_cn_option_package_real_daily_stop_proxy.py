#!/usr/bin/env python3
"""Search proxy daily stops over the raw historical China ETF option package.

This is a diagnostic, not an execution proof.  The historical option package
path is still monthly because complete daily option MTM is not available across
the whole window.  The script uses ETF/index daily prices only to approximate
whether a month-internal stop could have been triggered before large losses.
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

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases

OUT_DIR = ROOT / "data" / "backtests"
SWITCH_50_TO_300_DATE = dt.date(2019, 12, 23)


@dataclass(frozen=True)
class DailyStopRule:
    name: str
    stop_loss_pct: float
    normal_exposure: float
    post_stop_exposure: float
    stop_loss_shock: float


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
        return prefix.with_suffix(".json"), prefix.with_suffix(".csv")
    prefix = OUT_DIR / (
        "scorecard_csi_cn_option_package_real_daily_stop_proxy_"
        f"{args.underlying_mode}_miss{args.missing_package_policy}"
    )
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def build_rules() -> list[DailyStopRule]:
    rules: list[DailyStopRule] = []
    for stop_loss_pct in [-0.03, -0.04, -0.05, -0.06, -0.08, -0.10, -0.12]:
        for normal_exposure in [1.0, 1.15, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
            for post_stop_exposure in [0.0, 0.15, 0.25, 0.5]:
                for stop_loss_shock in [1.0, 1.5, 2.0]:
                    name = (
                        f"dstop{int(abs(stop_loss_pct) * 100):02d}"
                        f"_x{int(normal_exposure * 100)}"
                        f"_post{int(post_stop_exposure * 100)}"
                        f"_shock{int(stop_loss_shock * 10):02d}"
                    )
                    rules.append(
                        DailyStopRule(
                            name=name,
                            stop_loss_pct=stop_loss_pct,
                            normal_exposure=normal_exposure,
                            post_stop_exposure=post_stop_exposure,
                            stop_loss_shock=stop_loss_shock,
                        )
                    )
    return rules


def load_fund_series(conn, codes: list[str]) -> dict[str, list[tuple[dt.date, float]]]:
    series: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
    if not codes:
        return series
    placeholders = ",".join(["%s"] * len(codes))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ts_code, trade_date, close
            FROM fund_daily
            WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
            ORDER BY ts_code, trade_date
            """,
            codes,
        )
        for code, trade_date, close in cur.fetchall():
            series.setdefault(str(code), []).append((trade_date, float(close)))
    return series


def load_proxy_series() -> dict[str, list[tuple[dt.date, float]]]:
    conn = get_connection()
    try:
        series = load_price_series(conn)
        series.update(load_fund_series(conn, ["510050.SH", "510300.SH"]))
        return series
    finally:
        conn.close()


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date) -> float | None:
    idx = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[idx][1] if idx >= 0 else None


def rows_between(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    left = bisect_right(rows, (start, math.inf))
    right = bisect_right(rows, (end, math.inf))
    return rows[left:right]


def proxy_code_for_period(series: dict[str, list[tuple[dt.date, float]]], args, start: dt.date) -> str:
    preferred: list[str]
    if args.underlying_mode == "switch_50_to_300" and start < SWITCH_50_TO_300_DATE:
        preferred = ["510050.SH", CS300_CODE]
    else:
        preferred = ["510300.SH", CS300_CODE]
    for code in preferred:
        if series.get(code):
            return code
    return CS300_CODE


def find_stop_hit(
    series: dict[str, list[tuple[dt.date, float]]],
    args,
    start: dt.date,
    end: dt.date,
    stop_loss_pct: float,
) -> dict[str, Any]:
    code = proxy_code_for_period(series, args, start)
    rows = series.get(code, [])
    start_price = price_at(rows, start)
    path = rows_between(rows, start, end)
    if not start_price or start_price <= 0 or not path:
        return {"hit": False, "proxy_code": code, "trade_days": len(path), "hit_fraction": None, "hit_return": None}
    for idx, (day, close) in enumerate(path, start=1):
        cumulative = close / start_price - 1.0
        if cumulative <= stop_loss_pct:
            return {
                "hit": True,
                "proxy_code": code,
                "trade_days": len(path),
                "hit_day": day,
                "hit_fraction": min(1.0, idx / max(len(path), 1)),
                "hit_return": cumulative,
            }
    return {"hit": False, "proxy_code": code, "trade_days": len(path), "hit_fraction": None, "hit_return": None}


def stopped_period_return(raw_return: float, safe_return: float, rule: DailyStopRule, stop_hit: dict[str, Any]) -> float:
    if not stop_hit["hit"]:
        return rule.normal_exposure * raw_return + (1.0 - rule.normal_exposure) * safe_return

    hit_fraction = float(stop_hit["hit_fraction"] or 1.0)
    remaining_fraction = max(0.0, 1.0 - hit_fraction)
    stop_loss = rule.stop_loss_pct * rule.stop_loss_shock
    safe_until_stop = safe_return * hit_fraction
    safe_after_stop = safe_return * remaining_fraction
    risky_after_stop = raw_return * remaining_fraction
    before_stop = rule.normal_exposure * stop_loss + (1.0 - rule.normal_exposure) * safe_until_stop
    after_stop = rule.post_stop_exposure * risky_after_stop + (1.0 - rule.post_stop_exposure) * safe_after_stop
    return before_stop + after_stop


def run_case(raw_case: dict[str, Any], rule: DailyStopRule, series: dict[str, list[tuple[dt.date, float]]], args) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    stop_months = 0
    severe_loss_months = 0
    severe_loss_stopped = 0
    proxy_codes: dict[str, int] = {}
    for raw_return, safe_return, period in zip(raw_case["raw_returns"], raw_case["safe_returns"], raw_case["periods"]):
        start_exec = period["start_exec"]
        end_exec = period["end_exec"]
        stop_hit = find_stop_hit(series, args, start_exec, end_exec, rule.stop_loss_pct)
        proxy_codes[stop_hit["proxy_code"]] = proxy_codes.get(stop_hit["proxy_code"], 0) + 1
        if raw_return <= -0.10:
            severe_loss_months += 1
            if stop_hit["hit"]:
                severe_loss_stopped += 1
        if stop_hit["hit"]:
            stop_months += 1
        period_return = stopped_period_return(raw_return, safe_return, rule, stop_hit)
        capital *= 1.0 + period_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{raw_case['phase_month_offset']}_lag{raw_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": raw_case["phase_month_offset"],
        "execution_lag_days": raw_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "stop_months": stop_months,
        "severe_loss_months": severe_loss_months,
        "severe_loss_stopped": severe_loss_stopped,
        "proxy_codes": proxy_codes,
        "listed_package_months": raw_case["listed_package_months"],
        "missing_package_months": raw_case["missing_package_months"],
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
        "median_stop_months": statistics.median(item["stop_months"] for item in cases),
        "median_severe_loss_months": statistics.median(item["severe_loss_months"] for item in cases),
        "median_severe_loss_stopped": statistics.median(item["severe_loss_stopped"] for item in cases),
        "median_listed_package_months": statistics.median(item["listed_package_months"] for item in cases),
        "median_missing_package_months": statistics.median(item["missing_package_months"] for item in cases),
    }


def evaluate_rule(
    raw_cases: list[dict[str, Any]],
    rule: DailyStopRule,
    series: dict[str, list[tuple[dt.date, float]]],
    args,
) -> dict[str, Any]:
    cases = [run_case(raw_case, rule, series, args) for raw_case in raw_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def proxy_stop_diagnostics(raw_cases: list[dict[str, Any]], series: dict[str, list[tuple[dt.date, float]]], args) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    thresholds = [-0.03, -0.04, -0.05, -0.06, -0.08, -0.10, -0.12]
    severe_months = 0
    for threshold in thresholds:
        rows[f"{threshold:.0%}"] = {
            "threshold": threshold,
            "all_month_stop_hits": 0,
            "severe_loss_months": 0,
            "severe_loss_stop_hits": 0,
            "proxy_codes": {},
        }
    for raw_case in raw_cases:
        for raw_return, period in zip(raw_case["raw_returns"], raw_case["periods"]):
            is_severe = raw_return <= -0.10
            if is_severe:
                severe_months += 1
            for threshold in thresholds:
                key = f"{threshold:.0%}"
                hit = find_stop_hit(series, args, period["start_exec"], period["end_exec"], threshold)
                row = rows[key]
                row["proxy_codes"][hit["proxy_code"]] = row["proxy_codes"].get(hit["proxy_code"], 0) + 1
                if hit["hit"]:
                    row["all_month_stop_hits"] += 1
                if is_severe:
                    row["severe_loss_months"] += 1
                    if hit["hit"]:
                        row["severe_loss_stop_hits"] += 1
    for row in rows.values():
        severe = max(int(row["severe_loss_months"]), 1)
        row["severe_loss_capture_rate"] = row["severe_loss_stop_hits"] / severe
    return {
        "severe_loss_definition": "raw monthly return <= -10%",
        "total_severe_loss_months_across_cases": severe_months,
        "by_threshold": rows,
    }


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "objective": "Search proxy daily stop rules over raw historical listed China ETF option package returns.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "proxy_note": (
                "ETF/index daily prices are used only to approximate month-internal stop triggers. "
                "Historical option package returns remain monthly; this is not complete daily option MTM."
            ),
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "stop_loss_pct",
        "normal_exposure",
        "post_stop_exposure",
        "stop_loss_shock",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_stop_months",
        "median_severe_loss_months",
        "median_severe_loss_stopped",
        "median_listed_package_months",
        "median_missing_package_months",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search proxy daily stops over historical listed China ETF option package returns.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    raw_cases, meta = setup_cases(args)
    series = load_proxy_series()
    meta = {**meta, "proxy_stop_diagnostics": proxy_stop_diagnostics(raw_cases, series, args)}
    results = [evaluate_rule(raw_cases, rule, series, args) for rule in build_rules()]
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    json_path, csv_path = write_outputs(results, meta, args)
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['rule']['name']:<34} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"stop_med={summary['median_stop_months']:.1f} "
            f"severe_stop_med={summary['median_severe_loss_stopped']:.1f}/{summary['median_severe_loss_months']:.1f}"
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

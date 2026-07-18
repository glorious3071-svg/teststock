#!/usr/bin/env python3
"""Search stops using real daily MTM for the listed China ETF option package.

This is a diagnostic bridge between monthly listed-contract backtests and a
full daily portfolio engine.  The option package legs are marked with actual
cn_option_daily closes where available, while the core and satellite sleeve
returns are interpolated inside each month.  Results are therefore evidence
about whether option-price MTM exposes tail losses early, not a production
execution proof.
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
from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape, price_at
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases

OUT_DIR = ROOT / "data" / "backtests"


@dataclass(frozen=True)
class MtmStopRule:
    name: str
    stop_loss_pct: float
    normal_exposure: float
    post_stop_exposure: float


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
        return prefix.with_suffix(".json"), prefix.with_suffix(".csv")
    prefix = OUT_DIR / (
        "scorecard_csi_cn_option_package_daily_mtm_stop_"
        f"{args.underlying_mode}_miss{args.missing_package_policy}"
    )
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def build_rules() -> list[MtmStopRule]:
    rules: list[MtmStopRule] = []
    for stop_loss_pct in [-0.04, -0.05, -0.06, -0.08, -0.10, -0.12, -0.15, -0.20]:
        for normal_exposure in [1.0, 1.15, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
            for post_stop_exposure in [0.0, 0.15, 0.25, 0.5]:
                rules.append(
                    MtmStopRule(
                        name=(
                            f"mtmstop{int(abs(stop_loss_pct) * 100):02d}"
                            f"_x{int(normal_exposure * 100)}"
                            f"_post{int(post_stop_exposure * 100)}"
                        ),
                        stop_loss_pct=stop_loss_pct,
                        normal_exposure=normal_exposure,
                        post_stop_exposure=post_stop_exposure,
                    )
                )
    return rules


class DailyMtmPricer:
    def __init__(self, pricer: HistoricalCnPackagePricer, csi_series: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.pricer = pricer
        self.csi_series = csi_series
        self.daily_cache: dict[str, dict[dt.date, float]] = {}
        self.coverage = {
            "listed_periods": 0,
            "periods_with_daily_mtm": 0,
            "daily_mtm_points": [],
            "missing_periods": 0,
            "fallback_periods": 0,
        }

    def option_daily(self, ts_code: str) -> dict[dt.date, float]:
        if ts_code in self.daily_cache:
            return self.daily_cache[ts_code]
        with self.pricer.conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, close
                FROM cn_option_daily
                WHERE ts_code=%s AND close IS NOT NULL
                ORDER BY trade_date
                """,
                (ts_code,),
            )
            rows = {trade_date: float(close) for trade_date, close in cur.fetchall()}
        self.daily_cache[ts_code] = rows
        return rows

    def package_path(self, start: dt.date, end: dt.date, capital: float) -> dict[str, Any]:
        package_return, meta = self.pricer.package_return(self.csi_series, start, end, capital)
        if meta["source"] != "listed_contract":
            self.coverage["missing_periods"] += 1
            return {
                "source": meta["source"],
                "end_return": package_return,
                "points": [],
                "daily_points": 0,
            }

        self.coverage["listed_periods"] += 1
        quote_date = dt.date.fromisoformat(meta["quote_date"])
        underlying = self.pricer.underlying_for(start)
        if underlying is None:
            self.coverage["fallback_periods"] += 1
            return {"source": "missing_underlying", "end_return": package_return, "points": [], "daily_points": 0}
        selected = self.pricer.select_quote_legs_for_period(start, end)
        if selected is None:
            self.coverage["fallback_periods"] += 1
            return {"source": "missing_legs", "end_return": package_return, "points": [], "daily_points": 0}
        selected_underlying, selected_quote_date, _spot, selection = selected
        if selected_underlying.opt_code != underlying.opt_code or selected_quote_date != quote_date:
            self.coverage["fallback_periods"] += 1
            return {"source": "quote_selection_mismatch", "end_return": package_return, "points": [], "daily_points": 0}

        long_put, short_call = selection
        target_notional = capital * self.pricer.package.long_put_notional_pct
        contracts = max(1, math.ceil(target_notional / max(long_put.strike * long_put.per_unit, 1.0)))
        put_units = contracts * long_put.per_unit
        call_units = contracts * short_call.per_unit
        entry_value = long_put.close * put_units - short_call.close * call_units
        gross_notional = abs(long_put.strike * put_units) + abs(short_call.strike * call_units)
        slippage = gross_notional * self.pricer.slippage_bps_per_leg / 10000.0
        put_daily = self.option_daily(long_put.ts_code)
        call_daily = self.option_daily(short_call.ts_code)
        days = sorted(day for day in set(put_daily).intersection(call_daily) if quote_date < day <= end)
        points = []
        for day in days:
            mark_value = put_daily[day] * put_units - call_daily[day] * call_units
            points.append(
                {
                    "day": day,
                    "package_return": (mark_value - entry_value - slippage) / capital,
                }
            )
        if points:
            self.coverage["periods_with_daily_mtm"] += 1
            self.coverage["daily_mtm_points"].append(len(points))
        return {
            "source": "listed_contract",
            "end_return": points[-1]["package_return"] if points else package_return,
            "payoff_only_end_return": package_return,
            "points": points,
            "daily_points": len(points),
        }

    def summary(self) -> dict[str, Any]:
        points = self.coverage["daily_mtm_points"]
        return {
            **{key: value for key, value in self.coverage.items() if key != "daily_mtm_points"},
            "median_daily_mtm_points": statistics.median(points) if points else 0,
            "min_daily_mtm_points": min(points) if points else 0,
            "max_daily_mtm_points": max(points) if points else 0,
        }


def build_mtm_cases(raw_cases: list[dict[str, Any]], mtm: DailyMtmPricer) -> list[dict[str, Any]]:
    mtm_cases = []
    for raw_case in raw_cases:
        periods = []
        for raw_return, period in zip(raw_case["raw_returns"], raw_case["periods"]):
            start_exec = period["start_exec"]
            end_exec = period["end_exec"]
            base_without_package = (
                0.95 * float(period["core_period_return"])
                + 0.08 * float(period["satellite_return"])
                + (1.0 - 0.95 - 0.08) * float(period["safe_return"])
            )
            package = mtm.package_path(start_exec, end_exec, INITIAL_CAPITAL)
            periods.append(
                {
                    "start_exec": start_exec,
                    "end_exec": end_exec,
                    "safe_return": float(period["safe_return"]),
                    "raw_return": raw_return,
                    "base_without_package": base_without_package,
                    "package_end_return": float(package["end_return"]),
                    "payoff_only_package_return": package.get("payoff_only_end_return"),
                    "points": package["points"],
                    "source": package["source"],
                    "daily_points": package["daily_points"],
                }
            )
        mtm_cases.append(
            {
                "phase_month_offset": raw_case["phase_month_offset"],
                "execution_lag_days": raw_case["execution_lag_days"],
                "periods": periods,
                "listed_package_months": raw_case["listed_package_months"],
                "missing_package_months": raw_case["missing_package_months"],
            }
        )
    return mtm_cases


def period_return_with_stop(period: dict[str, Any], rule: MtmStopRule) -> tuple[float, bool]:
    no_stop_risky = period["base_without_package"] + period["package_end_return"]
    safe_return = period["safe_return"]
    points = period["points"]
    if not points:
        return rule.normal_exposure * no_stop_risky + (1.0 - rule.normal_exposure) * safe_return, False
    total_days = max(len(points), 1)
    for idx, point in enumerate(points, start=1):
        fraction = min(1.0, idx / total_days)
        risky_to_date = period["base_without_package"] * fraction + float(point["package_return"])
        blended_to_date = rule.normal_exposure * risky_to_date + (1.0 - rule.normal_exposure) * safe_return * fraction
        if blended_to_date <= rule.stop_loss_pct:
            remaining = max(0.0, 1.0 - fraction)
            risky_after = no_stop_risky - risky_to_date
            after_stop = (
                rule.post_stop_exposure * risky_after
                + (1.0 - rule.post_stop_exposure) * safe_return * remaining
            )
            return blended_to_date + after_stop, True
    return rule.normal_exposure * no_stop_risky + (1.0 - rule.normal_exposure) * safe_return, False


def run_case(mtm_case: dict[str, Any], rule: MtmStopRule) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    stop_months = 0
    severe_loss_months = 0
    severe_loss_stopped = 0
    listed_mtm_months = 0
    for period in mtm_case["periods"]:
        if period["daily_points"] > 0:
            listed_mtm_months += 1
        no_stop_risky = period["base_without_package"] + period["package_end_return"]
        if no_stop_risky <= -0.10:
            severe_loss_months += 1
        period_return, stopped = period_return_with_stop(period, rule)
        if stopped:
            stop_months += 1
            if no_stop_risky <= -0.10:
                severe_loss_stopped += 1
        capital *= 1.0 + period_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{mtm_case['phase_month_offset']}_lag{mtm_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": mtm_case["phase_month_offset"],
        "execution_lag_days": mtm_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "stop_months": stop_months,
        "severe_loss_months": severe_loss_months,
        "severe_loss_stopped": severe_loss_stopped,
        "listed_mtm_months": listed_mtm_months,
        "listed_package_months": mtm_case["listed_package_months"],
        "missing_package_months": mtm_case["missing_package_months"],
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
        "median_listed_mtm_months": statistics.median(item["listed_mtm_months"] for item in cases),
        "median_listed_package_months": statistics.median(item["listed_package_months"] for item in cases),
        "median_missing_package_months": statistics.median(item["missing_package_months"] for item in cases),
    }


def evaluate_rule(mtm_cases: list[dict[str, Any]], rule: MtmStopRule) -> dict[str, Any]:
    cases = [run_case(mtm_case, rule) for mtm_case in mtm_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def stop_capture_diagnostics(mtm_cases: list[dict[str, Any]]) -> dict[str, Any]:
    thresholds = [-0.04, -0.05, -0.06, -0.08, -0.10, -0.12, -0.15, -0.20]
    rows = {
        f"{threshold:.0%}": {
            "threshold": threshold,
            "severe_loss_months": 0,
            "severe_loss_stop_hits": 0,
            "all_month_stop_hits": 0,
        }
        for threshold in thresholds
    }
    for mtm_case in mtm_cases:
        for period in mtm_case["periods"]:
            no_stop = period["base_without_package"] + period["package_end_return"]
            severe = no_stop <= -0.10
            for threshold in thresholds:
                key = f"{threshold:.0%}"
                hit = False
                points = period["points"]
                total_days = max(len(points), 1)
                for idx, point in enumerate(points, start=1):
                    fraction = min(1.0, idx / total_days)
                    risky_to_date = period["base_without_package"] * fraction + float(point["package_return"])
                    if risky_to_date <= threshold:
                        hit = True
                        break
                if hit:
                    rows[key]["all_month_stop_hits"] += 1
                if severe:
                    rows[key]["severe_loss_months"] += 1
                    if hit:
                        rows[key]["severe_loss_stop_hits"] += 1
    for row in rows.values():
        severe_count = max(int(row["severe_loss_months"]), 1)
        row["severe_loss_capture_rate"] = row["severe_loss_stop_hits"] / severe_count
    return {"severe_loss_definition": "MTM no-stop monthly risky return <= -10%", "by_threshold": rows}


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search daily MTM stop rules over actual listed China ETF option package legs.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "note": (
                "Option legs use actual cn_option_daily closes. Core and satellite sleeve returns are "
                "interpolated within each month, so this is a daily MTM stop diagnostic rather than a "
                "complete daily production backtest."
            ),
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "stop_loss_pct",
        "normal_exposure",
        "post_stop_exposure",
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
        "median_listed_mtm_months",
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
    parser = argparse.ArgumentParser(description="Search daily MTM stops over actual listed China ETF option package legs.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    raw_cases, setup_meta = setup_cases(args)
    package = load_package_shape()
    conn = get_connection()
    try:
        pricer = HistoricalCnPackagePricer(
            conn,
            package,
            args.underlying_mode,
            args.max_quote_stale_days,
            args.slippage_bps_per_leg,
            args.missing_package_policy,
        )
        mtm = DailyMtmPricer(pricer, setup_meta.get("csi_series") or __import__(
            "scripts.backtest_scorecard_csi_dynamic_defense",
            fromlist=["load_price_series"],
        ).load_price_series(conn))
        mtm_cases = build_mtm_cases(raw_cases, mtm)
        meta = {
            **{key: value for key, value in setup_meta.items() if key != "csi_series"},
            "daily_mtm_coverage": mtm.summary(),
            "daily_mtm_stop_diagnostics": stop_capture_diagnostics(mtm_cases),
        }
        results = [evaluate_rule(mtm_cases, rule) for rule in build_rules()]
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
    json_path, csv_path = write_outputs(results, meta, args)
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['rule']['name']:<28} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"stop_med={summary['median_stop_months']:.1f} "
            f"severe_stop_med={summary['median_severe_loss_stopped']:.1f}/"
            f"{summary['median_severe_loss_months']:.1f}"
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

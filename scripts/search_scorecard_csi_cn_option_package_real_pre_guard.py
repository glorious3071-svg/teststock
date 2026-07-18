#!/usr/bin/env python3
"""Search pre-month guards for the raw historical listed China ETF option package."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases

OUT_DIR = ROOT / "data" / "backtests"


@dataclass(frozen=True)
class GuardRule:
    name: str
    cap_exposure: float
    leverage: float
    prev_raw_lte: float | None = None
    trail3_lte: float | None = None
    trail6_lte: float | None = None
    drawdown_lte: float | None = None
    cs300_6m_lte: float | None = None
    vix_gte: float | None = None
    combine: str = "any"


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
        return prefix.with_suffix(".json"), prefix.with_suffix(".csv")
    prefix = OUT_DIR / (
        "scorecard_csi_cn_option_package_real_pre_guard_"
        f"{args.underlying_mode}_miss{args.missing_package_policy}"
    )
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def build_rules() -> list[GuardRule]:
    rules: list[GuardRule] = []
    leverages = [1.0, 1.15, 1.25, 1.5, 1.75, 2.0]
    caps = [0.0, 0.25, 0.5]
    specs: list[tuple[str, dict[str, float | str | None]]] = []
    for threshold in [-0.02, -0.04, -0.06, -0.08]:
        specs.append((f"prev{int(abs(threshold)*100):02d}", {"prev_raw_lte": threshold}))
    for threshold in [-0.02, -0.04, -0.06, -0.08, -0.10]:
        specs.append((f"tr3_{int(abs(threshold)*100):02d}", {"trail3_lte": threshold}))
        specs.append((f"tr6_{int(abs(threshold)*100):02d}", {"trail6_lte": threshold}))
    for threshold in [-0.04, -0.06, -0.08, -0.10, -0.12]:
        specs.append((f"dd{int(abs(threshold)*100):02d}", {"drawdown_lte": threshold}))
    for threshold in [-0.04, -0.06, -0.08, -0.10, -0.12]:
        specs.append((f"cs6_{int(abs(threshold)*100):02d}", {"cs300_6m_lte": threshold}))
    for threshold in [25.0, 30.0, 35.0, 40.0]:
        specs.append((f"vix{int(threshold)}", {"vix_gte": threshold}))
    combos = [
        ("prev04_or_cs608", {"prev_raw_lte": -0.04, "cs300_6m_lte": -0.08, "combine": "any"}),
        ("prev04_and_cs604", {"prev_raw_lte": -0.04, "cs300_6m_lte": -0.04, "combine": "all"}),
        ("tr304_or_dd08", {"trail3_lte": -0.04, "drawdown_lte": -0.08, "combine": "any"}),
        ("tr604_or_vix30", {"trail6_lte": -0.04, "vix_gte": 30.0, "combine": "any"}),
        ("dd08_or_vix30", {"drawdown_lte": -0.08, "vix_gte": 30.0, "combine": "any"}),
        ("cs608_or_vix30", {"cs300_6m_lte": -0.08, "vix_gte": 30.0, "combine": "any"}),
    ]
    specs.extend(combos)
    for cap in caps:
        for leverage in leverages:
            for suffix, kwargs in specs:
                combine = str(kwargs.get("combine") or "any")
                clean = {key: value for key, value in kwargs.items() if key != "combine"}
                rules.append(
                    GuardRule(
                        name=f"preguard_{suffix}_cap{int(cap*100)}_lev{int(leverage*100)}",
                        cap_exposure=cap,
                        leverage=leverage,
                        combine=combine,
                        **clean,
                    )
                )
    return rules


def guard_hit(features: dict[str, float | None], rule: GuardRule) -> bool:
    checks: list[bool] = []
    specs = [
        ("prev_raw", "<=", rule.prev_raw_lte),
        ("trail3", "<=", rule.trail3_lte),
        ("trail6", "<=", rule.trail6_lte),
        ("drawdown", "<=", rule.drawdown_lte),
        ("cs300_6m", "<=", rule.cs300_6m_lte),
        ("vix", ">=", rule.vix_gte),
    ]
    for key, op, threshold in specs:
        if threshold is None:
            continue
        value = features.get(key)
        if value is None:
            checks.append(False)
        elif op == "<=":
            checks.append(float(value) <= threshold)
        else:
            checks.append(float(value) >= threshold)
    if not checks:
        return False
    return all(checks) if rule.combine == "all" else any(checks)


def trailing_sum(values: list[float], idx: int, months: int) -> float | None:
    start = idx - months
    if start < 0:
        return None
    compounded = 1.0
    for value in values[start:idx]:
        compounded *= 1.0 + value
    return compounded - 1.0


def run_case(raw_case: dict[str, Any], rule: GuardRule) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    prior_raw_returns: list[float] = []
    guard_months = 0
    severe_loss_exposed = 0
    exposures: list[float] = []
    for idx, (raw_return, safe_return, period) in enumerate(
        zip(raw_case["raw_returns"], raw_case["safe_returns"], raw_case["periods"])
    ):
        drawdown = capital / peak - 1.0
        features = {
            "prev_raw": prior_raw_returns[-1] if prior_raw_returns else None,
            "trail3": trailing_sum(prior_raw_returns, len(prior_raw_returns), 3),
            "trail6": trailing_sum(prior_raw_returns, len(prior_raw_returns), 6),
            "drawdown": drawdown,
            "cs300_6m": period.get("cs300_6m"),
            "vix": period.get("vix"),
        }
        exposure = rule.leverage
        if guard_hit(features, rule):
            exposure = min(exposure, rule.cap_exposure)
            guard_months += 1
        if raw_return <= -0.10 and exposure > 0.25:
            severe_loss_exposed += 1
        month_return = exposure * raw_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + month_return
        peak = max(peak, capital)
        curve.append(capital)
        prior_raw_returns.append(raw_return)
        exposures.append(exposure)
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
        "guard_months": guard_months,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "severe_loss_exposed": severe_loss_exposed,
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
        "median_guard_months": statistics.median(item["guard_months"] for item in cases),
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in cases),
        "median_severe_loss_exposed": statistics.median(item["severe_loss_exposed"] for item in cases),
        "median_listed_package_months": statistics.median(item["listed_package_months"] for item in cases),
        "median_missing_package_months": statistics.median(item["missing_package_months"] for item in cases),
    }


def evaluate_rule(raw_cases: list[dict[str, Any]], rule: GuardRule) -> dict[str, Any]:
    cases = [run_case(raw_case, rule) for raw_case in raw_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "objective": "Search observable pre-month guards for raw historical listed China ETF option packages.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "note": "No modeled monthly loss floor. Guards are applied before each period using prior raw returns, portfolio drawdown, CS300 6M trend, and VIX if available.",
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "cap_exposure",
        "leverage",
        "prev_raw_lte",
        "trail3_lte",
        "trail6_lte",
        "drawdown_lte",
        "cs300_6m_lte",
        "vix_gte",
        "combine",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_guard_months",
        "median_avg_exposure",
        "median_severe_loss_exposed",
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
    parser = argparse.ArgumentParser(description="Search pre-month guards over raw historical listed China ETF option packages.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    raw_cases, meta = setup_cases(args)
    results = [evaluate_rule(raw_cases, rule) for rule in build_rules()]
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
            f"{item['rule']['name']:<42} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guard={summary['median_guard_months']:5.1f} "
            f"sev_exp={summary['median_severe_loss_exposed']:4.1f}"
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

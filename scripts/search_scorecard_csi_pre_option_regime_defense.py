#!/usr/bin/env python3
"""Search pre-option regime defenses for the scorecard+CSI portfolio.

The listed China ETF option package only has executable daily MTM evidence from
2015 onward.  This search keeps the best current listed-option MTM stop fixed
and tests observable CS300 trend/drawdown rules for months where the listed
option package is unavailable, with all 12 month-start phases and execution
lags included.
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
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series, month_end_shift, period_return
from scripts.backtest_scorecard_csi_midyear_risk import CASH_ANNUAL_RATE, CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.search_scorecard_csi_cn_option_package_daily_mtm_stop import (
    DailyMtmPricer,
    MtmStopRule,
    build_mtm_cases,
    period_return_with_stop,
)
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases

OUT_DIR = ROOT / "data" / "backtests"
USER_TARGET_MDD = -0.10
PROXY_STOP_LOSS_PCT = -0.04
PROXY_POST_STOP_EXPOSURE = 0.0
OPTION_ERA_START = dt.date(2015, 2, 9)
ENABLE_PROXY_STOP = False
BUBBLE_REVERSAL_12M_GTE = 1.0
BUBBLE_REVERSAL_3M_LTE = -0.03


@dataclass(frozen=True)
class PreOptionRegimeRule:
    name: str
    listed_stop_loss_pct: float
    pre_normal_exposure: float
    pre_risk_exposure: float
    pre_crisis_exposure: float
    cs300_3m_lte: float
    cs300_6m_lte: float
    cs300_12m_lte: float
    dd60_lte: float
    dd120_lte: float
    ma200_lte: float
    min_bad_signals: int


def pct_token(value: float) -> str:
    sign = "n" if value < 0 else "p"
    return f"{sign}{abs(int(round(value * 100))):02d}"


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        prefix = OUT_DIR / (
            "scorecard_csi_pre_option_regime_defense_"
            f"{args.underlying_mode}_miss{args.missing_package_policy}"
        )
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def build_rules() -> list[PreOptionRegimeRule]:
    rules: list[PreOptionRegimeRule] = []
    for listed_stop_loss_pct in [-0.02, -0.03, -0.04]:
        for pre_normal_exposure in [1.0, 1.15]:
            for pre_risk_exposure in [0.0, 0.15, 0.30]:
                for pre_crisis_exposure in [0.0]:
                    for cs300_3m_lte in [-0.06, -0.10]:
                        for cs300_6m_lte in [-0.12, -0.18]:
                            for cs300_12m_lte in [-0.20, -0.30, -0.40]:
                                for dd60_lte in [-0.10, -0.15]:
                                    for dd120_lte in [-0.15, -0.25, -0.35]:
                                        for ma200_lte in [0.0, -0.10]:
                                            for min_bad_signals in [1, 2]:
                                                name = (
                                                    f"lststop{int(abs(listed_stop_loss_pct) * 100):02d}"
                                                    f"_preopt_norm{int(pre_normal_exposure * 100)}"
                                                    f"_risk{int(pre_risk_exposure * 100)}"
                                                    f"_cr{int(pre_crisis_exposure * 100)}"
                                                    f"_r3{pct_token(cs300_3m_lte)}"
                                                    f"_r6{pct_token(cs300_6m_lte)}"
                                                    f"_r12{pct_token(cs300_12m_lte)}"
                                                    f"_d60{pct_token(dd60_lte)}"
                                                    f"_d120{pct_token(dd120_lte)}"
                                                    f"_ma{pct_token(ma200_lte)}"
                                                    f"_sig{min_bad_signals}"
                                                )
                                                rules.append(
                                                    PreOptionRegimeRule(
                                                        name=name,
                                                        listed_stop_loss_pct=listed_stop_loss_pct,
                                                        pre_normal_exposure=pre_normal_exposure,
                                                        pre_risk_exposure=pre_risk_exposure,
                                                        pre_crisis_exposure=pre_crisis_exposure,
                                                        cs300_3m_lte=cs300_3m_lte,
                                                        cs300_6m_lte=cs300_6m_lte,
                                                        cs300_12m_lte=cs300_12m_lte,
                                                        dd60_lte=dd60_lte,
                                                        dd120_lte=dd120_lte,
                                                        ma200_lte=ma200_lte,
                                                        min_bad_signals=min_bad_signals,
                                                    )
                                                )
    return rules


def price_index(rows: list[tuple[dt.date, float]], boundary: dt.date) -> int:
    return bisect_right(rows, (boundary, math.inf)) - 1


def trailing_drawdown(rows: list[tuple[dt.date, float]], boundary: dt.date, points: int) -> float | None:
    idx = price_index(rows, boundary)
    if idx < 0:
        return None
    window = rows[max(0, idx - points + 1) : idx + 1]
    if not window:
        return None
    current = window[-1][1]
    peak = max(close for _day, close in window)
    if peak <= 0:
        return None
    return current / peak - 1.0


def ma_ratio(rows: list[tuple[dt.date, float]], boundary: dt.date, points: int) -> float | None:
    idx = price_index(rows, boundary)
    if idx + 1 < points:
        return None
    window = rows[idx - points + 1 : idx + 1]
    current = window[-1][1]
    avg = sum(close for _day, close in window) / len(window)
    if avg <= 0:
        return None
    return current / avg - 1.0


def price_at_rows(rows: list[tuple[dt.date, float]], boundary: dt.date) -> float | None:
    idx = price_index(rows, boundary)
    return rows[idx][1] if idx >= 0 else None


def proxy_path(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    start_price = price_at_rows(rows, start)
    if not start_price or start_price <= 0:
        return []
    left = bisect_right(rows, (start, math.inf))
    right = bisect_right(rows, (end, math.inf))
    return [
        {"day": day, "risky_to_date": close / start_price - 1.0}
        for day, close in rows[left:right]
        if close and close > 0
    ]


def prepare_proxy_paths(
    mtm_cases: list[dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> None:
    rows = series[CS300_CODE]
    for mtm_case in mtm_cases:
        for period in mtm_case["periods"]:
            if (
                ENABLE_PROXY_STOP
                and
                period["start_exec"] >= OPTION_ERA_START
                and (period["source"] != "listed_contract" or period["daily_points"] <= 0)
            ):
                period["proxy_points"] = proxy_path(rows, period["start_exec"], period["end_exec"])
            else:
                period["proxy_points"] = []


def build_feature_map(
    mtm_cases: list[dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> dict[dt.date, dict[str, float | None]]:
    rows = series[CS300_CODE]
    starts = sorted({period["start_exec"] for case in mtm_cases for period in case["periods"]})
    features: dict[dt.date, dict[str, float | None]] = {}
    for start in starts:
        features[start] = {
            "cs300_3m": period_return(series, CS300_CODE, month_end_shift(start, -3), start),
            "cs300_6m": period_return(series, CS300_CODE, month_end_shift(start, -6), start),
        "cs300_12m": period_return(series, CS300_CODE, month_end_shift(start, -12), start),
            "dd60": trailing_drawdown(rows, start, 60),
            "dd120": trailing_drawdown(rows, start, 120),
            "ma200": ma_ratio(rows, start, 200),
        }
    return features


def listed_stop_name(stop_loss_pct: float) -> str:
    return f"mtmstop{int(abs(stop_loss_pct) * 100):02d}_x100_post0"


def prepare_fixed_listed_stop(mtm_cases: list[dict[str, Any]], stop_loss_values: list[float]) -> None:
    listed_rules = [MtmStopRule(listed_stop_name(value), value, 1.0, 0.0) for value in stop_loss_values]
    for mtm_case in mtm_cases:
        for period in mtm_case["periods"]:
            period["no_stop_risky"] = period["base_without_package"] + period["package_end_return"]
            period["fixed_listed_returns"] = {}
            if period["source"] == "listed_contract":
                for listed_rule in listed_rules:
                    period_ret, stopped = period_return_with_stop(period, listed_rule)
                    period["fixed_listed_returns"][listed_rule.name] = {
                        "return": period_ret,
                        "stopped": stopped,
                    }


def is_bad(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def pre_option_exposure(rule: PreOptionRegimeRule, features: dict[str, float | None]) -> tuple[float, list[str]]:
    bubble_reversal = (
        features["cs300_12m"] is not None
        and features["cs300_3m"] is not None
        and features["cs300_12m"] >= BUBBLE_REVERSAL_12M_GTE
        and features["cs300_3m"] <= BUBBLE_REVERSAL_3M_LTE
    )
    signal_checks = [
        ("cs300_3m", is_bad(features["cs300_3m"], rule.cs300_3m_lte)),
        ("cs300_6m", is_bad(features["cs300_6m"], rule.cs300_6m_lte)),
        ("cs300_12m", is_bad(features["cs300_12m"], rule.cs300_12m_lte)),
        ("dd60", is_bad(features["dd60"], rule.dd60_lte)),
        ("dd120", is_bad(features["dd120"], rule.dd120_lte)),
        ("ma200", is_bad(features["ma200"], rule.ma200_lte)),
        ("bubble_reversal", bubble_reversal),
    ]
    active = [name for name, hit in signal_checks if hit]
    crisis = (
        is_bad(features["cs300_12m"], rule.cs300_12m_lte)
        and is_bad(features["dd120"], rule.dd120_lte)
    ) or len(active) >= rule.min_bad_signals + 2
    if crisis:
        return rule.pre_crisis_exposure, ["pre_option_crisis", *active]
    if len(active) >= rule.min_bad_signals:
        return rule.pre_risk_exposure, ["pre_option_risk", *active]
    return rule.pre_normal_exposure, []


def period_return_pre_option(
    period: dict[str, Any],
    rule: PreOptionRegimeRule,
    features: dict[str, float | None],
    force_normal_exposure: bool = False,
) -> tuple[float, float, str, list[str], bool]:
    no_stop_risky = period["no_stop_risky"]
    safe_return = period["safe_return"]
    if force_normal_exposure:
        exposure = 1.0
        reasons = ["listed_gap_proxy"]
    else:
        exposure, reasons = pre_option_exposure(rule, features)
    proxy_points = period.get("proxy_points") or []
    if proxy_points and exposure > 0:
        total_days = max(len(proxy_points), 1)
        for idx, point in enumerate(proxy_points, start=1):
            fraction = min(1.0, idx / total_days)
            risky_to_date = float(point["risky_to_date"])
            blended_to_date = exposure * risky_to_date + (1.0 - exposure) * safe_return * fraction
            if blended_to_date <= PROXY_STOP_LOSS_PCT:
                remaining = max(0.0, 1.0 - fraction)
                risky_after = no_stop_risky - risky_to_date
                after_stop = (
                    PROXY_POST_STOP_EXPOSURE * risky_after
                    + (1.0 - PROXY_POST_STOP_EXPOSURE) * safe_return * remaining
                )
                return blended_to_date + after_stop, exposure, "proxy_stopped", reasons, True
    period_ret = exposure * no_stop_risky + (1.0 - exposure) * safe_return
    regime = reasons[0] if reasons else "pre_option_normal"
    return period_ret, exposure, regime, reasons, False


def run_case(
    mtm_case: dict[str, Any],
    rule: PreOptionRegimeRule,
    feature_map: dict[dt.date, dict[str, float | None]],
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    pre_option_months = 0
    pre_option_risk_months = 0
    pre_option_crisis_months = 0
    proxy_stop_months = 0
    listed_stop_months = 0
    listed_mtm_months = 0
    severe_loss_months = 0
    severe_loss_defended = 0
    exposures: list[float] = []
    for period in mtm_case["periods"]:
        no_stop_risky = period["no_stop_risky"]
        severe = no_stop_risky <= -0.10
        if severe:
            severe_loss_months += 1
        if period["source"] == "listed_contract" and period["daily_points"] > 0:
            listed_item = period["fixed_listed_returns"][listed_stop_name(rule.listed_stop_loss_pct)]
            period_ret = listed_item["return"]
            stopped = listed_item["stopped"]
            exposure = 1.0
            regime = "listed_option_mtm"
            if stopped:
                listed_stop_months += 1
                regime = "listed_option_stopped"
            if period["daily_points"] > 0:
                listed_mtm_months += 1
        else:
            if period["source"] != "listed_contract":
                pre_option_months += 1
            features = feature_map[period["start_exec"]]
            period_ret, exposure, regime, _reasons, proxy_stopped = period_return_pre_option(
                period,
                rule,
                features,
                force_normal_exposure=period["source"] == "listed_contract",
            )
            if regime == "pre_option_risk":
                pre_option_risk_months += 1
            elif regime == "pre_option_crisis":
                pre_option_crisis_months += 1
            elif regime == "proxy_stopped":
                proxy_stop_months += 1
            if severe and (exposure < rule.pre_normal_exposure or proxy_stopped):
                severe_loss_defended += 1
        exposures.append(exposure)
        capital *= 1.0 + period_ret
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
        "target_met": capital >= TARGET_CAPITAL and mdd >= USER_TARGET_MDD,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "pre_option_months": pre_option_months,
        "pre_option_risk_months": pre_option_risk_months,
        "pre_option_crisis_months": pre_option_crisis_months,
        "proxy_stop_months": proxy_stop_months,
        "listed_stop_months": listed_stop_months,
        "listed_mtm_months": listed_mtm_months,
        "severe_loss_months": severe_loss_months,
        "severe_loss_defended": severe_loss_defended,
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
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in cases),
        "median_pre_option_risk_months": statistics.median(item["pre_option_risk_months"] for item in cases),
        "median_pre_option_crisis_months": statistics.median(item["pre_option_crisis_months"] for item in cases),
        "median_proxy_stop_months": statistics.median(item["proxy_stop_months"] for item in cases),
        "median_listed_stop_months": statistics.median(item["listed_stop_months"] for item in cases),
        "median_severe_loss_months": statistics.median(item["severe_loss_months"] for item in cases),
        "median_severe_loss_defended": statistics.median(item["severe_loss_defended"] for item in cases),
    }


def evaluate_rule(
    mtm_cases: list[dict[str, Any]],
    rule: PreOptionRegimeRule,
    feature_map: dict[dt.date, dict[str, float | None]],
) -> dict[str, Any]:
    cases = [run_case(mtm_case, rule, feature_map) for mtm_case in mtm_cases]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": (
            "Search observable pre-option CS300 regime defenses while keeping the listed-option "
            "daily-MTM stop fixed."
        ),
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": USER_TARGET_MDD,
        "cash_annual_rate": CASH_ANNUAL_RATE,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "listed_option_stop_values": [-0.02, -0.03, -0.04],
            "proxy_stop_loss_pct": PROXY_STOP_LOSS_PCT,
            "proxy_post_stop_exposure": PROXY_POST_STOP_EXPOSURE,
            "bubble_reversal_12m_gte": BUBBLE_REVERSAL_12M_GTE,
            "bubble_reversal_3m_lte": BUBBLE_REVERSAL_3M_LTE,
            "pre_option_features": ["cs300_3m", "cs300_6m", "cs300_12m", "dd60", "dd120", "ma200"],
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "listed_stop_loss_pct",
        "pre_normal_exposure",
        "pre_risk_exposure",
        "pre_crisis_exposure",
        "cs300_3m_lte",
        "cs300_6m_lte",
        "cs300_12m_lte",
        "dd60_lte",
        "dd120_lte",
        "ma200_lte",
        "min_bad_signals",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_avg_exposure",
        "median_pre_option_risk_months",
        "median_pre_option_crisis_months",
        "median_proxy_stop_months",
        "median_listed_stop_months",
        "median_severe_loss_months",
        "median_severe_loss_defended",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({field: row.get(field) for field in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search pre-option CS300 regime defenses.")
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
        csi_series = load_price_series(conn)
        pricer = HistoricalCnPackagePricer(
            conn,
            package,
            args.underlying_mode,
            args.max_quote_stale_days,
            args.slippage_bps_per_leg,
            args.missing_package_policy,
        )
        mtm = DailyMtmPricer(pricer, csi_series)
        mtm_cases = build_mtm_cases(raw_cases, mtm)
    finally:
        conn.close()

    prepare_fixed_listed_stop(mtm_cases, [-0.02, -0.03, -0.04])
    prepare_proxy_paths(mtm_cases, csi_series)
    feature_map = build_feature_map(mtm_cases, csi_series)
    meta = {
        **{key: value for key, value in setup_meta.items() if key != "csi_series"},
        "daily_mtm_coverage": mtm.summary(),
    }
    results = [evaluate_rule(mtm_cases, rule, feature_map) for rule in build_rules()]
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
            f"{item['rule']['name']:<96} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"risk_med={summary['median_pre_option_risk_months']:.1f} "
            f"crisis_med={summary['median_pre_option_crisis_months']:.1f} "
            f"proxy_stop_med={summary['median_proxy_stop_months']:.1f}"
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

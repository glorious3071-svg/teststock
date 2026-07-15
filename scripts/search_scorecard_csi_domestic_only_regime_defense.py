#!/usr/bin/env python3
"""Search A-share-only scorecard+CSI regime defenses.

This search deliberately excludes overseas sleeves.  The risky engine is the
existing CSI phase-ensemble selector using domestic cash/gold defensive choices,
plus listed China ETF option package returns where local historical quotes exist.
Pre-option months use observable CSI300 regime features; listed-option months can
use real option-leg daily MTM stops.
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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    GOLD_CODE,
    MONTH_PHASES,
    cash_return,
    month_end_shift,
    monthly_boundaries,
    period_return,
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
    defensive_return,
    ensemble_state,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_cn_option_package_daily_mtm_stop import DailyMtmPricer
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape

OUT_DIR = ROOT / "data" / "backtests"
USER_TARGET_MDD = -0.10
BUBBLE_REVERSAL_12M_GTE = 1.0
BUBBLE_REVERSAL_3M_LTE = -0.03

PHASE_RULE_BY_NAME = {rule.name: rule for rule in PHASE_RULES}
DOMESTIC_PHASE_RULE_NAMES = [
    "phase12_lever120_cash",
    "phase12_lever150_cash",
    "phase12_guard60_cash",
    "phase12_guard40_cash",
    "phase12_lever120_dd_cash",
    "phase12_mean_cash",
    "phase12_mean_gold",
]


@dataclass(frozen=True)
class DomesticOnlyRule:
    name: str
    phase_rule_name: str
    listed_stop_loss_pct: float
    listed_normal_exposure: float
    listed_post_stop_exposure: float
    pre_normal_exposure: float
    pre_risk_exposure: float
    pre_crisis_exposure: float
    pre_stop_loss_pct: float
    pre_post_stop_exposure: float
    drawdown_guard_lte: float
    drawdown_guard_scale: float
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


def short_phase_name(name: str) -> str:
    return (
        name.replace("phase12_", "p12_")
        .replace("lever", "l")
        .replace("guard", "g")
        .replace("mean", "mean")
        .replace("_cash", "c")
        .replace("_gold", "gld")
        .replace("_dd", "dd")
    )


def build_rules(quick: bool = False, max_rules: int = 0) -> list[DomesticOnlyRule]:
    rules: list[DomesticOnlyRule] = []
    phase_rule_names = DOMESTIC_PHASE_RULE_NAMES
    listed_stop_loss_values = [-0.02, -0.03, -0.04, -0.05]
    listed_exposures = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
    listed_post_exposures = [0.0, 0.15]
    pre_normal_values = [1.0, 1.15, 1.3, 1.5]
    pre_risk_values = [0.0, 0.15, 0.30]
    cs300_3m_values = [-0.06, -0.10]
    cs300_6m_values = [-0.12, -0.18]
    cs300_12m_values = [-0.20, -0.30, -0.40]
    dd120_values = [-0.15, -0.25]
    if quick:
        phase_rule_names = ["phase12_lever120_cash", "phase12_lever150_cash", "phase12_guard60_cash"]
        listed_stop_loss_values = [-0.01, -0.015, -0.02]
        listed_exposures = [1.5, 2.0, 2.5, 3.0]
        listed_post_exposures = [0.0]
        pre_normal_values = [1.0, 1.15]
        pre_risk_values = [0.0, 0.30]
        cs300_3m_values = [-0.10]
        cs300_6m_values = [-0.18]
        cs300_12m_values = [-0.40]
        dd120_values = [-0.15]

    for phase_rule_name in phase_rule_names:
        for listed_stop_loss_pct in listed_stop_loss_values:
            for listed_normal_exposure in listed_exposures:
                for listed_post_stop_exposure in listed_post_exposures:
                    for pre_normal_exposure in pre_normal_values:
                        for pre_risk_exposure in pre_risk_values:
                            pre_stop_values = [-0.015, -0.025, -0.04] if quick else [-0.01, -0.015, -0.025, -0.04, -0.06]
                            drawdown_guards = [(-1.0, 1.0)]
                            if quick:
                                drawdown_guards = [(-0.06, 0.0), (-0.08, 0.25), (-1.0, 1.0)]
                            for pre_stop_loss_pct in pre_stop_values:
                                for drawdown_guard_lte, drawdown_guard_scale in drawdown_guards:
                                    for cs300_3m_lte in cs300_3m_values:
                                        for cs300_6m_lte in cs300_6m_values:
                                            for cs300_12m_lte in cs300_12m_values:
                                                for dd120_lte in dd120_values:
                                                    name = (
                                                        f"dom_{short_phase_name(phase_rule_name)}"
                                                        f"_lst{int(abs(listed_stop_loss_pct) * 100):02d}"
                                                        f"_lx{int(listed_normal_exposure * 100)}"
                                                        f"_post{int(listed_post_stop_exposure * 100)}"
                                                        f"_pn{int(pre_normal_exposure * 100)}"
                                                        f"_pr{int(pre_risk_exposure * 100)}"
                                                        f"_pst{int(abs(pre_stop_loss_pct) * 100):02d}"
                                                        f"_dg{int(abs(drawdown_guard_lte) * 100):02d}"
                                                        f"s{int(drawdown_guard_scale * 100)}"
                                                        f"_r3{pct_token(cs300_3m_lte)}"
                                                        f"_r6{pct_token(cs300_6m_lte)}"
                                                        f"_r12{pct_token(cs300_12m_lte)}"
                                                        f"_d120{pct_token(dd120_lte)}"
                                                    )
                                                    rules.append(
                                                        DomesticOnlyRule(
                                                            name=name,
                                                            phase_rule_name=phase_rule_name,
                                                            listed_stop_loss_pct=listed_stop_loss_pct,
                                                            listed_normal_exposure=listed_normal_exposure,
                                                            listed_post_stop_exposure=listed_post_stop_exposure,
                                                            pre_normal_exposure=pre_normal_exposure,
                                                            pre_risk_exposure=pre_risk_exposure,
                                                            pre_crisis_exposure=0.0,
                                                            pre_stop_loss_pct=pre_stop_loss_pct,
                                                            pre_post_stop_exposure=0.0,
                                                            drawdown_guard_lte=drawdown_guard_lte,
                                                            drawdown_guard_scale=drawdown_guard_scale,
                                                            cs300_3m_lte=cs300_3m_lte,
                                                            cs300_6m_lte=cs300_6m_lte,
                                                            cs300_12m_lte=cs300_12m_lte,
                                                            dd60_lte=-0.15,
                                                            dd120_lte=dd120_lte,
                                                            ma200_lte=0.0,
                                                            min_bad_signals=1,
                                                        )
                                                    )
                                                    if max_rules > 0 and len(rules) >= max_rules:
                                                        return rules
    return rules


def load_domestic_price_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    holdings = load_hybrid_holdings()
    codes = sorted({CS300_CODE, *[code for rows in holdings.values() for code in rows]})
    series: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
    with conn.cursor() as cur:
        for chunk_start in range(0, len(codes), 500):
            chunk = codes[chunk_start : chunk_start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM index_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, trade_date, close in cur.fetchall():
                series.setdefault(str(code), []).append((trade_date, float(close)))
        cur.execute(
            """
            SELECT trade_date, close
            FROM gold_daily
            WHERE symbol=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (GOLD_CODE,),
        )
        series[GOLD_CODE] = [(trade_date, float(close)) for trade_date, close in cur.fetchall()]
    return series


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
    return current / peak - 1.0 if peak > 0 else None


def ma_ratio(rows: list[tuple[dt.date, float]], boundary: dt.date, points: int) -> float | None:
    idx = price_index(rows, boundary)
    if idx + 1 < points:
        return None
    window = rows[idx - points + 1 : idx + 1]
    current = window[-1][1]
    avg = sum(close for _day, close in window) / len(window)
    return current / avg - 1.0 if avg > 0 else None


def build_feature_map(cases: list[dict[str, Any]], series: dict[str, list[tuple[dt.date, float]]]) -> dict[dt.date, dict[str, float | None]]:
    rows = series[CS300_CODE]
    starts = sorted({period["start_exec"] for case in cases for period in case["periods"]})
    return {
        start: {
            "cs300_3m": period_return(series, CS300_CODE, month_end_shift(start, -3), start),
            "cs300_6m": period_return(series, CS300_CODE, month_end_shift(start, -6), start),
            "cs300_12m": period_return(series, CS300_CODE, month_end_shift(start, -12), start),
            "dd60": trailing_drawdown(rows, start, 60),
            "dd120": trailing_drawdown(rows, start, 120),
            "ma200": ma_ratio(rows, start, 200),
        }
        for start in starts
    }


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


def is_bad(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def pre_option_exposure(rule: DomesticOnlyRule, features: dict[str, float | None]) -> tuple[float, list[str]]:
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


def build_domestic_cases(args) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[tuple[dt.date, float]]]]:
    package_shape = load_package_shape()
    package_scale = float(getattr(args, "package_scale", 1.0) or 1.0)
    if package_scale <= 0:
        raise ValueError("--package-scale must be positive")
    if package_scale != 1.0:
        package_shape = replace(
            package_shape,
            source_json=f"{package_shape.source_json}#scale={package_scale:g}",
            long_put_notional_pct=package_shape.long_put_notional_pct * package_scale,
            short_call_notional_pct=package_shape.short_call_notional_pct * package_scale,
            cn_net_debit_pct=package_shape.cn_net_debit_pct * package_scale,
            total_net_debit_pct=package_shape.total_net_debit_pct * package_scale,
            margin_proxy_pct=package_shape.margin_proxy_pct * package_scale,
        )
    conn = get_connection()
    try:
        series = load_domestic_price_series(conn)
        trade_dates = [day for day, _px in series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        pricer = HistoricalCnPackagePricer(
            conn,
            package_shape,
            args.underlying_mode,
            args.max_quote_stale_days,
            args.slippage_bps_per_leg,
            args.missing_package_policy,
        )
        mtm = DailyMtmPricer(pricer, series)
        cases: list[dict[str, Any]] = []
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                periods = []
                listed_package_months = 0
                missing_package_months = 0
                for phase_rule_name in DOMESTIC_PHASE_RULE_NAMES:
                    if phase_rule_name not in PHASE_RULE_BY_NAME:
                        raise RuntimeError(f"missing phase rule: {phase_rule_name}")
                for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
                    start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
                    end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
                    package_path = mtm.package_path(start_exec, end_exec, INITIAL_CAPITAL)
                    if package_path["source"] == "listed_contract":
                        listed_package_months += 1
                    else:
                        missing_package_months += 1
                    period_by_phase: dict[str, dict[str, Any]] = {}
                    for phase_rule_name in DOMESTIC_PHASE_RULE_NAMES:
                        phase_rule = PHASE_RULE_BY_NAME[phase_rule_name]
                        equity_pct, equity_return, sleeves, reasons = ensemble_state(
                            conn,
                            series,
                            holdings,
                            phase_rule,
                            start_snapshot,
                            start_exec,
                            end_exec,
                            0.0,
                        )
                        equity_weight = equity_pct / 100.0
                        def_return, defensive_asset = defensive_return(series, [], phase_rule, start_exec, end_exec)
                        financing_return = cash_return(start_exec, end_exec)
                        non_equity_return = financing_return if equity_weight > 1.0 else def_return
                        base_without_package = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
                        period_by_phase[phase_rule_name] = {
                            "base_without_package": base_without_package,
                            "equity_pct": equity_pct,
                            "equity_return": equity_return,
                            "defensive_asset": defensive_asset,
                            "defensive_return": def_return,
                            "rebalance_reasons": reasons,
                            "sleeves": sleeves,
                        }
                    periods.append(
                        {
                            "start_snapshot": start_snapshot,
                            "end_snapshot": end_snapshot,
                            "start_exec": start_exec,
                            "end_exec": end_exec,
                            "safe_return": cash_return(start_exec, end_exec),
                            "package_source": package_path["source"],
                            "package_end_return": float(package_path["end_return"]),
                            "points": package_path["points"],
                            "daily_points": package_path["daily_points"],
                            "proxy_points": proxy_path(series[CS300_CODE], start_exec, end_exec),
                            "phase": period_by_phase,
                        }
                    )
                cases.append(
                    {
                        "phase_month_offset": phase,
                        "execution_lag_days": lag,
                        "periods": periods,
                        "listed_package_months": listed_package_months,
                        "missing_package_months": missing_package_months,
                    }
                )
        meta = {
            "package_shape_source": asdict(package_shape),
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "quote_dates_available": sum(len(items) for items in pricer.quote_dates.values()),
            "quote_dates_by_opt_code": {key: len(value) for key, value in pricer.quote_dates.items()},
            "quote_dates_used": len(pricer.used_quote_dates),
            "missing_reasons": pricer.missing_reasons,
            "daily_mtm_coverage": mtm.summary(),
            "domestic_only_universe": [
                "CSI index baskets from index_daily",
                "cash financing via project CASH_ANNUAL_RATE",
                f"domestic gold spot proxy {GOLD_CODE} only for rules that request gold",
                "SSE ETF options OP510050.SH / OP510300.SH from cn_option_daily and cn_option_contract_archive",
            ],
            "excluded_assets": ["QQQ", "SHY", "BTC-USD", "ETH-USD", "US10Y_PROXY", "SPX.US"],
        }
    finally:
        conn.close()
    return cases, meta, series


def period_return_with_listed_stop(
    period: dict[str, Any],
    base_without_package: float,
    rule: DomesticOnlyRule,
    normal_exposure: float | None = None,
    post_stop_exposure: float | None = None,
) -> tuple[float, bool]:
    no_stop_risky = base_without_package + period["package_end_return"]
    safe_return = period["safe_return"]
    normal_exposure = rule.listed_normal_exposure if normal_exposure is None else normal_exposure
    post_stop_exposure = rule.listed_post_stop_exposure if post_stop_exposure is None else post_stop_exposure
    points = period["points"]
    if not points:
        return (
            normal_exposure * no_stop_risky
            + (1.0 - normal_exposure) * safe_return,
            False,
        )
    total_days = max(len(points), 1)
    for idx, point in enumerate(points, start=1):
        fraction = min(1.0, idx / total_days)
        risky_to_date = base_without_package * fraction + float(point["package_return"])
        blended_to_date = (
            normal_exposure * risky_to_date
            + (1.0 - normal_exposure) * safe_return * fraction
        )
        if blended_to_date <= rule.listed_stop_loss_pct:
            remaining = max(0.0, 1.0 - fraction)
            risky_after = no_stop_risky - risky_to_date
            after_stop = (
                post_stop_exposure * risky_after
                + (1.0 - post_stop_exposure) * safe_return * remaining
            )
            return blended_to_date + after_stop, True
    return (
        normal_exposure * no_stop_risky
        + (1.0 - normal_exposure) * safe_return,
        False,
    )


def period_return_with_pre_stop(
    period: dict[str, Any],
    no_stop_risky: float,
    exposure: float,
    rule: DomesticOnlyRule,
) -> tuple[float, bool]:
    safe_return = float(period["safe_return"])
    points = period.get("proxy_points") or []
    if not points or exposure <= 0:
        return exposure * no_stop_risky + (1.0 - exposure) * safe_return, False
    total_days = max(len(points), 1)
    for idx, point in enumerate(points, start=1):
        fraction = min(1.0, idx / total_days)
        risky_to_date = float(point["risky_to_date"])
        blended_to_date = exposure * risky_to_date + (1.0 - exposure) * safe_return * fraction
        if blended_to_date <= rule.pre_stop_loss_pct:
            remaining = max(0.0, 1.0 - fraction)
            risky_after = no_stop_risky - risky_to_date
            after_stop = (
                rule.pre_post_stop_exposure * risky_after
                + (1.0 - rule.pre_post_stop_exposure) * safe_return * remaining
            )
            return blended_to_date + after_stop, True
    return exposure * no_stop_risky + (1.0 - exposure) * safe_return, False


def run_case(
    domestic_case: dict[str, Any],
    rule: DomesticOnlyRule,
    feature_map: dict[dt.date, dict[str, float | None]],
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    listed_stop_months = 0
    listed_mtm_months = 0
    pre_option_risk_months = 0
    pre_option_crisis_months = 0
    pre_stop_months = 0
    severe_loss_months = 0
    severe_loss_defended = 0
    exposures: list[float] = []
    for period in domestic_case["periods"]:
        current_drawdown = capital / peak - 1.0
        guard_scale = rule.drawdown_guard_scale if current_drawdown <= rule.drawdown_guard_lte else 1.0
        phase_item = period["phase"][rule.phase_rule_name]
        base_without_package = float(phase_item["base_without_package"])
        no_stop_risky = base_without_package + float(period["package_end_return"])
        severe = no_stop_risky <= -0.10
        if severe:
            severe_loss_months += 1
        if period["package_source"] == "listed_contract" and period["daily_points"] > 0:
            listed_normal_exposure = rule.listed_normal_exposure * guard_scale
            listed_post_stop_exposure = rule.listed_post_stop_exposure * guard_scale
            period_ret, stopped = period_return_with_listed_stop(
                period,
                base_without_package,
                rule,
                normal_exposure=listed_normal_exposure,
                post_stop_exposure=listed_post_stop_exposure,
            )
            exposure = listed_normal_exposure
            if stopped:
                listed_stop_months += 1
                if severe:
                    severe_loss_defended += 1
            listed_mtm_months += 1
        else:
            features = feature_map[period["start_exec"]]
            exposure, reasons = pre_option_exposure(rule, features)
            exposure *= guard_scale
            if reasons and reasons[0] == "pre_option_risk":
                pre_option_risk_months += 1
            elif reasons and reasons[0] == "pre_option_crisis":
                pre_option_crisis_months += 1
            if severe and exposure < rule.pre_normal_exposure:
                severe_loss_defended += 1
            period_ret, pre_stopped = period_return_with_pre_stop(period, no_stop_risky, exposure, rule)
            if pre_stopped:
                pre_stop_months += 1
                if severe:
                    severe_loss_defended += 1
        exposures.append(exposure)
        capital *= 1.0 + period_ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "name": f"{rule.name}_phase{domestic_case['phase_month_offset']}_lag{domestic_case['execution_lag_days']}",
        "rule": rule.name,
        "phase_month_offset": domestic_case["phase_month_offset"],
        "execution_lag_days": domestic_case["execution_lag_days"],
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= USER_TARGET_MDD,
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "listed_stop_months": listed_stop_months,
        "listed_mtm_months": listed_mtm_months,
        "pre_option_risk_months": pre_option_risk_months,
        "pre_option_crisis_months": pre_option_crisis_months,
        "pre_stop_months": pre_stop_months,
        "severe_loss_months": severe_loss_months,
        "severe_loss_defended": severe_loss_defended,
        "listed_package_months": domestic_case["listed_package_months"],
        "missing_package_months": domestic_case["missing_package_months"],
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
        "median_listed_stop_months": statistics.median(item["listed_stop_months"] for item in cases),
        "median_pre_option_risk_months": statistics.median(item["pre_option_risk_months"] for item in cases),
        "median_pre_option_crisis_months": statistics.median(item["pre_option_crisis_months"] for item in cases),
        "median_pre_stop_months": statistics.median(item["pre_stop_months"] for item in cases),
        "median_severe_loss_months": statistics.median(item["severe_loss_months"] for item in cases),
        "median_severe_loss_defended": statistics.median(item["severe_loss_defended"] for item in cases),
    }


def evaluate_rule(cases: list[dict[str, Any]], rule: DomesticOnlyRule, feature_map: dict[dt.date, dict[str, float | None]]) -> dict[str, Any]:
    scenario_cases = [run_case(case, rule, feature_map) for case in cases]
    summary = matrix_summary(scenario_cases)
    return {"rule": asdict(rule), "cases": scenario_cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        prefix = OUT_DIR / (
            "scorecard_csi_domestic_only_regime_defense_"
            f"{args.underlying_mode}_miss{args.missing_package_policy}"
        )
    return Path(f"{prefix}_report.json"), Path(f"{prefix}_search.csv")


def write_outputs(results: list[dict[str, Any]], meta: dict[str, Any], args) -> tuple[Path, Path]:
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search A-share-only CSI scorecard portfolios with domestic listed ETF option packages.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": USER_TARGET_MDD,
        "assumptions": {
            "underlying_mode": args.underlying_mode,
            "missing_package_policy": args.missing_package_policy,
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "package_scale": package_scale,
            "bubble_reversal_12m_gte": BUBBLE_REVERSAL_12M_GTE,
            "bubble_reversal_3m_lte": BUBBLE_REVERSAL_3M_LTE,
            "no_overseas_assets": True,
        },
        **meta,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "phase_rule_name",
        "listed_stop_loss_pct",
        "listed_normal_exposure",
        "listed_post_stop_exposure",
        "pre_normal_exposure",
        "pre_risk_exposure",
        "pre_crisis_exposure",
        "pre_stop_loss_pct",
        "pre_post_stop_exposure",
        "drawdown_guard_lte",
        "drawdown_guard_scale",
        "cs300_3m_lte",
        "cs300_6m_lte",
        "cs300_12m_lte",
        "dd120_lte",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_avg_exposure",
        "median_listed_stop_months",
        "median_pre_option_risk_months",
        "median_pre_option_crisis_months",
        "median_pre_stop_months",
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
    parser = argparse.ArgumentParser(description="Search A-share-only scorecard+CSI regime defenses.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--package-scale", type=float, default=1.0, help="Scale current CN ETF option package notional.")
    parser.add_argument("--output-prefix")
    parser.add_argument("--quick", action="store_true", help="Run a small representative grid for direction-finding.")
    parser.add_argument("--max-rules", type=int, default=0, help="Stop after this many generated rules; 0 means no cap.")
    args = parser.parse_args()

    cases, meta, series = build_domestic_cases(args)
    feature_map = build_feature_map(cases, series)
    rules = build_rules(quick=args.quick, max_rules=args.max_rules)
    results = [evaluate_rule(cases, rule, feature_map) for rule in rules]
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
            f"{item['rule']['name']:<112} pass={summary['pass_count']:>2}/{summary['count']} "
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

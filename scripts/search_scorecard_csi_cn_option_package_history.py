#!/usr/bin/env python3
"""Historical test of the current China ETF option-package CSI hedge shape."""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME, run_case as run_core_case
from scripts.backtest_scorecard_csi_blended_protection import load_option_data, precompute_csi_paths, precompute_option_paths
from scripts.backtest_scorecard_csi_crypto_satellite_mix import CORE_RULE_BY_NAME, SATELLITE_RULE_BY_NAME, crypto_period_returns
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import load_data
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series, period_return
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_cn_option_package_history_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_cn_option_package_history_search.csv"
PORTFOLIO_DIR = ROOT / "data" / "portfolio"


@dataclass(frozen=True)
class CnOptionPackageShape:
    source_json: str
    underlying_index_code: str
    long_put_strike_pct: float
    long_put_notional_pct: float
    short_call_strike_pct: float
    short_call_notional_pct: float
    cn_net_debit_pct: float
    total_net_debit_pct: float
    margin_proxy_pct: float


@dataclass(frozen=True)
class CnOptionHistoryRule:
    name: str
    core_rule_name: str
    satellite_rule_name: str
    core_weight: float
    satellite_weight: float
    monthly_loss_floor: float
    premium_monthly: float
    upside_haircut: float
    use_modeled_floor: bool
    package: CnOptionPackageShape


def latest_json(pattern: str) -> tuple[Path, dict[str, Any]]:
    paths = sorted(PORTFOLIO_DIR.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"no files match {PORTFOLIO_DIR / pattern}")
    path = paths[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def latest_fund_price(ts_code: str, as_of: dt.date) -> float:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT close
                FROM fund_daily
                WHERE ts_code=%s AND trade_date <= %s AND close IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (ts_code, as_of),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError(f"missing fund_daily close for {ts_code} <= {as_of}")
    return float(row[0])


def load_package_shape() -> CnOptionPackageShape:
    path, payload = latest_json("cn_etf_option_package_hedge_search_*.json")
    item = payload.get("cheapest_budget_all_pass")
    if not item:
        raise RuntimeError(f"{path} has no cheapest_budget_all_pass")
    underlying = str(item["underlying_option_code"]).removeprefix("OP")
    as_of = dt.date.fromisoformat(payload["as_of"])
    spot = latest_fund_price(underlying, as_of)
    long_put_notional_pct = float(item["protected_weight_pct"]) / 100.0
    short_call_notional_pct = (
        float(item["short_call_strike"]) * float(item["per_unit"]) * int(item["short_call_contracts"])
    ) / INITIAL_CAPITAL
    return CnOptionPackageShape(
        source_json=str(path.relative_to(ROOT)),
        underlying_index_code=CS300_CODE,
        long_put_strike_pct=float(item["long_put_strike"]) / spot,
        long_put_notional_pct=long_put_notional_pct,
        short_call_strike_pct=float(item["short_call_strike"]) / spot,
        short_call_notional_pct=short_call_notional_pct,
        cn_net_debit_pct=float(item["cn_net_debit_pct"]) / 100.0,
        total_net_debit_pct=float(item["total_net_debit_pct"]) / 100.0,
        margin_proxy_pct=float(item.get("margin_proxy_pct_capital") or 0.0) / 100.0,
    )


def build_rules(package: CnOptionPackageShape) -> list[CnOptionHistoryRule]:
    rules = []
    for use_floor in [False, True]:
        for premium in [0.0, 0.0025, 0.005, 0.0075]:
            rules.append(
                CnOptionHistoryRule(
                    name=(
                        "cnpkg_"
                        + ("floor" if use_floor else "raw")
                        + f"_prem{int(premium * 10000):03d}"
                    ),
                    core_rule_name="core_xbcppi_sub12_spread95_call108",
                    satellite_rule_name="sat_crypto_cppi",
                    core_weight=0.95,
                    satellite_weight=0.08,
                    monthly_loss_floor=-0.01,
                    premium_monthly=premium,
                    upside_haircut=0.0,
                    use_modeled_floor=use_floor,
                    package=package,
                )
            )
    return rules


def as_date(raw: Any) -> dt.date:
    return dt.date.fromisoformat(raw) if isinstance(raw, str) else raw


def package_return(
    series: dict[str, list[tuple[dt.date, float]]],
    package: CnOptionPackageShape,
    start: dt.date,
    end: dt.date,
) -> float:
    ret = period_return(series, package.underlying_index_code, start, end)
    long_put_payoff = package.long_put_notional_pct * max(1.0 - (1.0 + ret) / package.long_put_strike_pct, 0.0)
    short_call_payoff = -package.short_call_notional_pct * max((1.0 + ret) / package.short_call_strike_pct - 1.0, 0.0)
    return long_put_payoff + short_call_payoff - package.cn_net_debit_pct


def matrix_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "pass_count": sum(1 for item in items if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in items),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in items),
        "worst_max_drawdown": min(item["max_drawdown"] for item in items),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in items),
        "min_annualized_return": min(item["annualized_return"] for item in items),
        "median_floor_months": statistics.median(item["floor_months"] for item in items),
        "median_margin_proxy_pct": statistics.median(item["margin_proxy_pct"] for item in items),
    }


def run_case(
    csi_series: dict[str, list[tuple[dt.date, float]]],
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: CnOptionHistoryRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    core_case = core_cache[(rule.core_rule_name, phase, lag)]
    sat_returns = satellite_returns[(rule.satellite_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    curve = [capital]
    floor_months = 0
    positive_call_drag_months = 0
    for row, sat_return in zip(core_case["rows"], sat_returns):
        safe_return = row["safe_return"]
        start_exec = as_date(row["start_exec"])
        end_exec = as_date(row["end_exec"])
        csi_pkg_return = package_return(csi_series, rule.package, start_exec, end_exec)
        if period_return(csi_series, rule.package.underlying_index_code, start_exec, end_exec) > 0:
            positive_call_drag_months += 1
        raw_return = (
            rule.core_weight * row["period_return"]
            + rule.satellite_weight * sat_return
            + (1.0 - rule.core_weight - rule.satellite_weight) * safe_return
            + csi_pkg_return
        )
        if rule.use_modeled_floor:
            portfolio_return = max(rule.monthly_loss_floor, raw_return - rule.premium_monthly)
            if portfolio_return == rule.monthly_loss_floor and raw_return - rule.premium_monthly < rule.monthly_loss_floor:
                floor_months += 1
        else:
            portfolio_return = raw_return - rule.premium_monthly
        if portfolio_return > 0:
            portfolio_return *= 1.0 - rule.upside_haircut
        capital *= 1.0 + portfolio_return
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{phase}_lag{lag}",
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "floor_months": floor_months,
        "positive_call_drag_months": positive_call_drag_months,
        "margin_proxy_pct": rule.package.margin_proxy_pct,
    }


def evaluate_rule(csi_series, core_cache, satellite_returns, rule: CnOptionHistoryRule) -> dict[str, Any]:
    cases = [
        run_case(csi_series, core_cache, satellite_returns, rule, phase, lag)
        for phase in MONTH_PHASES
        for lag in EXECUTION_LAGS
    ]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]], package: CnOptionPackageShape) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test historical rolling shape of the latest China ETF option package hedge.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "package": asdict(package),
        "model_limits": (
            "This is an executable-shape historical proxy: it rolls the current package moneyness and net cost "
            "monthly against CSI300 index returns. It does not use historical option-chain bid/ask, implied vol, "
            "broker margin, exercise/assignment, or actual monthly listed contracts."
        ),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "core_rule_name",
        "satellite_rule_name",
        "core_weight",
        "satellite_weight",
        "monthly_loss_floor",
        "premium_monthly",
        "upside_haircut",
        "use_modeled_floor",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_floor_months",
        "median_margin_proxy_pct",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fields})


def main() -> int:
    package = load_package_shape()
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        core_rule = CORE_RULE_BY_NAME["core_xbcppi_sub12_spread95_call108"]
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].phase_rule_name},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[core_rule.base_blend_name].option_rule_name},
        )
        crypto_data = load_data(conn)
    finally:
        conn.close()

    core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
    for phase in MONTH_PHASES:
        for lag in EXECUTION_LAGS:
            case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
            core_cache[(core_rule.name, phase, lag)] = case
            periods_by_case[(phase, lag)] = [
                (as_date(row["start_exec"]), as_date(row["end_exec"]))
                for row in case["rows"]
            ]

    satellite_rule = SATELLITE_RULE_BY_NAME["sat_crypto_cppi"]
    satellite_returns: dict[tuple[str, int, int], list[float]] = {}
    for phase in MONTH_PHASES:
        for lag in EXECUTION_LAGS:
            satellite_returns[(satellite_rule.name, phase, lag)] = crypto_period_returns(
                crypto_data,
                satellite_rule,
                periods_by_case[(phase, lag)],
                phase,
                lag,
            )

    results = []
    for rule in build_rules(package):
        result = evaluate_rule(csi_series, core_cache, satellite_returns, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name:<28} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:9.1f}w "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"floor={summary['median_floor_months']:4.1f}"
        )

    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    write_outputs(results, package)
    best = results[0]["summary"]
    print(
        f"Wrote {OUT_JSON}; rules={len(results)} "
        f"best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

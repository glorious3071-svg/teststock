#!/usr/bin/env python3
"""Historical listed-contract diagnostic for the China ETF option package."""

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
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME, run_case as run_core_case
from scripts.backtest_scorecard_csi_blended_protection import load_option_data, precompute_csi_paths, precompute_option_paths
from scripts.backtest_scorecard_csi_crypto_satellite_mix import CORE_RULE_BY_NAME, SATELLITE_RULE_BY_NAME, crypto_period_returns
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import load_data
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series, period_return
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields
from scripts.search_scorecard_csi_cn_option_package_history import CnOptionPackageShape, latest_json, load_package_shape

OUT_DIR = ROOT / "data" / "backtests"


@dataclass(frozen=True)
class RealHistoryRule:
    name: str
    core_rule_name: str
    satellite_rule_name: str
    core_weight: float
    satellite_weight: float
    monthly_loss_floor: float
    premium_monthly: float
    use_modeled_floor: bool
    missing_package_policy: str
    package: CnOptionPackageShape


@dataclass(frozen=True)
class OptionRow:
    ts_code: str
    call_put: str
    strike: float
    per_unit: float
    maturity_date: dt.date
    close: float
    vol: float
    oi: float


@dataclass(frozen=True)
class UnderlyingConfig:
    opt_code: str
    fund_code: str
    start_date: dt.date
    end_date: dt.date


UNDERLYING_MODES = {
    "510300_only": [
        UnderlyingConfig("OP510300.SH", "510300.SH", dt.date(2019, 12, 23), dt.date(2100, 1, 1)),
    ],
    "switch_50_to_300": [
        UnderlyingConfig("OP510050.SH", "510050.SH", dt.date(2015, 2, 9), dt.date(2019, 12, 22)),
        UnderlyingConfig("OP510300.SH", "510300.SH", dt.date(2019, 12, 23), dt.date(2100, 1, 1)),
    ],
}


def as_date(raw: Any) -> dt.date:
    return dt.date.fromisoformat(raw) if isinstance(raw, str) else raw


def load_fund_series(conn, ts_code: str) -> list[tuple[dt.date, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, close
            FROM fund_daily
            WHERE ts_code=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (ts_code,),
        )
        return [(trade_date, float(close)) for trade_date, close in cur.fetchall()]


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


class HistoricalCnPackagePricer:
    def __init__(
        self,
        conn,
        package: CnOptionPackageShape,
        underlying_mode: str,
        max_quote_stale_days: int,
        slippage_bps_per_leg: float,
        missing_policy: str,
    ) -> None:
        self.conn = conn
        self.package = package
        self.underlying_mode = underlying_mode
        self.underlyings = UNDERLYING_MODES[underlying_mode]
        self.max_quote_stale_days = max_quote_stale_days
        self.slippage_bps_per_leg = slippage_bps_per_leg
        self.missing_policy = missing_policy
        self.fund_series = {item.fund_code: load_fund_series(conn, item.fund_code) for item in self.underlyings}
        self.quote_dates = self._load_quote_dates()
        self.option_cache: dict[tuple[str, dt.date], list[OptionRow]] = {}
        self.selection_cache: dict[tuple[str, dt.date, dt.date], tuple[OptionRow, OptionRow] | None] = {}
        self.missing_reasons: dict[str, int] = {}
        self.used_quote_dates: set[tuple[str, dt.date]] = set()

    def _load_quote_dates(self) -> dict[str, list[dt.date]]:
        out: dict[str, list[dt.date]] = {}
        with self.conn.cursor() as cur:
            for item in self.underlyings:
                cur.execute(
                    """
                    SELECT DISTINCT d.trade_date
                    FROM cn_option_daily d
                    JOIN cn_option_contract_archive a ON a.option_ts_code=d.ts_code
                    WHERE a.opt_code=%s
                    ORDER BY d.trade_date
                    """,
                    (item.opt_code,),
                )
                out[item.opt_code] = [row[0] for row in cur.fetchall()]
        return out

    def _bump_missing(self, reason: str) -> None:
        self.missing_reasons[reason] = self.missing_reasons.get(reason, 0) + 1

    def underlying_for(self, start: dt.date) -> UnderlyingConfig | None:
        return next((item for item in self.underlyings if item.start_date <= start <= item.end_date), None)

    def quote_date_for(self, start: dt.date) -> tuple[UnderlyingConfig, dt.date] | None:
        underlying = self.underlying_for(start)
        if underlying is None:
            return None
        quote_dates = self.quote_dates.get(underlying.opt_code) or []
        i = bisect_right(quote_dates, start) - 1
        if i < 0:
            return None
        quote_date = quote_dates[i]
        if (start - quote_date).days > self.max_quote_stale_days:
            return None
        return underlying, quote_date

    def quote_candidates_for(self, start: dt.date) -> list[tuple[UnderlyingConfig, dt.date]]:
        underlying = self.underlying_for(start)
        if underlying is None:
            return []
        quote_dates = self.quote_dates.get(underlying.opt_code) or []
        idx = bisect_right(quote_dates, start) - 1
        out: list[tuple[UnderlyingConfig, dt.date]] = []
        while idx >= 0:
            quote_date = quote_dates[idx]
            if (start - quote_date).days > self.max_quote_stale_days:
                break
            out.append((underlying, quote_date))
            idx -= 1
        return out

    def select_quote_legs_for_period(
        self,
        start: dt.date,
        end: dt.date,
    ) -> tuple[UnderlyingConfig, dt.date, float, tuple[OptionRow, OptionRow]] | None:
        for underlying, quote_date in self.quote_candidates_for(start):
            spot = price_at(self.fund_series[underlying.fund_code], quote_date)
            if spot is None:
                continue
            selection = self.select_legs(underlying.opt_code, quote_date, end, spot)
            if selection is not None:
                return underlying, quote_date, spot, selection
        return None

    def options_for_quote_date(self, opt_code: str, quote_date: dt.date) -> list[OptionRow]:
        key = (opt_code, quote_date)
        if key in self.option_cache:
            return self.option_cache[key]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.option_ts_code, a.call_put, a.exercise_price, a.contract_unit,
                    a.maturity_date, d.close, COALESCE(d.vol, 0), COALESCE(d.oi, 0)
                FROM cn_option_contract_archive a
                JOIN cn_option_daily d ON d.ts_code=a.option_ts_code
                WHERE a.opt_code=%s
                  AND d.trade_date=%s
                  AND a.call_put IN ('P','C')
                  AND a.exercise_price IS NOT NULL
                  AND a.contract_unit IS NOT NULL
                  AND a.maturity_date IS NOT NULL
                  AND d.close IS NOT NULL
                """,
                (opt_code, quote_date),
            )
            rows = [
                OptionRow(
                    ts_code=str(ts_code),
                    call_put=str(call_put),
                    strike=float(strike),
                    per_unit=float(per_unit),
                    maturity_date=maturity_date,
                    close=float(close),
                    vol=float(vol or 0.0),
                    oi=float(oi or 0.0),
                )
                for ts_code, call_put, strike, per_unit, maturity_date, close, vol, oi in cur.fetchall()
            ]
        self.option_cache[key] = rows
        return rows

    def select_legs(self, opt_code: str, quote_date: dt.date, end: dt.date, spot: float) -> tuple[OptionRow, OptionRow] | None:
        key = (opt_code, quote_date, end)
        if key in self.selection_cache:
            return self.selection_cache[key]
        options = [row for row in self.options_for_quote_date(opt_code, quote_date) if row.maturity_date >= end]
        puts = [row for row in options if row.call_put == "P"]
        calls = [row for row in options if row.call_put == "C"]
        if not puts or not calls:
            self.selection_cache[key] = None
            return None
        best: tuple[float, OptionRow, OptionRow] | None = None
        maturities = sorted({row.maturity_date for row in options})
        for maturity in maturities:
            m_puts = [row for row in puts if row.maturity_date == maturity]
            m_calls = [row for row in calls if row.maturity_date == maturity]
            if not m_puts or not m_calls:
                continue
            put = min(
                m_puts,
                key=lambda row: (
                    abs(row.strike / spot - self.package.long_put_strike_pct),
                    abs(row.strike / spot - 1.0),
                    -row.vol,
                ),
            )
            call = min(
                m_calls,
                key=lambda row: (
                    abs(row.strike / spot - self.package.short_call_strike_pct),
                    abs(row.strike / spot - 1.0),
                    -row.vol,
                ),
            )
            maturity_gap = abs((maturity - end).days)
            strike_gap = abs(put.strike / spot - self.package.long_put_strike_pct) + abs(
                call.strike / spot - self.package.short_call_strike_pct
            )
            score = maturity_gap / 30.0 + strike_gap * 25.0
            if best is None or score < best[0]:
                best = (score, put, call)
        self.selection_cache[key] = None if best is None else (best[1], best[2])
        return self.selection_cache[key]

    def proxy_return(self, csi_series: dict[str, list[tuple[dt.date, float]]], start: dt.date, end: dt.date) -> float:
        ret = period_return(csi_series, self.package.underlying_index_code, start, end)
        long_put_payoff = self.package.long_put_notional_pct * max(
            1.0 - (1.0 + ret) / self.package.long_put_strike_pct,
            0.0,
        )
        short_call_payoff = -self.package.short_call_notional_pct * max(
            (1.0 + ret) / self.package.short_call_strike_pct - 1.0,
            0.0,
        )
        return long_put_payoff + short_call_payoff - self.package.cn_net_debit_pct

    def package_return(
        self,
        csi_series: dict[str, list[tuple[dt.date, float]]],
        start: dt.date,
        end: dt.date,
        capital: float,
    ) -> tuple[float, dict[str, Any]]:
        selected = self.select_quote_legs_for_period(start, end)
        if selected is None:
            self._bump_missing("missing_quote_date")
            fallback = self.proxy_return(csi_series, start, end) if self.missing_policy == "proxy" else 0.0
            return fallback, {"source": "proxy_fallback" if self.missing_policy == "proxy" else "missing_zero"}
        underlying, quote_date, spot, selection = selected
        end_spot = price_at(self.fund_series[underlying.fund_code], end)
        if end_spot is None:
            self._bump_missing("missing_underlying_spot")
            fallback = self.proxy_return(csi_series, start, end) if self.missing_policy == "proxy" else 0.0
            return fallback, {"source": "proxy_fallback" if self.missing_policy == "proxy" else "missing_zero"}
        long_put, short_call = selection
        target_notional = capital * self.package.long_put_notional_pct
        contracts = max(1, math.ceil(target_notional / max(long_put.strike * long_put.per_unit, 1.0)))
        put_units = contracts * long_put.per_unit
        call_units = contracts * short_call.per_unit
        net_debit = long_put.close * put_units - short_call.close * call_units
        gross_notional = abs(long_put.strike * put_units) + abs(short_call.strike * call_units)
        slippage = gross_notional * self.slippage_bps_per_leg / 10000.0
        payoff = max(long_put.strike - end_spot, 0.0) * put_units
        payoff -= max(end_spot - short_call.strike, 0.0) * call_units
        package_ret = (payoff - net_debit - slippage) / capital
        stressed_spot = spot * 1.25
        short_call_loss = max(stressed_spot - short_call.strike, 0.0) * call_units
        margin_proxy = max(short_call_loss, spot * call_units * 0.12) / capital
        self.used_quote_dates.add((underlying.opt_code, quote_date))
        return package_ret, {
            "source": "listed_contract",
            "opt_code": underlying.opt_code,
            "fund_code": underlying.fund_code,
            "quote_date": quote_date.isoformat(),
            "spot": spot,
            "end_spot": end_spot,
            "long_put": long_put.ts_code,
            "long_put_strike_pct": long_put.strike / spot,
            "short_call": short_call.ts_code,
            "short_call_strike_pct": short_call.strike / spot,
            "maturity_date": long_put.maturity_date.isoformat(),
            "contracts": contracts,
            "net_debit_pct": net_debit / capital,
            "slippage_pct": slippage / capital,
            "margin_proxy_pct": margin_proxy,
        }


def build_rules(package: CnOptionPackageShape, missing_policy: str) -> list[RealHistoryRule]:
    rules = []
    for use_floor in [False, True]:
        for premium in [0.0, 0.0075]:
            rules.append(
                RealHistoryRule(
                    name=(
                        "cnreal_"
                        + ("floor" if use_floor else "raw")
                        + f"_prem{int(premium * 10000):03d}_miss{missing_policy}"
                    ),
                    core_rule_name="core_xbcppi_sub12_spread95_call108",
                    satellite_rule_name="sat_crypto_cppi",
                    core_weight=0.95,
                    satellite_weight=0.08,
                    monthly_loss_floor=-0.01,
                    premium_monthly=premium,
                    use_modeled_floor=use_floor,
                    missing_package_policy=missing_policy,
                    package=package,
                )
            )
    return rules


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
        "median_listed_package_months": statistics.median(item["listed_package_months"] for item in items),
        "median_missing_package_months": statistics.median(item["missing_package_months"] for item in items),
        "max_missing_package_months": max(item["missing_package_months"] for item in items),
        "median_package_net_debit_pct": statistics.median(item["median_package_net_debit_pct"] for item in items),
    }


def run_case(
    csi_series: dict[str, list[tuple[dt.date, float]]],
    pricer: HistoricalCnPackagePricer,
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: RealHistoryRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    core_case = core_cache[(rule.core_rule_name, phase, lag)]
    sat_returns = satellite_returns[(rule.satellite_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    curve = [capital]
    floor_months = 0
    listed_package_months = 0
    missing_package_months = 0
    package_net_debits: list[float] = []
    for row, sat_return in zip(core_case["rows"], sat_returns):
        safe_return = row["safe_return"]
        start_exec = as_date(row["start_exec"])
        end_exec = as_date(row["end_exec"])
        csi_pkg_return, meta = pricer.package_return(csi_series, start_exec, end_exec, capital)
        if meta["source"] == "listed_contract":
            listed_package_months += 1
            package_net_debits.append(float(meta["net_debit_pct"]))
        else:
            missing_package_months += 1
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
        capital *= 1.0 + portfolio_return
        curve.append(capital)
    mdd = max_drawdown(curve)
    years = 20
    return {
        "name": f"{rule.name}_phase{phase}_lag{lag}",
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "floor_months": floor_months,
        "listed_package_months": listed_package_months,
        "missing_package_months": missing_package_months,
        "median_package_net_debit_pct": statistics.median(package_net_debits) if package_net_debits else 0.0,
    }


def evaluate_rule(csi_series, pricer, core_cache, satellite_returns, rule: RealHistoryRule) -> dict[str, Any]:
    cases = [
        run_case(csi_series, pricer, core_cache, satellite_returns, rule, phase, lag)
        for phase in MONTH_PHASES
        for lag in EXECUTION_LAGS
    ]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def output_paths(args) -> tuple[Path, Path]:
    if args.output_prefix:
        prefix = Path(args.output_prefix)
        if not prefix.is_absolute():
            prefix = ROOT / prefix
    else:
        prefix = OUT_DIR / (
            "scorecard_csi_cn_option_package_real_history_"
            f"{args.underlying_mode}_miss{args.missing_package_policy}"
        )
    return prefix.with_suffix(".json"), prefix.with_suffix(".csv")


def write_outputs(results: list[dict[str, Any]], package: CnOptionPackageShape, pricer: HistoricalCnPackagePricer, args) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = output_paths(args)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test the China ETF option hedge using historical listed contracts where local quotes exist.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "package_shape_source": asdict(package),
        "underlying_mode": pricer.underlying_mode,
        "underlyings": [asdict(item) for item in pricer.underlyings],
        "quote_dates_available": sum(len(items) for items in pricer.quote_dates.values()),
        "quote_dates_by_opt_code": {key: len(value) for key, value in pricer.quote_dates.items()},
        "quote_date_min": min((value[0] for value in pricer.quote_dates.values() if value), default=None),
        "quote_date_max": max((value[-1] for value in pricer.quote_dates.values() if value), default=None),
        "quote_dates_used": len(pricer.used_quote_dates),
        "missing_reasons": pricer.missing_reasons,
        "assumptions": {
            "max_quote_stale_days": args.max_quote_stale_days,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "missing_package_policy": args.missing_package_policy,
            "selection_note": "Selects same-maturity OP510300.SH put/call legs nearest the current validated package moneyness, prices entry at historical close, and values payoff by ETF spot at period end. Residual time value is ignored.",
        },
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "missing_package_policy",
        "use_modeled_floor",
        "premium_monthly",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_floor_months",
        "median_listed_package_months",
        "median_missing_package_months",
        "max_missing_package_months",
        "median_package_net_debit_pct",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fields})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest historical listed OP510300 option package where quotes exist.")
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--missing-package-policy", choices=["zero", "proxy"], default="zero")
    parser.add_argument("--underlying-mode", choices=sorted(UNDERLYING_MODES), default="switch_50_to_300")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    package = load_package_shape()
    conn = get_connection()
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
    pricer = HistoricalCnPackagePricer(
        conn,
        package,
        args.underlying_mode,
        args.max_quote_stale_days,
        args.slippage_bps_per_leg,
        args.missing_package_policy,
    )

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

    try:
        results = []
        for rule in build_rules(package, args.missing_package_policy):
            result = evaluate_rule(csi_series, pricer, core_cache, satellite_returns, rule)
            results.append(result)
            summary = result["summary"]
            print(
                f"{rule.name:<38} pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:9.1f}w "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"listed={summary['median_listed_package_months']:5.1f} "
                f"missing={summary['median_missing_package_months']:5.1f}"
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
    json_path, csv_path = write_outputs(results, package, pricer, args)
    best = results[0]["summary"]
    print(
        f"Wrote {json_path}; rules={len(results)} "
        f"best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%} "
        f"quote_dates={sum(len(items) for items in pricer.quote_dates.values())} used={len(pricer.used_quote_dates)} "
        f"missing={pricer.missing_reasons}"
    )
    print(f"Wrote {csv_path}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

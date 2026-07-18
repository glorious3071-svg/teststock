#!/usr/bin/env python3
"""Backtest cross-asset ETF sleeves blended with scorecard+CSI sleeves.

The current frontier shows that the scorecard+CSI plus option-proxy structures
cannot hit the 4000w / -10% all-phase target by exposure tuning alone.  This
experiment uses the newly cached external ETF set as a separate cross-asset
momentum/defense sleeve, then applies monthly TIPP/CPPI risk budgeting to the
combined engine across the same 12 month phases and 4 execution lags.
"""

from __future__ import annotations

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
from scripts.backtest_scorecard_csi_blended_protection import precompute_csi_paths
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, cash_return, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_cross_asset_tipp_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_cross_asset_tipp_search.csv"

SYMBOLS = [
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "XLK",
    "XLE",
    "XLU",
    "TLT",
    "IEF",
    "SHY",
    "GLD",
    "DBC",
    "UUP",
    "LQD",
    "AGG",
    "TIP",
    "HYG",
    "SH",
    "PSQ",
    "RWM",
    "^VIX",
]

RISK_UNIVERSES = {
    "global_growth": ("QQQ", "SPY", "IWM", "EFA", "EEM", "XLK", "XLE", "XLU", "GLD", "DBC"),
    "sector_growth": ("QQQ", "IWM", "XLK", "XLE", "XLU", "GLD", "DBC"),
    "all_weather": ("QQQ", "SPY", "IWM", "EFA", "EEM", "XLK", "XLE", "XLU", "GLD", "DBC", "TLT", "IEF", "LQD", "AGG", "TIP"),
}

DEFENSE_UNIVERSES = {
    "bond_gold": ("TLT", "IEF", "LQD", "AGG", "TIP", "GLD", "SHY"),
    "quality_defense": ("IEF", "AGG", "TIP", "XLU", "GLD", "SHY"),
    "inverse_hedge": ("SH", "PSQ", "TLT", "IEF", "GLD", "SHY"),
}


@dataclass(frozen=True)
class CrossAssetRule:
    name: str
    phase_rule_name: str
    risk_key: str
    defense_key: str
    csi_weight: float
    external_weight: float
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    top_n: int
    target_vol: float
    max_external_leverage: float
    risk_off_vix: float
    risk_off_spy_3m: float = -0.08
    risk_off_max_external_leverage: float = 1.0
    require_positive_6m: bool = True
    fallback_symbol: str = "SHY"


class ExternalSeries:
    def __init__(self, rows_by_symbol: dict[str, list[tuple[dt.date, float]]]) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.dates_by_symbol = {symbol: [day for day, _value in rows] for symbol, rows in rows_by_symbol.items()}
        self.feature_cache: dict[tuple[str, dt.date], dict[str, float] | None] = {}
        self.return_cache: dict[tuple[str, dt.date, dt.date], float] = {}

    def index_at(self, symbol: str, day: dt.date) -> int:
        return bisect_right(self.dates_by_symbol[symbol], day) - 1

    def price(self, symbol: str, day: dt.date) -> float | None:
        idx = self.index_at(symbol, day)
        if idx < 0:
            return None
        return self.rows_by_symbol[symbol][idx][1]

    def period_return(self, symbol: str, start: dt.date, end: dt.date) -> float:
        key = (symbol, start, end)
        if key in self.return_cache:
            return self.return_cache[key]
        start_px = self.price(symbol, start)
        end_px = self.price(symbol, end)
        value = 0.0 if not start_px or not end_px or start_px <= 0 else end_px / start_px - 1.0
        self.return_cache[key] = value
        return value

    def feature(self, symbol: str, day: dt.date) -> dict[str, float] | None:
        key = (symbol, day)
        if key in self.feature_cache:
            return self.feature_cache[key]
        rows = self.rows_by_symbol[symbol]
        idx = self.index_at(symbol, day)
        if idx - 252 < 1:
            self.feature_cache[key] = None
            return None
        values = [rows[j][1] for j in range(idx - 252, idx + 1)]
        if any(value <= 0 for value in values):
            self.feature_cache[key] = None
            return None
        daily = [values[j] / values[j - 1] - 1.0 for j in range(1, len(values))]
        vol_window = daily[-63:]
        mean = sum(vol_window) / len(vol_window)
        vol = math.sqrt(sum((item - mean) ** 2 for item in vol_window) / len(vol_window)) * math.sqrt(252.0)
        self.feature_cache[key] = {
            "ret_12m": values[-1] / values[0] - 1.0,
            "ret_6m": values[-1] / values[-127] - 1.0,
            "ret_3m": values[-1] / values[-64] - 1.0,
            "vol_3m": vol,
        }
        return self.feature_cache[key]


def build_rules() -> list[CrossAssetRule]:
    rules: list[CrossAssetRule] = []
    phase_names = ["phase12_lever120_us10y", "phase12_guard60_us10y"]
    blends = [(0.0, 1.0), (0.20, 0.80), (0.35, 0.65), (0.50, 0.50)]
    engine_specs = [
        ("global_growth", "bond_gold", 2, 0.25, 3.0, 25.0),
        ("sector_growth", "quality_defense", 2, 0.28, 3.0, 25.0),
        ("all_weather", "bond_gold", 3, 0.20, 2.2, 25.0),
        ("all_weather", "quality_defense", 3, 0.16, 1.8, 20.0),
        ("global_growth", "inverse_hedge", 2, 0.22, 2.5, 20.0),
        ("all_weather", "inverse_hedge", 3, 0.18, 2.0, 18.0),
    ]
    for phase_name in phase_names:
        phase_short = phase_name.removeprefix("phase12_").replace("lever120_", "l120_").replace("guard60_", "g60_")
        for risk_key, defense_key, top_n, target_vol, max_lev, risk_off_vix in engine_specs:
            for csi_weight, external_weight in blends:
                mix = f"c{int(csi_weight * 100):02d}_x{int(external_weight * 100):02d}"
                for floor_pct in [0.84, 0.86, 0.88, 0.90]:
                    for multiplier, max_exposure in [(6.0, 1.0), (8.0, 1.25), (10.0, 1.5), (12.0, 1.5)]:
                        rules.append(
                            CrossAssetRule(
                                f"catipp_{phase_short}_{risk_key}_{mix}_f{int(floor_pct * 100)}_m{int(multiplier * 10):03d}_e{int(max_exposure * 100)}",
                                phase_name,
                                risk_key,
                                defense_key,
                                csi_weight,
                                external_weight,
                                "tipp",
                                floor_pct,
                                multiplier,
                                max_exposure,
                                top_n,
                                target_vol,
                                max_lev,
                                risk_off_vix,
                            )
                        )
                for floor_pct in [0.86, 0.88, 0.90]:
                    for multiplier, max_exposure in [(4.0, 1.0), (6.0, 1.25), (8.0, 1.5)]:
                        rules.append(
                            CrossAssetRule(
                                f"cacppi_{phase_short}_{risk_key}_{mix}_f{int(floor_pct * 100)}_m{int(multiplier * 10):03d}_e{int(max_exposure * 100)}",
                                phase_name,
                                risk_key,
                                defense_key,
                                csi_weight,
                                external_weight,
                                "cppi",
                                floor_pct,
                                multiplier,
                                max_exposure,
                                top_n,
                                target_vol,
                                max_lev,
                                risk_off_vix,
                            )
                        )
    return rules


RULES = build_rules()


def load_external_series(conn) -> ExternalSeries:
    rows_by_symbol: dict[str, list[tuple[dt.date, float]]] = {}
    with conn.cursor() as cur:
        for symbol in SYMBOLS:
            cur.execute(
                """
                SELECT trade_date, COALESCE(adj_close, close)
                FROM external_asset_daily
                WHERE symbol=%s AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (symbol, f"{START_YEAR - 2}-01-01", f"{END_YEAR + 1}-01-15"),
            )
            rows = [(row[0], float(row[1])) for row in cur.fetchall() if row[1] is not None]
            if not rows:
                raise RuntimeError(f"missing external_asset_daily rows for {symbol}")
            rows_by_symbol[symbol] = rows
    return ExternalSeries(rows_by_symbol)


def external_risk_off(series: ExternalSeries, snapshot: dt.date, rule: CrossAssetRule) -> bool:
    vix = series.price("^VIX", snapshot)
    spy = series.feature("SPY", snapshot)
    spy_3m = spy["ret_3m"] if spy else None
    return (vix is None or vix >= rule.risk_off_vix) or (spy_3m is None or spy_3m < rule.risk_off_spy_3m)


def external_period_return(
    series: ExternalSeries,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    rule: CrossAssetRule,
    drawdown: float,
) -> tuple[float, dict[str, Any]]:
    is_risk_off = external_risk_off(series, snapshot, rule) or drawdown <= -0.08
    universe = DEFENSE_UNIVERSES[rule.defense_key] if is_risk_off else RISK_UNIVERSES[rule.risk_key]
    scored = []
    for symbol in universe:
        feature = series.feature(symbol, snapshot)
        if feature is None:
            continue
        if rule.require_positive_6m and feature["ret_6m"] <= 0.0:
            continue
        raw_score = 0.50 * feature["ret_12m"] + 0.35 * feature["ret_6m"] + 0.15 * feature["ret_3m"]
        if raw_score <= 0.0:
            continue
        scored.append((raw_score / max(feature["vol_3m"], 0.04), raw_score, feature["vol_3m"], symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        fallback_return = series.period_return(rule.fallback_symbol, start_exec, end_exec)
        return fallback_return, {"picks": rule.fallback_symbol, "leverage": 0.0, "risk_off": is_risk_off}
    inv_vol = [1.0 / max(item[2], 0.04) for item in picks]
    denom = sum(inv_vol)
    avg_vol = sum(weight * item[2] for weight, item in zip(inv_vol, picks)) / denom
    leverage = min(rule.max_external_leverage, rule.target_vol / max(avg_vol, 0.04))
    if is_risk_off:
        leverage = min(leverage, rule.risk_off_max_external_leverage)
    risky_return = sum(
        leverage * weight / denom * series.period_return(item[3], start_exec, end_exec)
        for weight, item in zip(inv_vol, picks)
    )
    residual_return = (1.0 - leverage) * series.period_return(rule.fallback_symbol, start_exec, end_exec)
    return risky_return + residual_return, {
        "picks": ",".join(item[3] for item in picks),
        "leverage": leverage,
        "risk_off": is_risk_off,
    }


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    external_series: ExternalSeries,
    rule: CrossAssetRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    ext_leverages: list[float] = []
    risk_off_count = 0
    for csi_row in csi_paths[(rule.phase_rule_name, phase, lag)]:
        peak = max(peak, capital)
        drawdown = capital / peak - 1.0
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        ext_return, ext_meta = external_period_return(
            external_series,
            csi_row["period"],
            csi_row["start_exec"],
            csi_row["end_exec"],
            rule,
            drawdown,
        )
        residual_weight = 1.0 - rule.csi_weight - rule.external_weight
        safe_return = cash_return(csi_row["start_exec"], csi_row["end_exec"])
        engine_return = (
            rule.csi_weight * csi_row["csi_return"]
            + rule.external_weight * ext_return
            + residual_weight * safe_return
        )
        period_return = exposure * engine_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        if capital <= 0:
            capital = 1.0
        curve.append(capital)
        exposures.append(exposure)
        ext_leverages.append(float(ext_meta["leverage"]))
        if ext_meta["risk_off"]:
            risk_off_count += 1
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
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
        "avg_exposure": statistics.mean(exposures) if exposures else 0.0,
        "median_exposure": statistics.median(exposures) if exposures else 0.0,
        "median_external_leverage": statistics.median(ext_leverages) if ext_leverages else 0.0,
        "risk_off_count": risk_off_count,
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
        "median_avg_exposure": statistics.median(item["avg_exposure"] for item in items),
        "median_exposure": statistics.median(item["median_exposure"] for item in items),
        "median_external_leverage": statistics.median(item["median_external_leverage"] for item in items),
        "median_risk_off_count": statistics.median(item["risk_off_count"] for item in items),
    }


def evaluate_rule(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    external_series: ExternalSeries,
    rule: CrossAssetRule,
) -> dict[str, Any]:
    cases = [run_case(csi_paths, external_series, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test cross-asset ETF momentum/defense sleeves blended with scorecard+CSI sleeves under monthly TIPP/CPPI.",
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "symbols": SYMBOLS,
        "rule_count": len(RULES),
        "model_limits": "Monthly ETF rotation using cached Yahoo adjusted prices; no tax, transaction-cost, borrow, slippage, or live execution constraints.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "phase_rule_name",
            "risk_key",
            "defense_key",
            "csi_weight",
            "external_weight",
            "mode",
            "floor_pct",
            "multiplier",
            "max_exposure",
            "top_n",
            "target_vol",
            "max_external_leverage",
            "risk_off_vix",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_avg_exposure",
            "median_exposure",
            "median_external_leverage",
            "median_risk_off_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    phase_rule_names = {rule.phase_rule_name for rule in RULES}
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(conn, csi_series, yields, trade_dates, holdings, phase_rule_names)
        external_series = load_external_series(conn)
    finally:
        conn.close()
    results = []
    for rule in RULES:
        result = evaluate_rule(csi_paths, external_series, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:92]:<92} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"avg_exp={summary['median_avg_exposure'] * 100:5.1f}%"
        )
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    write_outputs(results)
    best = results[0]["summary"]
    print(
        f"Wrote {OUT_JSON}; rules={len(RULES)} "
        f"best_min={best['min_final_capital_wan']:.1f}万 "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

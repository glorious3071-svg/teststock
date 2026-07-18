#!/usr/bin/env python3
"""Search long-history futures trend sleeves for the scorecard+CSI target.

The sleeve uses Yahoo continuous futures/index series cached in
external_asset_daily.  This is an executable-direction feasibility screen, not
a broker-ready futures implementation: it does not model contract rolls,
margin, commissions, tax, or slippage.
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
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series
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
OUT_JSON = OUT_DIR / "scorecard_csi_futures_crisis_alpha_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_futures_crisis_alpha_search.csv"

FUTURES_SYMBOLS = ["GC=F", "SI=F", "CL=F", "NG=F", "ZB=F", "ZN=F", "ZF=F", "6E=F", "6J=F", "DX-Y.NYB"]
SYMBOLS = FUTURES_SYMBOLS + ["SHY", "^VIX"]

UNIVERSES = {
    "all": tuple(FUTURES_SYMBOLS),
    "macro": ("GC=F", "CL=F", "NG=F", "ZB=F", "ZN=F", "6E=F", "6J=F", "DX-Y.NYB"),
    "rates_fx_metal": ("GC=F", "ZB=F", "ZN=F", "ZF=F", "6E=F", "6J=F", "DX-Y.NYB"),
    "commodity_fx": ("GC=F", "SI=F", "CL=F", "NG=F", "6E=F", "6J=F", "DX-Y.NYB"),
}


@dataclass(frozen=True)
class FuturesCrisisRule:
    name: str
    phase_rule_name: str
    universe_key: str
    csi_weight: float
    futures_weight: float
    mode: str
    floor_pct: float
    multiplier: float
    max_exposure: float
    target_vol: float
    max_futures_leverage: float
    top_n: int
    signal_mode: str
    short_enabled: bool
    short_cost_annual: float = 0.02
    min_abs_signal: float = 0.03
    vix_boost_gte: float = 28.0
    vix_boost_scale: float = 1.0
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
        return self.rows_by_symbol[symbol][idx][1] if idx >= 0 else None

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
        vol = math.sqrt(sum((value - mean) ** 2 for value in vol_window) / len(vol_window)) * math.sqrt(252.0)
        self.feature_cache[key] = {
            "ret_12m": values[-1] / values[0] - 1.0,
            "ret_6m": values[-1] / values[-127] - 1.0,
            "ret_3m": values[-1] / values[-64] - 1.0,
            "vol_3m": vol,
        }
        return self.feature_cache[key]


def short_phase_name(phase_name: str) -> str:
    return phase_name.removeprefix("phase12_").replace("lever120_", "l120_").replace("guard60_", "g60_")


def build_rules() -> list[FuturesCrisisRule]:
    rules: list[FuturesCrisisRule] = []
    phase_names = ["phase12_lever120_us10y", "phase12_guard60_us10y"]
    blends = [(0.0, 1.0), (0.20, 0.80), (0.35, 0.65), (0.50, 0.50)]
    specs = [
        ("all", 4, 0.18, 2.0),
        ("all", 5, 0.24, 2.5),
        ("macro", 4, 0.20, 2.2),
        ("rates_fx_metal", 4, 0.16, 2.0),
        ("commodity_fx", 3, 0.22, 2.5),
    ]
    overlay_specs = [
        ("tipp", 0.84, 8.0, 1.25),
        ("tipp", 0.86, 8.0, 1.25),
        ("tipp", 0.88, 10.0, 1.50),
        ("tipp", 0.90, 12.0, 1.50),
        ("cppi", 0.86, 6.0, 1.25),
        ("cppi", 0.88, 8.0, 1.50),
        ("cppi", 0.90, 10.0, 1.50),
    ]
    for phase_name in phase_names:
        phase_short = short_phase_name(phase_name)
        for universe_key, top_n, target_vol, max_lev in specs:
            for csi_weight, futures_weight in blends:
                mix = f"c{int(csi_weight * 100):02d}_f{int(futures_weight * 100):02d}"
                for signal_mode in ["absolute", "relative", "dual"]:
                    for short_enabled in [True, False]:
                        side = "ls" if short_enabled else "long"
                        for mode, floor_pct, multiplier, max_exposure in overlay_specs:
                            rules.append(
                                FuturesCrisisRule(
                                    (
                                        f"fut_{mode}_{phase_short}_{universe_key}_{side}_{signal_mode}_{mix}"
                                        f"_fl{int(floor_pct * 100)}_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                    ),
                                    phase_name,
                                    universe_key,
                                    csi_weight,
                                    futures_weight,
                                    mode,
                                    floor_pct,
                                    multiplier,
                                    max_exposure,
                                    target_vol,
                                    max_lev,
                                    top_n,
                                    signal_mode,
                                    short_enabled,
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


def signal_value(feature: dict[str, float], mode: str) -> float:
    relative = 0.55 * feature["ret_12m"] + 0.30 * feature["ret_6m"] + 0.15 * feature["ret_3m"]
    absolute = feature["ret_12m"] - 0.5 * feature["vol_3m"]
    if mode == "relative":
        return relative
    if mode == "dual":
        return 0.5 * relative + 0.5 * absolute
    return absolute


def futures_period_return(
    series: ExternalSeries,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    rule: FuturesCrisisRule,
) -> tuple[float, dict[str, Any]]:
    scored = []
    for symbol in UNIVERSES[rule.universe_key]:
        feature = series.feature(symbol, snapshot)
        if feature is None:
            continue
        signal = signal_value(feature, rule.signal_mode)
        if abs(signal) < rule.min_abs_signal:
            continue
        if signal < 0 and not rule.short_enabled:
            continue
        direction = 1.0 if signal > 0 else -1.0
        scored.append((abs(signal) / max(feature["vol_3m"], 0.04), signal, feature["vol_3m"], direction, symbol))
    scored.sort(reverse=True)
    picks = scored[: rule.top_n]
    if not picks:
        return series.period_return(rule.fallback_symbol, start_exec, end_exec), {"picks": rule.fallback_symbol, "leverage": 0.0, "short_count": 0}

    inv_vol = [1.0 / max(item[2], 0.04) for item in picks]
    denom = sum(inv_vol)
    avg_vol = sum(weight * item[2] for weight, item in zip(inv_vol, picks)) / denom
    leverage = min(rule.max_futures_leverage, rule.target_vol / max(avg_vol, 0.04))
    vix = series.price("^VIX", snapshot) or 0.0
    if vix >= rule.vix_boost_gte:
        leverage *= rule.vix_boost_scale

    gross_return = 0.0
    short_count = 0
    for weight, item in zip(inv_vol, picks):
        _score, _signal, _vol, direction, symbol = item
        period = series.period_return(symbol, start_exec, end_exec)
        signed_return = period if direction > 0 else -period - rule.short_cost_annual / 12.0
        short_count += int(direction < 0)
        gross_return += leverage * weight / denom * signed_return
    residual_return = (1.0 - leverage) * series.period_return(rule.fallback_symbol, start_exec, end_exec)
    return gross_return + residual_return, {
        "picks": ",".join(("-" if item[3] < 0 else "+") + item[4] for item in picks),
        "leverage": leverage,
        "short_count": short_count,
    }


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    external_series: ExternalSeries,
    rule: FuturesCrisisRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    curve = [capital]
    exposures: list[float] = []
    futures_leverages: list[float] = []
    short_counts: list[int] = []

    for csi_row in csi_paths[(rule.phase_rule_name, phase, lag)]:
        peak = max(peak, capital)
        floor = peak * rule.floor_pct if rule.mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor)
        exposure = min(rule.max_exposure, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        futures_return, futures_meta = futures_period_return(
            external_series,
            csi_row["period"],
            csi_row["start_exec"],
            csi_row["end_exec"],
            rule,
        )
        residual_weight = 1.0 - rule.csi_weight - rule.futures_weight
        safe_return = external_series.period_return(rule.fallback_symbol, csi_row["start_exec"], csi_row["end_exec"])
        raw_return = (
            rule.csi_weight * float(csi_row.get("period_return") or csi_row.get("csi_return") or 0.0)
            + rule.futures_weight * futures_return
            + residual_weight * safe_return
        )
        period_return = exposure * raw_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        curve.append(capital)
        exposures.append(exposure)
        futures_leverages.append(float(futures_meta["leverage"]))
        short_counts.append(int(futures_meta["short_count"]))

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
        "median_overlay_exposure": statistics.median(exposures) if exposures else 0.0,
        "median_futures_leverage": statistics.median(futures_leverages) if futures_leverages else 0.0,
        "median_short_count": statistics.median(short_counts) if short_counts else 0.0,
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
        "median_overlay_exposure": statistics.median(item["median_overlay_exposure"] for item in items),
        "median_futures_leverage": statistics.median(item["median_futures_leverage"] for item in items),
        "median_short_count": statistics.median(item["median_short_count"] for item in items),
    }


def evaluate_rule(csi_paths, external_series, rule: FuturesCrisisRule) -> dict[str, Any]:
    cases = [run_case(csi_paths, external_series, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search long-history futures trend sleeves as a crisis-alpha source for scorecard+CSI.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "model_limits": "Yahoo continuous futures/index series; no contract roll, margin, tax, commission, or slippage model.",
        "rule_count": len(RULES),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "phase_rule_name",
        "universe_key",
        "csi_weight",
        "futures_weight",
        "mode",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "target_vol",
        "max_futures_leverage",
        "top_n",
        "signal_mode",
        "short_enabled",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_overlay_exposure",
        "median_futures_leverage",
        "median_short_count",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fields})


def main() -> int:
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {rule.phase_rule_name for rule in RULES},
        )
        external_series = load_external_series(conn)
    finally:
        conn.close()

    results = []
    for idx, rule in enumerate(RULES, start=1):
        result = evaluate_rule(csi_paths, external_series, rule)
        results.append(result)
        summary = result["summary"]
        if idx % 250 == 0 or summary["pass_count"]:
            print(
                f"{idx:>4}/{len(RULES)} {rule.name[:86]:<86} "
                f"pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}w "
                f"mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"lev={summary['median_futures_leverage']:.2f}"
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
        f"best_pass={best['pass_count']}/{best['count']} "
        f"best_min={best['min_final_capital_wan']:.1f}w "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

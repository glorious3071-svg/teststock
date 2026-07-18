#!/usr/bin/env python3
"""Backtest small crypto satellites on the strongest low-drawdown CSI core.

The current frontier has a low-drawdown core around 1000w and high-growth crypto
CPPI sleeves with unacceptable drawdown.  This experiment tests whether a small
fixed crypto satellite, optionally wrapped by portfolio-level TIPP, improves the
strict month-phase frontier without changing the validation matrix.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import (  # noqa: E402
    BLEND_RULE_BY_NAME,
    BlendTippRule,
    run_case as run_core_case,
)
from scripts.backtest_scorecard_csi_blended_protection import (  # noqa: E402
    load_option_data,
    precompute_csi_paths,
    precompute_option_paths,
)
from scripts.backtest_scorecard_csi_crypto_tipp_overlay import (  # noqa: E402
    CryptoTippRule,
    DailyData,
    choose_risky_weights,
    load_data,
)
from scripts.backtest_scorecard_csi_dynamic_defense import (  # noqa: E402
    EXECUTION_LAGS,
    MONTH_PHASES,
    load_price_series,
)
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CS300_CODE,
    INITIAL_CAPITAL,
    TARGET_CAPITAL,
    load_hybrid_holdings,
    max_drawdown,
)
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_crypto_satellite_mix_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_crypto_satellite_mix_search.csv"

CORE_RULES = [
    BlendTippRule(
        "core_xbtipp_sub10",
        "tipp",
        "blend_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
        0.88,
        8.0,
        1.0,
    ),
    BlendTippRule(
        "core_xbcppi_sub12",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
        0.86,
        8.0,
        1.0,
    ),
    BlendTippRule(
        "core_xbcppi_sub12_call103",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_call103_lev125_c20_o80",
        0.86,
        8.0,
        1.0,
    ),
    BlendTippRule(
        "core_xbcppi_sub12_put99_call102",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put99_call102_lev125_c20_o80",
        0.86,
        8.0,
        1.0,
    ),
    BlendTippRule(
        "core_xbcppi_sub12_spread95_call108",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_95spread_call108_lev125_c20_o80",
        0.86,
        8.0,
        1.0,
    ),
    BlendTippRule(
        "core_xbcppi_sub12_spread94_call108",
        "cppi",
        "blend_phase12_lever120_us10y_qqq_put98_94spread_call108_lev125_c20_o80",
        0.86,
        8.0,
        1.0,
    ),
]
CORE_RULE_BY_NAME = {rule.name: rule for rule in CORE_RULES}

SATELLITE_RULES = [
    CryptoTippRule("sat_btc_cppi", "cppi", "btc", 0.90, 6.0, 1.0, 1),
    CryptoTippRule("sat_crypto_cppi", "cppi", "crypto", 0.90, 6.0, 1.0, 2),
]
SATELLITE_RULE_BY_NAME = {rule.name: rule for rule in SATELLITE_RULES}


@dataclass(frozen=True)
class SatelliteMixRule:
    name: str
    core_rule_name: str
    satellite_rule_name: str
    core_weight: float
    satellite_weight: float
    overlay_mode: str
    overlay_floor_pct: float = 0.0
    overlay_multiplier: float = 0.0
    overlay_max_exposure: float = 1.0
    drawdown_scale_lte: float = -1.0
    drawdown_scale: float = 1.0


def build_rules() -> list[SatelliteMixRule]:
    rules: list[SatelliteMixRule] = []
    weights = [(0.95, 0.08), (0.90, 0.10), (0.85, 0.12), (0.80, 0.15), (0.75, 0.18), (0.70, 0.20)]
    for core_name in CORE_RULE_BY_NAME:
        for sat_name in SATELLITE_RULE_BY_NAME:
            for core_weight, satellite_weight in weights:
                suffix = f"{core_name}_{sat_name}_c{int(core_weight * 100)}_s{int(satellite_weight * 100)}"
                rules.append(
                    SatelliteMixRule(
                        f"satmix_plain_{suffix}",
                        core_name,
                        sat_name,
                        core_weight,
                        satellite_weight,
                        "plain",
                    )
                )
                for floor_pct in [0.84, 0.86, 0.88, 0.90]:
                    for multiplier in [4.0, 6.0, 8.0, 10.0]:
                        for max_exposure in [1.25, 1.75]:
                            for drawdown_scale_lte, drawdown_scale in [(-1.0, 1.0), (-0.06, 0.10)]:
                                rules.append(
                                    SatelliteMixRule(
                                        (
                                            f"satmix_tipp_{suffix}_f{int(floor_pct * 100)}"
                                            f"_m{int(multiplier * 10):03d}_x{int(max_exposure * 100)}"
                                            f"_dd{int(abs(drawdown_scale_lte) * 100):02d}s{int(drawdown_scale * 100)}"
                                        ),
                                        core_name,
                                        sat_name,
                                        core_weight,
                                        satellite_weight,
                                        "tipp",
                                        floor_pct,
                                        multiplier,
                                        max_exposure,
                                        drawdown_scale_lte,
                                        drawdown_scale,
                                    )
                                )
    return rules


RULES = build_rules()


def crypto_period_returns(
    data: DailyData,
    rule: CryptoTippRule,
    periods: list[tuple[dt.date, dt.date]],
    phase: int,
    lag: int,
) -> list[float]:
    start_idx = max(data.start_index(phase, lag), 253)
    capital = INITIAL_CAPITAL
    initial_floor = INITIAL_CAPITAL * rule.floor_pct
    returns: list[float] = []
    for start, end in periods:
        start_idx_period = max(bisect_left(data.dates, start), start_idx)
        end_idx_period = bisect_right(data.dates, end) - 1
        before = capital
        for idx in range(start_idx_period + 1, end_idx_period + 1):
            cushion = max(0.0, capital - initial_floor)
            exposure = min(rule.max_leverage, max(rule.min_exposure, rule.multiplier * cushion / max(capital, 1.0)))
            weights, _selected, trend = choose_risky_weights(data, rule, idx)
            vix = data.prices["^VIX"][idx - 1] or 99.0
            if vix >= rule.risk_off_vix:
                exposure *= rule.risk_off_scale
            if trend <= rule.trend_12m_lte:
                exposure *= rule.trend_scale
            risky_return = sum(weight * data.returns[symbol][idx] for symbol, weight in weights)
            safe_return = data.returns[rule.safe_symbol][idx]
            capital *= 1.0 + exposure * risky_return + (1.0 - exposure) * safe_return
            if capital <= 0:
                capital = 1.0
        returns.append(capital / before - 1.0 if before > 0 else 0.0)
    return returns


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
        "median_drawdown_scaled_months": statistics.median(item["drawdown_scaled_months"] for item in items),
    }


def run_mix_case(
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: SatelliteMixRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    core_case = core_cache[(rule.core_rule_name, phase, lag)]
    sat_returns = satellite_returns[(rule.satellite_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    overlay_exposures: list[float] = []
    drawdown_scaled_months = 0
    for row, sat_return in zip(core_case["rows"], sat_returns):
        safe_return = row["safe_return"]
        raw_return = (
            rule.core_weight * row["period_return"]
            + rule.satellite_weight * sat_return
            + (1.0 - rule.core_weight - rule.satellite_weight) * safe_return
        )
        if rule.overlay_mode == "tipp":
            peak = max(peak, capital)
            drawdown = capital / peak - 1.0
            cushion = max(0.0, capital - peak * rule.overlay_floor_pct)
            exposure = min(rule.overlay_max_exposure, rule.overlay_multiplier * cushion / max(capital, 1.0))
            if drawdown <= rule.drawdown_scale_lte:
                exposure *= rule.drawdown_scale
                drawdown_scaled_months += 1
            period_return = exposure * raw_return + (1.0 - exposure) * safe_return
        else:
            exposure = 1.0
            period_return = raw_return
        capital *= 1.0 + period_return
        curve.append(capital)
        overlay_exposures.append(exposure)
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
        "median_overlay_exposure": statistics.median(overlay_exposures) if overlay_exposures else 0.0,
        "drawdown_scaled_months": drawdown_scaled_months,
    }


def evaluate_rule(
    core_cache: dict[tuple[str, int, int], dict[str, Any]],
    satellite_returns: dict[tuple[str, int, int], list[float]],
    rule: SatelliteMixRule,
) -> dict[str, Any]:
    cases = [run_mix_case(core_cache, satellite_returns, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {"rule": asdict(rule), "cases": cases, "summary": summary, "target_met": summary["pass_count"] == summary["count"]}


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Test low-drawdown CSI/option core plus small BTC/crypto CPPI satellite under all month-phase drift cases.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "model_limits": "Crypto history starts after 2006 and uses ETF fallback logic from the crypto TIPP probe; no tax, custody, liquidity, or intramonth execution evidence.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames = [
        "name",
        "core_rule_name",
        "satellite_rule_name",
        "core_weight",
        "satellite_weight",
        "overlay_mode",
        "overlay_floor_pct",
        "overlay_multiplier",
        "overlay_max_exposure",
        "drawdown_scale_lte",
        "drawdown_scale",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "median_overlay_exposure",
        "median_drawdown_scaled_months",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    conn = get_connection()
    try:
        csi_series = load_price_series(conn)
        option_data = load_option_data(conn)
        yields = load_us10y_yields(conn)
        trade_dates = [day for day, _px in csi_series[CS300_CODE]]
        holdings = load_hybrid_holdings()
        csi_paths = precompute_csi_paths(
            conn,
            csi_series,
            yields,
            trade_dates,
            holdings,
            {BLEND_RULE_BY_NAME[rule.base_blend_name].phase_rule_name for rule in CORE_RULES},
        )
        option_paths = precompute_option_paths(
            option_data,
            csi_paths,
            {BLEND_RULE_BY_NAME[rule.base_blend_name].option_rule_name for rule in CORE_RULES},
        )
        crypto_data = load_data(conn)
    finally:
        conn.close()

    core_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    periods_by_case: dict[tuple[int, int], list[tuple[dt.date, dt.date]]] = {}
    for core_rule in CORE_RULES:
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                case = run_core_case(csi_paths, option_paths, core_rule, phase, lag, include_rows=True)
                core_cache[(core_rule.name, phase, lag)] = case
                periods_by_case.setdefault(
                    (phase, lag),
                    [(dt.date.fromisoformat(row["start_exec"]), dt.date.fromisoformat(row["end_exec"])) for row in case["rows"]],
                )

    satellite_returns: dict[tuple[str, int, int], list[float]] = {}
    for sat_rule in SATELLITE_RULES:
        for phase in MONTH_PHASES:
            for lag in EXECUTION_LAGS:
                satellite_returns[(sat_rule.name, phase, lag)] = crypto_period_returns(
                    crypto_data,
                    sat_rule,
                    periods_by_case[(phase, lag)],
                    phase,
                    lag,
                )

    results = []
    for rule in RULES:
        result = evaluate_rule(core_cache, satellite_returns, rule)
        results.append(result)
        summary = result["summary"]
        print(
            f"{rule.name[:96]:<96} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"ov_exp={summary['median_overlay_exposure'] * 100:5.1f}%"
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

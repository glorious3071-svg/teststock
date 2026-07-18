#!/usr/bin/env python3
"""Search pre-month feature guards for executable CSI + CBOE blends."""

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
from scripts.backtest_scorecard_csi_blended_protection import precompute_csi_paths
from scripts.backtest_scorecard_csi_dynamic_defense import EXECUTION_LAGS, MONTH_PHASES, load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, load_hybrid_holdings, max_drawdown
from scripts.backtest_scorecard_csi_option_protection import DailyOptionData, OptionProtectionRule, load_data as load_option_data
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields
from scripts.search_scorecard_csi_cboe_blend import BASE_CBOE_RULES, CBOE_RULE_BY_NAME, CboeBlendRule, base_day_return

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_cboe_feature_guard_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_cboe_feature_guard_search.csv"

EXTERNAL_SYMBOLS = ["QQQ", "SPY", "SHY", "^VIX", "VIX3M", "VVIX", "VXTH", "PPUT"]


@dataclass(frozen=True)
class GuardSpec:
    name: str
    signal_names: tuple[str, ...]
    trigger_count: int
    risk_scale: float
    safe_symbol: str = "SHY"


@dataclass(frozen=True)
class FeatureGuardRule:
    name: str
    base: CboeBlendRule
    guard: GuardSpec


def build_base_rules() -> list[CboeBlendRule]:
    rules: list[CboeBlendRule] = []
    phase_names = ["phase12_lever120_us10y", "phase12_guard60_us10y"]
    weights = [(0.20, 0.80), (0.35, 0.65), (0.50, 0.50), (0.65, 0.35)]
    overlay_specs = [
        ("plain", 0.0, 0.0, 1.0, -1.0, 1.0),
        ("cppi", 0.86, 6.0, 1.25, -1.0, 1.0),
        ("cppi", 0.86, 8.0, 1.5, -1.0, 1.0),
        ("cppi", 0.88, 8.0, 1.5, -1.0, 1.0),
        ("tipp", 0.88, 8.0, 1.5, -1.0, 1.0),
    ]
    for phase in phase_names:
        for cboe in BASE_CBOE_RULES:
            for csi_weight, cboe_weight in weights:
                for mode, floor, mult, max_exp, cut_lte, cut_scale in overlay_specs:
                    rules.append(
                        CboeBlendRule(
                            (
                                f"cboeguardbase_{phase}_{cboe.name}_c{int(csi_weight * 100):02d}"
                                f"_b{int(cboe_weight * 100):02d}_{mode}_f{int(floor * 100):02d}"
                                f"_m{int(mult * 10):03d}_x{int(max_exp * 100)}"
                            ),
                            phase,
                            cboe.name,
                            csi_weight,
                            cboe_weight,
                            mode,
                            floor,
                            mult,
                            max_exp,
                            cut_lte,
                            cut_scale,
                        )
                    )
    return rules


def build_guard_specs() -> list[GuardSpec]:
    specs: list[GuardSpec] = []
    single_signals = [
        "vix_ge_22",
        "vix_ge_25",
        "vix_pct_ge_80",
        "vix_pct_ge_90",
        "term_backward",
        "vvix_ge_110",
        "qqq_3m_lt_0",
        "qqq_dd_3m_lte_8",
        "vxth_1m_lt_0",
    ]
    for signal in single_signals:
        for scale in [0.0, 0.25, 0.50]:
            specs.append(GuardSpec(f"{signal}_scale{int(scale * 100):02d}", (signal,), 1, scale))

    composites = [
        ("macrostress2", ("vix_ge_22", "vix_pct_ge_80", "term_backward", "qqq_3m_lt_0"), 2),
        ("macrostress3", ("vix_ge_22", "vix_pct_ge_80", "term_backward", "qqq_3m_lt_0"), 3),
        ("voltrend2", ("vix_ge_25", "vvix_ge_110", "qqq_dd_3m_lte_8", "vxth_1m_lt_0"), 2),
        ("voltrend3", ("vix_ge_25", "vvix_ge_110", "qqq_dd_3m_lte_8", "vxth_1m_lt_0"), 3),
        ("broad2", ("vix_pct_ge_90", "term_backward", "qqq_3m_lt_0", "qqq_dd_3m_lte_8", "vxth_1m_lt_0"), 2),
        ("broad3", ("vix_pct_ge_90", "term_backward", "qqq_3m_lt_0", "qqq_dd_3m_lte_8", "vxth_1m_lt_0"), 3),
    ]
    for prefix, signals, count in composites:
        for scale in [0.0, 0.25, 0.50]:
            specs.append(GuardSpec(f"{prefix}_n{count}_scale{int(scale * 100):02d}", signals, count, scale))
    return specs


BASE_RULES = build_base_rules()
GUARD_SPECS = build_guard_specs()
RULES = [
    FeatureGuardRule(f"{base.name}_{guard.name}", base, guard)
    for base in BASE_RULES
    for guard in GUARD_SPECS
]


def value_at(rows: list[tuple[dt.date, float]], day: dt.date) -> float | None:
    idx = bisect_right(rows, (day, float("inf"))) - 1
    return rows[idx][1] if idx >= 0 else None


def trailing_values(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> list[float]:
    idx = bisect_right(rows, (day, float("inf")))
    return [value for _date, value in rows[max(0, idx - days) : idx]]


def ret_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    idx = bisect_left(rows, (day - dt.timedelta(days=days), -float("inf")))
    if current is None or idx < 0 or idx >= len(rows):
        return None
    past = rows[idx][1]
    return current / past - 1.0 if past > 0 else None


def drawdown_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    values = trailing_values(rows, day, days)
    if current is None or len(values) < max(20, days // 3):
        return None
    high = max(values)
    return current / high - 1.0 if high > 0 else None


def percentile_at(rows: list[tuple[dt.date, float]], day: dt.date, days: int) -> float | None:
    current = value_at(rows, day)
    values = trailing_values(rows, day, days)
    if current is None or len(values) < max(60, days // 3):
        return None
    return sum(1 for value in values if value <= current) / len(values)


def load_external_series(conn) -> dict[str, list[tuple[dt.date, float]]]:
    out: dict[str, list[tuple[dt.date, float]]] = {symbol: [] for symbol in EXTERNAL_SYMBOLS}
    placeholders = ",".join(["%s"] * len(EXTERNAL_SYMBOLS))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, trade_date, COALESCE(adj_close, close)
            FROM external_asset_daily
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, trade_date
            """,
            EXTERNAL_SYMBOLS,
        )
        for symbol, trade_date, value in cur.fetchall():
            if value is not None:
                out[symbol].append((trade_date, float(value)))
    missing = [symbol for symbol, rows in out.items() if not rows]
    if missing:
        raise RuntimeError(f"missing external_asset_daily rows for {missing}")
    return out


def safe_period_return(option_data: DailyOptionData, symbol: str, start: dt.date, end: dt.date) -> float:
    start_idx = max(bisect_left(option_data.dates, start), 1)
    end_idx = bisect_right(option_data.dates, end) - 1
    capital = 1.0
    for idx in range(start_idx + 1, max(start_idx + 1, end_idx + 1)):
        capital *= 1.0 + option_data.returns[symbol][idx]
    return capital - 1.0


def cboe_period_return(data: DailyOptionData, rule: OptionProtectionRule, start: dt.date, end: dt.date) -> tuple[float, int]:
    start_idx = max(bisect_left(data.dates, start), 253)
    end_idx = bisect_right(data.dates, end) - 1
    capital = 1.0
    risk_days = 0
    for idx in range(start_idx + 1, max(start_idx + 1, end_idx + 1)):
        day_return, is_risk = base_day_return(data, rule, idx)
        capital *= 1.0 + day_return
        risk_days += int(is_risk)
    return capital - 1.0, risk_days


def feature_snapshot(series: dict[str, list[tuple[dt.date, float]]], day: dt.date) -> dict[str, float | None]:
    vix = value_at(series["^VIX"], day)
    vix3m = value_at(series["VIX3M"], day)
    vvix = value_at(series["VVIX"], day)
    qqq_3m = ret_at(series["QQQ"], day, 63)
    qqq_dd_3m = drawdown_at(series["QQQ"], day, 63)
    vxth_1m = ret_at(series["VXTH"], day, 21)
    return {
        "vix": vix,
        "vix_pct_1y": percentile_at(series["^VIX"], day, 252),
        "vix_vix3m_ratio": vix / vix3m if vix is not None and vix3m is not None and vix3m > 0 else None,
        "vvix": vvix,
        "qqq_3m": qqq_3m,
        "qqq_dd_3m": qqq_dd_3m,
        "vxth_1m": vxth_1m,
    }


def signals(features: dict[str, float | None]) -> dict[str, bool]:
    def ge(name: str, threshold: float) -> bool:
        value = features.get(name)
        return value is not None and value >= threshold

    def le(name: str, threshold: float) -> bool:
        value = features.get(name)
        return value is not None and value <= threshold

    return {
        "vix_ge_22": ge("vix", 22.0),
        "vix_ge_25": ge("vix", 25.0),
        "vix_pct_ge_80": ge("vix_pct_1y", 0.80),
        "vix_pct_ge_90": ge("vix_pct_1y", 0.90),
        "term_backward": ge("vix_vix3m_ratio", 1.0),
        "vvix_ge_110": ge("vvix", 110.0),
        "qqq_3m_lt_0": le("qqq_3m", 0.0),
        "qqq_dd_3m_lte_8": le("qqq_dd_3m", -0.08),
        "vxth_1m_lt_0": le("vxth_1m", 0.0),
    }


def guard_on(guard: GuardSpec, signal_values: dict[str, bool]) -> bool:
    return sum(1 for name in guard.signal_names if signal_values.get(name, False)) >= guard.trigger_count


def overlay_exposure(rule: CboeBlendRule, capital: float, peak: float, initial_floor: float) -> float:
    if rule.overlay_mode == "plain":
        exposure = 1.0
    else:
        floor_value = peak * rule.floor_pct if rule.overlay_mode == "tipp" else initial_floor
        cushion = max(0.0, capital - floor_value)
        exposure = min(rule.max_exposure, rule.multiplier * cushion / max(capital, 1.0))
    drawdown = capital / peak - 1.0
    if drawdown <= rule.drawdown_cut_lte:
        exposure *= rule.drawdown_cut_scale
    return exposure


def precompute_paths(
    option_data: DailyOptionData,
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    sample_paths: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for (_phase_rule, phase, lag), rows in csi_paths.items():
        sample_paths.setdefault((phase, lag), rows)

    out: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for cboe_rule in BASE_CBOE_RULES:
        for (phase, lag), rows in sample_paths.items():
            path_rows = []
            for row in rows:
                start_raw = row["start_exec"]
                end_raw = row["end_exec"]
                start = dt.date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
                end = dt.date.fromisoformat(end_raw) if isinstance(end_raw, str) else end_raw
                ret, risk_days = cboe_period_return(option_data, cboe_rule, start, end)
                safe_return = safe_period_return(option_data, "SHY", start, end)
                features = feature_snapshot(series, start)
                path_rows.append(
                    {
                        "period_return": ret,
                        "risk_days": risk_days,
                        "safe_return": safe_return,
                        "features": features,
                        "signals": signals(features),
                    }
                )
            out[(cboe_rule.name, phase, lag)] = path_rows
    return out


def run_case(
    csi_paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    paths: dict[tuple[str, int, int], list[dict[str, Any]]],
    rule: FeatureGuardRule,
    phase: int,
    lag: int,
) -> dict[str, Any]:
    csi_rows = csi_paths[(rule.base.phase_rule_name, phase, lag)]
    cboe_rows = paths[(rule.base.cboe_rule_name, phase, lag)]
    capital = INITIAL_CAPITAL
    peak = capital
    initial_floor = INITIAL_CAPITAL * rule.base.floor_pct
    curve = [capital]
    exposures = []
    guard_months = 0
    risk_days = []
    for csi_row, cboe_row in zip(csi_rows, cboe_rows):
        safe_return = float(cboe_row["safe_return"])
        raw_return = (
            rule.base.csi_weight * float(csi_row.get("period_return") or csi_row.get("csi_return") or 0.0)
            + rule.base.cboe_weight * float(cboe_row["period_return"])
            + (1.0 - rule.base.csi_weight - rule.base.cboe_weight) * safe_return
        )
        if guard_on(rule.guard, cboe_row["signals"]):
            guard_months += 1
            raw_return = rule.guard.risk_scale * raw_return + (1.0 - rule.guard.risk_scale) * safe_return
        exposure = overlay_exposure(rule.base, capital, peak, initial_floor)
        period_return = exposure * raw_return + (1.0 - exposure) * safe_return
        capital *= 1.0 + period_return
        peak = max(peak, capital)
        curve.append(capital)
        exposures.append(exposure)
        risk_days.append(cboe_row["risk_days"])
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
        "guard_months": guard_months,
        "median_overlay_exposure": statistics.median(exposures) if exposures else 0.0,
        "median_cboe_risk_days": statistics.median(risk_days) if risk_days else 0.0,
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
        "min_guard_months": min(item["guard_months"] for item in items),
        "median_guard_months": statistics.median(item["guard_months"] for item in items),
        "median_overlay_exposure": statistics.median(item["median_overlay_exposure"] for item in items),
        "median_cboe_risk_days": statistics.median(item["median_cboe_risk_days"] for item in items),
    }


def evaluate_rule(csi_paths, paths, rule: FeatureGuardRule) -> dict[str, Any]:
    cases = [run_case(csi_paths, paths, rule, phase, lag) for phase in MONTH_PHASES for lag in EXECUTION_LAGS]
    summary = matrix_summary(cases)
    return {
        "rule": {
            "name": rule.name,
            "base": asdict(rule.base),
            "guard": asdict(rule.guard),
        },
        "cases": cases,
        "summary": summary,
        "target_met": summary["pass_count"] == summary["count"],
    }


def write_outputs(results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Search observable pre-month guards for executable CSI plus CBOE option-strategy blends.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "rule_count": len(RULES),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = [
        "name",
        "base_name",
        "phase_rule_name",
        "cboe_rule_name",
        "csi_weight",
        "cboe_weight",
        "overlay_mode",
        "floor_pct",
        "multiplier",
        "max_exposure",
        "guard_name",
        "trigger_count",
        "risk_scale",
        "pass_count",
        "count",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "min_guard_months",
        "median_guard_months",
        "median_overlay_exposure",
        "median_cboe_risk_days",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            base = item["rule"]["base"]
            guard = item["rule"]["guard"]
            row = {
                "name": item["rule"]["name"],
                "base_name": base["name"],
                "phase_rule_name": base["phase_rule_name"],
                "cboe_rule_name": base["cboe_rule_name"],
                "csi_weight": base["csi_weight"],
                "cboe_weight": base["cboe_weight"],
                "overlay_mode": base["overlay_mode"],
                "floor_pct": base["floor_pct"],
                "multiplier": base["multiplier"],
                "max_exposure": base["max_exposure"],
                "guard_name": guard["name"],
                "trigger_count": guard["trigger_count"],
                "risk_scale": guard["risk_scale"],
                **item["summary"],
            }
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
            {rule.base.phase_rule_name for rule in RULES},
        )
        option_data = load_option_data(conn)
        external_series = load_external_series(conn)
    finally:
        conn.close()

    paths = precompute_paths(option_data, csi_paths, external_series)
    results = []
    for idx, rule in enumerate(RULES, start=1):
        result = evaluate_rule(csi_paths, paths, rule)
        results.append(result)
        summary = result["summary"]
        if idx % 200 == 0 or summary["pass_count"]:
            print(
                f"{idx:>4}/{len(RULES)} {rule.name[:88]:<88} "
                f"pass={summary['pass_count']:>2}/{summary['count']} "
                f"min={summary['min_final_capital_wan']:8.1f}w "
                f"mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
                f"guard={summary['median_guard_months']:.0f}"
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

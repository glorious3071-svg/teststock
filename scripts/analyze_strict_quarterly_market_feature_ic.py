#!/usr/bin/env python3
"""Screen point-in-time quarter-boundary features against next-quarter ETF returns."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SELECTOR_POLICIES
from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    load_equity_etf_return_universe,
)
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_tipp import (
    build_daily_path,
    code_return,
    load_selector_price_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (
    ANNUAL_MARKET_SCORECARD,
    mark_frozen_positions,
)
from scripts.validate_scorecard_csi_generalization import (
    MONTH_DRIFT_PHASES,
    SCHEDULE_12M_3M,
)


DEFAULT_OUTPUT = ROOT / "data/backtests/strict_quarterly_market_feature_ic_report.json"
SELECTOR_NAME = "expanded_value_risk_top5"
DIRECT_POLICY_NAME = "blend_index_weighted_stable_v5_top1_regime_w49_s71"


def named(items, name):
    return next(item for item in items if item.name == name)


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = rank
        cursor = end
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 8 or len(xs) != len(ys):
        return None
    ranked_x = average_ranks(xs)
    ranked_y = average_ranks(ys)
    mean_x = statistics.mean(ranked_x)
    mean_y = statistics.mean(ranked_y)
    numerator = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(ranked_x, ranked_y)
    )
    denominator = (
        sum((x - mean_x) ** 2 for x in ranked_x)
        * sum((y - mean_y) ** 2 for y in ranked_y)
    ) ** 0.5
    return numerator / denominator if denominator > 0 else None


def era(day: date) -> str:
    if day.year <= 2012:
        return "2005_2012"
    if day.year <= 2018:
        return "2013_2018"
    return "2019_latest"


def collect_observations(path, equity_series) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    factor = 1.0
    peak_factor = 1.0
    max_drawdown = 0.0
    day_count = 0
    positions: dict[str, float] = {}
    for row in path["daily"]:
        if row["window_start"]:
            if current is not None and day_count >= 40:
                current["forward_risk_return_3m"] = factor - 1.0
                current["forward_risk_max_drawdown_3m"] = max_drawdown
                current["holding_day_count"] = day_count
                observations.append(current)
            current = {
                "phase_month_offset": path["phase"],
                "decision_date": row["previous_day"].isoformat(),
                "era": era(row["previous_day"]),
                "features": dict(row["market_state"]),
            }
            factor = 1.0
            peak_factor = 1.0
            max_drawdown = 0.0
            day_count = 0
            weights = {
                str(code): float(weight)
                for code, weight in row["equity_etf_weights"].items()
                if float(weight) > 0
            }
            total_weight = sum(weights.values())
            positions = {
                code: weight / total_weight for code, weight in weights.items()
            }
        daily_returns = {
            code: code_return(
                equity_series,
                code,
                row["previous_day"],
                row["day"],
            )
            for code in positions
        }
        positions = mark_frozen_positions(positions, daily_returns)
        factor = sum(positions.values())
        peak_factor = max(peak_factor, factor)
        max_drawdown = min(max_drawdown, factor / peak_factor - 1.0)
        day_count += 1
    if current is not None and day_count >= 40:
        current["forward_risk_return_3m"] = factor - 1.0
        current["forward_risk_max_drawdown_3m"] = max_drawdown
        current["holding_day_count"] = day_count
        observations.append(current)
    return observations


def feature_screen(
    observations: list[dict[str, Any]], outcome_key: str
) -> list[dict[str, Any]]:
    features = sorted(
        {
            feature
            for row in observations
            for feature, value in row["features"].items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        }
    )
    results = []
    for feature in features:
        cells = []
        for phase in MONTH_DRIFT_PHASES:
            for era_name in ("2005_2012", "2013_2018", "2019_latest"):
                usable = [
                    row
                    for row in observations
                    if row["phase_month_offset"] == phase
                    and row["era"] == era_name
                    and isinstance(row["features"].get(feature), (int, float))
                    and not isinstance(row["features"].get(feature), bool)
                    and math.isfinite(float(row["features"][feature]))
                ]
                correlation = spearman(
                    [float(row["features"][feature]) for row in usable],
                    [float(row[outcome_key]) for row in usable],
                )
                if correlation is not None:
                    time_correlation = spearman(
                        [
                            float(date.fromisoformat(row["decision_date"]).toordinal())
                            for row in usable
                        ],
                        [float(row["features"][feature]) for row in usable],
                    )
                    cells.append(
                        {
                            "phase_month_offset": phase,
                            "era": era_name,
                            "count": len(usable),
                            "spearman_ic": correlation,
                            "time_spearman": time_correlation,
                        }
                    )
        if not cells:
            continue
        correlations = [float(cell["spearman_ic"]) for cell in cells]
        median_ic = statistics.median(correlations)
        orientation = 1.0 if median_ic >= 0 else -1.0
        aligned = [orientation * value for value in correlations]
        time_correlations = [
            abs(float(cell["time_spearman"]))
            for cell in cells
            if cell["time_spearman"] is not None
        ]
        median_abs_time_spearman = (
            statistics.median(time_correlations) if time_correlations else None
        )
        time_trend_high_rate = (
            sum(value >= 0.80 for value in time_correlations)
            / len(time_correlations)
            if time_correlations
            else None
        )
        time_trend_leakage_flag = bool(
            median_abs_time_spearman is not None
            and time_trend_high_rate is not None
            and (
                median_abs_time_spearman >= 0.80
                or time_trend_high_rate >= 0.50
            )
        )
        results.append(
            {
                "feature": feature,
                "orientation": "higher" if orientation > 0 else "lower",
                "cell_count": len(cells),
                "phase_count": len({cell["phase_month_offset"] for cell in cells}),
                "era_count": len({cell["era"] for cell in cells}),
                "median_ic": median_ic,
                "mean_ic": statistics.mean(correlations),
                "aligned_positive_rate": sum(value > 0 for value in aligned)
                / len(aligned),
                "aligned_worst_ic": min(aligned),
                "aligned_q25_ic": statistics.quantiles(aligned, n=4)[0]
                if len(aligned) >= 4
                else min(aligned),
                "median_abs_time_spearman": median_abs_time_spearman,
                "time_trend_high_rate": time_trend_high_rate,
                "time_trend_leakage_flag": time_trend_leakage_flag,
                "cells": cells,
            }
        )
    results.sort(
        key=lambda item: (
            not item["time_trend_leakage_flag"],
            item["phase_count"] == 12,
            item["era_count"] == 3,
            item["aligned_positive_rate"],
            item["aligned_q25_ic"],
            abs(item["median_ic"]),
        ),
        reverse=True,
    )
    return results


def oracle_endpoint_summary(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-investable ceiling using the known next-quarter endpoint return."""

    rows = []
    for phase in MONTH_DRIFT_PHASES:
        usable = [
            row for row in observations if row["phase_month_offset"] == phase
        ]
        risk_multiple = math.prod(
            1.0 + float(row["forward_risk_return_3m"]) for row in usable
        )
        positive_only_multiple = math.prod(
            max(1.0, 1.0 + float(row["forward_risk_return_3m"])) for row in usable
        )
        rows.append(
            {
                "phase_month_offset": phase,
                "quarter_count": len(usable),
                "all_risk_multiple": risk_multiple,
                "positive_quarter_oracle_multiple": positive_only_multiple,
                "lookahead": True,
            }
        )
    return rows


def value(features: dict[str, Any], name: str) -> float | None:
    raw = features.get(name)
    return float(raw) if isinstance(raw, (int, float)) and math.isfinite(float(raw)) else None


def condition_screen(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit economically specified late-cycle conditions before using them."""

    def late_cycle(row: dict[str, Any], *, valuation: float | None, m1: bool) -> bool:
        f = row["features"]
        required = {
            name: value(f, name)
            for name in (
                "domestic_shibor_on_percentile_3y",
                "external_fed_funds_percentile_3y",
                "external_us_curve_percentile_3y",
                "domestic_m1_m2_scissors_change_3m",
                "market_pe_ttm_percentile_3y",
                "cs300_return_6m",
            )
        }
        if any(item is None for item in required.values()):
            return False
        return (
            required["domestic_shibor_on_percentile_3y"] >= 0.85
            and required["external_fed_funds_percentile_3y"] >= 0.85
            and required["external_us_curve_percentile_3y"] <= 0.15
            and (not m1 or required["domestic_m1_m2_scissors_change_3m"] <= -1.0)
            and (valuation is None or required["market_pe_ttm_percentile_3y"] >= valuation)
            and required["cs300_return_6m"] >= 0.0
        )

    conditions = {
        "late_cycle_rates": lambda row: late_cycle(row, valuation=None, m1=False),
        "late_cycle_rates_m1": lambda row: late_cycle(row, valuation=None, m1=True),
        "late_cycle_rates_m1_pe65": lambda row: late_cycle(row, valuation=0.65, m1=True),
        "late_cycle_rates_m1_pe80": lambda row: late_cycle(row, valuation=0.80, m1=True),
    }
    output = []
    for name, predicate in conditions.items():
        triggered = [row for row in observations if predicate(row)]
        returns = [float(row["forward_risk_return_3m"]) for row in triggered]
        drawdowns = [float(row["forward_risk_max_drawdown_3m"]) for row in triggered]
        output.append(
            {
                "condition": name,
                "count": len(triggered),
                "phase_count": len({row["phase_month_offset"] for row in triggered}),
                "era_count": len({row["era"] for row in triggered}),
                "mean_forward_return_3m": statistics.mean(returns) if returns else None,
                "median_forward_return_3m": statistics.median(returns) if returns else None,
                "negative_return_rate": (
                    sum(item < 0 for item in returns) / len(returns) if returns else None
                ),
                "mean_forward_max_drawdown_3m": (
                    statistics.mean(drawdowns) if drawdowns else None
                ),
                "drawdown_below_10pct_rate": (
                    sum(item <= -0.10 for item in drawdowns) / len(drawdowns)
                    if drawdowns
                    else None
                ),
                "trigger_dates": sorted(
                    {str(row["decision_date"]) for row in triggered}
                ),
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--selector-policy", default=SELECTOR_NAME)
    parser.add_argument("--direct-etf-policy", default=DIRECT_POLICY_NAME)
    args = parser.parse_args()
    selector = named(SELECTOR_POLICIES, args.selector_policy)
    direct_policy = named(DIRECT_ETF_POLICIES, args.direct_etf_policy)
    conn = get_connection()
    try:
        index_series = load_price_series(conn)
        load_selector_price_series(conn, index_series)
        equity_metas, equity_series = load_equity_etf_return_universe(conn)
        trade_dates = [day for day, _value in index_series[CS300_CODE]]
        paths = [
            build_daily_path(
                index_series,
                trade_dates,
                SCHEDULE_12M_3M,
                phase,
                0,
                equity_metas,
                equity_series,
                ANNUAL_MARKET_SCORECARD,
                True,
                True,
                selector,
                direct_policy,
                False,
                False,
                True,
                max(MONTH_DRIFT_PHASES),
                5,
                date(2005, 2, 28),
            )
            for phase in MONTH_DRIFT_PHASES
        ]
    finally:
        conn.close()
    observations = [
        row for path in paths for row in collect_observations(path, equity_series)
    ]
    return_results = feature_screen(observations, "forward_risk_return_3m")
    drawdown_results = feature_screen(
        observations, "forward_risk_max_drawdown_3m"
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    "point-in-time quarter-boundary feature Spearman IC versus "
                    "next exact-three-month frozen-share domestic passive-ETF "
                    "basket return; features with near-monotonic time correlation "
                    "are flagged as coverage/history proxies"
                ),
                "selector_policy": args.selector_policy,
                "direct_etf_policy": args.direct_etf_policy,
                "phase_offsets": list(MONTH_DRIFT_PHASES),
                "execution_lag_days": 0,
                "observation_count": len(observations),
                "return_results": return_results,
                "drawdown_results": drawdown_results,
                "oracle_endpoint_summary": oracle_endpoint_summary(observations),
                "condition_screens": condition_screen(observations),
                "analysis_rows": observations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in return_results[:20]:
        print(
            f"{item['feature']:<52} {item['orientation']:<6} "
            f"cells={item['cell_count']:>2} median={item['median_ic']:+.3f} "
            f"positive={item['aligned_positive_rate']:.1%} "
            f"q25={item['aligned_q25_ic']:+.3f} "
            f"time_leak={item['time_trend_leakage_flag']}"
        )
    print("Top drawdown-preservation features:")
    for item in drawdown_results[:20]:
        print(
            f"{item['feature']:<52} {item['orientation']:<6} "
            f"cells={item['cell_count']:>2} median={item['median_ic']:+.3f} "
            f"positive={item['aligned_positive_rate']:.1%} "
            f"q25={item['aligned_q25_ic']:+.3f} "
            f"time_leak={item['time_trend_leakage_flag']}"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

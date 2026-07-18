#!/usr/bin/env python3
"""Point-in-time quarterly IC audit for CSI selector features.

Each monthly snapshot is treated as a possible three-month rebalance anchor.
Feature rows use only information available at the snapshot; labels are the
subsequent three-month index returns. Results are split into eras so a feature
cannot qualify only because it worked in one part of the 20-year sample.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR
from backtest.monthly_online_selector import rank_correlation
from backtest.phase_schedule import shift_month_end
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_monthly import period_return
from scripts.backtest_calendar_neutral_csi_tipp import load_selector_price_series
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE


DEFAULT_FEATURES = (
    "annual_score",
    "momentum_1m",
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "momentum_12m_skip1m",
    "trend_6m",
    "trend_12m",
    "stable_trend_6m",
    "risk_adjusted_trend_6m",
    "risk_adjusted_momentum_12m",
    "trend_acceleration_3m_vs_6m",
    "trend_consistency_3m_6m",
    "positive_month_ratio_12m",
    "calmar_12m",
    "drawdown_6m",
    "drawdown_12m",
    "volatility_3m",
    "volatility_6m",
    "pe_ttm_history_percentile_3y",
    "pb_history_percentile_3y",
    "turnover_crowding_percentile_3y",
    "turnover_acceleration_1m_6m",
    "etf_amount_acceleration_1m_6m",
    "etf_amount_crowding_percentile_3y",
    "etf_positive_turnover_pressure_1m",
)
EXECUTION_LAGS = (0, 1, 3, 5)


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def era(day: date) -> str:
    if day.year <= 2012:
        return "2005_2012"
    if day.year <= 2018:
        return "2013_2018"
    return "2019_latest"


def market_regime(return_6m: float | None) -> str:
    if return_6m is None:
        return "unknown"
    if return_6m >= 0.10:
        return "bull"
    if return_6m <= -0.10:
        return "bear"
    return "neutral"


def execution_boundary(
    trade_dates: list[date], boundary: date, lag_days: int
) -> date | None:
    """Match the strict engine: lag zero is the first trade after the signal."""
    index = bisect_right(trade_dates, boundary) + lag_days
    if index >= len(trade_dates):
        return None
    return trade_dates[index]


def complete_period_returns(
    price_series: dict[str, list[tuple[date, float]]],
    rows: list[dict[str, Any]],
    start: date,
    end: date,
) -> dict[str, float]:
    """Return labels only when both boundary prices genuinely exist."""
    outcomes = {}
    for row in rows:
        code = str(row["ts_code"])
        price_rows = price_series.get(code) or []
        start_index = bisect_right(price_rows, (start, float("inf"))) - 1
        end_index = bisect_right(price_rows, (end, float("inf"))) - 1
        start_price = (
            price_rows[start_index][1]
            if start_index >= 0 and price_rows[start_index][0] == start
            else None
        )
        end_price = (
            price_rows[end_index][1]
            if end_index >= 0 and price_rows[end_index][0] == end
            else None
        )
        if (
            start_price is None
            or end_price is None
            or start_price <= 0
            or end_price <= 0
        ):
            continue
        outcomes[code] = period_return(price_series, code, start, end)
    return outcomes


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "mean_ic": None,
            "median_ic": None,
            "positive_rate": None,
            "abs_mean_ic": None,
        }
    return {
        "count": len(values),
        "mean_ic": statistics.mean(values),
        "median_ic": statistics.median(values),
        "positive_rate": sum(value > 0 for value in values) / len(values),
        "abs_mean_ic": statistics.mean(abs(value) for value in values),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=date(2005, 3, 31))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument(
        "--output-prefix",
        default="data/backtests/csi_quarterly_feature_ic",
    )
    args = parser.parse_args()

    conn = get_connection()
    observations: list[dict[str, Any]] = []
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        trade_dates = [day for day, _value in price_series[CS300_CODE]]
        with conn.cursor() as cur:
            snapshot = args.start
            while snapshot <= args.end:
                end_snapshot = shift_month_end(snapshot, 3)
                if end_snapshot > args.end:
                    break
                rows = SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, snapshot)
                market_6m = period_return(
                    price_series,
                    CS300_CODE,
                    shift_month_end(snapshot, -6),
                    snapshot,
                )
                regime = market_regime(market_6m)
                for lag_days in EXECUTION_LAGS:
                    start_exec = execution_boundary(trade_dates, snapshot, lag_days)
                    end_exec = execution_boundary(trade_dates, end_snapshot, lag_days)
                    if start_exec is None or end_exec is None:
                        continue
                    outcomes = complete_period_returns(
                        price_series,
                        rows,
                        start_exec,
                        end_exec,
                    )
                    if len(outcomes) >= 5:
                        for feature in DEFAULT_FEATURES:
                            usable = [
                                row
                                for row in rows
                                if row.get(feature) is not None
                                and str(row["ts_code"]) in outcomes
                            ]
                            ic = rank_correlation(
                                [float(row[feature]) for row in usable],
                                [outcomes[str(row["ts_code"])] for row in usable],
                            )
                            if ic is not None:
                                observations.append(
                                    {
                                        "snapshot": snapshot.isoformat(),
                                        "end_snapshot": end_snapshot.isoformat(),
                                        "start_execution": start_exec.isoformat(),
                                        "end_execution": end_exec.isoformat(),
                                        "execution_lag_days": lag_days,
                                        "era": era(snapshot),
                                        "market_regime": regime,
                                        "market_return_6m": market_6m,
                                        "feature": feature,
                                        "candidate_count": len(usable),
                                        "outcome_count": len(outcomes),
                                        "coverage_ratio": len(usable) / len(outcomes),
                                        "ic": ic,
                                    }
                                )
                snapshot = shift_month_end(snapshot, max(args.step_months, 1))
    finally:
        conn.close()

    grouped: dict[str, list[float]] = defaultdict(list)
    grouped_era: dict[tuple[str, str], list[float]] = defaultdict(list)
    grouped_regime: dict[tuple[str, str], list[float]] = defaultdict(list)
    grouped_lag: dict[tuple[str, int], list[float]] = defaultdict(list)
    grouped_candidate_count: dict[str, list[int]] = defaultdict(list)
    grouped_outcome_count: dict[str, list[int]] = defaultdict(list)
    grouped_coverage: dict[str, list[float]] = defaultdict(list)
    for row in observations:
        grouped[row["feature"]].append(float(row["ic"]))
        grouped_era[(row["feature"], row["era"])].append(float(row["ic"]))
        grouped_regime[(row["feature"], row["market_regime"])].append(float(row["ic"]))
        grouped_lag[(row["feature"], int(row["execution_lag_days"]))].append(
            float(row["ic"])
        )
        grouped_candidate_count[row["feature"]].append(int(row["candidate_count"]))
        grouped_outcome_count[row["feature"]].append(int(row["outcome_count"]))
        grouped_coverage[row["feature"]].append(float(row["coverage_ratio"]))

    summaries = []
    for feature in DEFAULT_FEATURES:
        total = summarize(grouped[feature])
        eras = {
            name: summarize(grouped_era[(feature, name)])
            for name in ("2005_2012", "2013_2018", "2019_latest")
        }
        regimes = {
            name: summarize(grouped_regime[(feature, name)])
            for name in ("bull", "neutral", "bear")
        }
        lags = {
            str(lag): summarize(grouped_lag[(feature, lag)])
            for lag in EXECUTION_LAGS
        }
        era_medians = [
            float(item["median_ic"])
            for item in eras.values()
            if item["median_ic"] is not None
        ]
        direction = 1.0 if (total["median_ic"] or 0.0) >= 0 else -1.0
        stable_eras = sum(value * direction > 0 for value in era_medians)
        lag_medians = [
            float(item["median_ic"])
            for item in lags.values()
            if item["median_ic"] is not None
        ]
        stable_lags = sum(value * direction > 0 for value in lag_medians)
        summaries.append(
            {
                "feature": feature,
                **total,
                "stable_era_count": stable_eras,
                "era_count": len(era_medians),
                "stable_lag_count": stable_lags,
                "lag_count": len(lag_medians),
                "median_candidate_count": statistics.median(
                    grouped_candidate_count[feature]
                ),
                "median_outcome_count": statistics.median(
                    grouped_outcome_count[feature]
                ),
                "median_coverage_ratio": statistics.median(
                    grouped_coverage[feature]
                ),
                "minimum_coverage_ratio": min(grouped_coverage[feature]),
                "eras": eras,
                "regimes": regimes,
                "lags": lags,
            }
        )
    summaries.sort(
        key=lambda item: (
            item["stable_era_count"],
            item["stable_lag_count"],
            abs(float(item["median_ic"] or 0.0)),
            abs(float(item["mean_ic"] or 0.0)),
        ),
        reverse=True,
    )

    prefix = Path(args.output_prefix)
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_summary.csv")
    json_path.write_text(
        json.dumps(
            {
                "method": (
                    "monthly anchors, point-in-time features, next three-month "
                    "returns at strict-engine execution lags"
                ),
                "execution_lags": list(EXECUTION_LAGS),
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "observations": observations,
                "summaries": summaries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "feature",
            "count",
            "mean_ic",
            "median_ic",
            "positive_rate",
            "abs_mean_ic",
            "stable_era_count",
            "era_count",
            "stable_lag_count",
            "lag_count",
            "median_candidate_count",
            "median_outcome_count",
            "median_coverage_ratio",
            "minimum_coverage_ratio",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in summaries:
            writer.writerow({key: item[key] for key in fields})
    for item in summaries[:12]:
        print(
            f"{item['feature']:<38} n={item['count']:>3} "
            f"median={float(item['median_ic'] or 0):+.4f} "
            f"mean={float(item['mean_ic'] or 0):+.4f} "
            f"eras={item['stable_era_count']}/{item['era_count']} "
            f"lags={item['stable_lag_count']}/{item['lag_count']} "
            f"coverage={float(item['median_coverage_ratio']):.1%}"
        )
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

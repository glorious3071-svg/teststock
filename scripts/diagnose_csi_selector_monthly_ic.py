#!/usr/bin/env python3
"""Diagnose CSI selector features against next-month returns without calendar inputs."""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR  # noqa: E402
from backtest.phase_schedule import shift_month_end  # noqa: E402
from db.connection import get_connection  # noqa: E402
from scripts.backtest_calendar_neutral_csi_tipp import load_selector_price_series  # noqa: E402
from scripts.backtest_scorecard_csi_dynamic_defense import (  # noqa: E402
    load_price_series,
    period_return,
    price_at,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests" / "csi_selector_monthly_ic"
OUT_JSON = OUT_DIR / "report.json"
OUT_CSV = OUT_DIR / "features.csv"
EXECUTION_LAGS = (0, 1, 3, 5)
MIN_HISTORY_MONTHS = 36
META_FIELDS = {"ts_code", "index_name", "recommendation_as_of"}


def month_ends(start: date, end: date) -> list[date]:
    output = []
    cursor = start
    while shift_month_end(cursor, 1) <= end:
        output.append(cursor)
        cursor = shift_month_end(cursor, 1)
    return output


def cross_section_metric(rows: list[dict[str, Any]], feature: str) -> dict[str, float] | None:
    usable = [
        row
        for row in rows
        if row.get(feature) is not None
        and row.get("forward_return") is not None
        and math.isfinite(float(row[feature]))
        and math.isfinite(float(row["forward_return"]))
    ]
    if len(usable) < 5 or len({float(row[feature]) for row in usable}) < 3:
        return None
    frame = pd.DataFrame(usable)
    ic = frame[feature].rank(method="average").corr(
        frame["forward_return"].rank(method="average")
    )
    if pd.isna(ic):
        return None
    count = max(1, len(frame) // 5)
    ordered = frame.sort_values(feature)
    universe_return = float(frame["forward_return"].mean())
    return {
        "ic": float(ic),
        "high_excess": float(ordered.tail(count)["forward_return"].mean()) - universe_return,
        "low_excess": float(ordered.head(count)["forward_return"].mean()) - universe_return,
    }


def build_snapshots() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        trade_dates = [day for day, _value in price_series[CS300_CODE]]
        latest_complete = shift_month_end(trade_dates[-1], -1)
        snapshots = []
        with conn.cursor() as cur:
            for snapshot in month_ends(date(2005, 12, 31), latest_complete):
                candidates = SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, snapshot)
                lag_rows = {}
                for lag in EXECUTION_LAGS:
                    start_exec = shifted_boundary(trade_dates, snapshot, lag)
                    end_exec = shifted_boundary(
                        trade_dates,
                        shift_month_end(snapshot, 1),
                        lag,
                    )
                    rows = []
                    for candidate in candidates:
                        code = candidate["ts_code"]
                        if (
                            code not in price_series
                            or price_at(price_series, code, start_exec) is None
                            or price_at(price_series, code, end_exec) is None
                        ):
                            continue
                        rows.append(
                            {
                                **candidate,
                                "forward_return": period_return(
                                    price_series,
                                    code,
                                    start_exec,
                                    end_exec,
                                ),
                            }
                        )
                    lag_rows[str(lag)] = rows
                snapshots.append(
                    {
                        "snapshot": snapshot.isoformat(),
                        "phase": (snapshot.year * 12 + snapshot.month) % 12,
                        "lag_rows": lag_rows,
                    }
                )
        return snapshots
    finally:
        conn.close()


def diagnose_feature(snapshots: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    lag_summaries = {}
    all_phase_excess: dict[int, list[float]] = defaultdict(list)
    for lag in EXECUTION_LAGS:
        metrics = []
        for snapshot in snapshots:
            metric = cross_section_metric(snapshot["lag_rows"][str(lag)], feature)
            if metric is not None:
                metrics.append(
                    {
                        **metric,
                        "snapshot": snapshot["snapshot"],
                        "phase": snapshot["phase"],
                    }
                )
        excess = []
        for index, metric in enumerate(metrics):
            history = metrics[:index]
            if len(history) < MIN_HISTORY_MONTHS:
                continue
            orientation = 1 if statistics.median(row["ic"] for row in history) >= 0 else -1
            value = metric["high_excess"] if orientation > 0 else metric["low_excess"]
            excess.append(value)
            all_phase_excess[metric["phase"]].append(value)
        lag_summaries[str(lag)] = {
            "snapshot_count": len(metrics),
            "expanding_month_count": len(excess),
            "mean_excess": statistics.mean(excess) if excess else None,
            "median_excess": statistics.median(excess) if excess else None,
            "positive_rate": sum(value > 0 for value in excess) / len(excess) if excess else None,
        }
    lag_means = [
        float(summary["mean_excess"])
        for summary in lag_summaries.values()
        if summary["mean_excess"] is not None
    ]
    phase_means = {
        str(phase): statistics.mean(values) if values else None
        for phase, values in sorted(all_phase_excess.items())
    }
    return {
        "feature": feature,
        "lag_summaries": lag_summaries,
        "mean_excess_across_lags": statistics.mean(lag_means) if lag_means else None,
        "worst_lag_mean_excess": min(lag_means) if lag_means else None,
        "positive_phase_count": sum(
            value is not None and value > 0 for value in phase_means.values()
        ),
        "phase_mean_excess": phase_means,
        "candidate": bool(
            lag_means
            and min(lag_means) > 0.0
            and sum(value is not None and value > 0 for value in phase_means.values()) >= 8
        ),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = build_snapshots()
    features = sorted(
        {
            key
            for snapshot in snapshots
            for rows in snapshot["lag_rows"].values()
            for row in rows
            for key, value in row.items()
            if key not in META_FIELDS
            and key != "forward_return"
            and isinstance(value, (int, float))
        }
    )
    diagnostics = [diagnose_feature(snapshots, feature) for feature in features]
    diagnostics.sort(
        key=lambda row: (
            row["candidate"],
            row["worst_lag_mean_excess"] or -1.0,
            row["mean_excess_across_lags"] or -1.0,
        ),
        reverse=True,
    )
    for row in diagnostics:
        print(
            f"{row['feature']:<34} candidate={str(row['candidate']):<5} "
            f"mean={row['mean_excess_across_lags']} "
            f"worst_lag={row['worst_lag_mean_excess']} "
            f"phase_win={row['positive_phase_count']}/12"
        )
    payload = {
        "method": {
            "outcome": "next-month return of each point-in-time investable passive ETF benchmark",
            "execution_lags": list(EXECUTION_LAGS),
            "minimum_history_months": MIN_HISTORY_MONTHS,
            "feature_orientation": "expanding history only; calendar phase is never an input",
            "phase_usage": "post-hoc robustness grouping only",
        },
        "snapshot_count": len(snapshots),
        "features": diagnostics,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "feature",
                "candidate",
                "mean_excess_across_lags",
                "worst_lag_mean_excess",
                "positive_phase_count",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(diagnostics)
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

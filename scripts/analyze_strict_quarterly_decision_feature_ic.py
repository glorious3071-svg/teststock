#!/usr/bin/env python3
"""Audit quarter-boundary features across all 12 phases and four execution lags."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


ERAS = ("2005_2012", "2013_2018", "2019_latest")


def era(day: date) -> str:
    if day.year <= 2012:
        return ERAS[0]
    if day.year <= 2018:
        return ERAS[1]
    return ERAS[2]


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
    left = average_ranks(xs)
    right = average_ranks(ys)
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    denominator = (
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    ) ** 0.5
    return numerator / denominator if denominator > 0 else None


def finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def observations(report: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for result in report["results"]:
        for case in result["cases"]:
            for row in case.get("decision_rows", []):
                anchor = date.fromisoformat(str(row["rebalance_anchor"]))
                output.append(
                    {
                        "phase": int(case["phase_month_offset"]),
                        "lag": int(case["execution_lag_days"]),
                        "anchor": anchor.isoformat(),
                        "era": era(anchor),
                        "features": dict(row.get("market_state") or {}),
                        "forward_return": float(row["realized_risk_return"]),
                        "forward_max_drawdown": float(
                            row["realized_risk_max_drawdown"]
                        ),
                    }
                )
    return output


def feature_screen(rows: list[dict[str, Any]], outcome: str) -> list[dict[str, Any]]:
    features = sorted(
        {
            name
            for row in rows
            for name, raw in row["features"].items()
            if finite(raw)
        }
    )
    results = []
    for feature in features:
        cells = []
        for phase in range(12):
            for lag in (0, 1, 3, 5):
                for era_name in ERAS:
                    usable = [
                        row
                        for row in rows
                        if row["phase"] == phase
                        and row["lag"] == lag
                        and row["era"] == era_name
                        and finite(row["features"].get(feature))
                    ]
                    ic = spearman(
                        [float(row["features"][feature]) for row in usable],
                        [float(row[outcome]) for row in usable],
                    )
                    if ic is None:
                        continue
                    time_ic = spearman(
                        [
                            float(date.fromisoformat(row["anchor"]).toordinal())
                            for row in usable
                        ],
                        [float(row["features"][feature]) for row in usable],
                    )
                    cells.append(
                        {
                            "phase": phase,
                            "lag": lag,
                            "era": era_name,
                            "count": len(usable),
                            "spearman_ic": ic,
                            "time_spearman": time_ic,
                        }
                    )
        if not cells:
            continue
        values = [float(cell["spearman_ic"]) for cell in cells]
        median_ic = statistics.median(values)
        orientation = 1.0 if median_ic >= 0 else -1.0
        aligned = [orientation * value for value in values]
        time_values = [
            abs(float(cell["time_spearman"]))
            for cell in cells
            if cell["time_spearman"] is not None
        ]
        median_abs_time = statistics.median(time_values) if time_values else None
        time_high_rate = (
            sum(value >= 0.80 for value in time_values) / len(time_values)
            if time_values
            else None
        )
        results.append(
            {
                "feature": feature,
                "orientation": "higher" if orientation > 0 else "lower",
                "cell_count": len(cells),
                "phase_count": len({cell["phase"] for cell in cells}),
                "lag_count": len({cell["lag"] for cell in cells}),
                "era_count": len({cell["era"] for cell in cells}),
                "median_ic": median_ic,
                "mean_ic": statistics.mean(values),
                "aligned_positive_rate": sum(value > 0 for value in aligned)
                / len(aligned),
                "aligned_worst_ic": min(aligned),
                "aligned_q25_ic": statistics.quantiles(aligned, n=4)[0]
                if len(aligned) >= 4
                else min(aligned),
                "median_abs_time_spearman": median_abs_time,
                "time_trend_high_rate": time_high_rate,
                "time_trend_leakage_flag": bool(
                    median_abs_time is not None
                    and time_high_rate is not None
                    and (median_abs_time >= 0.80 or time_high_rate >= 0.50)
                ),
                "cells": cells,
            }
        )
    results.sort(
        key=lambda item: (
            not item["time_trend_leakage_flag"],
            item["phase_count"] == 12,
            item["lag_count"] == 4,
            item["era_count"] == 3,
            item["aligned_positive_rate"],
            item["aligned_q25_ic"],
            abs(item["median_ic"]),
        ),
        reverse=True,
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    rows = observations(report)
    expected = 48 * 80
    if len(rows) != expected:
        raise RuntimeError(f"expected {expected} decision rows, got {len(rows)}")
    return_results = feature_screen(rows, "forward_return")
    drawdown_results = feature_screen(rows, "forward_max_drawdown")
    payload = {
        "method": (
            "point-in-time feature Spearman IC in 12 phase x 4 execution-lag x "
            "3 era cells against exact-quarter frozen-share ETF counterfactual labels"
        ),
        "source_report": str(args.report),
        "decision_count": len(rows),
        "return_results": return_results,
        "drawdown_results": drawdown_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in return_results[:20]:
        print(
            f"{item['feature']:<52} {item['orientation']:<6} "
            f"cells={item['cell_count']:>3} median={item['median_ic']:+.3f} "
            f"aligned={item['aligned_positive_rate']:.1%} "
            f"q25={item['aligned_q25_ic']:+.3f} "
            f"time_leak={item['time_trend_leakage_flag']}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Audit natural-year scorecard-entry features across every strict drift path."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analyze_strict_quarterly_decision_feature_ic import finite, spearman


ERAS = ("2005_2014", "2015_latest")


def era(year: int) -> str:
    return ERAS[0] if year <= 2014 else ERAS[1]


def annual_observations(report: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    cases = [case for result in report["results"] for case in result["cases"]]
    if len(cases) != 48:
        raise RuntimeError(f"expected 48 strict cases, got {len(cases)}")
    for case in cases:
        groups: OrderedDict[tuple[int, str], dict[str, Any]] = OrderedDict()
        current_key: tuple[int, str] | None = None
        for row in case.get("decision_rows", []):
            if row.get("realized_risk_return") is None:
                continue
            context = dict(row.get("scorecard_context") or {})
            if bool(context.get("allocation_entry")) or current_key is None:
                current_key = (
                    int(context["allocation_year"]),
                    str(row["decision_date"]),
                )
                features = dict(row.get("market_state") or {})
                features["scorecard_score"] = context.get("score")
                features["scorecard_raw_base_weight"] = row[
                    "exposure_formation"
                ]["raw_base_weight"]
                for name, value in dict(context.get("known_inputs") or {}).items():
                    features[f"scorecard_input_{name}"] = value
                groups[current_key] = {
                    "entry": row,
                    "features": features,
                    "returns": [],
                    "drawdowns": [],
                }
            group = groups[current_key]
            group["returns"].append(float(row["realized_risk_return"]))
            group["drawdowns"].append(float(row["realized_risk_max_drawdown"]))
        if len(groups) < 19:
            raise RuntimeError(
                f"phase={case['phase_month_offset']} lag={case['execution_lag_days']} "
                f"has only {len(groups)} annual scorecard periods"
            )
        for (year, entry_day), group in groups.items():
            output.append(
                {
                    "phase": int(case["phase_month_offset"]),
                    "lag": int(case["execution_lag_days"]),
                    "year": year,
                    "entry_day": entry_day,
                    "era": era(year),
                    "quarter_count": len(group["returns"]),
                    "features": group["features"],
                    "forward_return": math.prod(
                        1.0 + value for value in group["returns"]
                    )
                    - 1.0,
                    "forward_worst_quarter_drawdown": min(group["drawdowns"]),
                }
            )
    return output


def feature_screen(
    rows: list[dict[str, Any]], outcome: str
) -> list[dict[str, Any]]:
    feature_names = sorted(
        {
            name
            for row in rows
            for name, value in row["features"].items()
            if finite(value)
        }
    )
    results: list[dict[str, Any]] = []
    for feature in feature_names:
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
                        [float(row["year"]) for row in usable],
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
        orientation = 1.0 if median_ic >= 0.0 else -1.0
        aligned = [orientation * value for value in values]
        time_values = [
            abs(float(cell["time_spearman"]))
            for cell in cells
            if cell["time_spearman"] is not None
        ]
        median_abs_time = statistics.median(time_values) if time_values else None
        high_time_rate = (
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
                "aligned_q25_ic": statistics.quantiles(aligned, n=4)[0]
                if len(aligned) >= 4
                else min(aligned),
                "aligned_worst_ic": min(aligned),
                "median_abs_time_spearman": median_abs_time,
                "time_trend_high_rate": high_time_rate,
                "time_trend_leakage_flag": bool(
                    median_abs_time is not None
                    and high_time_rate is not None
                    and (median_abs_time >= 0.80 or high_time_rate >= 0.50)
                ),
                "cells": cells,
            }
        )
    results.sort(
        key=lambda item: (
            not item["time_trend_leakage_flag"],
            item["phase_count"] == 12,
            item["lag_count"] == 4,
            item["era_count"] == 2,
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
    rows = annual_observations(report)
    payload = {
        "method": (
            "natural-year scorecard-entry features in 12 phase x 4 lag x 2 era "
            "cells; labels compound completed frozen-share quarterly ETF returns"
        ),
        "source_report": str(args.report),
        "annual_observation_count": len(rows),
        "return_results": feature_screen(rows, "forward_return"),
        "drawdown_results": feature_screen(
            rows, "forward_worst_quarter_drawdown"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in payload["return_results"][:20]:
        print(
            f"{item['feature']:<54} {item['orientation']:<6} "
            f"cells={item['cell_count']:>2} median={item['median_ic']:+.3f} "
            f"aligned={item['aligned_positive_rate']:.1%} "
            f"q25={item['aligned_q25_ic']:+.3f} "
            f"time_leak={item['time_trend_leakage_flag']}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

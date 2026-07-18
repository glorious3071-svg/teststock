#!/usr/bin/env python3
"""Audit point-in-time ETF feature stability across historical eras.

The input dataset must contain monthly cross-sections and three-month labels.
Each information coefficient is computed inside one snapshot, so the growing
ETF universe cannot create a spurious time-series relationship.  Labels are
used only for research diagnostics and are never exposed to a selector before
their ``end_snapshot``.
"""

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

from backtest.monthly_online_selector import rank_correlation


DEFAULT_DATASET = ROOT / "data/backtests/passive_etf_quarterly_enriched_v2_dataset.json"
DEFAULT_OUTPUT = ROOT / "data/backtests/passive_etf_feature_stability_v2_report.json"
LABELS = {
    "forward_return_3m",
    "forward_max_drawdown_3m",
    "forward_worst_from_start_3m",
}
IDENTIFIERS = {
    "snapshot",
    "end_snapshot",
    "era",
    "market_regime",
    "ts_code",
    "index_code",
}


def era(day: date) -> str:
    if day.year <= 2012:
        return "2005_2012"
    if day.year <= 2018:
        return "2013_2018"
    return "2019_latest"


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def summarize(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean_ic": statistics.mean(values) if values else None,
        "median_ic": statistics.median(values) if values else None,
        "positive_rate": (
            sum(value > 0.0 for value in values) / len(values) if values else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--feature-prefix", action="append")
    parser.add_argument("--minimum-cross-section", type=int, default=5)
    args = parser.parse_args()

    dataset = args.dataset if args.dataset.is_absolute() else ROOT / args.dataset
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    rows = list(payload["candidate_observations"])
    features = sorted(
        {
            key
            for row in rows
            for key, value in row.items()
            if key not in LABELS | IDENTIFIERS
            and finite_number(value)
            and (
                not args.feature_prefix
                or any(key.startswith(prefix) for prefix in args.feature_prefix)
            )
        }
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["snapshot"])].append(row)

    feature_ics: dict[str, list[float]] = defaultdict(list)
    era_ics: dict[tuple[str, str], list[float]] = defaultdict(list)
    regime_ics: dict[tuple[str, str], list[float]] = defaultdict(list)
    usable_rows: dict[str, int] = defaultdict(int)
    for snapshot, cross_section in sorted(grouped.items()):
        snapshot_era = era(date.fromisoformat(snapshot))
        regime = str(cross_section[0].get("market_regime") or "unknown")
        for feature in features:
            usable = [
                row
                for row in cross_section
                if finite_number(row.get(feature))
                and finite_number(row.get("forward_return_3m"))
            ]
            if len(usable) < args.minimum_cross_section:
                continue
            ic = rank_correlation(
                [float(row[feature]) for row in usable],
                [float(row["forward_return_3m"]) for row in usable],
            )
            if ic is None or not math.isfinite(ic):
                continue
            feature_ics[feature].append(ic)
            era_ics[(feature, snapshot_era)].append(ic)
            regime_ics[(feature, regime)].append(ic)
            usable_rows[feature] += len(usable)

    era_names = ("2005_2012", "2013_2018", "2019_latest")
    regime_names = ("bull", "neutral", "bear", "unknown")
    results = []
    for feature in features:
        overall = summarize(feature_ics[feature])
        eras = {name: summarize(era_ics[(feature, name)]) for name in era_names}
        regimes = {
            name: summarize(regime_ics[(feature, name)]) for name in regime_names
        }
        median = float(overall["median_ic"] or 0.0)
        direction = 1.0 if median >= 0.0 else -1.0
        era_medians = [
            float(item["median_ic"])
            for item in eras.values()
            if item["median_ic"] is not None
        ]
        results.append(
            {
                "feature": feature,
                **overall,
                "usable_row_count": usable_rows[feature],
                "stable_era_count": sum(value * direction > 0.0 for value in era_medians),
                "era_count": len(era_medians),
                "eras": eras,
                "regimes": regimes,
            }
        )
    results.sort(
        key=lambda item: (
            item["stable_era_count"],
            abs(float(item["median_ic"] or 0.0)),
            int(item["count"]),
        ),
        reverse=True,
    )

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    "monthly point-in-time cross-sectional rank IC; "
                    "next-three-month labels used only as outcomes"
                ),
                "dataset": str(dataset),
                "snapshot_count": len(grouped),
                "candidate_count": len(rows),
                "minimum_cross_section": args.minimum_cross_section,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        print(
            f"{item['feature']:<48} n={item['count']:>3} "
            f"median={float(item['median_ic'] or 0.0):+.4f} "
            f"mean={float(item['mean_ic'] or 0.0):+.4f} "
            f"eras={item['stable_era_count']}/{item['era_count']}"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

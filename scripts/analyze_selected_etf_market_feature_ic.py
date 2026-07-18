#!/usr/bin/env python3
"""Audit selected-ETF time-series features across all quarterly drift phases."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analyze_strict_quarterly_market_feature_ic import (  # noqa: E402
    era,
    feature_screen,
)


DEFAULT_DATASET = ROOT / "data/backtests/passive_etf_selected_market_dataset_v4_pboc.json"
DEFAULT_OUTPUT = ROOT / "data/backtests/passive_etf_selected_market_feature_ic_v4_pboc_report.json"
LABELS = {
    "snapshot",
    "end_snapshot",
    "selected_codes",
    "forward_return_3m",
    "forward_max_drawdown_3m",
}


def drift_observations(
    rows: list[dict[str, Any]], periods: int = 80
) -> list[dict[str, Any]]:
    """Expand the 12 valid monthly starts into exact three-month paths."""

    observations: list[dict[str, Any]] = []
    for phase, _start in enumerate(rows[:12]):
        for row in rows[phase::3][:periods]:
            if row.get("forward_return_3m") is None or row.get("forward_max_drawdown_3m") is None:
                continue
            snapshot = date.fromisoformat(str(row["snapshot"]))
            features = {
                name: float(value)
                for name, value in row.items()
                if name not in LABELS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            }
            observations.append(
                {
                    "phase_month_offset": phase,
                    "decision_date": snapshot.isoformat(),
                    "era": era(snapshot),
                    "features": features,
                    "forward_risk_return_3m": float(row["forward_return_3m"]),
                    "forward_risk_max_drawdown_3m": float(
                        row["forward_max_drawdown_3m"]
                    ),
                }
            )
    return observations


def oracle_summary(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for phase in range(12):
        rows = [row for row in observations if row["phase_month_offset"] == phase]
        output.append(
            {
                "phase_month_offset": phase,
                "quarter_count": len(rows),
                "all_selected_multiple": math.prod(
                    1.0 + float(row["forward_risk_return_3m"]) for row in rows
                ),
                "positive_quarter_oracle_multiple": math.prod(
                    max(1.0, 1.0 + float(row["forward_risk_return_3m"]))
                    for row in rows
                ),
                "lookahead": True,
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    observations = drift_observations(list(payload["selected_observations"]))
    result = {
        "dataset": str(args.dataset),
        "observation_count": len(observations),
        "phase_count": len({row["phase_month_offset"] for row in observations}),
        "return_results": feature_screen(observations, "forward_risk_return_3m"),
        "drawdown_results": feature_screen(
            observations, "forward_risk_max_drawdown_3m"
        ),
        "oracle_summary": oracle_summary(observations),
        "analysis_rows": observations,
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for label, rows in (
        ("return", result["return_results"]),
        ("drawdown", result["drawdown_results"]),
    ):
        print(f"Top {label} features:")
        for row in rows[:20]:
            print(
                f"{row['feature']:<58} {row['orientation']:<6} "
                f"cells={row['cell_count']:2d} median={row['median_ic']:+.3f} "
                f"positive={row['aligned_positive_rate']*100:5.1f}% "
                f"q25={row['aligned_q25_ic']:+.3f}"
            )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

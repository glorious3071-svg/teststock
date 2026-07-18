#!/usr/bin/env python3
"""Cross-sectional feature IC diagnostics for point-in-time ETF benchmarks."""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR  # noqa: E402
from backtest.phase_schedule import build_windows  # noqa: E402
from db.connection import get_connection  # noqa: E402
from scripts.validate_scorecard_csi_generalization import (  # noqa: E402
    END_YEAR,
    START_YEAR,
    SCHEDULE_12M_12M,
    boundary_return,
    complete_schedule_anchor,
    schedule_execution_boundary,
)

OUT_DIR = ROOT / "data" / "backtests" / "csi_selector_feature_ic"
OUT_JSON = OUT_DIR / "report.json"
OUT_CSV = OUT_DIR / "features.csv"
EXECUTION_LAG_DAYS = 3
META_FIELDS = {
    "ts_code",
    "index_name",
    "recommendation_as_of",
}


def snapshot_metrics(rows: list[dict[str, Any]], feature: str) -> dict[str, float] | None:
    usable = [row for row in rows if row.get(feature) is not None and math.isfinite(float(row[feature]))]
    if len(usable) < 5 or len({float(row[feature]) for row in usable}) < 3:
        return None
    frame = pd.DataFrame(usable)
    ic = frame[feature].rank(method="average").corr(frame["forward_return"].rank(method="average"))
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


def build_snapshots(cur) -> list[dict[str, Any]]:
    snapshots = []
    cycles = END_YEAR - START_YEAR + 1
    for phase in range(12):
        anchor, _shifted = complete_schedule_anchor(
            cur,
            date(START_YEAR - 1, 12, 31),
            SCHEDULE_12M_12M,
            phase,
            EXECUTION_LAG_DAYS,
            cycles,
        )
        for window in build_windows(anchor, SCHEDULE_12M_12M, phase, cycles):
            start_exec = schedule_execution_boundary(cur, window.start_snapshot, EXECUTION_LAG_DAYS)
            end_exec = schedule_execution_boundary(cur, window.end_snapshot, EXECUTION_LAG_DAYS)
            rows = []
            for item in SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, window.start_snapshot):
                rows.append(
                    {
                        **item,
                        "forward_return": boundary_return(cur, item["ts_code"], start_exec, end_exec),
                    }
                )
            snapshots.append(
                {
                    "phase": phase,
                    "snapshot": window.start_snapshot.isoformat(),
                    "snapshot_year": window.start_snapshot.year,
                    "candidate_count": len(rows),
                    "rows": rows,
                }
            )
    return snapshots


def feature_diagnostic(snapshots: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    metrics = []
    for snapshot in snapshots:
        value = snapshot_metrics(snapshot["rows"], feature)
        if value is not None:
            metrics.append({**value, "phase": snapshot["phase"], "snapshot_year": snapshot["snapshot_year"]})
    leave_phase_edges = {}
    leave_phase_ics = {}
    for phase in range(12):
        train = [row for row in metrics if row["phase"] != phase]
        test = [row for row in metrics if row["phase"] == phase]
        orientation = 1 if statistics.median(row["ic"] for row in train) >= 0 else -1
        edges = [row["high_excess"] if orientation > 0 else row["low_excess"] for row in test]
        leave_phase_edges[str(phase)] = statistics.mean(edges) if edges else None
        leave_phase_ics[str(phase)] = statistics.median(row["ic"] * orientation for row in test) if test else None

    expanding_edges = []
    for year in sorted({row["snapshot_year"] for row in metrics}):
        train = [row for row in metrics if row["snapshot_year"] < year]
        test = [row for row in metrics if row["snapshot_year"] == year]
        if len(train) < 36 or not test:
            continue
        orientation = 1 if statistics.median(row["ic"] for row in train) >= 0 else -1
        expanding_edges.extend(row["high_excess"] if orientation > 0 else row["low_excess"] for row in test)

    phase_edges = [float(value) for value in leave_phase_edges.values() if value is not None]
    phase_ics = [float(value) for value in leave_phase_ics.values() if value is not None]
    overall_ic = statistics.median(row["ic"] for row in metrics) if metrics else None
    return {
        "feature": feature,
        "snapshot_count": len(metrics),
        "median_ic": overall_ic,
        "leave_phase_median_aligned_ic": statistics.median(phase_ics) if phase_ics else None,
        "leave_phase_mean_top_quintile_excess": statistics.mean(phase_edges) if phase_edges else None,
        "leave_phase_worst_top_quintile_excess": min(phase_edges) if phase_edges else None,
        "positive_phase_count": sum(value > 0 for value in phase_edges),
        "expanding_year_mean_top_quintile_excess": statistics.mean(expanding_edges) if expanding_edges else None,
        "leave_phase_edges": leave_phase_edges,
        "leave_phase_aligned_ic": leave_phase_ics,
        "candidate": (
            len(metrics) >= 120
            and sum(value > 0 for value in phase_edges) >= 8
            and (statistics.mean(phase_edges) if phase_edges else -1.0) > 0.0
            and (statistics.mean(expanding_edges) if expanding_edges else -1.0) > 0.0
        ),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            snapshots = build_snapshots(cur)
    finally:
        conn.close()
    features = sorted(
        {
            key
            for snapshot in snapshots
            for row in snapshot["rows"]
            for key, value in row.items()
            if key not in META_FIELDS and key != "forward_return" and isinstance(value, (int, float))
        }
    )
    diagnostics = [feature_diagnostic(snapshots, feature) for feature in features]
    diagnostics.sort(
        key=lambda row: (
            row["candidate"],
            row["leave_phase_mean_top_quintile_excess"] or -1.0,
            row["expanding_year_mean_top_quintile_excess"] or -1.0,
        ),
        reverse=True,
    )
    for row in diagnostics:
        print(
            f"{row['feature']:<32} candidate={str(row['candidate']):<5} "
            f"ic={row['median_ic']} phase_edge={row['leave_phase_mean_top_quintile_excess']} "
            f"wins={row['positive_phase_count']}/12 time_edge={row['expanding_year_mean_top_quintile_excess']}"
        )
    payload = {
        "method": {
            "universe": "Domestic passive ETF benchmark indices available at each snapshot",
            "outcome": "Next twelve-month benchmark index return",
            "phase_offsets": list(range(12)),
            "execution_lag_days": EXECUTION_LAG_DAYS,
            "leave_phase_out": "Feature orientation learned on eleven phases; top quintile evaluated on held-out phase",
            "expanding_year": "Feature orientation learned only from prior snapshot years",
        },
        "snapshot_count": len(snapshots),
        "candidate_count_range": [
            min(snapshot["candidate_count"] for snapshot in snapshots),
            max(snapshot["candidate_count"] for snapshot in snapshots),
        ],
        "features": diagnostics,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fields = ["feature", "snapshot_count", "median_ic", "leave_phase_median_aligned_ic", "leave_phase_mean_top_quintile_excess", "leave_phase_worst_top_quintile_excess", "positive_phase_count", "expanding_year_mean_top_quintile_excess", "candidate"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(diagnostics)
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build a point-in-time CSI selector dataset with execution-lag labels.

Every month-end is a possible quarterly rebalance anchor.  Candidate features
are computed at that anchor; forward index returns start only on the selected
post-anchor execution day.  The four lag labels let selector research optimize
calendar robustness instead of one privileged execution convention.
"""

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

from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR
from backtest.phase_schedule import shift_month_end
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_monthly import period_return
from scripts.backtest_calendar_neutral_csi_tipp import load_selector_price_series
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.validate_scorecard_csi_generalization import schedule_execution_boundary


DEFAULT_OUTPUT = ROOT / "data/backtests/csi_quarterly_selector_dataset_v2.json"
EXECUTION_LAGS = (0, 1, 3, 5)
IDENTIFIERS = {
    "ts_code",
    "index_name",
    "recommendation_as_of",
}


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=date(2005, 3, 31))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    observations: list[dict[str, Any]] = []
    snapshot_count = 0
    conn = get_connection()
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        with conn.cursor() as cur:
            snapshot = args.start
            while shift_month_end(snapshot, 3) <= args.end:
                end_snapshot = shift_month_end(snapshot, 3)
                execution_windows = {
                    lag: (
                        schedule_execution_boundary(cur, snapshot, lag),
                        schedule_execution_boundary(cur, end_snapshot, lag),
                    )
                    for lag in EXECUTION_LAGS
                }
                rows = SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, snapshot)
                current_count = 0
                for row in rows:
                    code = str(row["ts_code"])
                    labels = {
                        f"forward_return_3m_lag{lag}": period_return(
                            price_series,
                            code,
                            start_exec,
                            end_exec,
                        )
                        for lag, (start_exec, end_exec) in execution_windows.items()
                    }
                    if any(value is None for value in labels.values()):
                        continue
                    features = {
                        key: float(value)
                        for key, value in row.items()
                        if key not in IDENTIFIERS and finite_number(value)
                    }
                    observations.append(
                        {
                            "snapshot": snapshot.isoformat(),
                            "end_snapshot": end_snapshot.isoformat(),
                            "era": era(snapshot),
                            "ts_code": code,
                            "index_name": str(row.get("index_name") or code),
                            **features,
                            **{key: float(value) for key, value in labels.items()},
                        }
                    )
                    current_count += 1
                snapshot_count += current_count >= 5
                snapshot = shift_month_end(snapshot, 1)
    finally:
        conn.close()

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    "monthly point-in-time domestic passive-ETF benchmark candidates; "
                    "three-month index-return labels begin after each execution lag"
                ),
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "execution_lags": list(EXECUTION_LAGS),
                "snapshot_count": snapshot_count,
                "candidate_count": len(observations),
                "candidate_observations": observations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote {output}; snapshots={snapshot_count} "
        f"candidates={len(observations)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

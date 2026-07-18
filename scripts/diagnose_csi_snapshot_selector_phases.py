#!/usr/bin/env python3
"""Evaluate arbitrary-snapshot CSI selection across twelve cycle phases."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SELECTOR_POLICIES, SNAPSHOT_CSI_SELECTOR  # noqa: E402
from backtest.phase_schedule import build_windows  # noqa: E402
from db.connection import get_connection  # noqa: E402
from scripts.backtest_scorecard_csi_midyear_risk import (  # noqa: E402
    CS300_CODE,
    INITIAL_CAPITAL,
    max_drawdown,
)
from scripts.validate_scorecard_csi_generalization import (  # noqa: E402
    END_YEAR,
    START_YEAR,
    SCHEDULE_12M_12M,
    boundary_return,
    complete_schedule_anchor,
    schedule_execution_boundary,
)

OUT_DIR = ROOT / "data" / "backtests" / "csi_snapshot_selector"
OUT_JSON = OUT_DIR / "report.json"
OUT_CSV = OUT_DIR / "summary.csv"
EXECUTION_LAG_DAYS = 3


def run_case(cur, policy, phase: int, include_rows: bool = False) -> dict[str, Any]:
    cycles = END_YEAR - START_YEAR + 1
    anchor, shifted_cycles = complete_schedule_anchor(
        cur,
        date(START_YEAR - 1, 12, 31),
        SCHEDULE_12M_12M,
        phase,
        EXECUTION_LAG_DAYS,
        cycles,
    )
    windows = build_windows(anchor, SCHEDULE_12M_12M, phase, cycles)
    capital = INITIAL_CAPITAL
    benchmark_capital = INITIAL_CAPITAL
    curve = [capital]
    benchmark_curve = [benchmark_capital]
    rows = []
    for window in windows:
        start_exec = schedule_execution_boundary(cur, window.start_snapshot, EXECUTION_LAG_DAYS)
        end_exec = schedule_execution_boundary(cur, window.end_snapshot, EXECUTION_LAG_DAYS)
        selected = SNAPSHOT_CSI_SELECTOR.select(cur, window.start_snapshot, policy)
        if selected:
            basket_return = sum(
                float(item["weight"]) * boundary_return(cur, item["ts_code"], start_exec, end_exec)
                for item in selected
            )
        else:
            basket_return = boundary_return(cur, CS300_CODE, start_exec, end_exec)
        benchmark_return = boundary_return(cur, CS300_CODE, start_exec, end_exec)
        capital *= 1.0 + basket_return
        benchmark_capital *= 1.0 + benchmark_return
        curve.append(capital)
        benchmark_curve.append(benchmark_capital)
        if include_rows:
            rows.append(
                {
                    "cycle_index": window.cycle_index,
                    "snapshot": window.start_snapshot.isoformat(),
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "basket_return": basket_return,
                    "benchmark_return": benchmark_return,
                    "capital": capital,
                    "holdings": [
                        {
                            "ts_code": item["ts_code"],
                            "index_name": item["index_name"],
                            "weight": item["weight"],
                            "selector_score": item["selector_score"],
                            "recommendation_as_of": (
                                item["recommendation_as_of"].isoformat()
                                if item["recommendation_as_of"] is not None
                                else None
                            ),
                        }
                        for item in selected
                    ],
                }
            )
    return {
        "name": f"{policy.name}_phase{phase}_lag{EXECUTION_LAG_DAYS}",
        "policy": policy.name,
        "phase_month_offset": phase,
        "sample_shift_cycles": shifted_cycles,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / cycles) - 1.0,
        "max_drawdown": max_drawdown(curve),
        "benchmark_final_capital_wan": benchmark_capital / 10_000.0,
        "benchmark_max_drawdown": max_drawdown(benchmark_curve),
        "rows": rows,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "min_final_capital_wan": min(case["final_capital_wan"] for case in cases),
        "median_final_capital_wan": statistics.median(case["final_capital_wan"] for case in cases),
        "worst_max_drawdown": min(case["max_drawdown"] for case in cases),
        "min_annualized_return": min(case["annualized_return"] for case in cases),
        "min_excess_final_capital_wan": min(
            case["final_capital_wan"] - case["benchmark_final_capital_wan"] for case in cases
        ),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            results = []
            for policy in SELECTOR_POLICIES:
                cases = [run_case(cur, policy, phase, include_rows=True) for phase in range(12)]
                summary = summarize(cases)
                results.append({"policy": policy.__dict__, "summary": summary, "cases": cases})
                print(f"{policy.name:<32} {summary}")
    finally:
        conn.close()
    results.sort(key=lambda item: (item["summary"]["min_final_capital_wan"], item["summary"]["worst_max_drawdown"]), reverse=True)
    payload = {
        "objective": "Evaluate point-in-time CSI selector across twelve calendar-neutral cycle phases.",
        "eligibility": "A benchmark is eligible only after a domestic non-QDII passive ETF tracking it has listed; broad index fallback is used when the eligible set is empty.",
        "execution_lag_days": EXECUTION_LAG_DAYS,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fields = ["policy", "count", "min_final_capital_wan", "median_final_capital_wan", "worst_max_drawdown", "min_annualized_return", "min_excess_final_capital_wan"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow({"policy": item["policy"]["name"], **item["summary"]})
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

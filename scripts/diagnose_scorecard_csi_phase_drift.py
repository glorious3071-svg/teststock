#!/usr/bin/env python3
"""Explain allocation errors on representative calendar-neutral phase schedules."""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_midyear_risk import CASH_ANNUAL_RATE
from scripts.validate_scorecard_csi_generalization import (
    SCHEDULE_12M_3M,
    SCHEDULE_12M_12M,
    run_phase_schedule,
)

OUT_DIR = ROOT / "data" / "backtests" / "phase_drift_diagnosis"
OUT_JSON = OUT_DIR / "report.json"
OUT_CSV = OUT_DIR / "windows.csv"
PHASES = [0, 4, 5]
EXECUTION_LAG_DAYS = 3


def enrich(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in result["rows"]:
        start_exec = date.fromisoformat(row["start_exec"])
        end_exec = date.fromisoformat(row["end_exec"])
        holding_days = max((end_exec - start_exec).days, 0)
        cash_return = CASH_ANNUAL_RATE * holding_days / 365.25
        equity_return = float(row["mean_equity_return"])
        equity_weight = float(row["start_equity_pct"]) / 100.0
        excess_equity_return = equity_return - cash_return
        rows.append(
            {
                "schedule": result["schedule"]["name"],
                "phase_month_offset": result["phase_month_offset"],
                "execution_lag_days": result["execution_lag_days"],
                "cycle_index": row["cycle_index"],
                "review_index": row["review_index"],
                "start_snapshot_date": row["start_snapshot_date"],
                "end_snapshot_date": row["end_snapshot_date"],
                "start_exec": row["start_exec"],
                "end_exec": row["end_exec"],
                "selection_key": row["selection_key"],
                "score": row["score"],
                "target_equity_pct": row["equity_pct"],
                "start_equity_pct": row["start_equity_pct"],
                "equity_return": equity_return,
                "cash_return": cash_return,
                "portfolio_return": row["portfolio_return"],
                "log_wealth_contribution": math.log1p(row["portfolio_return"]),
                "missed_upside_vs_full_equity": max(excess_equity_return, 0.0) * (1.0 - equity_weight),
                "downside_capture_vs_cash": min(excess_equity_return, 0.0) * equity_weight,
                "portfolio_drawdown": row["portfolio_drawdown"],
                "capital": row["capital"],
                "rebalance_reasons": row["rebalance_reasons"],
                "known_inputs": row["known_inputs"],
                "top_score_items": row["top_score_items"],
            }
        )
    return rows


def slim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule": row["schedule"],
        "phase_month_offset": row["phase_month_offset"],
        "cycle_index": row["cycle_index"],
        "review_index": row["review_index"],
        "window": f"{row['start_exec']}..{row['end_exec']}",
        "score": row["score"],
        "start_equity_pct": row["start_equity_pct"],
        "equity_return": row["equity_return"],
        "portfolio_return": row["portfolio_return"],
        "portfolio_drawdown": row["portfolio_drawdown"],
        "missed_upside_vs_full_equity": row["missed_upside_vs_full_equity"],
        "downside_capture_vs_cash": row["downside_capture_vs_cash"],
        "rebalance_reasons": row["rebalance_reasons"],
        "known_inputs": row["known_inputs"],
        "top_score_items": row["top_score_items"],
    }


def main() -> int:
    results = []
    all_rows = []
    for spec in [SCHEDULE_12M_12M, SCHEDULE_12M_3M]:
        for phase in PHASES:
            result = run_phase_schedule(
                spec,
                phase,
                EXECUTION_LAG_DAYS,
                include_rows=True,
            )
            rows = enrich(result)
            all_rows.extend(rows)
            results.append(
                {
                    "schedule": spec.name,
                    "phase_month_offset": phase,
                    "sample_start": result["sample_start"],
                    "sample_end": result["sample_end"],
                    "final_capital_wan": result["final_capital_wan"],
                    "annualized_return": result["annualized_return"],
                    "max_drawdown": result["max_drawdown"],
                    "target_met": result["target_met"],
                    "top_missed_upside": [
                        slim(row)
                        for row in sorted(rows, key=lambda item: item["missed_upside_vs_full_equity"], reverse=True)[:5]
                    ],
                    "top_downside_capture": [
                        slim(row)
                        for row in sorted(rows, key=lambda item: item["downside_capture_vs_cash"])[:5]
                    ],
                    "largest_portfolio_losses": [
                        slim(row)
                        for row in sorted(rows, key=lambda item: item["portfolio_return"])[:5]
                    ],
                }
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "framework": {
                    "time_fields": [
                        "cycle_months",
                        "review_interval_months",
                        "phase_month_offset",
                        "execution_lag_days",
                    ],
                    "representative_phases": PHASES,
                    "execution_lag_days": EXECUTION_LAG_DAYS,
                    "basket_policy": "frozen_saved_selection_per_12m_cycle",
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [key for key in all_rows[0] if key not in {"known_inputs", "top_score_items", "rebalance_reasons"}]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    for result in results:
        print(
            f"{result['schedule']} phase={result['phase_month_offset']} "
            f"final={result['final_capital_wan']:.1f}w "
            f"mdd={result['max_drawdown'] * 100:.1f}%"
        )
        for label in ["top_missed_upside", "top_downside_capture"]:
            row = result[label][0]
            print(
                f"  {label}: {row['window']} score={row['score']} "
                f"equity={row['start_equity_pct']:.1f}% equity_ret={row['equity_return'] * 100:.1f}% "
                f"portfolio_ret={row['portfolio_return'] * 100:.1f}%"
            )
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

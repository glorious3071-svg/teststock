#!/usr/bin/env python3
"""Audit whether missing index fundamentals weaken the direct-ETF selector.

The V9 selector assigns a neutral cross-sectional rank to missing values.  This
script reconstructs every point-in-time V9 selection, groups the selected ETFs
by fundamental-data coverage, and compares each incomplete selection with the
best fully covered candidate available in the same snapshot.  Forward labels
are used only after selection for diagnosis.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.passive_etf_supervised_selector import (  # noqa: E402
    CONSTITUENT_V4_DATASET,
    weighted_stable_combo_v9_scores,
)


DEFAULT_OUTPUT = (
    ROOT / "data/backtests/direct_etf_v9_fundamental_coverage_audit_report.json"
)
FUNDAMENTAL_FIELDS = (
    "index_fundamental_roe_proxy",
    "index_fundamental_book_growth_12m",
    "index_constituent_earnings_yield",
    "index_constituent_weight_hhi",
)


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def coverage_count(row: dict[str, Any]) -> int:
    return sum(finite_number(row.get(field)) for field in FUNDAMENTAL_FIELDS)


def coverage_group(count: int) -> str:
    if count == 0:
        return "none"
    if count == len(FUNDAMENTAL_FIELDS):
        return "complete"
    return "partial"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [
        float(row["forward_return_3m"])
        for row in rows
        if finite_number(row.get("forward_return_3m"))
    ]
    drawdowns = [
        float(row["forward_max_drawdown_3m"])
        for row in rows
        if finite_number(row.get("forward_max_drawdown_3m"))
    ]
    return {
        "count": len(rows),
        "return_count": len(returns),
        "mean_forward_return_3m": statistics.mean(returns) if returns else None,
        "median_forward_return_3m": statistics.median(returns) if returns else None,
        "loss_rate": (
            sum(value < 0.0 for value in returns) / len(returns) if returns else None
        ),
        "loss_10pct_rate": (
            sum(value <= -0.10 for value in returns) / len(returns)
            if returns
            else None
        ),
        "mean_forward_max_drawdown_3m": (
            statistics.mean(drawdowns) if drawdowns else None
        ),
        "drawdown_15pct_rate": (
            sum(value <= -0.15 for value in drawdowns) / len(drawdowns)
            if drawdowns
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=CONSTITUENT_V4_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    dataset = args.dataset if args.dataset.is_absolute() else ROOT / args.dataset
    output = args.output if args.output.is_absolute() else ROOT / args.output
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    observations = list(payload["candidate_observations"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[str(row["snapshot"])].append(row)

    selections: list[dict[str, Any]] = []
    matched_complete_alternatives: list[dict[str, Any]] = []
    for snapshot, rows in sorted(grouped.items()):
        snapshot_day = __import__("datetime").date.fromisoformat(snapshot)
        scores = weighted_stable_combo_v9_scores(observations, snapshot_day)
        if not scores:
            continue
        by_code = {str(row["ts_code"]): row for row in rows}
        selected_code = max(
            scores, key=lambda code: (round(float(scores[code]), 12), code)
        )
        selected = by_code[selected_code]
        count = coverage_count(selected)
        record = {
            "snapshot": snapshot,
            "end_snapshot": selected.get("end_snapshot"),
            "era": selected.get("era"),
            "market_regime": selected.get("market_regime"),
            "ts_code": selected_code,
            "index_code": selected.get("index_code"),
            "selector_score": float(scores[selected_code]),
            "fundamental_coverage_count": count,
            "fundamental_coverage_group": coverage_group(count),
            "forward_return_3m": selected.get("forward_return_3m"),
            "forward_max_drawdown_3m": selected.get("forward_max_drawdown_3m"),
            "momentum_12m": selected.get("momentum_12m"),
            "momentum_12m_skip1m": selected.get("momentum_12m_skip1m"),
            "max_drawdown_6m": selected.get("max_drawdown_6m"),
        }
        selections.append(record)

        complete_codes = [
            code
            for code, row in by_code.items()
            if coverage_count(row) == len(FUNDAMENTAL_FIELDS) and code in scores
        ]
        if count == len(FUNDAMENTAL_FIELDS) or not complete_codes:
            continue
        alternative_code = max(
            complete_codes,
            key=lambda code: (round(float(scores[code]), 12), code),
        )
        alternative = by_code[alternative_code]
        selected_return = selected.get("forward_return_3m")
        alternative_return = alternative.get("forward_return_3m")
        selected_drawdown = selected.get("forward_max_drawdown_3m")
        alternative_drawdown = alternative.get("forward_max_drawdown_3m")
        matched_complete_alternatives.append(
            {
                "snapshot": snapshot,
                "era": selected.get("era"),
                "selected_code": selected_code,
                "selected_coverage_count": count,
                "selected_score": float(scores[selected_code]),
                "selected_forward_return_3m": selected_return,
                "selected_forward_max_drawdown_3m": selected_drawdown,
                "complete_alternative_code": alternative_code,
                "complete_alternative_score": float(scores[alternative_code]),
                "complete_alternative_forward_return_3m": alternative_return,
                "complete_alternative_forward_max_drawdown_3m": alternative_drawdown,
                "complete_minus_selected_return_3m": (
                    float(alternative_return) - float(selected_return)
                    if finite_number(alternative_return) and finite_number(selected_return)
                    else None
                ),
                "complete_minus_selected_max_drawdown_3m": (
                    float(alternative_drawdown) - float(selected_drawdown)
                    if finite_number(alternative_drawdown) and finite_number(selected_drawdown)
                    else None
                ),
            }
        )

    by_coverage = {
        group: summarize(
            [row for row in selections if row["fundamental_coverage_group"] == group]
        )
        for group in ("none", "partial", "complete")
    }
    eras = sorted({str(row["era"]) for row in selections})
    by_era_and_coverage = {
        era: {
            group: summarize(
                [
                    row
                    for row in selections
                    if str(row["era"]) == era
                    and row["fundamental_coverage_group"] == group
                ]
            )
            for group in ("none", "partial", "complete")
        }
        for era in eras
    }
    matched_deltas = [
        float(row["complete_minus_selected_return_3m"])
        for row in matched_complete_alternatives
        if finite_number(row.get("complete_minus_selected_return_3m"))
    ]
    matched_dd_deltas = [
        float(row["complete_minus_selected_max_drawdown_3m"])
        for row in matched_complete_alternatives
        if finite_number(row.get("complete_minus_selected_max_drawdown_3m"))
    ]
    severe_incomplete = sorted(
        (
            row
            for row in selections
            if row["fundamental_coverage_group"] != "complete"
            and finite_number(row.get("forward_return_3m"))
            and float(row["forward_return_3m"]) <= -0.10
        ),
        key=lambda row: float(row["forward_return_3m"]),
    )
    report = {
        "dataset": str(dataset),
        "method": "point_in_time_v9_selection_then_forward_3m_diagnosis",
        "fundamental_fields": list(FUNDAMENTAL_FIELDS),
        "snapshot_count": len(selections),
        "by_coverage": by_coverage,
        "by_era_and_coverage": by_era_and_coverage,
        "matched_complete_alternative": {
            "count": len(matched_complete_alternatives),
            "mean_complete_minus_selected_return_3m": (
                statistics.mean(matched_deltas) if matched_deltas else None
            ),
            "median_complete_minus_selected_return_3m": (
                statistics.median(matched_deltas) if matched_deltas else None
            ),
            "complete_return_win_rate": (
                sum(value > 0.0 for value in matched_deltas) / len(matched_deltas)
                if matched_deltas
                else None
            ),
            "mean_complete_minus_selected_max_drawdown_3m": (
                statistics.mean(matched_dd_deltas) if matched_dd_deltas else None
            ),
        },
        "severe_incomplete_selections": severe_incomplete,
        "matched_complete_alternatives": matched_complete_alternatives,
        "selections": selections,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in (
        "snapshot_count", "by_coverage", "by_era_and_coverage",
        "matched_complete_alternative", "severe_incomplete_selections",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

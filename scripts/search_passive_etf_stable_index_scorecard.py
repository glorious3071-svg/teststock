#!/usr/bin/env python3
"""Screen a low-parameter stable index+ETF scorecard.

The feature directions are admitted only when their cross-sectional return IC
has the same sign in all three historical eras.  The search varies only the
weight assigned to the four index crowding/acceleration signals, basket size,
and score concentration.  It is a research screen; successful policies must
still pass the actual-ETF 48-path daily backtest.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.phase_schedule import shift_month_end

DEFAULT_DATASET = ROOT / "data/backtests/passive_etf_quarterly_enriched_v2_dataset.json"
DEFAULT_OUTPUT = ROOT / "data/backtests/passive_etf_stable_index_scorecard_report.json"

PRICE_COMPONENTS = (
    ("market_beta_6m", False, 4.0 / 13.0),
    ("distance_high_12m", True, 1.0 / 13.0),
    ("return_autocorrelation_3m", False, 4.0 / 13.0),
    ("volatility_3m", False, 4.0 / 13.0),
)
INDEX_COMPONENTS = (
    ("index_turnover_acceleration_1m_6m", False, 0.25),
    ("index_trend_acceleration_3m_vs_6m", False, 0.25),
    ("index_etf_positive_turnover_pressure_1m", False, 0.25),
    ("index_etf_amount_crowding_percentile_3y", False, 0.25),
)


@dataclass(frozen=True)
class ScorecardPolicy:
    name: str
    index_weight: float
    top_n: int
    score_power: float


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def ranks(rows: list[dict[str, Any]], feature: str, higher: bool) -> dict[str, float]:
    usable = sorted(
        (
            (str(row["ts_code"]), float(row[feature]))
            for row in rows
            if finite(row.get(feature))
        ),
        key=lambda item: (item[1], item[0]),
    )
    if len(usable) <= 1:
        return {str(row["ts_code"]): 0.5 for row in rows}
    denominator = len(usable) - 1
    values = {code: index / denominator for index, (code, _value) in enumerate(usable)}
    return {
        str(row["ts_code"]): (
            values.get(str(row["ts_code"]), 0.5)
            if higher
            else 1.0 - values.get(str(row["ts_code"]), 0.5)
        )
        for row in rows
    }


def group_score(
    rows: list[dict[str, Any]],
    components: tuple[tuple[str, bool, float], ...],
) -> dict[str, float]:
    component_ranks = [
        (ranks(rows, feature, higher), weight)
        for feature, higher, weight in components
    ]
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])]
            for values, weight in component_ranks
        )
        for row in rows
    }


def select(rows: list[dict[str, Any]], policy: ScorecardPolicy) -> dict[str, float]:
    price = group_score(rows, PRICE_COMPONENTS)
    index = group_score(rows, INDEX_COMPONENTS)
    scores = {
        code: (1.0 - policy.index_weight) * price[code] + policy.index_weight * index[code]
        for code in price
    }
    selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
    powered = {code: max(scores[code], 0.01) ** policy.score_power for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()}


def path_summary(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    by_snapshot = {str(row["snapshot"]): row for row in predictions}
    strict_anchor = date(2005, 2, 28)
    cases = []
    for phase in range(12):
        first = shift_month_end(strict_anchor, phase)
        anchors = [shift_month_end(first, quarter * 3) for quarter in range(80)]
        rows = [
            by_snapshot[anchor.isoformat()]
            for anchor in anchors
            if anchor.isoformat() in by_snapshot
        ]
        missing = [
            anchor.isoformat()
            for anchor in anchors
            if anchor.isoformat() not in by_snapshot
        ]
        factor = math.prod(1.0 + float(row["return"]) for row in rows)
        cases.append(
            {
                "phase_month_offset": phase,
                "start_snapshot": first.isoformat(),
                "end_snapshot": shift_month_end(first, 240).isoformat(),
                "anchor_count": len(anchors),
                "observed_period_count": len(rows),
                "missing_snapshot_count": len(missing),
                "missing_snapshots": missing,
                "capital_factor": factor,
                "worst_constituent_drawdown": min(
                    (float(row["drawdown"]) for row in rows), default=0.0
                ),
            }
        )
    complete = [case for case in cases if case["missing_snapshot_count"] == 0]
    return {
        "case_count": len(cases),
        "complete_20y_case_count": len(complete),
        "strict_anchor": strict_anchor.isoformat(),
        "min_capital_factor": min(case["capital_factor"] for case in cases),
        "median_capital_factor": statistics.median(
            case["capital_factor"] for case in cases
        ),
        "worst_constituent_drawdown": min(
            case["worst_constituent_drawdown"] for case in cases
        ),
        "cases": cases,
    }


def policies() -> list[ScorecardPolicy]:
    return [
        ScorecardPolicy(
            f"stable_index_w{int(index_weight * 100):02d}_top{top_n}_p{int(power)}",
            index_weight,
            top_n,
            power,
        )
        for index_weight in (0.0, 0.10, 0.20, 0.30, 0.40, 0.50)
        for top_n in (1, 3, 5)
        for power in (1.0, 2.0, 4.0)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    dataset = args.dataset if args.dataset.is_absolute() else ROOT / args.dataset
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload["candidate_observations"]:
        grouped[str(row["snapshot"])].append(row)

    results = []
    for policy in policies():
        predictions = []
        for snapshot, rows in sorted(grouped.items()):
            weights = select(rows, policy)
            by_code = {str(row["ts_code"]): row for row in rows}
            predictions.append(
                {
                    "snapshot": snapshot,
                    "codes": list(weights),
                    "return": sum(
                        weight * float(by_code[code]["forward_return_3m"])
                        for code, weight in weights.items()
                    ),
                    "drawdown": sum(
                        weight * float(by_code[code]["forward_max_drawdown_3m"])
                        for code, weight in weights.items()
                    ),
                }
            )
        results.append({"policy": asdict(policy), "summary": path_summary(predictions)})
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    "fixed cross-era stable feature directions; monthly anchors; "
                    "strict three-month label windows"
                ),
                "dataset": str(dataset),
                "price_components": PRICE_COMPONENTS,
                "index_components": INDEX_COMPONENTS,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:20]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<32} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"dd={summary['worst_constituent_drawdown'] * 100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

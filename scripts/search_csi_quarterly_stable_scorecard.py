#!/usr/bin/env python3
"""Search low-degree CSI scorecards across all quarterly calendar phases.

Feature directions are fixed by the three-era IC audit.  The search varies
only small integer weights, basket size, and score concentration.  A candidate
must beat the current expanded-value selector in every historical era before
it is eligible for the 48-path strict portfolio backtest.
"""

from __future__ import annotations

import argparse
import itertools
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

DEFAULT_DATASET = ROOT / "data/backtests/csi_quarterly_selector_dataset_v2.json"
DEFAULT_OUTPUT = ROOT / "data/backtests/csi_quarterly_stable_scorecard_search_report.json"
LAGS = (0, 1, 3, 5)
ERAS = ("2005_2012", "2013_2018", "2019_latest")


@dataclass(frozen=True)
class Component:
    feature: str
    weight: float
    higher_is_better: bool


@dataclass(frozen=True)
class Policy:
    name: str
    components: tuple[Component, ...]
    top_n: int
    score_power: float


BASELINE_COMPONENTS = (
    Component("momentum_6m", 0.05, True),
    Component("volatility_3m", 0.10, False),
    Component("drawdown_6m", 0.05, True),
    Component("trend_6m", 0.15, True),
    Component("positive_month_ratio_12m", 0.05, True),
    Component("calmar_12m", 0.10, True),
    Component("pe_ttm_history_percentile_3y", 0.15, True),
    Component("pb_history_percentile_3y", 0.20, True),
    Component("turnover_crowding_percentile_3y", 0.10, True),
    Component("turnover_acceleration_1m_6m", 0.05, False),
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def rank(rows: list[dict[str, Any]], component: Component) -> dict[str, float]:
    usable = sorted(
        (
            (str(row["ts_code"]), float(row[component.feature]))
            for row in rows
            if finite(row.get(component.feature))
        ),
        key=lambda item: (item[1], item[0]),
    )
    if len(usable) <= 1:
        return {str(row["ts_code"]): 0.5 for row in rows}
    denominator = len(usable) - 1
    raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
    return {
        str(row["ts_code"]): (
            raw.get(str(row["ts_code"]), 0.5)
            if component.higher_is_better
            else 1.0 - raw.get(str(row["ts_code"]), 0.5)
        )
        for row in rows
    }


def select(rows: list[dict[str, Any]], policy: Policy) -> dict[str, float]:
    ranks = [(rank(rows, item), item.weight) for item in policy.components]
    total_component_weight = sum(item.weight for item in policy.components)
    scores = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in ranks
        )
        / total_component_weight
        for row in rows
    }
    selected = sorted(
        scores,
        key=lambda code: (scores[code], code),
        reverse=True,
    )[: policy.top_n]
    powered = {code: max(scores[code], 0.01) ** policy.score_power for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()}


def predictions(
    grouped: dict[str, list[dict[str, Any]]],
    policy: Policy,
) -> list[dict[str, Any]]:
    output = []
    for snapshot, rows in sorted(grouped.items()):
        weights = select(rows, policy)
        by_code = {str(row["ts_code"]): row for row in rows}
        output.append(
            {
                "snapshot": snapshot,
                "era": str(rows[0]["era"]),
                **{
                    f"return_lag{lag}": sum(
                        weight * float(by_code[code][f"forward_return_3m_lag{lag}"])
                        for code, weight in weights.items()
                    )
                    for lag in LAGS
                },
            }
        )
    return output


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_snapshot = {str(row["snapshot"]): row for row in rows}
    strict_anchor = date(2005, 2, 28)
    cases = []
    for phase in range(12):
        start = shift_month_end(strict_anchor, phase)
        anchors = [shift_month_end(start, quarter * 3) for quarter in range(80)]
        selected = [
            by_snapshot[anchor.isoformat()]
            for anchor in anchors
            if anchor.isoformat() in by_snapshot
        ]
        missing = [
            anchor.isoformat()
            for anchor in anchors
            if anchor.isoformat() not in by_snapshot
        ]
        for lag in LAGS:
            factor = math.prod(1.0 + float(row[f"return_lag{lag}"]) for row in selected)
            cases.append(
                {
                    "phase": phase,
                    "lag": lag,
                    "start_snapshot": start.isoformat(),
                    "end_snapshot": shift_month_end(start, 240).isoformat(),
                    "anchor_count": len(anchors),
                    "observed_period_count": len(selected),
                    "missing_snapshot_count": len(missing),
                    "missing_snapshots": missing,
                    "capital_factor": factor,
                }
            )
    strict_snapshots = {
        shift_month_end(shift_month_end(strict_anchor, phase), quarter * 3).isoformat()
        for phase in range(12)
        for quarter in range(80)
    }
    era_log_returns = {
        era: statistics.mean(
            math.log1p(float(row[f"return_lag{lag}"]))
            for row in rows
            if row["era"] == era and str(row["snapshot"]) in strict_snapshots
            for lag in LAGS
        )
        for era in ERAS
    }
    return {
        "case_count": len(cases),
        "strict_anchor": strict_anchor.isoformat(),
        "min_capital_factor": min(case["capital_factor"] for case in cases),
        "median_capital_factor": statistics.median(
            case["capital_factor"] for case in cases
        ),
        "era_mean_log_return": era_log_returns,
        "cases": cases,
    }


def policies() -> list[Policy]:
    output = [Policy("expanded_value_risk_top7_power2", BASELINE_COMPONENTS, 7, 2.0)]
    for weights in itertools.product((1, 2, 3, 4), (1, 2, 3), (1, 2, 3), (1, 2)):
        acceleration, crowding, risk_adjusted, calmar = weights
        components = (
            Component("turnover_acceleration_1m_6m", acceleration, False),
            Component("turnover_crowding_percentile_3y", crowding, True),
            Component("risk_adjusted_momentum_12m", risk_adjusted, True),
            Component("calmar_12m", calmar, True),
        )
        for top_n in (3, 5, 7, 10):
            for power in (1.0, 2.0, 4.0):
                output.append(
                    Policy(
                        f"stable4_a{acceleration}c{crowding}r{risk_adjusted}m{calmar}"
                        f"_top{top_n}_p{int(power)}",
                        components,
                        top_n,
                        power,
                    )
                )
    return output


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

    baseline_policy, *candidates = policies()
    baseline_summary = summarize(predictions(grouped, baseline_policy))
    baseline_era = baseline_summary["era_mean_log_return"]
    results = []
    for policy in candidates:
        summary = summarize(predictions(grouped, policy))
        era_edges = {
            era: summary["era_mean_log_return"][era] - baseline_era[era]
            for era in ERAS
        }
        results.append(
            {
                "policy": {
                    "name": policy.name,
                    "components": [asdict(item) for item in policy.components],
                    "top_n": policy.top_n,
                    "score_power": policy.score_power,
                },
                "summary": summary,
                "era_log_return_edges_vs_baseline": era_edges,
                "all_eras_improve": all(value > 0.0 for value in era_edges.values()),
            }
        )
    results.sort(
        key=lambda item: (
            item["all_eras_improve"],
            min(item["era_log_return_edges_vs_baseline"].values()),
            item["summary"]["min_capital_factor"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    "fixed three-era-stable feature directions; low integer-weight grid; "
                    "12 monthly phases x execution lags 0/1/3/5; 80 quarters per case"
                ),
                "dataset": str(dataset),
                "baseline_policy": {
                    "name": baseline_policy.name,
                    "components": [asdict(item) for item in baseline_policy.components],
                    "top_n": baseline_policy.top_n,
                    "score_power": baseline_policy.score_power,
                },
                "baseline_summary": baseline_summary,
                "candidate_count": len(results),
                "all_eras_improve_count": sum(item["all_eras_improve"] for item in results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"baseline min={baseline_summary['min_capital_factor']:.3f}x "
        f"median={baseline_summary['median_capital_factor']:.3f}x"
    )
    for item in results[:20]:
        summary = item["summary"]
        edges = item["era_log_return_edges_vs_baseline"]
        print(
            f"{item['policy']['name']:<38} all_eras={item['all_eras_improve']} "
            f"min={summary['min_capital_factor']:7.3f}x "
            f"median={summary['median_capital_factor']:7.3f}x "
            f"worst_era_edge={min(edges.values()) * 100:+.3f}%/q"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Walk-forward nonlinear binned selector for domestic passive ETFs."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.search_passive_etf_walkforward_ridge import (
    DATASET,
    FEATURE_SETS,
    cross_section_matrix,
    fallback_scores,
    path_summary,
    ranked_target,
)


OUTPUT = ROOT / "data/backtests/passive_etf_walkforward_binned_screen_report.json"


@dataclass(frozen=True)
class BinnedPolicy:
    name: str
    feature_set: str
    history_periods: int
    bins: int
    top_n: int
    drawdown_penalty: float
    shrink_count: float


def prepare(
    grouped: list[tuple[date, list[dict[str, Any]]]],
    feature_set: str,
) -> list[dict[str, Any]]:
    features = FEATURE_SETS[feature_set]
    return [
        {
            "snapshot": snapshot,
            "end_snapshot": date.fromisoformat(str(rows[0]["end_snapshot"])),
            "rows": rows,
            "matrix": cross_section_matrix(rows, features),
        }
        for snapshot, rows in grouped
    ]


def binned_scores(
    history: list[dict[str, Any]],
    current_matrix: np.ndarray,
    policy: BinnedPolicy,
) -> np.ndarray | None:
    if len(history) < min(12, policy.history_periods):
        return None
    matrices = []
    targets = []
    for item in history[-policy.history_periods :]:
        matrix = item["matrix"]
        target = ranked_target(item["rows"], policy.drawdown_penalty)
        matrices.append(matrix)
        targets.append(target)
    x = np.vstack(matrices)
    y = np.concatenate(targets)
    predictions = np.zeros(current_matrix.shape[0], dtype=float)
    edges = np.linspace(-1.000001, 1.000001, policy.bins + 1)
    for feature_index in range(x.shape[1]):
        historical_bins = np.clip(
            np.digitize(x[:, feature_index], edges[1:-1]),
            0,
            policy.bins - 1,
        )
        current_bins = np.clip(
            np.digitize(current_matrix[:, feature_index], edges[1:-1]),
            0,
            policy.bins - 1,
        )
        means = np.zeros(policy.bins, dtype=float)
        strengths = np.zeros(policy.bins, dtype=float)
        for bucket in range(policy.bins):
            values = y[historical_bins == bucket]
            if len(values):
                means[bucket] = float(np.mean(values))
                strengths[bucket] = len(values) / (len(values) + policy.shrink_count)
        centered = means - float(np.mean(means))
        feature_strength = float(np.std(centered))
        if feature_strength <= 1e-9:
            continue
        predictions += centered[current_bins] * strengths[current_bins] * feature_strength
    return predictions


def evaluate(prepared: list[dict[str, Any]], policy: BinnedPolicy) -> dict[str, Any]:
    predictions = []
    online_count = 0
    for current in prepared:
        history = [
            item for item in prepared if item["end_snapshot"] <= current["snapshot"]
        ]
        scores = binned_scores(history, current["matrix"], policy)
        if scores is None:
            scores = fallback_scores(current["rows"])
            mode = "fallback"
        else:
            mode = "binned"
            online_count += 1
        order = sorted(
            range(len(current["rows"])),
            key=lambda index: (
                -float(scores[index]),
                str(current["rows"][index]["ts_code"]),
            ),
        )[: policy.top_n]
        chosen = np.asarray([float(scores[index]) for index in order])
        shifted = np.maximum(chosen - min(float(np.min(chosen)), 0.0) + 0.10, 0.01)
        weights = shifted / shifted.sum()
        predictions.append(
            {
                "snapshot": current["snapshot"].isoformat(),
                "mode": mode,
                "basket_return": sum(
                    float(weight)
                    * float(current["rows"][index]["forward_return_3m"])
                    for weight, index in zip(weights, order)
                ),
                "basket_average_drawdown": sum(
                    float(weight)
                    * float(current["rows"][index]["forward_max_drawdown_3m"])
                    for weight, index in zip(weights, order)
                ),
            }
        )
    summary = path_summary(predictions)
    summary["online_snapshot_count"] = online_count
    return {"policy": asdict(policy), "summary": summary}


def policies(feature_sets: list[str]) -> list[BinnedPolicy]:
    output = []
    for feature_set in feature_sets:
        for history in (60, 120):
            for bins in (3, 5):
                for top_n in (1, 3):
                    for penalty in (0.0, 1.0, 2.0):
                        for shrink in (10.0, 50.0):
                            output.append(
                                BinnedPolicy(
                                    f"binned_{feature_set}_h{history}_b{bins}_top{top_n}_dd{penalty:g}_s{int(shrink)}",
                                    feature_set,
                                    history,
                                    bins,
                                    top_n,
                                    penalty,
                                    shrink,
                                )
                            )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--feature-set", action="append", choices=sorted(FEATURE_SETS))
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    grouped_map: dict[date, list[dict[str, Any]]] = {}
    for row in payload["candidate_observations"]:
        grouped_map.setdefault(date.fromisoformat(str(row["snapshot"])), []).append(row)
    grouped = sorted(grouped_map.items())
    feature_sets = args.feature_set or list(FEATURE_SETS)
    prepared = {name: prepare(grouped, name) for name in feature_sets}
    results = [
        evaluate(prepared[policy.feature_set], policy)
        for policy in policies(feature_sets)
    ]
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
                "method": "strict label release, point-in-time cross-sectional bins",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<57} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

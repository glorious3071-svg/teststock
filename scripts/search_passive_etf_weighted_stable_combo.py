#!/usr/bin/env python3
"""Search weighted versions of the cross-era-stable passive ETF scorecard."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.search_passive_etf_stable_feature_combinations import prepare
from scripts.search_passive_etf_walkforward_ridge import path_summary


OUTPUT = ROOT / "data/backtests/passive_etf_weighted_stable_combo_report.json"
DATASET = ROOT / "data/backtests/passive_etf_quarterly_fundamental_v3_dataset.json"
FEATURES = (
    "market_beta_6m",
    "distance_high_12m",
    "return_autocorrelation_3m",
    "volatility_3m",
    "ulcer_index_6m",
    "index_fundamental_roe_proxy",
)


def evaluate(prepared, raw_weights, top_n: int, score_power: float):
    weights = np.asarray(raw_weights, dtype=float)
    weights /= weights.sum()
    predictions = []
    for item in prepared:
        score = sum(
            weight * item["ranks"][feature]
            for weight, feature in zip(weights, FEATURES)
        )
        selected = sorted(
            range(len(item["rows"])),
            key=lambda index: (-float(score[index]), str(item["rows"][index]["ts_code"])),
        )[:top_n]
        chosen = np.asarray(
            [max(float(score[index]), 0.01) ** score_power for index in selected]
        )
        selected_weights = chosen / chosen.sum()
        predictions.append(
            {
                "snapshot": item["snapshot"].isoformat(),
                "basket_return": sum(
                    float(weight) * float(item["rows"][index]["forward_return_3m"])
                    for weight, index in zip(selected_weights, selected)
                ),
                "basket_average_drawdown": sum(
                    float(weight) * float(item["rows"][index]["forward_max_drawdown_3m"])
                    for weight, index in zip(selected_weights, selected)
                ),
            }
        )
    return {
        "features": list(FEATURES),
        "feature_weights": [float(value) for value in weights],
        "top_n": top_n,
        "score_power": score_power,
        "summary": path_summary(predictions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    dataset = args.dataset if args.dataset.is_absolute() else ROOT / args.dataset
    output = args.output if args.output.is_absolute() else ROOT / args.output
    prepared = prepare(dataset)
    results = [
        evaluate(prepared, weights, top_n, power)
        for weights in itertools.product((0.25, 0.50, 1.0, 2.0), repeat=len(FEATURES))
        for top_n in (1, 3, 5)
        for power in (1.0, 2.0, 4.0)
    ]
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
        ),
        reverse=True,
    )
    output.write_text(
        json.dumps(
            {
                "method": "weighted cross-era-stable point-in-time rank scorecard",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:40]:
        summary = item["summary"]
        print(
            f"top{item['top_n']} p{item['score_power']:g} w={item['feature_weights']} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

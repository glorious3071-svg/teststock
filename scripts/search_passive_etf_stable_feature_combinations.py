#!/usr/bin/env python3
"""Search simple cross-era-stable passive ETF feature combinations."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.monthly_online_selector import _average_ranks
from scripts.search_passive_etf_walkforward_ridge import DATASET, path_summary


OUTPUT = ROOT / "data/backtests/passive_etf_stable_feature_combinations_report.json"
EXPANDED_DATASET = ROOT / "data/backtests/passive_etf_quarterly_constituent_v4_dataset.json"
FEATURE_DIRECTIONS = {
    "market_beta_6m": -1.0,
    "volatility_1m": -1.0,
    "distance_high_12m": 1.0,
    "log_amount_1m": -1.0,
    "ulcer_index_6m": -1.0,
    "return_autocorrelation_3m": -1.0,
    "volatility_3m": -1.0,
    "historical_cvar_5pct_3m": 1.0,
    "max_drawdown_6m": 1.0,
    "listing_age_years": 1.0,
    "drawdown_3m": 1.0,
    # Quarterly ETF cross-sections show a consistent three-era reversal effect:
    # higher trailing 12-month momentum predicts lower next-quarter return.
    "momentum_12m_skip1m": -1.0,
    "momentum_12m": -1.0,
    "index_fundamental_roe_proxy": -1.0,
}

EXPANDED_FEATURE_DIRECTIONS = {
    **FEATURE_DIRECTIONS,
    # Point-in-time index fundamentals with direction agreement in all three
    # historical eras.  Constituent-snapshot features are intentionally
    # excluded here because their qualified history starts only in 2019.
    "index_fundamental_roe_proxy": -1.0,
    "index_fundamental_book_growth_6m": -1.0,
    "index_fundamental_pb_change_6m": 1.0,
    "index_fundamental_book_yield": 1.0,
    "index_fundamental_pb_change_3m": 1.0,
    "index_fundamental_earnings_growth_12m": -1.0,
    "index_fundamental_book_growth_12m": -1.0,
    "index_fundamental_pe_change_6m": 1.0,
}


def aligned_rank(
    rows: list[dict], feature: str, directions: dict[str, float]
) -> np.ndarray:
    usable = [
        (index, float(row[feature]))
        for index, row in enumerate(rows)
        if isinstance(row.get(feature), (int, float))
        and math.isfinite(float(row[feature]))
    ]
    raw = np.full(len(rows), 0.5, dtype=float)
    if len(usable) >= 2:
        ranks = _average_ranks([value for _index, value in usable])
        denominator = len(usable) - 1
        for (index, _value), rank in zip(usable, ranks):
            raw[index] = rank / denominator
    return raw if directions[feature] > 0 else 1.0 - raw


def prepare(
    dataset: Path = DATASET,
    directions: dict[str, float] = FEATURE_DIRECTIONS,
) -> list[dict]:
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    grouped: dict[date, list[dict]] = defaultdict(list)
    for row in payload["candidate_observations"]:
        if row.get("forward_return_3m") is None or row.get("forward_max_drawdown_3m") is None:
            continue
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)
    output = []
    for snapshot, rows in sorted(grouped.items()):
        output.append(
            {
                "snapshot": snapshot,
                "rows": rows,
                "ranks": {
                    feature: aligned_rank(rows, feature, directions)
                    for feature in directions
                },
            }
        )
    return output


def evaluate(prepared: list[dict], features: tuple[str, ...], top_n: int) -> dict:
    predictions = []
    for item in prepared:
        scores = sum((item["ranks"][feature] for feature in features), np.zeros(len(item["rows"])))
        scores /= len(features)
        selected = sorted(
            range(len(item["rows"])),
            key=lambda index: (-float(scores[index]), str(item["rows"][index]["ts_code"])),
        )[:top_n]
        chosen = np.asarray([max(float(scores[index]), 0.01) ** 2.0 for index in selected])
        weights = chosen / chosen.sum()
        predictions.append(
            {
                "snapshot": item["snapshot"].isoformat(),
                "basket_return": sum(
                    float(weight) * float(item["rows"][index]["forward_return_3m"])
                    for weight, index in zip(weights, selected)
                ),
                "basket_average_drawdown": sum(
                    float(weight) * float(item["rows"][index]["forward_max_drawdown_3m"])
                    for weight, index in zip(weights, selected)
                ),
            }
        )
    return {
        "features": list(features),
        "top_n": top_n,
        "summary": path_summary(predictions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--feature-family",
        choices=("base", "expanded_v4"),
        default="base",
    )
    parser.add_argument("--min-size", type=int, default=2)
    parser.add_argument("--max-size", type=int, default=5)
    args = parser.parse_args()
    directions = (
        EXPANDED_FEATURE_DIRECTIONS
        if args.feature_family == "expanded_v4"
        else FEATURE_DIRECTIONS
    )
    prepared = prepare(args.dataset, directions)
    results = []
    names = tuple(directions)
    for size in range(args.min_size, args.max_size + 1):
        for features in itertools.combinations(names, size):
            for top_n in (1, 3, 5):
                results.append(evaluate(prepared, features, top_n))
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
            item["summary"]["worst_average_constituent_drawdown"],
        ),
        reverse=True,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": "equal-weight combinations of cross-era-stable point-in-time ranks",
                "feature_family": args.feature_family,
                "dataset": str(args.dataset),
                "feature_directions": directions,
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
            f"top{item['top_n']} {','.join(item['features']):<110} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Add point-in-time tracked-index features to passive ETF candidate labels."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SNAPSHOT_CSI_SELECTOR
from db.connection import get_connection


SOURCE = ROOT / "data/backtests/passive_etf_quarterly_supervised_dataset.json"
OUTPUT = ROOT / "data/backtests/passive_etf_quarterly_enriched_dataset.json"

INDEX_FEATURES = (
    "annual_score",
    "policy_score",
    "news_score",
    "pe_ttm_history_percentile_3y",
    "pb_history_percentile_3y",
    "turnover_crowding_percentile_3y",
    "turnover_acceleration_1m_6m",
    "etf_amount_acceleration_1m_6m",
    "etf_amount_crowding_percentile_3y",
    "etf_positive_turnover_pressure_1m",
    "positive_month_ratio_12m",
    "risk_adjusted_momentum_12m",
    "risk_adjusted_trend_6m",
    "stable_trend_6m",
    "trend_12m",
    "trend_6m",
    "trend_acceleration_3m_vs_6m",
    "trend_acceleration_geometric_3m_vs_6m",
    "trend_consistency_3m_6m",
    "calmar_12m",
    "drawdown_12m",
    "fundamental_earnings_yield",
    "fundamental_book_yield",
    "fundamental_roe_proxy",
    "fundamental_earnings_growth_3m",
    "fundamental_earnings_growth_6m",
    "fundamental_earnings_growth_12m",
    "fundamental_book_growth_6m",
    "fundamental_book_growth_12m",
    "fundamental_pe_change_3m",
    "fundamental_pe_change_6m",
    "fundamental_pb_change_3m",
    "fundamental_pb_change_6m",
    "constituent_earnings_yield",
    "constituent_book_yield",
    "constituent_roe_proxy",
    "constituent_dividend_yield",
    "constituent_positive_earnings_weight",
    "constituent_weight_hhi",
    "constituent_earnings_yield_change_12m",
    "constituent_roe_change_12m",
    "constituent_dividend_yield_change_12m",
    "constituent_positive_earnings_change_12m",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    rows = list(payload["candidate_observations"])
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)

    matched = 0
    total = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for snapshot, current in sorted(grouped.items()):
                index_rows = {
                    str(row["ts_code"]): row
                    for row in SNAPSHOT_CSI_SELECTOR.candidate_rows(cur, snapshot)
                }
                for row in current:
                    source = index_rows.get(str(row["index_code"]))
                    total += 1
                    if source is not None:
                        matched += 1
                    for feature in INDEX_FEATURES:
                        raw = source.get(feature) if source is not None else None
                        row[f"index_{feature}"] = (
                            float(raw)
                            if isinstance(raw, (int, float))
                            and not isinstance(raw, bool)
                            and math.isfinite(float(raw))
                            else None
                        )
    finally:
        conn.close()

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                **{key: value for key, value in payload.items() if key != "candidate_observations"},
                "index_features": [f"index_{feature}" for feature in INDEX_FEATURES],
                "index_feature_match_count": matched,
                "index_feature_match_rate": matched / total if total else 0.0,
                "candidate_observations": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote {output}; candidates={total} matched={matched} "
        f"rate={matched / total if total else 0.0:.1%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

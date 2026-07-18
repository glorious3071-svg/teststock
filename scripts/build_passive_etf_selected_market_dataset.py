#!/usr/bin/env python3
"""Build selected-ETF quarterly labels with point-in-time market features."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.phase_features import PHASE_FEATURE_STORE
from db.connection import get_connection
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE
from scripts.search_passive_etf_absolute_exposure import selected_observations


SOURCE = ROOT / "data/backtests/passive_etf_quarterly_enriched_dataset.json"
OUTPUT = ROOT / "data/backtests/passive_etf_selected_market_dataset.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--selector-version", choices=("legacy", "v3", "v5", "v9"), default="legacy"
    )
    args = parser.parse_args()
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    candidates = list(payload["candidate_observations"])
    selected = selected_observations(candidates, args.selector_version)
    candidate_map = {
        (str(row["snapshot"]), str(row["ts_code"])): row for row in candidates
    }
    all_market_features: set[str] = set()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for row in selected:
                snapshot = date.fromisoformat(str(row["snapshot"]))
                index_codes = sorted(
                    {
                        str(candidate_map[(row["snapshot"], code)]["index_code"])
                        for code in row["selected_codes"]
                        if (row["snapshot"], code) in candidate_map
                    }
                )
                market = PHASE_FEATURE_STORE.snapshot_features(
                    cur,
                    index_codes or [CS300_CODE],
                    CS300_CODE,
                    snapshot,
                )
                for name, raw in market.items():
                    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                        continue
                    value = float(raw)
                    if not math.isfinite(value):
                        continue
                    key = f"market_{name}"
                    row[key] = value
                    all_market_features.add(key)
    finally:
        conn.close()
    for row in selected:
        for feature in all_market_features:
            row.setdefault(feature, None)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "method": (
                    f"{args.selector_version} selected domestic passive ETF labels with point-in-time "
                    "basket, A-share market, macro, flow, and strictly lagged external features"
                ),
                "market_features": sorted(all_market_features),
                "selected_observations": selected,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote {output}; observations={len(selected)} "
        f"market_features={len(all_market_features)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

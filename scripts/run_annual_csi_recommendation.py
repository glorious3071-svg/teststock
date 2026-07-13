#!/usr/bin/env python3
"""One-shot annual CSI recommendation: daily rollup + rank + validate.

Daily L1-L3 processing should run via launchd (21:00). This script rollups + ranks.

Usage:
  python scripts/run_annual_csi_recommendation.py --year 2026
  python scripts/run_annual_csi_recommendation.py --year 2026 --ensure-daily
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> int:
    print(f"\n>>> {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument(
        "--ensure-daily",
        action="store_true",
        help="Run daily processing batch before aggregate (if launchd missed)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Full historical cluster+extract+daily before aggregate",
    )
    args = parser.parse_args()

    py = sys.executable

    if args.backfill:
        rc = run([py, "scripts/run_news_daily_processing.py", "--backfill"])
        if rc != 0:
            print("Warning: backfill had errors (continuing)")

    elif args.ensure_daily:
        rc = run([py, "scripts/run_news_daily_processing.py"])
        if rc != 0:
            print("Warning: daily processing had errors (continuing)")

    if not args.skip_news:
        rc = run([
            py, "scripts/aggregate_theme_news_signals.py",
            "--year", str(args.year), "--live",
        ])
        if rc != 0:
            return rc

    rc = run([
        py, "scripts/rank_annual_csi.py",
        "--year", str(args.year),
        "--top", str(args.top),
        "--suffix", "CSI",
        "--save",
    ])
    if rc != 0:
        return rc

    rc = run([py, "scripts/validate_csi_rank.py", "--year", str(args.year)])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

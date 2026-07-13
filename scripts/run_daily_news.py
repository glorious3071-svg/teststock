#!/usr/bin/env python3
"""Unified daily news collection orchestrator.

Usage:
  python scripts/run_daily_news.py --tier flash
  python scripts/run_daily_news.py --tier daily
  python scripts/run_daily_news.py --tier all
  python scripts/run_daily_news.py --collector eastmoney_flash
  python scripts/run_daily_news.py --list
  python scripts/run_daily_news.py --tier flash --dry-run
  python scripts/run_daily_news.py --tier all --since 2025-06-01
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collectors.registry import COLLECTORS, TIER_COLLECTORS, collectors_for_tier, get_collector
from db.connection import ensure_schema, get_connection


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid --since date: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run daily news collectors")
    parser.add_argument("--tier", choices=list(TIER_COLLECTORS.keys()), default=None)
    parser.add_argument("--collector", choices=list(COLLECTORS.keys()), default=None)
    parser.add_argument("--since", default=None, help="Only ingest items on/after this datetime")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and count without DB writes")
    parser.add_argument("--list", action="store_true", help="List collectors and tiers")
    args = parser.parse_args()

    if args.list:
        print("Collectors:")
        for name, col in COLLECTORS.items():
            print(f"  {name:20s} tier={col.tier:6s} category={col.category}")
        print("\nTiers:")
        for tier, names in TIER_COLLECTORS.items():
            print(f"  {tier}: {', '.join(names)}")
        return 0

    if not args.tier and not args.collector:
        parser.error("Specify --tier or --collector (or --list)")

    since = parse_since(args.since)
    if args.collector:
        collectors = [get_collector(args.collector)]
    else:
        collectors = collectors_for_tier(args.tier or "flash")

    run_id = str(uuid.uuid4())
    conn = get_connection(apply_schema=not args.dry_run)

    totals = {"fetched": 0, "inserted": 0, "skipped_dup": 0, "failed": 0}
    print(f"run_id={run_id} collectors={len(collectors)} dry_run={args.dry_run}")

    try:
        for collector in collectors:
            print(f"\n[{collector.name}] running...")
            result = collector.run(conn, run_id=run_id, since=since, dry_run=args.dry_run)
            totals["fetched"] += result.fetched
            totals["inserted"] += result.inserted
            totals["skipped_dup"] += result.skipped_dup
            if result.error_msg:
                totals["failed"] += 1
                print(f"  ERROR: {result.error_msg}")
            print(
                f"  fetched={result.fetched} inserted={result.inserted} "
                f"skipped_dup={result.skipped_dup} status={result.status}"
            )
    finally:
        conn.close()

    print(
        f"\nDone: fetched={totals['fetched']} inserted={totals['inserted']} "
        f"skipped_dup={totals['skipped_dup']} failed_collectors={totals['failed']}"
    )
    return 1 if totals["failed"] == len(collectors) else 0


if __name__ == "__main__":
    raise SystemExit(main())

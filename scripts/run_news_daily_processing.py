#!/usr/bin/env python3
"""Daily news processing batch: cluster → extract → theme_news_daily.

Usage:
  python scripts/run_news_daily_processing.py
  python scripts/run_news_daily_processing.py --date 2026-06-29
  python scripts/run_news_daily_processing.py --backfill
  python scripts/run_news_daily_processing.py --backfill --extract-limit 2000
  python scripts/run_news_daily_processing.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from news.processing.batch import (
    backfill_cluster_by_day,
    backfill_daily_signals,
    backfill_weekly_signals,
    ensure_all_schema,
    extract_pending_events,
    link_existing_extractions,
    run_daily_batch,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily news L1-L3 processing")
    parser.add_argument("--date", default=None, help="Process date YYYY-MM-DD (default today)")
    parser.add_argument("--backfill", action="store_true", help="Cluster all history + daily rollup")
    parser.add_argument("--extract-limit", type=int, default=2000)
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock extractor (default)")
    parser.add_argument("--no-mock", action="store_true", help="Use real LLM")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    process_date = date.fromisoformat(args.date) if args.date else date.today()
    mock = not args.no_mock

    conn = get_connection()
    ensure_all_schema(conn)

    if args.backfill:
        print("=== Backfill cluster by day ===")
        cstats = backfill_cluster_by_day(conn)
        print(f"  days={cstats['days']} scanned={cstats['scanned']} "
              f"created={cstats['created']} updated={cstats['updated']}")

        print("=== Extract pending events ===")
        link_existing_extractions(conn)
        n_ext = skipped = 0
        while True:
            batch = extract_pending_events(conn, limit=args.extract_limit, mock=mock)
            n_ext += batch["extracted"]
            skipped += batch["skipped_prefilter"]
            print(f"  batch extracted={batch['extracted']} skipped={batch['skipped_prefilter']}")
            if batch["extracted"] == 0:
                break

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(DATE(COALESCE(pub_time, created_at))),
                       MAX(DATE(COALESCE(pub_time, created_at)))
                FROM news_article
                """
            )
            d0, d1 = cur.fetchone()
        if d0 and d1:
            print(f"=== Backfill daily signals {d0}..{d1} ===")
            nd = backfill_daily_signals(conn, start_date=d0, end_date=d1)
            print(f"  days_with_signals={nd}")
        conn.close()
        return 0

    result = run_daily_batch(
        conn,
        process_date=process_date,
        mock_extract=mock,
        extract_limit=args.extract_limit,
        dry_run=args.dry_run,
    )
    conn.close()

    print(f"run_date={result['run_date']} status={result['status']}")
    print(f"  cluster: {result.get('cluster')}")
    print(f"  closed_events: {result.get('closed_events')}")
    print(f"  extractions: {result.get('extractions')}")
    print(f"  daily_themes: {result.get('daily_themes')}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

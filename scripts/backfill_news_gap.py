#!/usr/bin/env python3
"""Backfill news_article from news_flash + CCTV for a date range.

Usage:
  python scripts/backfill_news_gap.py --from 2026-06-27 --to 2026-06-28
  python scripts/backfill_news_gap.py --from 2026-06-27 --to 2026-06-28 --process
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collectors.cctv_daily import CctvDailyCollector
from collectors.dedup import normalize_text
from collectors.models import RawArticle
from collectors.registry import collectors_for_tier
from collectors.storage import insert_articles
from db.connection import get_connection


def migrate_news_flash(conn, start: date, end: date) -> dict:
    end_dt = datetime.combine(end, time(23, 59, 59))
    start_dt = datetime.combine(start, time.min)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT src, pub_time, title, content
            FROM news_flash
            WHERE pub_time >= %s AND pub_time <= %s
            ORDER BY pub_time
            """,
            (start_dt, end_dt),
        )
        rows = cur.fetchall()

    articles: list[RawArticle] = []
    for src, pub_time, title, content in rows:
        title = normalize_text(str(title or ""))
        if len(title) < 4:
            continue
        source = {"em": "eastmoney"}.get(str(src or ""), str(src or "unknown"))
        articles.append(
            RawArticle(
                source=source,
                category="flash",
                title=title[:490],
                body_text=normalize_text(str(content or "")) or None,
                pub_time=pub_time,
                extra_json={"migrated_from": "news_flash"},
            )
        )

    result = insert_articles(conn, articles)
    return {
        "read": len(rows),
        "candidates": len(articles),
        "inserted": result.inserted,
        "skipped_dup": result.skipped_dup,
    }


def backfill_cctv(conn, start: date, end: date) -> dict:
    collector = CctvDailyCollector(lookback_days=(date.today() - start).days + 1)
    run_id = str(uuid.uuid4())
    result = collector.run(conn, run_id=run_id, since=datetime.combine(start, time.min))
    return {
        "fetched": result.fetched,
        "inserted": result.inserted,
        "skipped_dup": result.skipped_dup,
        "error": result.error_msg,
    }


def backfill_daily_tier(conn, start: date) -> dict:
    totals = {"fetched": 0, "inserted": 0, "skipped_dup": 0, "failed": 0}
    run_id = str(uuid.uuid4())
    since = datetime.combine(start, time.min)
    skip = {"cctv_daily", "eastmoney_flash", "sina_flash", "ths_flash"}
    for collector in collectors_for_tier("daily"):
        if collector.name in skip:
            continue
        r = collector.run(conn, run_id=run_id, since=since)
        totals["fetched"] += r.fetched
        totals["inserted"] += r.inserted
        totals["skipped_dup"] += r.skipped_dup
        if r.error_msg:
            totals["failed"] += 1
            print(f"  [{collector.name}] ERROR: {r.error_msg[:120]}")
        else:
            print(
                f"  [{collector.name}] fetched={r.fetched} inserted={r.inserted} "
                f"dup={r.skipped_dup}"
            )
    return totals


def process_range(conn, start: date, end: date) -> dict:
    from news.processing.batch import (
        ensure_all_schema,
        extract_pending_events,
        link_existing_extractions,
    )
    from news.processing.cluster import cluster_stream_all
    from news.processing.daily import aggregate_daily, rollup_weekly

    ensure_all_schema(conn)
    st = cluster_stream_all(conn)
    link_existing_extractions(conn)
    ext = {"extracted": 0, "skipped": 0}
    for _ in range(10):
        b = extract_pending_events(conn, limit=2000, mock=True)
        ext["extracted"] += b["extracted"]
        ext["skipped"] += b["skipped_prefilter"]
        if b["extracted"] == 0:
            break

    daily_n = 0
    d = start
    while d <= end:
        if aggregate_daily(conn, d):
            daily_n += 1
        d += timedelta(days=1)
    weekly_n = 0
    w = start
    while w <= end:
        if rollup_weekly(conn, w):
            weekly_n += 1
        w += timedelta(days=7)

    return {
        "cluster": {"created": st.created, "updated": st.updated, "scanned": st.scanned},
        "extract": ext,
        "daily_days": daily_n,
    }


def cleanup_stuck_runs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collect_run
            SET status='failed', finished_at=NOW(),
                error_msg=COALESCE(error_msg, 'stale cleanup on backfill')
            WHERE status='running' AND started_at < DATE_SUB(NOW(), INTERVAL 2 HOUR)
            """
        )
        n = cur.rowcount
    conn.commit()
    return n


def count_by_day(conn, start: date, end: date) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DATE(COALESCE(pub_time, created_at)) d, COUNT(*) n
            FROM news_article
            WHERE DATE(COALESCE(pub_time, created_at)) BETWEEN %s AND %s
            GROUP BY d ORDER BY d
            """,
            (start, end),
        )
        return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--process", action="store_true", help="Run cluster/extract/daily after ingest")
    parser.add_argument("--skip-flash", action="store_true")
    parser.add_argument("--skip-cctv", action="store_true")
    parser.add_argument("--skip-daily", action="store_true")
    args = parser.parse_args()

    start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    end = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    if end < start:
        parser.error("--to must be >= --from")

    conn = get_connection(apply_schema=True)
    print(f"Before: {count_by_day(conn, start, end)}")

    stuck = cleanup_stuck_runs(conn)
    if stuck:
        print(f"Cleaned {stuck} stuck collect_run row(s)")

    if not args.skip_flash:
        print("\n=== Migrate news_flash → news_article ===")
        flash_stats = migrate_news_flash(conn, start, end)
        print(flash_stats)

    if not args.skip_cctv:
        print("\n=== CCTV daily ===")
        cctv_stats = backfill_cctv(conn, start, end)
        print(cctv_stats)

    if not args.skip_daily:
        print("\n=== Daily tier (intl / ndrc / research) ===")
        daily_stats = backfill_daily_tier(conn, start)
        print(daily_stats)

    if args.process:
        print("\n=== Processing pipeline ===")
        proc = process_range(conn, start, end)
        print(proc)

    print(f"\nAfter: {count_by_day(conn, start, end)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backfill 2025 news into news_article with temporal validation.

Sources:
  1. cctv_news_daily (full-year policy news)
  2. npr_policy (NDRC / policy corpus)
  3. Eastmoney industry research API (2025 reports)
  4. run_daily_news collectors (cjzc, ndrc scrape, flash latest)

Usage:
  python scripts/backfill_news_2025.py
  python scripts/backfill_news_2025.py --year 2025 --skip-research
  python scripts/backfill_news_2025.py --validate-only
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from collectors.dedup import content_hash, html_to_text, normalize_text, parse_datetime
from collectors.models import RawArticle
from collectors.research_industry import IndustryResearchCollector
from collectors.storage import insert_articles
from db.connection import ensure_schema, get_connection, mysql_config

YEAR_START = date(2025, 1, 1)
YEAR_END = date(2025, 12, 31)
H2_START = date(2025, 7, 1)
MAX_PUB_FUTURE = datetime(2026, 1, 15)  # allow slight clock skew after year-end


def _cctv_pub_time(news_date: date) -> datetime:
    return datetime.combine(news_date, time(19, 0, 0))


def validate_pub_time(pub_time: datetime | None, *, label: str) -> tuple[bool, str]:
    if pub_time is None:
        return False, f"{label}: missing pub_time"
    if pub_time > MAX_PUB_FUTURE:
        return False, f"{label}: pub_time in future ({pub_time})"
    if pub_time.date() < YEAR_START or pub_time.date() > YEAR_END:
        return False, f"{label}: pub_time outside 2025 ({pub_time.date()})"
    return True, "ok"


def migrate_cctv(conn) -> dict:
    stats = {"read": 0, "inserted": 0, "invalid_time": 0, "skipped_dup": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT news_date, title, content
            FROM cctv_news_daily
            WHERE news_date >= %s AND news_date <= %s
            ORDER BY news_date, title
            """,
            (YEAR_START, YEAR_END),
        )
        rows = cur.fetchall()

    articles: list[RawArticle] = []
    for news_date, title, content in rows:
        stats["read"] += 1
        title = normalize_text(str(title or ""))
        if len(title) < 4:
            continue
        pub_time = _cctv_pub_time(news_date)
        ok, _ = validate_pub_time(pub_time, label="cctv")
        if not ok:
            stats["invalid_time"] += 1
            continue
        body = normalize_text(str(content or "")) or None
        articles.append(
            RawArticle(
                source="cctv",
                category="policy",
                title=title[:490],
                body_text=body,
                pub_time=pub_time,
                extra_json={"news_date": news_date.isoformat(), "migrated_from": "cctv_news_daily"},
            )
        )

    result = insert_articles(conn, articles)
    stats["inserted"] = result.inserted
    stats["skipped_dup"] = result.skipped_dup
    return stats


def migrate_npr_policy(conn) -> dict:
    stats = {"read": 0, "inserted": 0, "invalid_time": 0, "skipped_dup": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pubtime, title, url, puborg, ptype, content_html
            FROM npr_policy
            WHERE pubtime >= %s AND pubtime < %s
            ORDER BY pubtime
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        rows = cur.fetchall()

    articles: list[RawArticle] = []
    for pubtime, title, url, puborg, ptype, content_html in rows:
        stats["read"] += 1
        title = normalize_text(str(title or ""))
        if len(title) < 4:
            continue
        ok, _ = validate_pub_time(pubtime, label="npr")
        if not ok:
            stats["invalid_time"] += 1
            continue
        body = html_to_text(content_html) if content_html else None
        articles.append(
            RawArticle(
                source="ndrc",
                category="policy",
                title=title[:490],
                body_text=body,
                pub_time=pubtime,
                url=url,
                author=puborg,
                fetch_status="ok" if body else "partial",
                extra_json={"ptype": ptype, "migrated_from": "npr_policy"},
            )
        )

    result = insert_articles(conn, articles)
    stats["inserted"] = result.inserted
    stats["skipped_dup"] = result.skipped_dup
    return stats


def repair_research_pub_times(conn, *, max_pages: int = 50) -> dict:
    """Backfill pub_time on eastmoney_research rows inserted without dates."""
    collector = IndustryResearchCollector(max_pages=max_pages)
    title_to_pub: dict[str, datetime] = {}
    total_pages = 1
    for page_no in range(1, max_pages + 1):
        payload = collector._fetch_page(2025, page_no)
        if page_no == 1:
            total_pages = min(payload.get("total_pages") or 1, max_pages)
        for report in payload.get("reports") or []:
            title = normalize_text(str(report.get("title") or ""))
            pub_time = parse_datetime(
                str(report.get("publishDate") or report.get("date") or "")
            )
            if title and pub_time and YEAR_START <= pub_time.date() <= YEAR_END:
                title_to_pub[title] = pub_time
        if page_no >= total_pages:
            break

    stats = {"mapped": len(title_to_pub), "updated": 0, "invalid": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title FROM news_article
            WHERE source = 'eastmoney_research' AND pub_time IS NULL
            """
        )
        rows = cur.fetchall()
        for aid, title in rows:
            pub_time = title_to_pub.get(normalize_text(str(title or "")))
            if not pub_time:
                continue
            ok, _ = validate_pub_time(pub_time, label="research")
            if not ok:
                stats["invalid"] += 1
                continue
            cur.execute(
                "UPDATE news_article SET pub_time = %s WHERE id = %s",
                (pub_time, aid),
            )
            stats["updated"] += 1
    conn.commit()
    return stats


def fetch_research_2025(conn, *, max_pages: int = 30) -> dict:
    collector = IndustryResearchCollector(max_pages=max_pages)
    since = datetime(2025, 1, 1)
    articles: list[RawArticle] = []
    total_pages = 1
    for page_no in range(1, max_pages + 1):
        payload = collector._fetch_page(2025, page_no)
        if page_no == 1:
            total_pages = min(payload.get("total_pages") or 1, max_pages)
        for report in payload.get("reports") or []:
            article = collector._to_article(report, since)
            if article:
                articles.append(article)
        if page_no >= total_pages:
            break
    result = insert_articles(conn, articles)
    return {
        "fetched": len(articles),
        "inserted": result.inserted,
        "skipped_dup": result.skipped_dup,
        "error": None,
    }


def run_collectors(conn, since: datetime) -> dict:
    from collectors.registry import collectors_for_tier

    totals = {"fetched": 0, "inserted": 0, "skipped_dup": 0, "failed": 0}
    run_id = str(uuid.uuid4())
    for collector in collectors_for_tier("all"):
        if collector.name == "research_industry":
            continue  # handled separately with 2025 year
        r = collector.run(conn, run_id=run_id, since=since)
        totals["fetched"] += r.fetched
        totals["inserted"] += r.inserted
        totals["skipped_dup"] += r.skipped_dup
        if r.error_msg:
            totals["failed"] += 1
            print(f"  [{collector.name}] ERROR: {r.error_msg[:200]}")
        else:
            print(
                f"  [{collector.name}] fetched={r.fetched} inserted={r.inserted} "
                f"dup={r.skipped_dup}"
            )
    return totals


def validate_db(conn) -> dict:
    report: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM news_article
            WHERE pub_time >= %s AND pub_time < %s
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        report["total_2025"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM news_article
            WHERE pub_time >= %s AND pub_time < %s
            """,
            (H2_START, date(2026, 1, 1)),
        )
        report["h2_2025"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM news_article
            WHERE pub_time >= %s AND pub_time < %s AND pub_time IS NULL
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        report["null_pub_time"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM news_article
            WHERE pub_time >= %s AND pub_time < %s
              AND (LENGTH(title) < 4 OR title IS NULL)
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        report["bad_title"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM news_article
            WHERE source = 'eastmoney_research' AND pub_time IS NULL
            """
        )
        report["research_null_pub_time"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM news_article a
            LEFT JOIN news_extraction e ON e.article_id = a.id
            WHERE a.pub_time >= %s AND a.pub_time < %s AND e.id IS NULL
            """,
            (H2_START, date(2026, 1, 1)),
        )
        report["h2_pending_extraction"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT source, category, COUNT(*), MIN(pub_time), MAX(pub_time)
            FROM news_article
            WHERE pub_time >= %s AND pub_time < %s
            GROUP BY source, category ORDER BY COUNT(*) DESC
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        report["by_source"] = [
            {
                "source": r[0],
                "category": r[1],
                "count": r[2],
                "min_pub": str(r[3]) if r[3] else None,
                "max_pub": str(r[4]) if r[4] else None,
            }
            for r in cur.fetchall()
        ]

        # CCTV coverage: days in 2025 with at least one article
        cur.execute(
            """
            SELECT COUNT(DISTINCT DATE(pub_time))
            FROM news_article
            WHERE source = 'cctv' AND pub_time >= %s AND pub_time < %s
            """,
            (YEAR_START, date(2026, 1, 1)),
        )
        report["cctv_days_covered"] = cur.fetchone()[0]
        report["cctv_days_expected"] = 365

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--skip-research", action="store_true")
    parser.add_argument("--skip-collectors", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--repair-research-only", action="store_true")
    parser.add_argument("--skip-cctv-reimport", action="store_true")
    parser.add_argument("--research-pages", type=int, default=30)
    args = parser.parse_args()

    conn = get_connection(apply_schema=True)

    if args.validate_only:
        report = validate_db(conn)
        conn.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.repair_research_only:
        print("=== Repair eastmoney_research pub_time ===")
        stats = repair_research_pub_times(conn, max_pages=args.research_pages)
        print(stats)
        report = validate_db(conn)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        conn.close()
        return 0

    print("=== Step 1: migrate cctv_news_daily → news_article ===")
    cctv_stats = migrate_cctv(conn)
    print(cctv_stats)

    print("\n=== Step 2: migrate npr_policy → news_article ===")
    npr_stats = migrate_npr_policy(conn)
    print(npr_stats)

    if not args.skip_research:
        print(f"\n=== Step 3: fetch 2025 industry research (max_pages={args.research_pages}) ===")
        res_stats = fetch_research_2025(conn, max_pages=args.research_pages)
        print(res_stats)

    if not args.skip_collectors:
        print("\n=== Step 4: run all collectors since 2025-01-01 ===")
        since = datetime(2025, 1, 1)
        col_stats = run_collectors(conn, since)
        print(col_stats)

    if not args.skip_cctv_reimport:
        print("\n=== Step 5: import missing CCTV days (legacy script) ===")
        import subprocess

        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "import_cctv_news.py"), "--since", "2025-01-01"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        print(r.stdout[-800:] if r.stdout else "")
        if r.returncode != 0:
            print("import_cctv_news warning:", (r.stderr or "")[-400:])
        # Re-migrate any new CCTV rows
        cctv_stats2 = migrate_cctv(conn)
        print("cctv re-migrate:", cctv_stats2)

    print("\n=== Step 6: repair research pub_time ===")
    repair_stats = repair_research_pub_times(conn, max_pages=args.research_pages)
    print(repair_stats)

    print("\n=== Validation report ===")
    report = validate_db(conn)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    out = ROOT / "data" / "backtests" / "news_backfill_2025_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport saved: {out}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run LLM extraction on unprocessed news_article rows.

Usage:
  python scripts/run_news_extraction.py --limit 10
  python scripts/run_news_extraction.py --limit 5 --dry-run
  python scripts/run_news_extraction.py --since 2025-06-01 --limit 50
  python scripts/run_news_extraction.py --article-id 123 --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# LLM API (domestic MaaS) should not route through SOCKS proxy
for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

from agents.annual_direction.llm_client import LLMError, llm_config
from collectors.enrichment import extract_article
from db.connection import ensure_schema, get_connection


def fetch_pending(
    conn,
    *,
    limit: int,
    since: datetime | None,
    until: datetime | None,
    article_id: int | None,
    force: bool,
) -> list[tuple[int, str, str | None]]:
    with conn.cursor() as cur:
        if article_id:
            cur.execute(
                "SELECT id, title, body_text FROM news_article WHERE id = %s",
                (article_id,),
            )
        elif force:
            sql = """
                SELECT id, title, body_text FROM news_article
                WHERE 1=1
            """
            params: list = []
            if since:
                sql += " AND (pub_time >= %s OR created_at >= %s)"
                params.extend([since, since])
            if until:
                sql += " AND (pub_time < %s OR created_at < %s)"
                params.extend([until, until])
            sql += " ORDER BY COALESCE(pub_time, created_at) DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
        else:
            sql = """
                SELECT a.id, a.title, a.body_text
                FROM news_article a
                LEFT JOIN news_extraction e ON a.id = e.article_id
                WHERE e.id IS NULL
            """
            params = []
            if since and until:
                sql += " AND a.pub_time >= %s AND a.pub_time < %s"
                params.extend([since, until])
            elif since:
                sql += " AND (a.pub_time >= %s OR a.created_at >= %s)"
                params.extend([since, since])
            elif until:
                sql += " AND (a.pub_time < %s OR a.created_at < %s)"
                params.extend([until, until])
            sql += " ORDER BY COALESCE(a.pub_time, a.created_at) ASC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
        return list(cur.fetchall())


def save_extraction(conn, article_id: int, model: str, data: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_extraction
                (article_id, extracted_at, model, sentiment, themes, industries,
                 ts_codes, event_type, magnitude, summary, reasoning, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                extracted_at = VALUES(extracted_at),
                sentiment = VALUES(sentiment),
                themes = VALUES(themes),
                industries = VALUES(industries),
                ts_codes = VALUES(ts_codes),
                event_type = VALUES(event_type),
                magnitude = VALUES(magnitude),
                summary = VALUES(summary),
                reasoning = VALUES(reasoning),
                confidence = VALUES(confidence)
            """,
            (
                article_id,
                datetime.now(),
                model,
                data["sentiment"],
                json.dumps(data["themes"], ensure_ascii=False),
                json.dumps(data["industries"], ensure_ascii=False),
                json.dumps(data["ts_codes"], ensure_ascii=False),
                data["event_type"],
                data["magnitude"],
                data["summary"],
                data["reasoning"],
                data["confidence"],
            ),
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM extraction for news_article")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD inclusive")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD exclusive upper bound")
    parser.add_argument("--article-id", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Re-extract even if exists")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true", help="Use keyword mock extractor (offline test)")
    parser.add_argument("--sleep", type=float, default=0.5, help="Delay between LLM calls")
    args = parser.parse_args()

    since = None
    until = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    if args.until:
        until = datetime.strptime(args.until, "%Y-%m-%d")

    conn = get_connection(apply_schema=True)
    model = llm_config()["model"] if not args.mock else "mock"
    rows = fetch_pending(
        conn,
        limit=args.limit,
        since=since,
        until=until,
        article_id=args.article_id,
        force=args.force,
    )

    if not rows:
        print("No pending articles.")
        conn.close()
        return 0

    ok = 0
    failed = 0
    for article_id, title, body in rows:
        print(f"[{article_id}] {title[:60]}...")
        if args.dry_run:
            print("  dry-run skip")
            ok += 1
            continue
        try:
            data = extract_article(title, body, mock=args.mock)
            save_extraction(conn, article_id, model, data)
            print(f"  -> {data['sentiment']} themes={data['themes']} mag={data['magnitude']}")
            ok += 1
        except Exception as exc:
            print(f"  LLM error: {exc}")
            failed += 1
        time.sleep(args.sleep)

    conn.close()
    print(f"\nDone: ok={ok} failed={failed}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

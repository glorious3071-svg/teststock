"""Database helpers for news collectors."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pymysql

from collectors.dedup import content_hash, normalize_text
from collectors.models import CollectResult, RawArticle


def insert_articles(
    conn: pymysql.connections.Connection,
    articles: list[RawArticle],
    *,
    dry_run: bool = False,
) -> CollectResult:
    result = CollectResult(collector="batch", fetched=len(articles))
    if not articles:
        return result

    hashes = [content_hash(a.source, a.title, a.body_text, a.pub_time) for a in articles]
    existing = _load_existing_hashes(conn, hashes)

    dup_hashes: list[str] = []
    to_insert: list[tuple[Any, ...]] = []
    for article, h in zip(articles, hashes):
        if h in existing:
            result.skipped_dup += 1
            dup_hashes.append(h)
            continue
        to_insert.append(_article_row(article, h))
        existing.add(h)

    if dry_run:
        result.inserted = len(to_insert)
        return result

    if to_insert:
        sql = """
            INSERT INTO news_article
                (content_hash, source, category, pub_time, title, body_text,
                 url, author, lang, extra_json, fetch_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                body_text = IF(LENGTH(VALUES(body_text)) > LENGTH(COALESCE(body_text, '')),
                               VALUES(body_text), body_text),
                fetch_status = VALUES(fetch_status),
                updated_at = CURRENT_TIMESTAMP
        """
        with conn.cursor() as cur:
            cur.executemany(sql, to_insert)
        conn.commit()
        result.inserted = len(to_insert)

    if dup_hashes and not dry_run:
        _bump_mention_counters(conn, dup_hashes)

    return result


def mirror_to_news_flash(
    conn: pymysql.connections.Connection,
    articles: list[RawArticle],
) -> int:
    """Optional legacy mirror for flash-category articles."""
    flash_rows = [
        (a.source, a.pub_time, normalize_text(a.title)[:490], a.body_text)
        for a in articles
        if a.category == "flash" and a.title
    ]
    if not flash_rows:
        return 0
    sql = """
        INSERT INTO news_flash (src, pub_time, title, content)
        VALUES (%s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, flash_rows)
    conn.commit()
    return len(flash_rows)


def start_run_log(
    conn: pymysql.connections.Connection,
    run_id: str,
    collector: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO collect_run (run_id, collector, started_at, status)
            VALUES (%s, %s, %s, 'running')
            """,
            (run_id, collector, datetime.now()),
        )
        log_id = cur.lastrowid
    conn.commit()
    return log_id


def finish_run_log(
    conn: pymysql.connections.Connection,
    log_id: int,
    result: CollectResult,
) -> None:
    status = result.status
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collect_run
            SET finished_at = %s, status = %s, fetched = %s, inserted = %s,
                skipped_dup = %s, error_msg = %s
            WHERE id = %s
            """,
            (
                datetime.now(),
                status,
                result.fetched,
                result.inserted,
                result.skipped_dup,
                result.error_msg,
                log_id,
            ),
        )
    conn.commit()


def _bump_mention_counters(conn: pymysql.connections.Connection, hashes: list[str]) -> None:
    """Increment mention_count for exact duplicates without weakening signal."""
    if not hashes:
        return
    try:
        placeholders = ",".join(["%s"] * len(hashes))
        now = datetime.now()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT content_hash, id FROM news_article
                WHERE content_hash IN ({placeholders})
                """,
                hashes,
            )
            rows = {h: aid for h, aid in cur.fetchall()}
            for h in hashes:
                aid = rows.get(h)
                if not aid:
                    continue
                cur.execute(
                    """
                    INSERT INTO news_mention_counter
                        (content_hash, canonical_article_id, mention_count, first_seen_at, last_seen_at)
                    VALUES (%s, %s, 1, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        mention_count = mention_count + 1,
                        last_seen_at = VALUES(last_seen_at)
                    """,
                    (h, aid, now, now),
                )
        conn.commit()
    except pymysql.err.ProgrammingError:
        pass
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1146:
            raise


def _load_existing_hashes(conn: pymysql.connections.Connection, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    placeholders = ",".join(["%s"] * len(hashes))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT content_hash FROM news_article WHERE content_hash IN ({placeholders})",
            hashes,
        )
        return {row[0] for row in cur.fetchall()}


def _article_row(article: RawArticle, h: str) -> tuple[Any, ...]:
    extra = json.dumps(article.extra_json, ensure_ascii=False) if article.extra_json else None
    return (
        h,
        article.source,
        article.category,
        article.pub_time,
        normalize_text(article.title)[:490],
        article.body_text,
        article.url,
        article.author,
        article.lang,
        extra,
        article.fetch_status,
    )

"""Daily batch orchestrator: prefilter → cluster → extract → aggregate."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pymysql

from collectors.enrichment import extract_article
from news.processing.cluster import close_stale_events, cluster_articles, cluster_stream_all
from news.processing.daily import aggregate_daily, rollup_weekly
from news.processing.schema import ensure_processing_schema
from news.retrieval.prefilter import backfill_prefilter, seed_theme_keywords, should_extract_llm
from news.retrieval.schema import ensure_retrieval_schema


def ensure_all_schema(conn: pymysql.connections.Connection) -> None:
    ensure_processing_schema(conn)
    ensure_retrieval_schema(conn)


def link_existing_extractions(conn: pymysql.connections.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE news_extraction e
            JOIN news_event ev ON ev.canonical_article_id = e.article_id
            SET e.event_id = ev.id
            WHERE e.event_id IS NULL
            """
        )
        n = cur.rowcount
        cur.execute(
            """
            UPDATE news_extraction e
            JOIN news_event_member m ON m.article_id = e.article_id
            SET e.event_id = m.event_id
            WHERE e.event_id IS NULL
            """
        )
        n += cur.rowcount
    conn.commit()
    return n


def extract_pending_events(
    conn: pymysql.connections.Connection,
    *,
    limit: int = 500,
    mock: bool = True,
    model: str = "mock",
    use_prefilter: bool = True,
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ev.id, ev.canonical_article_id, a.title, a.body_text, a.category,
                   a.prefilter_score, a.prefilter_themes
            FROM news_event ev
            JOIN news_article a ON a.id = ev.canonical_article_id
            LEFT JOIN news_extraction e ON e.article_id = ev.canonical_article_id
            WHERE e.id IS NULL
            ORDER BY ev.last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    from news.retrieval.prefilter import PrefilterResult, match_keywords

    ok = skipped = 0
    for event_id, article_id, title, body, category, pf_score, pf_themes in rows:
        if use_prefilter:
            if pf_score is None:
                pf = match_keywords(title, body)
            else:
                themes = json.loads(pf_themes) if isinstance(pf_themes, str) else (pf_themes or [])
                pf = PrefilterResult(themes, float(pf_score or 0), [])
            if not should_extract_llm(category or "flash", pf):
                skipped += 1
                continue
        data = extract_article(title, body, mock=mock)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO news_extraction
                    (article_id, event_id, extracted_at, model, sentiment, themes, industries,
                     ts_codes, event_type, magnitude, summary, reasoning, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    event_id=VALUES(event_id),
                    extracted_at=VALUES(extracted_at),
                    sentiment=VALUES(sentiment),
                    themes=VALUES(themes),
                    magnitude=VALUES(magnitude),
                    confidence=VALUES(confidence)
                """,
                (
                    article_id, event_id, datetime.now(), model,
                    data["sentiment"],
                    json.dumps(data["themes"], ensure_ascii=False),
                    json.dumps(data["industries"], ensure_ascii=False),
                    json.dumps(data["ts_codes"], ensure_ascii=False),
                    data["event_type"], data["magnitude"],
                    data["summary"], data["reasoning"], data["confidence"],
                ),
            )
        conn.commit()
        ok += 1
    return {"extracted": ok, "skipped_prefilter": skipped}


def backfill_cluster_by_day(conn, *, start_date=None, end_date=None) -> dict:
    st = cluster_stream_all(conn)
    return {
        "days": "cluster_stream_all",
        "created": st.created,
        "updated": st.updated,
        "scanned": st.scanned,
    }


def backfill_daily_signals(conn, *, start_date: date, end_date: date) -> int:
    n = 0
    d = start_date
    while d <= end_date:
        if aggregate_daily(conn, d):
            n += 1
        d += timedelta(days=1)
    return n


def backfill_weekly_signals(conn, *, start_date: date, end_date: date) -> int:
    n = 0
    d = start_date
    while d <= end_date:
        if rollup_weekly(conn, d):
            n += 1
        d += timedelta(days=7)
    return n


def run_daily_batch(
    conn: pymysql.connections.Connection,
    *,
    process_date: date | None = None,
    mock_extract: bool = True,
    extract_limit: int = 500,
    dry_run: bool = False,
) -> dict:
    ensure_all_schema(conn)
    pd = process_date or date.today()
    started = datetime.now()

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO news_processing_run (run_date, started_at, status)
                VALUES (%s, %s, 'running')
                ON DUPLICATE KEY UPDATE started_at=VALUES(started_at), status='running',
                    finished_at=NULL, error_msg=NULL
                """,
                (pd, started),
            )
        conn.commit()

    result: dict = {"run_date": str(pd), "dry_run": dry_run}
    try:
        cluster_stats = cluster_articles(conn, process_date=pd)
        result["cluster"] = {
            "scanned": cluster_stats.scanned,
            "created": cluster_stats.created,
            "updated": cluster_stats.updated,
        }
        stale_before = datetime.combine(pd, datetime.min.time()) - timedelta(hours=72)
        result["closed_events"] = close_stale_events(conn, before=stale_before)

        if not dry_run:
            ext = extract_pending_events(conn, limit=extract_limit, mock=mock_extract)
            result["extractions"] = ext["extracted"]
            result["skipped_prefilter"] = ext["skipped_prefilter"]
            daily = aggregate_daily(conn, pd)
            result["daily_themes"] = len(daily)
            result["weekly_themes"] = len(rollup_weekly(conn, pd) or [])
        else:
            result["extractions"] = 0
            daily = aggregate_daily(conn, pd, dry_run=True)
            result["daily_themes"] = len(daily)

        result["status"] = "success"
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE news_processing_run
                    SET finished_at=%s, status='success',
                        articles_scanned=%s, events_created=%s, events_updated=%s,
                        extractions_run=%s, daily_themes=%s
                    WHERE run_date=%s
                    """,
                    (
                        datetime.now(),
                        cluster_stats.scanned,
                        cluster_stats.created,
                        cluster_stats.updated,
                        result.get("extractions", 0),
                        result.get("daily_themes", 0),
                        pd,
                    ),
                )
            conn.commit()
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE news_processing_run
                    SET finished_at=%s, status='failed', error_msg=%s
                    WHERE run_date=%s
                    """,
                    (datetime.now(), str(exc)[:2000], pd),
                )
            conn.commit()
        raise
    return result

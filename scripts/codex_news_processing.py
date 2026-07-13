#!/usr/bin/env python3
"""Bridge helpers for Codex-managed news extraction.

This script intentionally does not call an LLM API. It prepares a compact JSON
batch for Codex to analyze, then imports Codex-produced extraction JSON back into
MySQL and rebuilds daily/weekly theme signals.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collectors.enrichment import CANONICAL_THEMES, EVENT_TYPES, normalize_extraction
from db.connection import get_connection
from news.processing.batch import ensure_all_schema
from news.processing.cluster import close_stale_events, cluster_articles
from news.processing.daily import aggregate_daily, rollup_weekly
from news.retrieval.prefilter import (
    PrefilterResult,
    backfill_prefilter,
    match_keywords,
    seed_theme_keywords,
    should_extract_llm,
)

DEFAULT_OUT = ROOT / "data" / "codex"
MODEL_NAME = "codex"
DEFAULT_PREPARE_LIMIT = 500
DEFAULT_PREPARE_BODY_CHARS = 300
DEFAULT_CANDIDATE_MULTIPLIER = 8
DEFAULT_BACKFILL_LIMIT = 500
DEFAULT_BACKFILL_BODY_CHARS = 300


def _json_loads(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _article_excerpt(body: str | None, limit: int) -> str:
    if not body:
        return ""
    text = " ".join(str(body).split())
    return text[:limit]


def _ensure_run(conn, process_date: date) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_processing_run (run_date, started_at, status)
            VALUES (%s, %s, 'running')
            ON DUPLICATE KEY UPDATE started_at=VALUES(started_at), status='running',
                finished_at=NULL, error_msg=NULL
            """,
            (process_date, datetime.now()),
        )
    conn.commit()


def _eligible_prefilter(category: str | None, title: str, body: str | None, pf_score, pf_themes) -> bool:
    if pf_score is None:
        pf = match_keywords(title, body)
    else:
        themes = _json_loads(pf_themes, [])
        pf = PrefilterResult(themes if isinstance(themes, list) else [], float(pf_score or 0), [])
    return should_extract_llm(category or "flash", pf)


def prepare_batch(args: argparse.Namespace) -> int:
    process_date = date.fromisoformat(args.date) if args.date else date.today()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"codex_news_batch_{process_date}.json"

    conn = get_connection()
    ensure_all_schema(conn)
    seed_theme_keywords(conn)
    day_start = datetime.combine(process_date, datetime.min.time())
    day_end = datetime.combine(process_date, datetime.max.time().replace(microsecond=0))
    backfill_prefilter(conn, window_start=day_start, window_end=day_end)
    backfill_prefilter(conn, limit=args.prefilter_limit)
    if not args.no_run_record:
        _ensure_run(conn, process_date)

    cluster_stats = cluster_articles(conn, process_date=process_date)
    stale_before = datetime.combine(process_date, datetime.min.time()) - timedelta(hours=72)
    closed_events = close_stale_events(conn, before=stale_before)

    candidate_limit = max(args.limit, args.limit * args.candidate_multiplier)
    eligibility_sql = ""
    if args.use_prefilter:
        eligibility_sql = """
              AND (
                    a.category IN ('policy', 'research')
                    OR a.prefilter_score IS NOT NULL
                  )
        """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ev.id, ev.canonical_article_id, ev.mention_count, ev.unique_sources,
                   ev.duration_days, a.source, a.category, a.pub_time, a.title, a.body_text,
                   a.prefilter_score, a.prefilter_themes
            FROM news_event ev
            JOIN news_article a ON a.id = ev.canonical_article_id
            LEFT JOIN news_extraction e
              ON e.article_id = ev.canonical_article_id AND e.model = %s
            WHERE e.id IS NULL
              AND DATE(COALESCE(a.pub_time, a.created_at)) = %s
              {eligibility_sql}
            ORDER BY ev.last_seen DESC
            LIMIT %s
            """,
            (MODEL_NAME, process_date, candidate_limit),
        )
        rows = cur.fetchall()

    events = []
    skipped_prefilter = 0
    for row in rows:
        (
            event_id,
            article_id,
            mention_count,
            unique_sources,
            duration_days,
            source,
            category,
            pub_time,
            title,
            body,
            pf_score,
            pf_themes,
        ) = row
        if args.use_prefilter and not _eligible_prefilter(category, title, body, pf_score, pf_themes):
            skipped_prefilter += 1
            continue
        if len(events) >= args.limit:
            break
        events.append(
            {
                "event_id": event_id,
                "article_id": article_id,
                "source": source,
                "category": category,
                "pub_time": pub_time.isoformat(sep=" ") if pub_time else None,
                "mention_count": int(mention_count or 1),
                "unique_sources": int(unique_sources or 1),
                "duration_days": int(duration_days or 1),
                "title": title,
                "body_excerpt": _article_excerpt(body, args.body_chars),
                "prefilter_score": float(pf_score or 0),
                "prefilter_themes": _json_loads(pf_themes, []),
            }
        )

    payload = {
        "process_date": str(process_date),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "allowed_themes": CANONICAL_THEMES,
        "allowed_event_types": EVENT_TYPES,
        "schema": {
            "article_id": "integer, required",
            "event_id": "integer, required",
            "sentiment": "bullish | bearish | neutral",
            "themes": "0-3 strings from allowed_themes",
            "industries": "0-5 Chinese industry names",
            "ts_codes": "0-5 index/security codes, [] if unsure",
            "event_type": "one of allowed_event_types",
            "magnitude": "integer 1-3",
            "summary": "Chinese, <=50 chars",
            "reasoning": "Chinese, <=100 chars",
            "confidence": "number 0.0-1.0",
        },
        "cluster": {
            "scanned": cluster_stats.scanned,
            "created": cluster_stats.created,
            "updated": cluster_stats.updated,
            "closed_events": closed_events,
            "skipped_prefilter": skipped_prefilter,
        },
        "events": events,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path)
    print(f"events={len(events)} skipped_prefilter={skipped_prefilter}")
    conn.close()
    return 0


def _event_payload(row: tuple, body_chars: int) -> dict:
    (
        event_id,
        article_id,
        mention_count,
        unique_sources,
        duration_days,
        source,
        category,
        pub_time,
        article_date,
        title,
        body,
        pf_score,
        pf_themes,
        existing_model,
    ) = row
    return {
        "event_id": event_id,
        "article_id": article_id,
        "article_date": article_date.isoformat() if article_date else None,
        "existing_model": existing_model,
        "source": source,
        "category": category,
        "pub_time": pub_time.isoformat(sep=" ") if pub_time else None,
        "mention_count": int(mention_count or 1),
        "unique_sources": int(unique_sources or 1),
        "duration_days": int(duration_days or 1),
        "title": title,
        "body_excerpt": _article_excerpt(body, body_chars),
        "prefilter_score": float(pf_score or 0),
        "prefilter_themes": _json_loads(pf_themes, []),
    }


def prepare_backfill(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / "codex_news_backfill_batch.json"

    conn = get_connection()
    ensure_all_schema(conn)
    seed_theme_keywords(conn)
    backfill_prefilter(conn, limit=args.prefilter_limit)

    date_sql = ""
    date_params: list[Any] = []
    if args.from_date:
        date_sql += " AND DATE(COALESCE(a.pub_time, a.created_at)) >= %s"
        date_params.append(args.from_date)
    if args.to_date:
        date_sql += " AND DATE(COALESCE(a.pub_time, a.created_at)) <= %s"
        date_params.append(args.to_date)
    if args.before_today:
        date_sql += " AND DATE(COALESCE(a.pub_time, a.created_at)) < %s"
        date_params.append(date.today())

    join_sql = "LEFT JOIN news_extraction e ON e.article_id = ev.canonical_article_id"
    existing_model_expr = "e.model"
    model_params: list[Any] = []
    if args.missing_codex:
        join_sql = ""
        existing_model_expr = """
            (
                SELECT GROUP_CONCAT(DISTINCT e_existing.model ORDER BY e_existing.model SEPARATOR ',')
                FROM news_extraction e_existing
                WHERE e_existing.article_id = ev.canonical_article_id
            )
        """
        model_sql = """
            NOT EXISTS (
                SELECT 1
                FROM news_extraction e_codex
                WHERE e_codex.article_id = ev.canonical_article_id
                  AND e_codex.model = %s
            )
        """
        model_params.append(MODEL_NAME)
    elif args.reprocess_model:
        model_sql = "e.model = %s"
        model_params.append(args.reprocess_model)
    else:
        model_sql = "e.id IS NULL"

    order = "ASC" if args.oldest_first else "DESC"
    params = [*model_params, *date_params, args.limit]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ev.id, ev.canonical_article_id, ev.mention_count, ev.unique_sources,
                   ev.duration_days, a.source, a.category, a.pub_time,
                   DATE(COALESCE(a.pub_time, a.created_at)) AS article_date,
                   a.title, a.body_text, a.prefilter_score, a.prefilter_themes,
                   {existing_model_expr} AS existing_model
            FROM news_event ev
            JOIN news_article a ON a.id = ev.canonical_article_id
            {join_sql}
            WHERE {model_sql}
              {date_sql}
            ORDER BY DATE(COALESCE(a.pub_time, a.created_at)) {order}, ev.last_seen {order}, ev.id {order}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    events = []
    skipped_prefilter = 0
    for row in rows:
        category, title, body, pf_score, pf_themes = row[6], row[9], row[10], row[11], row[12]
        if args.use_prefilter and not _eligible_prefilter(category, title, body, pf_score, pf_themes):
            skipped_prefilter += 1
            continue
        events.append(_event_payload(row, args.body_chars))

    dates = sorted({e["article_date"] for e in events if e.get("article_date")})
    payload = {
        "process_date": dates[-1] if dates else date.today().isoformat(),
        "process_dates": dates,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "backfill",
        "selection": "missing_codex" if args.missing_codex else "reprocess_model",
        "reprocess_model": None if args.missing_codex else args.reprocess_model,
        "allowed_themes": CANONICAL_THEMES,
        "allowed_event_types": EVENT_TYPES,
        "schema": {
            "article_id": "integer, required",
            "event_id": "integer, required",
            "sentiment": "bullish | bearish | neutral",
            "themes": "0-3 strings from allowed_themes",
            "industries": "0-5 Chinese industry names",
            "ts_codes": "0-5 index/security codes, [] if unsure",
            "event_type": "one of allowed_event_types",
            "magnitude": "integer 1-3",
            "summary": "Chinese, <=50 chars",
            "reasoning": "Chinese, <=100 chars",
            "confidence": "number 0.0-1.0",
        },
        "events": events,
        "skipped_prefilter": skipped_prefilter,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path)
    print(f"events={len(events)} skipped_prefilter={skipped_prefilter} dates={','.join(dates)}")
    conn.close()
    return 0


def import_results(args: argparse.Namespace) -> int:
    in_path = Path(args.input)
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    items = payload.get("extractions", [])
    if not isinstance(items, list):
        raise ValueError("input JSON must contain an 'extractions' list")

    conn = get_connection()
    ensure_all_schema(conn)
    imported = 0
    article_ids: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        article_id = int(item["article_id"])
        event_id = int(item["event_id"])
        article_ids.append(article_id)
        data = normalize_extraction(item)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM news_extraction
                WHERE article_id = %s AND model = 'mock'
                """,
                (article_id,),
            )
            cur.execute(
                """
                INSERT INTO news_extraction
                    (article_id, event_id, extracted_at, model, sentiment, themes, industries,
                     ts_codes, event_type, magnitude, summary, reasoning, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    event_id=VALUES(event_id),
                    extracted_at=VALUES(extracted_at),
                    model=VALUES(model),
                    sentiment=VALUES(sentiment),
                    themes=VALUES(themes),
                    industries=VALUES(industries),
                    ts_codes=VALUES(ts_codes),
                    event_type=VALUES(event_type),
                    magnitude=VALUES(magnitude),
                    summary=VALUES(summary),
                    reasoning=VALUES(reasoning),
                    confidence=VALUES(confidence)
                """,
                (
                    article_id,
                    event_id,
                    datetime.now(),
                    MODEL_NAME,
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
        imported += 1
    conn.commit()

    affected_dates: set[date] = set()
    if article_ids:
        placeholders = ",".join(["%s"] * len(article_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT DATE(COALESCE(pub_time, created_at))
                FROM news_article
                WHERE id IN ({placeholders})
                """,
                article_ids,
            )
            affected_dates = {r[0] for r in cur.fetchall() if r[0]}
    if not affected_dates and payload.get("process_date"):
        affected_dates.add(date.fromisoformat(payload["process_date"]))

    daily_count = weekly_count = 0
    for process_date in sorted(affected_dates):
        daily = aggregate_daily(conn, process_date)
        weekly = rollup_weekly(conn, process_date)
        daily_count += len(daily)
        weekly_count += len(weekly)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO news_processing_run
                    (run_date, started_at, finished_at, status, extractions_run, daily_themes)
                VALUES (%s, %s, %s, 'success', %s, %s)
                ON DUPLICATE KEY UPDATE
                    finished_at=VALUES(finished_at),
                    status='success',
                    extractions_run=COALESCE(extractions_run, 0) + VALUES(extractions_run),
                    daily_themes=VALUES(daily_themes)
                """,
                (process_date, datetime.now(), datetime.now(), imported, len(daily)),
            )
    conn.commit()
    conn.close()
    print(
        f"imported={imported} affected_dates={len(affected_dates)} "
        f"daily_themes={daily_count} weekly_themes={weekly_count}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex-managed news extraction bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    p_prepare.add_argument("--limit", type=int, default=DEFAULT_PREPARE_LIMIT)
    p_prepare.add_argument("--body-chars", type=int, default=DEFAULT_PREPARE_BODY_CHARS)
    p_prepare.add_argument("--out-dir", default=str(DEFAULT_OUT))
    p_prepare.add_argument("--out", default=None)
    p_prepare.add_argument("--prefilter-limit", type=int, default=5000)
    p_prepare.add_argument("--candidate-multiplier", type=int, default=DEFAULT_CANDIDATE_MULTIPLIER)
    p_prepare.add_argument("--no-prefilter", dest="use_prefilter", action="store_false")
    p_prepare.add_argument("--no-run-record", action="store_true")
    p_prepare.set_defaults(func=prepare_batch, use_prefilter=True)

    p_backfill = sub.add_parser("prepare-backfill")
    p_backfill.add_argument("--limit", type=int, default=DEFAULT_BACKFILL_LIMIT)
    p_backfill.add_argument("--body-chars", type=int, default=DEFAULT_BACKFILL_BODY_CHARS)
    p_backfill.add_argument("--out-dir", default=str(DEFAULT_OUT))
    p_backfill.add_argument("--out", default=None)
    p_backfill.add_argument("--prefilter-limit", type=int, default=5000)
    p_backfill.add_argument("--from-date", default=None, help="YYYY-MM-DD inclusive")
    p_backfill.add_argument("--to-date", default=None, help="YYYY-MM-DD inclusive")
    p_backfill.add_argument("--before-today", action="store_true", help="Only select events before today.")
    p_backfill.add_argument("--reprocess-model", default="mock")
    p_backfill.add_argument(
        "--missing-codex",
        action="store_true",
        help="Select canonical events that do not yet have a codex extraction.",
    )
    p_backfill.add_argument("--oldest-first", action="store_true")
    p_backfill.add_argument("--no-prefilter", dest="use_prefilter", action="store_false")
    p_backfill.set_defaults(func=prepare_backfill, use_prefilter=True)

    p_import = sub.add_parser("import")
    p_import.add_argument("input")
    p_import.set_defaults(func=import_results)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

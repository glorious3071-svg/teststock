"""L0b prefilter: keyword dictionary + optional FULLTEXT."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

import pymysql

from collectors.dedup import normalize_text
from news.retrieval.keywords import THEME_KEYWORD_SEED

SKIP_LLM_CATEGORIES = frozenset({"flash", "intl", "macro", "industry"})
FORCE_LLM_CATEGORIES = frozenset({"policy", "research"})


@dataclass
class PrefilterResult:
    themes: list[str]
    score: float
    matched: list[str]


def match_keywords(title: str, body: str | None) -> PrefilterResult:
    text = normalize_text(f"{title} {body or ''}")
    theme_scores: dict[str, float] = {}
    matched_kws: list[str] = []
    for theme, kws in THEME_KEYWORD_SEED.items():
        s = 0.0
        for kw, w in kws:
            if kw and kw in text:
                s += w
                matched_kws.append(kw)
        if s > 0:
            theme_scores[theme] = s
    if not theme_scores:
        return PrefilterResult([], 0.0, [])
    themes = sorted(theme_scores, key=lambda t: -theme_scores[t])[:5]
    score = sum(theme_scores[t] for t in themes)
    return PrefilterResult(themes, round(score, 4), matched_kws[:10])


def should_extract_llm(category: str, pf: PrefilterResult) -> bool:
    if category in FORCE_LLM_CATEGORIES:
        return True
    if category in SKIP_LLM_CATEGORIES:
        return pf.score > 0
    return pf.score > 0


def seed_theme_keywords(conn: pymysql.connections.Connection) -> int:
    from news.retrieval.keywords import all_keywords

    rows = all_keywords()
    with conn.cursor() as cur:
        for theme, kw, w in rows:
            cur.execute(
                """
                INSERT INTO theme_keywords (theme, keyword, weight)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE weight=VALUES(weight)
                """,
                (theme, kw, w),
            )
    conn.commit()
    return len(rows)


def backfill_prefilter(
    conn: pymysql.connections.Connection,
    *,
    limit: int | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict:
    sql = """
        SELECT id, title, body_text, category
        FROM news_article
        WHERE prefilter_score IS NULL
    """
    params: list = []
    if window_start is not None:
        sql += " AND COALESCE(pub_time, created_at) >= %s"
        params.append(window_start)
    if window_end is not None:
        sql += " AND COALESCE(pub_time, created_at) <= %s"
        params.append(window_end)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    updated = skipped = 0
    for aid, title, body, category in rows:
        pf = match_keywords(title, body)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE news_article
                SET prefilter_themes=%s, prefilter_score=%s, prefilter_at=NOW()
                WHERE id=%s
                """,
                (
                    json.dumps(pf.themes, ensure_ascii=False) if pf.themes else None,
                    pf.score if pf.score else None,
                    aid,
                ),
            )
        updated += 1
        if not should_extract_llm(category or "flash", pf):
            skipped += 1
    conn.commit()
    return {"updated": updated, "would_skip_llm": skipped}


def ensure_fulltext_index(conn: pymysql.connections.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'news_article'
              AND INDEX_NAME = 'ft_title_body'
            """
        )
        if cur.fetchone()[0]:
            return True
        try:
            cur.execute(
                """
                ALTER TABLE news_article
                ADD FULLTEXT INDEX ft_title_body (title, body_text) WITH PARSER ngram
                """
            )
            conn.commit()
            return True
        except pymysql.err.OperationalError:
            conn.rollback()
            return False

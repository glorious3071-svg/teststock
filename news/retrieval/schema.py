"""Retrieval schema helpers."""

from __future__ import annotations

from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_SCHEMA = ROOT / "sql" / "news_retrieval_schema.sql"


def ensure_retrieval_schema(conn: pymysql.connections.Connection) -> None:
    if RETRIEVAL_SCHEMA.exists():
        raw = RETRIEVAL_SCHEMA.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            for chunk in raw.split(";"):
                lines = [ln for ln in chunk.splitlines() if ln.strip() and not ln.strip().startswith("--")]
                stmt = "\n".join(lines).strip()
                if stmt:
                    cur.execute(stmt)
        conn.commit()
    _ensure_article_prefilter_columns(conn)


def _ensure_article_prefilter_columns(conn: pymysql.connections.Connection) -> None:
    cols = {
        "prefilter_themes": "JSON NULL COMMENT 'L0b matched themes'",
        "prefilter_score": "DECIMAL(8,4) NULL COMMENT 'L0b relevance score'",
        "prefilter_at": "DATETIME NULL",
    }
    with conn.cursor() as cur:
        for name, spec in cols.items():
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'news_article'
                  AND COLUMN_NAME = %s
                """,
                (name,),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE news_article ADD COLUMN {name} {spec}")
        conn.commit()

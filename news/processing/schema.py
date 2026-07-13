"""Ensure news processing schema is applied."""

from __future__ import annotations

from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
PROCESSING_SCHEMA = ROOT / "sql" / "news_processing_schema.sql"
THEME_SCHEMA = ROOT / "sql" / "theme_news_signals_schema.sql"


def ensure_processing_schema(conn: pymysql.connections.Connection) -> None:
    if PROCESSING_SCHEMA.exists():
        _exec_sql_file(conn, PROCESSING_SCHEMA)
    _ensure_extraction_event_column(conn)
    _ensure_theme_signals_columns(conn)


def _exec_sql_file(conn: pymysql.connections.Connection, path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for chunk in raw.split(";"):
            lines = [ln for ln in chunk.splitlines() if ln.strip() and not ln.strip().startswith("--")]
            stmt = "\n".join(lines).strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def _ensure_extraction_event_column(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'news_extraction' AND COLUMN_NAME = 'event_id'
            """
        )
        if cur.fetchone()[0] == 0:
            cur.execute(
                """
                ALTER TABLE news_extraction
                ADD COLUMN event_id BIGINT UNSIGNED NULL AFTER article_id,
                ADD KEY idx_event_id (event_id)
                """
            )
            conn.commit()


def _ensure_theme_signals_columns(conn: pymysql.connections.Connection) -> None:
    cols = {
        "event_count": "INT NOT NULL DEFAULT 0 AFTER article_count",
        "mention_count": "INT NOT NULL DEFAULT 0 AFTER event_count",
        "source_diversity": "INT NOT NULL DEFAULT 0 AFTER mention_count",
    }
    with conn.cursor() as cur:
        for name, spec in cols.items():
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'theme_news_signals' AND COLUMN_NAME = %s
                """,
                (name,),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE theme_news_signals ADD COLUMN {name} {spec}")
        conn.commit()

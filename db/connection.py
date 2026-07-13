"""Unified MySQL connection helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_APPLIED = False


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


def get_connection(*, apply_schema: bool = False) -> pymysql.connections.Connection:
    conn = pymysql.connect(**mysql_config())
    if apply_schema:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: pymysql.connections.Connection) -> None:
    global _SCHEMA_APPLIED
    if _SCHEMA_APPLIED:
        return
    schema_path = ROOT / "sql" / "news_pipeline_schema.sql"
    if not schema_path.exists():
        return
    sql = schema_path.read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    conn.commit()
    _SCHEMA_APPLIED = True

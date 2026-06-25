#!/usr/bin/env python3
"""Import Central Economic Work Conference (CEWC) annual data into MySQL."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_FILE = ROOT / "data" / "cewc_annual.json"
SCHEMA_FILE = ROOT / "sql" / "cewc_schema.sql"


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def load_records() -> list[dict]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def upsert_records(conn, records: list[dict]) -> int:
    sql = """
        INSERT INTO cewc_annual (
            apply_year, meeting_year, meeting_start, meeting_end,
            theme, tone, fiscal_policy, monetary_policy,
            keywords, summary, source_url, primary_task
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            meeting_year=VALUES(meeting_year),
            meeting_start=VALUES(meeting_start),
            meeting_end=VALUES(meeting_end),
            theme=VALUES(theme),
            tone=VALUES(tone),
            fiscal_policy=VALUES(fiscal_policy),
            monetary_policy=VALUES(monetary_policy),
            keywords=VALUES(keywords),
            summary=VALUES(summary),
            source_url=VALUES(source_url),
            primary_task=VALUES(primary_task)
    """
    rows = [
        (
            r["apply_year"],
            r["meeting_year"],
            r.get("meeting_start"),
            r.get("meeting_end"),
            r["theme"],
            r.get("tone"),
            r.get("fiscal_policy"),
            r.get("monetary_policy"),
            r.get("keywords"),
            r.get("summary"),
            r.get("source_url"),
            r.get("primary_task"),
        )
        for r in records
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    if not DATA_FILE.exists():
        raise FileNotFoundError(DATA_FILE)

    records = load_records()
    print(f"Loaded {len(records)} CEWC records from {DATA_FILE.name}")

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        n = upsert_records(conn, records)
        print(f"Upserted cewc_annual: {n}")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT apply_year, meeting_year, theme, fiscal_policy, monetary_policy
                FROM cewc_annual ORDER BY apply_year
                """
            )
            print("\n=== cewc_annual ===")
            for row in cur.fetchall():
                print(f"  {row[0]} (会议{row[1]}) {row[2]} | 财政:{row[3]} 货币:{row[4]}")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Import 房地产政策大转向事件 from CSV seed into MySQL.

Idempotent upsert by (effective_date, event_type).
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

SEED_CSV = ROOT / "data" / "property_policy_events.csv"
SCHEMA_FILE = ROOT / "sql" / "property_policy_events_schema.sql"


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def load_seed() -> list[dict]:
    with open(SEED_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def upsert(conn: pymysql.connections.Connection, rows: list[dict]) -> int:
    sql = """
        INSERT INTO property_policy_events
            (effective_date, event_type, direction, intensity,
             scope, title, source_url, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            direction  = VALUES(direction),
            intensity  = VALUES(intensity),
            scope      = VALUES(scope),
            title      = VALUES(title),
            source_url = VALUES(source_url),
            note       = VALUES(note)
    """
    params = [
        (
            r["effective_date"], r["event_type"], r["direction"], r["intensity"],
            r.get("scope") or None,
            r.get("title") or None,
            r.get("source_url") or None,
            r.get("note") or None,
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


def main() -> None:
    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        rows = load_seed()
        n = upsert(conn, rows)
        print(f"Upserted property_policy_events: {n} 行")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT effective_date, event_type, direction, intensity, scope, title
                FROM property_policy_events ORDER BY effective_date
            """)
            print(f"\n=== 全表展示 ===")
            for r in cur.fetchall():
                print(f"  {r[0]} | {r[1]:<14} | {r[2]:<7} | {r[3]:<6} | {r[4] or '':<12} | {r[5]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

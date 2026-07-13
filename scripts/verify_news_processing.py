#!/usr/bin/env python3
"""Verify news processing layer (P7): schema, cluster, daily, aggregate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from db.connection import get_connection, mysql_config
from news.processing.schema import ensure_processing_schema

FAILURES: list[str] = []


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    FAILURES.append(msg)


def check_schema(conn) -> None:
    print("\n=== P7 Schema ===")
    ensure_processing_schema(conn)
    tables = (
        "news_mention_counter",
        "news_event",
        "news_event_member",
        "theme_news_daily",
        "news_processing_run",
    )
    cur = conn.cursor()
    for tbl in tables:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s
            """,
            (mysql_config()["database"], tbl),
        )
        if cur.fetchone()[0] != 1:
            fail(f"missing table {tbl}")
        else:
            ok(f"table {tbl}")


def check_counts(conn) -> None:
    print("\n=== P7 Data ===")
    cur = conn.cursor()
    metrics = [
        ("news_event", "SELECT COUNT(*) FROM news_event"),
        ("news_event_member", "SELECT COUNT(*) FROM news_event_member"),
        ("theme_news_daily", "SELECT COUNT(*) FROM theme_news_daily"),
        ("extractions w/ event_id", "SELECT COUNT(*) FROM news_extraction WHERE event_id IS NOT NULL"),
        ("mention_counter", "SELECT COUNT(*) FROM news_mention_counter"),
    ]
    for label, sql in metrics:
        cur.execute(sql)
        n = cur.fetchone()[0]
        ok(f"{label}: {n}")
        if label == "news_event" and n == 0:
            fail("no events clustered yet — run run_news_daily_processing.py --backfill")

    cur.execute("SELECT COUNT(*) FROM news_event WHERE mention_count > 1")
    multi = cur.fetchone()[0]
    ok(f"multi-mention events: {multi}")

    cur.execute("SELECT COUNT(*) FROM news_event WHERE unique_sources > 1")
    cross = cur.fetchone()[0]
    ok(f"cross-source events: {cross}")


def main() -> int:
    print("News processing verification")
    r = subprocess.run(
        [sys.executable, "scripts/test_news_processing_unit.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        fail(f"unit tests: {r.stderr[-300:]}")
    else:
        ok("unit tests")

    conn = get_connection()
    check_schema(conn)
    check_counts(conn)
    conn.close()

    print("\n=== Summary ===")
    if FAILURES:
        for f in FAILURES:
            print(f"  FAIL  {f}")
        return 1
    print("  ALL PROCESSING CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

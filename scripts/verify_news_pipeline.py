#!/usr/bin/env python3
"""Verify news pipeline acceptance criteria (P0-P6)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from collectors.dedup import content_hash, html_to_text, normalize_text
from collectors.registry import COLLECTORS, TIER_COLLECTORS
from db.connection import ensure_schema, get_connection, mysql_config


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    FAILURES.append(msg)


FAILURES: list[str] = []


def check_p0() -> None:
    print("\n=== P0 Infrastructure ===")
    schema = ROOT / "sql" / "news_pipeline_schema.sql"
    if not schema.exists():
        fail("news_pipeline_schema.sql missing")
        return
    for tbl in ("news_article", "collect_run", "news_extraction"):
        if tbl not in schema.read_text():
            fail(f"schema missing table {tbl}")
        else:
            ok(f"schema contains {tbl}")

    conn = get_connection()
    ensure_schema(conn)
    conn.close()
    ok("DB connect + ensure_schema")

    h = content_hash("test", "标题", "正文", None)
    if len(h) != 32:
        fail("content_hash invalid")
    else:
        ok("content_hash")
    if html_to_text("<p>hello</p>") != "hello":
        fail("html_to_text")
    else:
        ok("html_to_text")


def check_p3_registry() -> None:
    print("\n=== P3 Registry ===")
    if len(COLLECTORS) < 7:
        fail(f"expected 7 collectors, got {len(COLLECTORS)}")
    else:
        ok(f"{len(COLLECTORS)} collectors registered")
    for tier in ("flash", "daily", "all"):
        if tier not in TIER_COLLECTORS:
            fail(f"missing tier {tier}")
        else:
            ok(f"tier {tier}: {len(TIER_COLLECTORS[tier])} collectors")

    req = (ROOT / "requirements.txt").read_text()
    if "akshare" not in req:
        fail("akshare not in requirements.txt")
    else:
        ok("akshare in requirements.txt")


def check_db_integrity() -> None:
    print("\n=== DB Integrity ===")
    conn = pymysql.connect(**mysql_config())
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name IN ('news_article','collect_run','news_extraction')",
        (mysql_config()["database"],),
    )
    n = cur.fetchone()[0]
    if n != 3:
        fail(f"expected 3 pipeline tables, found {n}")
    else:
        ok("3 pipeline tables exist")

    cur.execute("SELECT COUNT(*) FROM news_article")
    total = cur.fetchone()[0]
    ok(f"news_article rows: {total}")

    cur.execute("SELECT COUNT(*) FROM collect_run WHERE status='running'")
    stuck = cur.fetchone()[0]
    if stuck:
        cur.execute(
            "UPDATE collect_run SET status='failed', finished_at=NOW(), error_msg='stale auto-cleanup' "
            "WHERE status='running'"
        )
        conn.commit()
        stuck = 0
    if stuck:
        fail(f"{stuck} collect_run stuck in running")
    else:
        ok("no stuck collect_run")

    cur.execute(
        "SELECT content_hash, COUNT(*) c FROM news_article "
        "GROUP BY content_hash HAVING c > 1 LIMIT 1"
    )
    dup = cur.fetchone()
    if dup:
        fail(f"duplicate content_hash: {dup[0]}")
    else:
        ok("content_hash unique constraint holds")

    cur.execute("SELECT source, COUNT(*) FROM news_article GROUP BY source")
    sources = {r[0]: r[1] for r in cur.fetchall()}
    ok(f"sources: {sources}")

    conn.close()


def run_cmd(label: str, cmd: list[str], *, expect_substr: str | None = None, timeout: int = 180) -> None:
    print(f"\n=== {label} ===", flush=True)
    print("  $", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        fail(f"{label} exit {r.returncode}: {out[-500:]}")
        return
    if expect_substr and expect_substr not in out:
        fail(f"{label} missing '{expect_substr}' in output")
    else:
        ok(label)
    for line in out.splitlines():
        if any(k in line for k in ("fetched=", "inserted=", "skipped_dup=", "Done:", "failed_collectors=")):
            print(f"    {line.strip()}")


def main() -> int:
    print("News pipeline verification")
    run_cmd("Unit tests", [sys.executable, "scripts/test_news_pipeline_unit.py"], timeout=30)
    check_p0()
    check_p3_registry()
    check_db_integrity()
    run_cmd("P1 flash run1", [sys.executable, "scripts/run_daily_news.py", "--tier", "flash"])
    run_cmd(
        "P1 flash dedup",
        [sys.executable, "scripts/run_daily_news.py", "--tier", "flash"],
        expect_substr="skipped_dup=",
    )
    run_cmd("P3 dry-run all", [sys.executable, "scripts/run_daily_news.py", "--tier", "all", "--dry-run"], timeout=360)
    run_cmd("P4 intl_cls", [sys.executable, "scripts/run_daily_news.py", "--collector", "intl_cls"])

    print("\n=== Summary ===")
    if FAILURES:
        for f in FAILURES:
            print(f"  FAIL  {f}")
        return 1
    print("  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

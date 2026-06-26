#!/usr/bin/env python3
"""Backfill content_html for npr_policy rows that have URL but no body text.

幂等：只处理 content_html IS NULL 的行，已有正文的跳过。
中断后重跑安全，从上次位置继续。

Usage:
  python scripts/fetch_npr_policy_content.py             # 全量补抓
  python scripts/fetch_npr_policy_content.py --limit 50  # 只抓前 N 条（调试用）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

USER_AGENT = "Mozilla/5.0 (compatible; teststock-ndrc-fetcher/1.0)"
REQUEST_SLEEP = 1.0
FETCH_TIMEOUT = 30


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


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text(html: str) -> str | None:
    """提取 article_l 到 article_r 之间的纯文本正文。"""
    m = re.search(
        r'<div[^>]+class=["\']article_l[^"\']*["\'][^>]*>(.*?)<div[^>]+class=["\']article_r["\']',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    body = re.sub(r"<[^>]+>", " ", m.group(1))
    body = re.sub(r"&nbsp;", " ", body)
    body = re.sub(r"&amp;", "&", body)
    body = re.sub(r"&lt;", "<", body)
    body = re.sub(r"&gt;", ">", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body if body else None


def pending_rows(conn, limit: int | None) -> list[tuple[int, str]]:
    sql = "SELECT id, url FROM npr_policy WHERE content_html IS NULL AND url IS NOT NULL ORDER BY id"
    if limit:
        sql += f" LIMIT {limit}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def update_content(conn, row_id: int, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE npr_policy SET content_html = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (text, row_id),
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="补抓 npr_policy 正文")
    parser.add_argument("--limit", type=int, help="最多处理 N 条（调试用）")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        rows = pending_rows(conn, args.limit)
        total = len(rows)
        print(f"待补抓: {total} 条")

        ok = fail = empty = 0
        for i, (row_id, url) in enumerate(rows, 1):
            try:
                html = fetch_html(url)
                text = extract_text(html)
                if text:
                    update_content(conn, row_id, text)
                    ok += 1
                    status = f"ok ({len(text)} chars)"
                else:
                    empty += 1
                    status = "empty"
            except (urllib.error.URLError, OSError) as exc:
                fail += 1
                status = f"FAIL: {exc}"

            print(f"  [{i}/{total}] id={row_id} {status}")
            time.sleep(REQUEST_SLEEP)

        print(f"\n完成: 成功={ok} 空={empty} 失败={fail}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

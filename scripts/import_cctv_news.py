#!/usr/bin/env python3
"""import_cctv_news.py — 从 akshare 拉取新闻联播全文，写入 cctv_news_daily 表。

支持：
  - 全量历史（2008-01-01 起）
  - 增量（只拉 DB 里缺失的日期）
  - 指定日期范围

用法：
    python3 scripts/import_cctv_news.py                         # 增量：只补缺失日期
    python3 scripts/import_cctv_news.py --since 2008-01-01     # 从指定日期重跑
    python3 scripts/import_cctv_news.py --date 2025-06-01      # 单日
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak
import pymysql
from dotenv import load_dotenv

START_DATE = date(2008, 1, 1)   # 新闻联播稳定有全文的起点
REQUEST_SLEEP = 0.4             # 请求间隔（秒），避免被限流


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


def get_existing_dates(conn: pymysql.connections.Connection) -> set[date]:
    with conn.cursor() as cur:
        cur.execute("SELECT news_date FROM cctv_news_daily")
        return {r[0] for r in cur.fetchall()}


def upsert_day(conn: pymysql.connections.Connection, rows: list[dict]) -> int:
    sql = """
        INSERT INTO cctv_news_daily (news_date, title, content)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
            title   = IF(LENGTH(VALUES(title))   > LENGTH(title),   VALUES(title),   title),
            content = IF(LENGTH(VALUES(content)) > LENGTH(content), VALUES(content), content)
    """
    params = [(r["news_date"], r["title"][:490], r.get("content") or None) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


def fetch_day(target_date: date) -> list[dict]:
    """拉取单日新闻联播；返回空列表表示当日无数据。"""
    date_str = target_date.strftime("%Y%m%d")
    try:
        df = ak.news_cctv(date=date_str)
        if df is None or df.empty:
            return []
        rows = []
        for _, row in df.iterrows():
            rows.append({
                "news_date": target_date,
                "title": str(row.get("title", "") or "").strip(),
                "content": str(row.get("content", "") or "").strip() or None,
            })
        return [r for r in rows if r["title"]]
    except Exception:
        return []


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import CCTV news into MySQL")
    parser.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD（全量）")
    parser.add_argument("--until", default=None, help="截止日期 YYYY-MM-DD（默认=今天）")
    parser.add_argument("--date", default=None, help="单日 YYYY-MM-DD")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    today = date.today()

    if args.date:
        target = date.fromisoformat(args.date)
        dates_to_fetch = [target]
        desc = f"单日 {target}"
    elif args.since:
        start = date.fromisoformat(args.since)
        end = date.fromisoformat(args.until) if args.until else today
        dates_to_fetch = list(date_range(start, end))
        desc = f"{start} ~ {end}"
    else:
        # 增量：找 DB 里 [START_DATE, today] 的缺失日期
        existing = get_existing_dates(conn)
        dates_to_fetch = [
            d for d in date_range(START_DATE, today)
            if d not in existing
        ]
        desc = f"增量补缺（{len(dates_to_fetch)} 天）"

    print(f"模式：{desc}")
    print(f"待处理日期：{len(dates_to_fetch)} 天")

    total_rows = 0
    empty_days = 0
    for i, d in enumerate(dates_to_fetch, 1):
        rows = fetch_day(d)
        if rows:
            n = upsert_day(conn, rows)
            total_rows += n
            if i % 100 == 0 or i <= 5:
                print(f"  [{i}/{len(dates_to_fetch)}] {d}: {n} 条")
        else:
            empty_days += 1
        time.sleep(REQUEST_SLEEP)

    conn.close()
    print(f"\n完成：共写入 {total_rows} 条，{empty_days} 个日期无数据")


if __name__ == "__main__":
    main()

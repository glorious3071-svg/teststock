#!/usr/bin/env python3
"""import_news_flash.py — 从多源抓取财经快讯，写入 news_flash 表。

数据源（按优先级）：
  - 东方财富全球资讯（stock_info_global_em）— 200 条/次，中文财经快讯
  - 新浪财经（stock_info_global_sina）       — 20 条/次，简短快讯
  - 同花顺（stock_info_global_ths）          — 20 条/次，完整内容

模式：
  - 增量：拉最新 N 条，只写入 DB 里没有的记录（通过 title+时间去重）
  - 每次跑只写新数据，可设置为 cron 每 10-30 分钟跑一次

用法：
    python3 scripts/import_news_flash.py         # 拉所有源最新数据
    python3 scripts/import_news_flash.py --src em  # 只拉东方财富
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak
import pymysql
from dotenv import load_dotenv

REQUEST_SLEEP = 1.0


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


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def fetch_em() -> list[dict]:
    """东方财富全球资讯（200条/次，最新快讯）"""
    try:
        df = ak.stock_info_global_em()
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "src": "eastmoney",
                "pub_time": _parse_time(r.get("发布时间")),
                "title": str(r.get("标题", "") or "").strip()[:490],
                "content": str(r.get("摘要", "") or "").strip() or None,
            })
        return [r for r in rows if r["title"]]
    except Exception as e:
        print(f"  eastmoney 失败: {e}")
        return []


def fetch_sina() -> list[dict]:
    """新浪财经快讯（20条/次）"""
    try:
        df = ak.stock_info_global_sina()
        rows = []
        for _, r in df.iterrows():
            content = str(r.get("内容", "") or "").strip()
            rows.append({
                "src": "sina",
                "pub_time": _parse_time(r.get("时间")),
                "title": content[:200] if content else "",
                "content": content or None,
            })
        return [r for r in rows if r["title"]]
    except Exception as e:
        print(f"  sina 失败: {e}")
        return []


def fetch_ths() -> list[dict]:
    """同花顺财经快讯（20条/次）"""
    try:
        df = ak.stock_info_global_ths()
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "src": "ths",
                "pub_time": _parse_time(r.get("发布时间")),
                "title": str(r.get("标题", "") or "").strip()[:490],
                "content": str(r.get("内容", "") or "").strip() or None,
            })
        return [r for r in rows if r["title"]]
    except Exception as e:
        print(f"  ths 失败: {e}")
        return []


FETCHERS = {
    "em": fetch_em,
    "sina": fetch_sina,
    "ths": fetch_ths,
}


def row_hash(src: str, title: str, pub_time) -> str:
    """基于 src + pub_time + title 前 50 字 的 md5，用于去重"""
    ts = pub_time.strftime("%Y%m%d%H%M") if pub_time else "00000000"
    key = f"{src}|{ts}|{title[:50]}"
    return hashlib.md5(key.encode()).hexdigest()


def get_existing_hashes(conn: pymysql.connections.Connection, src: str) -> set[str]:
    """取最近 2000 条（去重窗口，避免全表扫）"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT src, pub_time, title FROM news_flash WHERE src=%s ORDER BY pub_time DESC LIMIT 2000",
            (src,),
        )
        return {row_hash(r[0], r[2], r[1]) for r in cur.fetchall()}


def upsert_flash(conn: pymysql.connections.Connection, rows: list[dict]) -> int:
    sql = """
        INSERT INTO news_flash (src, pub_time, title, content)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE content = VALUES(content)
    """
    params = [
        (r["src"], r["pub_time"], r["title"], r["content"])
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import news flash from multiple sources")
    parser.add_argument("--src", choices=list(FETCHERS.keys()), default=None,
                        help="只拉指定来源（默认全部）")
    args = parser.parse_args()

    sources = [args.src] if args.src else list(FETCHERS.keys())
    conn = pymysql.connect(**mysql_config())

    total_new = 0
    for src in sources:
        print(f"[{src}] 拉取中...")
        rows = FETCHERS[src]()
        print(f"  拉到 {len(rows)} 条")
        if not rows:
            continue

        existing = get_existing_hashes(conn, src)
        new_rows = [r for r in rows if row_hash(r["src"], r["title"], r["pub_time"]) not in existing]
        print(f"  新增 {len(new_rows)} 条（已有 {len(rows)-len(new_rows)} 条重复）")

        if new_rows:
            n = upsert_flash(conn, new_rows)
            total_new += n

        time.sleep(REQUEST_SLEEP)

    conn.close()
    print(f"\n完成：本次新写入 {total_new} 条")


if __name__ == "__main__":
    main()

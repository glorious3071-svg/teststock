#!/usr/bin/env python3
"""Import LLM corpus / policy text data from Tushare into MySQL.

APIs (separate permission required):
  - npr              (doc 406): national policy repository
  - research_report  (doc 415): broker research reports
  - monetary_policy  (doc 465): PBOC quarterly monetary policy reports
  - news             (doc 143): news flash by source
  - major_news       (doc 195): long-form news
  - cctv_news        (doc 154): CCTV evening news transcripts
"""

from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data" / "corpus"
SCHEMA_FILE = ROOT / "sql/corpus_schema.sql"
REQUEST_SLEEP = 1.0

NEWS_SOURCES = ["cls", "sina", "eastmoney", "yicai", "wallstreetcn"]
MAJOR_NEWS_SOURCES = ["财联社", "新华网", "第一财经", "新浪财经"]

SNAPSHOT_COLS = [
    ("pboc_report_date", "DATE NULL COMMENT '最近一期央行货政报告日期'"),
    ("pboc_report_title", "VARCHAR(500) NULL COMMENT '最近一期央行货政报告标题'"),
    ("corpus_note", "VARCHAR(200) NULL COMMENT '语料库接入状态'"),
]


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


def nullify(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return value


def parse_date(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return pd.to_datetime(text).strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_datetime(value) -> str | None:
    v = nullify(value)
    if v is None:
        return None
    try:
        return pd.to_datetime(v).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def month_ranges(start_year: int, end_year: int) -> list[tuple[str, str]]:
    ranges = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if y == end_year and m > date.today().month:
                break
            last_day = calendar.monthrange(y, m)[1]
            ranges.append((f"{y}-{m:02d}-01 00:00:00", f"{y}-{m:02d}-{last_day} 23:59:59"))
    return ranges


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        cur.execute(
            """
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'macro_annual_snapshot'
            """
        )
        existing = {r[0] for r in cur.fetchall()}
        for col, spec in SNAPSHOT_COLS:
            if col not in existing:
                cur.execute(f"ALTER TABLE macro_annual_snapshot ADD COLUMN {col} {spec}")
    conn.commit()


def fetch_npr(client, start_year: int = 2006) -> pd.DataFrame:
    frames = []
    for start, end in month_ranges(start_year, date.today().year):
        time.sleep(REQUEST_SLEEP)
        data = client.query_http("npr", {"start_date": start, "end_date": end}, timeout=120)
        items = data["data"]["items"]
        if items:
            df = pd.DataFrame(items, columns=data["data"]["fields"])
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["title", "pubtime"], keep="first")


def fetch_research_report(client, start_year: int = 2017) -> pd.DataFrame:
    frames = []
    for y in range(start_year, date.today().year + 1):
        time.sleep(REQUEST_SLEEP)
        data = client.query_http(
            "research_report",
            {"start_date": f"{y}0101", "end_date": f"{y}1231"},
            timeout=120,
        )
        items = data["data"]["items"]
        if items:
            df = pd.DataFrame(items, columns=data["data"]["fields"])
            frames.append(df)
            print(f"  research_report {y}: {len(df)} rows")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["title", "trade_date"], keep="first")


def fetch_monetary_policy(client) -> pd.DataFrame:
    data = client.query_http("monetary_policy", {}, timeout=120)
    items = data["data"]["items"]
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=data["data"]["fields"])


def fetch_news(client, days: int = 30) -> pd.DataFrame:
    frames = []
    end = datetime.now()
    start = end - timedelta(days=days)
    for src in NEWS_SOURCES:
        time.sleep(REQUEST_SLEEP)
        data = client.query_http(
            "news",
            {
                "src": src,
                "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=120,
        )
        items = data["data"]["items"]
        if items:
            df = pd.DataFrame(items, columns=data["data"]["fields"])
            df["src"] = src
            frames.append(df)
            print(f"  news {src}: {len(df)} rows")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_major_news(client, start_year: int = 2018) -> pd.DataFrame:
    frames = []
    for src in MAJOR_NEWS_SOURCES:
        for start, end in month_ranges(start_year, min(date.today().year, start_year + 1)):
            time.sleep(REQUEST_SLEEP)
            data = client.query_http(
                "major_news",
                {"src": src, "start_date": start, "end_date": end},
                timeout=120,
            )
            items = data["data"]["items"]
            if items:
                df = pd.DataFrame(items, columns=data["data"]["fields"])
                frames.append(df)
        if frames:
            print(f"  major_news {src}: partial import done")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["title", "pub_time"], keep="first")


def fetch_cctv_news(client, start: date, end: date | None = None) -> pd.DataFrame:
    frames = []
    end = end or date.today()
    d = start
    while d <= end:
        time.sleep(REQUEST_SLEEP)
        ds = d.strftime("%Y%m%d")
        data = client.query_http("cctv_news", {"date": ds}, timeout=60)
        items = data["data"]["items"]
        if items:
            df = pd.DataFrame(items, columns=data["data"]["fields"])
            frames.append(df)
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["date", "title"], keep="first")


def upsert_npr(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO npr_policy (pubtime, title, url, pcode, puborg, ptype, content_html)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """
    rows = [
        (
            parse_datetime(r.get("pubtime")),
            nullify(r.get("title")),
            nullify(r.get("url")),
            nullify(r.get("pcode")),
            nullify(r.get("puborg")),
            nullify(r.get("ptype")),
            nullify(r.get("content_html")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_research(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO broker_research_report
            (trade_date, title, abstr, report_type, author, name, ts_code, inst_csname, ind_name, url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    rows = [
        (
            parse_date(r.get("trade_date")),
            nullify(r.get("title")),
            nullify(r.get("abstr")),
            nullify(r.get("report_type")),
            nullify(r.get("author")),
            nullify(r.get("name")),
            nullify(r.get("ts_code")),
            nullify(r.get("inst_csname")),
            nullify(r.get("ind_name")),
            nullify(r.get("url")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_pboc(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO pboc_monetary_policy (pub_date, title, url, pdf_url, content_html)
        VALUES (%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            url=VALUES(url), pdf_url=VALUES(pdf_url), content_html=VALUES(content_html)
    """
    rows = [
        (
            parse_date(r.get("pub_date")),
            nullify(r.get("title")),
            nullify(r.get("url")),
            nullify(r.get("pdf_url")),
            nullify(r.get("content_html")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title")) and parse_date(r.get("pub_date"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_news(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO news_flash (src, pub_time, title, content)
        VALUES (%s,%s,%s,%s)
    """
    rows = [
        (
            nullify(r.get("src")) or "unknown",
            parse_datetime(r.get("datetime") or r.get("pub_time")),
            nullify(r.get("title")),
            nullify(r.get("content")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_major_news(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO major_news_article (src, pub_time, title, content)
        VALUES (%s,%s,%s,%s)
    """
    rows = [
        (
            nullify(r.get("src")),
            parse_datetime(r.get("pub_time")),
            nullify(r.get("title")),
            nullify(r.get("content")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_cctv(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO cctv_news_daily (news_date, title, content)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE content=VALUES(content)
    """
    rows = [
        (
            parse_date(r.get("date")),
            nullify(r.get("title")),
            nullify(r.get("content")),
        )
        for r in df.to_dict("records")
        if nullify(r.get("title")) and parse_date(r.get("date"))
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Tushare corpus data")
    parser.add_argument("--full", action="store_true", help="full historical import (slow)")
    args = parser.parse_args()

    client = create_client()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = pymysql.connect(**mysql_config())

    try:
        print("Applying schema...")
        apply_schema(conn)

        print("Fetching npr (政策法规)...")
        npr = fetch_npr(client, 2006 if args.full else 2023)
        print(f"  npr: {len(npr)} rows")
        if not npr.empty:
            npr.to_csv(DATA_DIR / "npr_policy.csv", index=False)
        n1 = upsert_npr(conn, npr)
        print(f"  Upserted npr_policy: {n1}")

        print("Fetching monetary_policy (央行货政报告)...")
        pboc = fetch_monetary_policy(client)
        print(f"  monetary_policy: {len(pboc)} rows")
        if not pboc.empty:
            pboc.to_csv(DATA_DIR / "pboc_monetary_policy.csv", index=False)
        n2 = upsert_pboc(conn, pboc)
        print(f"  Upserted pboc_monetary_policy: {n2}")

        print("Fetching research_report (券商研报)...")
        rr = fetch_research_report(client, 2017 if args.full else date.today().year - 1)
        print(f"  research_report: {len(rr)} rows")
        if not rr.empty:
            rr.to_csv(DATA_DIR / "broker_research_report.csv", index=False)
        n3 = upsert_research(conn, rr)
        print(f"  Upserted broker_research_report: {n3}")

        print("Fetching news (快讯)...")
        news = fetch_news(client, days=365 if args.full else 30)
        print(f"  news: {len(news)} rows")
        n4 = upsert_news(conn, news)
        print(f"  Upserted news_flash: {n4}")

        print("Fetching major_news (长篇通讯)...")
        mn = fetch_major_news(client, 2018 if args.full else date.today().year)
        print(f"  major_news: {len(mn)} rows")
        n5 = upsert_major_news(conn, mn)
        print(f"  Upserted major_news_article: {n5}")

        print("Fetching cctv_news (新闻联播)...")
        if args.full:
            cctv = fetch_cctv_news(client, date(2017, 1, 1))
        else:
            cctv = fetch_cctv_news(client, date.today() - timedelta(days=30))
        print(f"  cctv_news: {len(cctv)} rows")
        if not cctv.empty:
            cctv.to_csv(DATA_DIR / "cctv_news_daily.csv", index=False)
        n6 = upsert_cctv(conn, cctv)
        print(f"  Upserted cctv_news_daily: {n6}")

        if n1 + n2 + n3 + n4 + n5 + n6 == 0:
            print("\n⚠ 全部接口返回 0 条：这些语料接口需 Tushare 单独开权限（与积分无关）")
            print("  权限说明: https://tushare.pro/document/1?doc_id=290")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n7 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n7} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

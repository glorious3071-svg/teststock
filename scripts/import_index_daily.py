#!/usr/bin/env python3
"""从 Tushare 拉取主要指数日行情写入 index_daily 表。

用法：
    python3 scripts/import_index_daily.py                # 全量（2006-至今）
    python3 scripts/import_index_daily.py --since 20240101  # 增量
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

SCHEMA_FILE = ROOT / "sql" / "index_daily_schema.sql"
REQUEST_SLEEP = 0.6   # teajoin 限速保护
START_DATE    = "20060101"

# 需要入库的指数
INDEX_CODES: list[tuple[str, str]] = [
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证综指"),
    ("000016.SH", "上证50"),
    ("000905.SH", "中证500"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
]

# Tushare index_daily 字段 → 入库列名映射（change 是保留字，改为 change_pt）
FIELD_MAP = {
    "open":     "open",
    "high":     "high",
    "low":      "low",
    "close":    "close",
    "pre_close": "pre_close",
    "change":   "change_pt",
    "pct_chg":  "pct_chg",
    "vol":      "vol",
    "amount":   "amount",
}
DB_COLS = list(FIELD_MAP.values())


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER",     "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def apply_schema(conn: pymysql.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def nullify(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return v


def to_float(v) -> float | None:
    x = nullify(v)
    return None if x is None else float(x)


def parse_date(v) -> str | None:
    x = nullify(v)
    if x is None:
        return None
    s = str(int(x)) if isinstance(x, (int, float)) else str(x).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return pd.to_datetime(s).strftime("%Y-%m-%d")


def fetch_range(client, ts_code: str, start: str, end: str) -> pd.DataFrame:
    raw = client.query_http(
        "index_daily",
        {"ts_code": ts_code, "start_date": start, "end_date": end},
        timeout=120,
    )
    items  = (raw.get("data") or {}).get("items") or []
    fields = (raw.get("data") or {}).get("fields") or []
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def fetch_all(client, ts_code: str, start_date: str) -> pd.DataFrame:
    today = date.today().strftime("%Y%m%d")
    frames: list[pd.DataFrame] = []
    chunk = start_date

    while chunk <= today:
        end_year  = int(chunk[:4]) + 4
        chunk_end = min(f"{end_year}1231", today)
        df = fetch_range(client, ts_code, chunk, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= today:
            break
        chunk = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"])
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns={"change": "change_pt"}).copy()
    out["trade_date"] = out["trade_date"].map(parse_date)
    out = out.dropna(subset=["trade_date"])
    keep = ["ts_code", "trade_date"] + DB_COLS
    available = [c for c in keep if c in out.columns]
    return out[available]


def upsert(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols    = [c for c in DB_COLS if c in df.columns]
    sql     = f"""
        INSERT INTO index_daily
            (ts_code, trade_date, {', '.join(cols)})
        VALUES (%s, %s, {', '.join(['%s'] * len(cols))})
        ON DUPLICATE KEY UPDATE
            {', '.join(f'{c}=VALUES({c})' for c in cols)}
    """
    rows = [
        (r.ts_code, r.trade_date, *[to_float(getattr(r, c, None)) for c in cols])
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def last_date_in_db(conn: pymysql.Connection, ts_code: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(trade_date) FROM index_daily WHERE ts_code = %s", (ts_code,)
        )
        r = cur.fetchone()
    if r and r[0]:
        return r[0].strftime("%Y%m%d")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Import index daily price from Tushare")
    parser.add_argument("--since", default=None,
                        help="起始日期 YYYYMMDD，默认自动检测增量或全量 20060101")
    args = parser.parse_args()

    client = create_client()
    conn   = pymysql.connect(**mysql_config())

    try:
        print("建表（如不存在）...")
        apply_schema(conn)

        total = 0
        for ts_code, name in INDEX_CODES:
            if args.since:
                start = args.since
            else:
                last = last_date_in_db(conn, ts_code)
                if last:
                    # 从最新日期后一天开始增量
                    d = date.fromisoformat(last)
                    from datetime import timedelta
                    start = (d + timedelta(days=1)).strftime("%Y%m%d")
                    print(f"{ts_code} {name}: 增量，从 {start} 开始")
                else:
                    start = START_DATE
                    print(f"{ts_code} {name}: 全量，从 {start} 开始")

            today = date.today().strftime("%Y%m%d")
            if start > today:
                print(f"  已是最新，跳过")
                continue

            raw  = fetch_all(client, ts_code, start)
            if raw.empty:
                print(f"  无数据")
                continue

            prep = prepare(raw)
            n    = upsert(conn, prep)
            print(f"  写入 {n} 行（{prep['trade_date'].min()} ~ {prep['trade_date'].max()}）")
            total += n

        print(f"\n完成，共写入 {total} 行")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

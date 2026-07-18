#!/usr/bin/env python3
"""Import CFFEX index-futures continuous contracts into fut_daily.

The scorecard portfolio uses CSI-linked hedge diagnostics. This script keeps the
real executable hedge instruments available locally instead of relying only on
index-return proxies.
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

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

REQUEST_SLEEP = 0.6
START_DATE = "20060101"

FUTURE_CODES: list[tuple[str, str]] = [
    ("IF.CFX", "沪深300股指期货连续"),
    ("IH.CFX", "上证50股指期货连续"),
    ("IC.CFX", "中证500股指期货连续"),
    ("IM.CFX", "中证1000股指期货连续"),
]

DB_COLS = ["open", "high", "low", "close", "settle", "vol", "amount", "oi"]


def latest_possible_trade_date() -> str:
    today = date.today()
    if today.weekday() == 5:
        today -= timedelta(days=1)
    elif today.weekday() == 6:
        today -= timedelta(days=2)
    return today.strftime("%Y%m%d")


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


def apply_schema(conn: pymysql.Connection) -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS fut_daily (
            ts_code varchar(20) NOT NULL COMMENT '期货代码',
            trade_date date NOT NULL COMMENT '交易日',
            open decimal(14,4) DEFAULT NULL COMMENT '开盘价',
            high decimal(14,4) DEFAULT NULL COMMENT '最高价',
            low decimal(14,4) DEFAULT NULL COMMENT '最低价',
            close decimal(14,4) DEFAULT NULL COMMENT '收盘价',
            settle decimal(14,4) DEFAULT NULL COMMENT '结算价',
            vol decimal(20,2) DEFAULT NULL COMMENT '成交量(手)',
            amount decimal(22,4) DEFAULT NULL COMMENT '成交额(万元)',
            oi decimal(20,2) DEFAULT NULL COMMENT '持仓量(手)',
            created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (ts_code, trade_date),
            KEY idx_fut_daily_date (trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='期货日行情（Tushare fut_daily）'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def nullify(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return value


def to_float(value) -> float | None:
    value = nullify(value)
    return None if value is None else float(value)


def parse_date(value) -> str | None:
    value = nullify(value)
    if value is None:
        return None
    text = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


def fetch_range(client, ts_code: str, start: str, end: str) -> pd.DataFrame:
    raw = client.query_http(
        "fut_daily",
        {"ts_code": ts_code, "start_date": start, "end_date": end},
        timeout=120,
    )
    data = raw.get("data") or {}
    items = data.get("items") or []
    fields = data.get("fields") or []
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def fetch_all(client, ts_code: str, start_date: str) -> pd.DataFrame:
    today = latest_possible_trade_date()
    frames: list[pd.DataFrame] = []
    chunk = start_date

    while chunk <= today:
        end_year = int(chunk[:4]) + 3
        chunk_end = min(f"{end_year}1231", today)
        df = fetch_range(client, ts_code, chunk, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            df = df.dropna(axis=1, how="all")
            frames.append(df)
        if chunk_end >= today:
            break
        chunk = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = out["trade_date"].map(parse_date)
    out = out.dropna(subset=["trade_date"])
    keep = ["ts_code", "trade_date"] + [c for c in DB_COLS if c in out.columns]
    return out[keep]


def upsert(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = [c for c in DB_COLS if c in df.columns]
    sql = f"""
        INSERT INTO fut_daily
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
        cur.execute("SELECT MAX(trade_date) FROM fut_daily WHERE ts_code = %s", (ts_code,))
        row = cur.fetchone()
    if row and row[0]:
        return row[0].isoformat()
    return None


def print_summary(conn: pymysql.Connection) -> None:
    with conn.cursor() as cur:
        print("\nCFFEX index-futures coverage:")
        for ts_code, name in FUTURE_CODES:
            cur.execute(
                """
                SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
                FROM fut_daily
                WHERE ts_code = %s
                """,
                (ts_code,),
            )
            rows, min_date, max_date = cur.fetchone()
            print(f"  {ts_code} {name}: rows={rows}, range={min_date} ~ {max_date}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import CFFEX index-futures daily data")
    parser.add_argument("--since", default=None, help="起始日期 YYYYMMDD，默认自动增量或全量")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="只打印 IF/IH/IC/IM 连续合约覆盖情况",
    )
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        print_summary(conn)
        if args.summary_only:
            return 0

        client = create_client()
        total = 0
        for ts_code, name in FUTURE_CODES:
            if args.since:
                start = args.since
                print(f"{ts_code} {name}: 指定起始 {start}")
            else:
                last = last_date_in_db(conn, ts_code)
                if last:
                    start = (date.fromisoformat(last) + timedelta(days=1)).strftime("%Y%m%d")
                    print(f"{ts_code} {name}: 增量，从 {start} 开始")
                else:
                    start = START_DATE
                    print(f"{ts_code} {name}: 全量，从 {start} 开始")

            today = latest_possible_trade_date()
            if start > today:
                print("  已是最新，跳过")
                continue

            raw = fetch_all(client, ts_code, start)
            if raw.empty:
                print("  无数据")
                continue

            prepared = prepare(raw)
            inserted = upsert(conn, prepared)
            print(f"  写入 {inserted} 行（{prepared['trade_date'].min()} ~ {prepared['trade_date'].max()}）")
            total += inserted

        print_summary(conn)
        print(f"\n完成，共写入 {total} 行")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

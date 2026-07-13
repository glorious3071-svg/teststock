#!/usr/bin/env python3
"""Import major index PE/PB from Tushare index_dailybasic (doc_id=128)."""

from __future__ import annotations

import os
import sys
import time
import argparse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "index_valuation_schema.sql"
REQUEST_SLEEP = 0.8
START_DATE = "20040101"
BATCH_LIMIT = 2900

# Tushare index_dailybasic coverage
INDEX_CODES: list[tuple[str, str]] = [
    ("000001.SH", "上证综指"),
    ("399001.SZ", "深证成指"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("399005.SZ", "中小板指"),
    ("399006.SZ", "创业板指"),
]

SNAPSHOT_COLS = [
    ("valuation_date", "DATE NULL COMMENT '宽基估值取数日'"),
    ("hs300_pe", "DECIMAL(10,4) NULL COMMENT '沪深300静态PE'"),
    ("hs300_pe_ttm", "DECIMAL(10,4) NULL COMMENT '沪深300 PE-TTM'"),
    ("hs300_pb", "DECIMAL(10,4) NULL COMMENT '沪深300 PB'"),
    ("sz50_pe_ttm", "DECIMAL(10,4) NULL COMMENT '上证50 PE-TTM'"),
    ("sz50_pb", "DECIMAL(10,4) NULL COMMENT '上证50 PB'"),
    ("zz500_pe_ttm", "DECIMAL(10,4) NULL COMMENT '中证500 PE-TTM'"),
    ("zz500_pb", "DECIMAL(10,4) NULL COMMENT '中证500 PB'"),
    ("valuation_stance", "VARCHAR(20) NULL COMMENT '估值态势'"),
]

DB_COLS = [
    "total_mv",
    "float_mv",
    "total_share",
    "float_share",
    "free_share",
    "turnover_rate",
    "turnover_rate_f",
    "pe",
    "pe_ttm",
    "pb",
]


def latest_possible_trade_date() -> str:
    from datetime import timedelta

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


def nullify(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return value


def to_float(value) -> float | None:
    v = nullify(value)
    return None if v is None else float(v)


def parse_trade_date(value) -> str | None:
    v = nullify(value)
    if v is None:
        return None
    text = str(int(v)) if isinstance(v, (int, float)) else str(v).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


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


def fetch_index_range(client, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    data = client.query_http(
        "index_dailybasic",
        {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        timeout=120,
    )
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame(columns=["ts_code", "trade_date", *DB_COLS])
    return pd.DataFrame(items, columns=fields)


def fetch_index_all(client, ts_code: str, start_date: str = START_DATE) -> pd.DataFrame:
    end_date = latest_possible_trade_date()
    frames: list[pd.DataFrame] = []
    chunk_start = start_date

    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + 4
        chunk_end = min(f"{chunk_end_year}1231", end_date)
        df = fetch_index_range(client, ts_code, chunk_start, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= end_date:
            break
        next_year = int(chunk_end[:4]) + 1
        chunk_start = f"{next_year}0101"

    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", *DB_COLS])
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"])
    return out


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trade_date"] = out["trade_date"].map(parse_trade_date)
    out = out.dropna(subset=["trade_date"])
    keep = ["ts_code", "trade_date", *DB_COLS]
    return out[keep]


def upsert_rows(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO index_dailybasic
            (ts_code, trade_date, {", ".join(DB_COLS)})
        VALUES (%s,%s,{",".join(["%s"] * len(DB_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in DB_COLS)}
    """
    rows = [
        (
            r.ts_code,
            r.trade_date,
            *[to_float(getattr(r, c)) for c in DB_COLS],
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def last_date_in_db(conn, ts_code: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(trade_date) FROM index_dailybasic WHERE ts_code = %s",
            (ts_code,),
        )
        row = cur.fetchone()
    if row and row[0]:
        return row[0].isoformat()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Import major index PE/PB from Tushare")
    parser.add_argument(
        "--since",
        default=None,
        help="起始日期 YYYYMMDD；默认全量。与 --incremental 同时传入时优先使用 --since",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="按每个指数 index_dailybasic 已有最大 trade_date + 1 续传",
    )
    args = parser.parse_args()

    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        total = 0
        for ts_code, name in INDEX_CODES:
            if args.since:
                start = args.since
            elif args.incremental:
                last = last_date_in_db(conn, ts_code)
                if last:
                    from datetime import timedelta

                    d = date.fromisoformat(last)
                    start = (d + timedelta(days=1)).strftime("%Y%m%d")
                    print(f"{ts_code} {name}: 增量，从 {start} 开始")
                else:
                    start = START_DATE
                    print(f"{ts_code} {name}: 无历史数据，从 {start} 开始")
            else:
                start = START_DATE

            today = latest_possible_trade_date()
            if start > today:
                print(f"{ts_code} {name}: 已是最新，跳过")
                continue

            print(f"Fetching {ts_code} {name} from {start}...")
            raw = fetch_index_all(client, ts_code, start)
            prepared = prepare_df(raw)
            print(
                f"  rows: {len(prepared)}"
                + (f" ({prepared['trade_date'].min()} .. {prepared['trade_date'].max()})" if len(prepared) else "")
            )
            prepared.to_csv(DATA_DIR / f"index_dailybasic_{ts_code.replace('.', '_')}.csv", index=False)
            n = upsert_rows(conn, prepared)
            print(f"  upserted: {n}")
            total += n

        from macro.annual_snapshot import rebuild_annual_snapshots

        n_snap = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n_snap} years")
        print(f"Total index_dailybasic rows upserted: {total}")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()

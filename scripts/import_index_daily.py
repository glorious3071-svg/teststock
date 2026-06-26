#!/usr/bin/env python3
"""Import major broad-base index daily quotes from Tushare index_daily (doc_id=95).

把仓库历史上手工建表/手工跑过的 6 个宽基指数行情沉淀到可复现 import 流程。
本次只覆盖 7 个宽基（含 399005 中小板，与 index_dailybasic 对齐）；
申万一级/二级行业指数走独立脚本 import_sw_industry.py。

幂等：ON DUPLICATE KEY UPDATE，重跑安全。
字段映射：Tushare `change` 字段 → DB 列 `change_pt`（MySQL 保留字）。
"""

from __future__ import annotations

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

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "index_daily_schema.sql"
REQUEST_SLEEP = 0.8
START_DATE = "20040101"
CHUNK_YEARS = 4

BROAD_BASE_INDEX_CODES: list[tuple[str, str]] = [
    ("000001.SH", "上证综指"),
    ("399001.SZ", "深证成指"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("399005.SZ", "中小板指"),
    ("399006.SZ", "创业板指"),
]

# Tushare index_daily 返回字段（按 doc_id=95）
TUSHARE_FIELDS = [
    "ts_code", "trade_date", "close", "open", "high", "low",
    "pre_close", "change", "pct_chg", "vol", "amount",
]
# 落表列顺序（与 schema 对齐）
DB_PRICE_COLS = [
    "open", "high", "low", "close", "pre_close",
    "change_pt", "pct_chg", "vol", "amount",
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
    conn.commit()


def fetch_index_range(client, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    data = client.query_http(
        "index_daily",
        {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        timeout=120,
    )
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame(columns=TUSHARE_FIELDS)
    return pd.DataFrame(items, columns=fields)


def fetch_index_all(client, ts_code: str, start_date: str = START_DATE) -> pd.DataFrame:
    end_date = date.today().strftime("%Y%m%d")
    frames: list[pd.DataFrame] = []
    chunk_start = start_date

    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + CHUNK_YEARS
        chunk_end = min(f"{chunk_end_year - 1}1231", end_date)
        df = fetch_index_range(client, ts_code, chunk_start, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= end_date:
            break
        chunk_start = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame(columns=TUSHARE_FIELDS)
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ts_code", "trade_date"]
    )


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    """Tushare `change` → DB `change_pt`，归一化 trade_date 为 YYYY-MM-DD。"""
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = out["trade_date"].map(parse_trade_date)
    out = out.dropna(subset=["trade_date"])
    if "change" in out.columns:
        out = out.rename(columns={"change": "change_pt"})
    return out


def upsert_rows(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO index_daily
            (ts_code, trade_date, {", ".join(DB_PRICE_COLS)})
        VALUES (%s, %s, {", ".join(["%s"] * len(DB_PRICE_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in DB_PRICE_COLS)}
    """
    rows = [
        (
            r.ts_code,
            r.trade_date,
            *[to_float(getattr(r, c)) for c in DB_PRICE_COLS],
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        total = 0
        for ts_code, name in BROAD_BASE_INDEX_CODES:
            print(f"Fetching {ts_code} {name}...")
            raw = fetch_index_all(client, ts_code)
            prepared = prepare_df(raw)
            if not prepared.empty:
                print(
                    f"  rows: {len(prepared)} "
                    f"({prepared['trade_date'].min()} .. {prepared['trade_date'].max()})"
                )
                prepared.to_csv(
                    DATA_DIR / f"index_daily_{ts_code.replace('.', '_')}.csv",
                    index=False,
                )
                n = upsert_rows(conn, prepared)
                print(f"  upserted: {n}")
                total += n
            else:
                print("  rows: 0 (skip)")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts_code, COUNT(*) AS bars,
                       MIN(trade_date) AS first_d, MAX(trade_date) AS last_d
                FROM index_daily
                WHERE ts_code IN ({})
                GROUP BY ts_code
                ORDER BY ts_code
                """.format(",".join(["%s"] * len(BROAD_BASE_INDEX_CODES))),
                [c for c, _ in BROAD_BASE_INDEX_CODES],
            )
            print("\nindex_daily 当前宽基覆盖：")
            for r in cur.fetchall():
                print(f"  {r[0]:<14} bars={r[1]:<6} {r[2]} ~ {r[3]}")

        print(f"\nTotal upserted: {total}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

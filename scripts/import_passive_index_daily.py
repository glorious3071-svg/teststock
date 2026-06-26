#!/usr/bin/env python3
"""Import index_daily + index_dailybasic for indices tracked by passive ETFs.

覆盖范围：passive_etf（在市、国内权益）关联的跟踪指数，排除已有数据的。
起始日期：2019-01-01（满足 5Y PB 分位 + 1Y 动量评分需求）。
幂等：ON DUPLICATE KEY UPDATE，中断后可重跑续传。

Usage:
  python scripts/import_passive_index_daily.py
  python scripts/import_passive_index_daily.py --limit 10   # 调试：只跑前 N 个
  python scripts/import_passive_index_daily.py --no-basic   # 只拉行情不拉估值
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

START_DATE = "20190101"
CHUNK_YEARS = 4
REQUEST_SLEEP = 0.8

EXCLUDE_SUFFIXES = {"HI", "NASDAQ", "SGE", "SHF", "DCE", "CZCE", "FP", "UN", "GY"}
EXCLUDE_NAME_KWORDS = ("债", "货币", "国债", "信用", "可转债")

PRICE_COLS = ["open", "high", "low", "close", "pre_close", "change_pt", "pct_chg", "vol", "amount"]
BASIC_COLS = ["total_mv", "float_mv", "total_share", "float_share", "free_share",
              "turnover_rate", "turnover_rate_f", "pe", "pe_ttm", "pb"]


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


def nullify(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def to_float(v) -> float | None:
    v = nullify(v)
    return None if v is None else float(v)


def parse_date(v) -> str | None:
    v = nullify(v)
    if v is None:
        return None
    text = str(int(v)) if isinstance(v, (int, float)) else str(v).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


def load_index_list(conn) -> list[tuple[str, str]]:
    """返回需要补充的 (index_ts_code, index_name) 列表。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ts_code FROM index_daily")
        existing = {r[0] for r in cur.fetchall()}

        cur.execute("""SELECT index_ts_code, index_name, COUNT(*) n
            FROM passive_etf
            WHERE list_status = 'L'
              AND index_ts_code IS NOT NULL
              AND (etf_type IS NULL OR etf_type = '纯境内')
              AND (index_name NOT LIKE '%债%'
               AND index_name NOT LIKE '%货币%'
               AND index_name NOT LIKE '%国债%'
               AND index_name NOT LIKE '%信用%'
               AND index_name NOT LIKE '%可转债%')
            GROUP BY index_ts_code, index_name
            ORDER BY n DESC""")
        rows = cur.fetchall()

    result = []
    for code, name, _ in rows:
        if code in existing:
            continue
        suffix = code.split(".")[-1]
        if suffix in EXCLUDE_SUFFIXES:
            continue
        result.append((code, name or ""))
    return result


def fetch_chunk(client, api: str, ts_code: str, start: str, end: str) -> pd.DataFrame:
    params = {"ts_code": ts_code, "start_date": start, "end_date": end}
    data = client.query_http(api, params, timeout=120)
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def fetch_all_chunks(client, api: str, ts_code: str) -> pd.DataFrame:
    end_date = date.today().strftime("%Y%m%d")
    frames: list[pd.DataFrame] = []
    chunk_start = START_DATE

    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + CHUNK_YEARS
        chunk_end = min(f"{chunk_end_year - 1}1231", end_date)
        df = fetch_chunk(client, api, ts_code, chunk_start, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= end_date:
            break
        chunk_start = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    date_col = "trade_date" if "trade_date" in merged.columns else merged.columns[1]
    merged[date_col] = merged[date_col].map(parse_date)
    return merged.dropna(subset=[date_col]).drop_duplicates(subset=["ts_code", date_col])


def upsert_price(conn, df: pd.DataFrame) -> int:
    if df.empty or "trade_date" not in df.columns:
        return 0
    if "change" in df.columns and "change_pt" not in df.columns:
        df = df.rename(columns={"change": "change_pt"})

    sql = f"""INSERT INTO index_daily
        (ts_code, trade_date, {', '.join(PRICE_COLS)})
        VALUES (%s, %s, {', '.join(['%s'] * len(PRICE_COLS))})
        ON DUPLICATE KEY UPDATE
        {', '.join(f'{c}=VALUES({c})' for c in PRICE_COLS)}"""

    rows = []
    for r in df.itertuples(index=False):
        if not hasattr(r, "trade_date") or r.trade_date is None:
            continue
        vals = [to_float(getattr(r, c, None)) for c in PRICE_COLS]
        rows.append((r.ts_code, r.trade_date, *vals))

    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_basic(conn, df: pd.DataFrame) -> int:
    if df.empty or "trade_date" not in df.columns:
        return 0

    sql = f"""INSERT INTO index_dailybasic
        (ts_code, trade_date, {', '.join(BASIC_COLS)})
        VALUES (%s, %s, {', '.join(['%s'] * len(BASIC_COLS))})
        ON DUPLICATE KEY UPDATE
        {', '.join(f'{c}=VALUES({c})' for c in BASIC_COLS)}"""

    rows = []
    for r in df.itertuples(index=False):
        if not hasattr(r, "trade_date") or r.trade_date is None:
            continue
        vals = [to_float(getattr(r, c, None)) for c in BASIC_COLS]
        rows.append((r.ts_code, r.trade_date, *vals))

    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="补充被动ETF跟踪指数行情+估值数据")
    parser.add_argument("--limit", type=int, help="只处理前 N 个指数（调试）")
    parser.add_argument("--no-basic", action="store_true", help="跳过 index_dailybasic")
    args = parser.parse_args()

    client = create_client()
    conn = pymysql.connect(**mysql_config())

    try:
        index_list = load_index_list(conn)
        if args.limit:
            index_list = index_list[: args.limit]

        total = len(index_list)
        print(f"需要补充: {total} 个指数 (起始日期 {START_DATE})")

        price_total = basic_total = 0
        empty_price = empty_basic = fail = 0

        for i, (ts_code, name) in enumerate(index_list, 1):
            print(f"[{i}/{total}] {ts_code} {name}")

            # --- 行情 ---
            try:
                df_price = fetch_all_chunks(client, "index_daily", ts_code)
                if df_price.empty:
                    empty_price += 1
                    print(f"  price: 无数据")
                else:
                    n = upsert_price(conn, df_price)
                    price_total += n
                    print(f"  price: {n} 行 ({df_price['trade_date'].min()}~{df_price['trade_date'].max()})")
            except Exception as exc:
                fail += 1
                print(f"  price: FAIL {exc}")

            # --- 估值 ---
            if not args.no_basic:
                try:
                    df_basic = fetch_all_chunks(client, "index_dailybasic", ts_code)
                    if df_basic.empty:
                        empty_basic += 1
                        print(f"  basic: 无数据")
                    else:
                        n = upsert_basic(conn, df_basic)
                        basic_total += n
                        print(f"  basic: {n} 行")
                except Exception as exc:
                    print(f"  basic: FAIL {exc}")

        print(f"\n完成: price={price_total} 行, basic={basic_total} 行")
        print(f"      空={empty_price}/{empty_basic}, 失败={fail}")

        # 汇总
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(DISTINCT ts_code) FROM index_daily
                           WHERE ts_code NOT LIKE '%.SI'""")
            print(f"index_daily 非申万指数共: {cur.fetchone()[0]} 个")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

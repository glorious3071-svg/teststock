#!/usr/bin/env python3
"""Import index_daily + index_dailybasic for indices tracked by passive ETFs.

覆盖范围：passive_etf（在市、国内权益）关联的跟踪指数，排除已有数据的。
起始日期：2019-01-01（满足 5Y PB 分位 + 1Y 动量评分需求）。
幂等：ON DUPLICATE KEY UPDATE，中断后可重跑续传。

Usage:
  python scripts/import_passive_index_daily.py
  python scripts/import_passive_index_daily.py --limit 10   # 调试：只跑前 N 个
  python scripts/import_passive_index_daily.py --no-basic   # 只拉行情不拉估值
  python scripts/import_passive_index_daily.py --update-existing  # 每日续传已有指数，新指数只试探近期
  python scripts/import_passive_index_daily.py --update-existing --existing-basic-only
  python scripts/import_passive_index_daily.py --update-existing --skip-stale-days 21
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

START_DATE = "20190101"
CHUNK_YEARS = 4
REQUEST_SLEEP = 0.8
NEW_INDEX_LOOKBACK_DAYS = 21

EXCLUDE_SUFFIXES = {"HI", "NASDAQ", "SGE", "SHF", "DCE", "CZCE", "FP", "UN", "GY"}
EXCLUDE_NAME_KWORDS = ("债", "货币", "国债", "信用", "可转债")

PRICE_COLS = ["open", "high", "low", "close", "pre_close", "change_pt", "pct_chg", "vol", "amount"]
BASIC_COLS = ["total_mv", "float_mv", "total_share", "float_share", "free_share",
              "turnover_rate", "turnover_rate_f", "pe", "pe_ttm", "pb"]


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


def load_all_domestic_index_list(conn) -> list[tuple[str, str]]:
    """返回 passive_etf 中所有在市境内权益 ETF 的跟踪指数。"""
    with conn.cursor() as cur:
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
    seen: set[str] = set()
    for code, name, _ in rows:
        if not code or code in seen:
            continue
        suffix = code.split(".")[-1]
        if suffix in EXCLUDE_SUFFIXES:
            continue
        seen.add(code)
        result.append((code, name or ""))
    return result


def fetch_existing_last_dates(conn, table: str) -> dict[str, date]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT ts_code, MAX(trade_date) FROM {table} GROUP BY ts_code")
        return {ts_code: last_d for ts_code, last_d in cur.fetchall() if last_d}


def fetch_chunk(client, api: str, ts_code: str, start: str, end: str) -> pd.DataFrame:
    params = {"ts_code": ts_code, "start_date": start, "end_date": end}
    data = client.query_http(api, params, timeout=120)
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def fetch_all_chunks(client, api: str, ts_code: str, start_date: str = START_DATE) -> pd.DataFrame:
    end_date = latest_possible_trade_date()
    frames: list[pd.DataFrame] = []
    chunk_start = start_date

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
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="同步所有在市境内权益 ETF 跟踪指数；已有指数从各自最大 trade_date+1 续传，新指数只试探近期",
    )
    parser.add_argument(
        "--new-lookback-days",
        type=int,
        default=NEW_INDEX_LOOKBACK_DAYS,
        help=f"--update-existing 下无历史覆盖指数的试探回看天数，默认 {NEW_INDEX_LOOKBACK_DAYS}",
    )
    parser.add_argument(
        "--existing-basic-only",
        action="store_true",
        help="只续传 index_dailybasic 已有覆盖的指数；无历史估值覆盖的指数跳过 basic",
    )
    parser.add_argument(
        "--skip-stale-days",
        type=int,
        default=0,
        help="--update-existing 下跳过最近覆盖早于 N 天前的 price/basic；默认 0 不跳过",
    )
    args = parser.parse_args()

    client = create_client()
    conn = pymysql.connect(**mysql_config())

    try:
        if args.update_existing:
            index_list = load_all_domestic_index_list(conn)
            price_last = fetch_existing_last_dates(conn, "index_daily")
            basic_last = fetch_existing_last_dates(conn, "index_dailybasic")
        else:
            index_list = load_index_list(conn)
            price_last = {}
            basic_last = {}

        if args.limit:
            index_list = index_list[: args.limit]

        total = len(index_list)
        mode = "同步已有+新指数" if args.update_existing else "只补缺失指数"
        print(f"需要处理: {total} 个指数 ({mode}, 默认起始日期 {START_DATE})")

        price_total = basic_total = 0
        empty_price = empty_basic = fail = skipped_price = skipped_basic = 0
        today = latest_possible_trade_date()
        recent_start = (date.today() - timedelta(days=args.new_lookback_days)).strftime("%Y%m%d")
        stale_cutoff = (
            date.fromisoformat(f"{today[:4]}-{today[4:6]}-{today[6:8]}")
            - timedelta(days=args.skip_stale_days)
            if args.skip_stale_days > 0
            else None
        )

        for i, (ts_code, name) in enumerate(index_list, 1):
            print(f"[{i}/{total}] {ts_code} {name}")

            # --- 行情 ---
            try:
                price_start = START_DATE
                if args.update_existing:
                    if ts_code in price_last:
                        if stale_cutoff and price_last[ts_code] < stale_cutoff:
                            skipped_price += 1
                            print(f"  price: 最近覆盖 {price_last[ts_code]} 早于 {stale_cutoff}，跳过")
                            continue
                        price_start = (price_last[ts_code] + timedelta(days=1)).strftime("%Y%m%d")
                    else:
                        price_start = recent_start

                if price_start > today:
                    skipped_price += 1
                    print("  price: 已是最新，跳过")
                else:
                    df_price = fetch_all_chunks(client, "index_daily", ts_code, price_start)
                    if df_price.empty:
                        empty_price += 1
                        print(f"  price: 无数据 (start={price_start})")
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
                    basic_start = START_DATE
                    if args.update_existing:
                        if ts_code in basic_last:
                            if stale_cutoff and basic_last[ts_code] < stale_cutoff:
                                skipped_basic += 1
                                print(f"  basic: 最近覆盖 {basic_last[ts_code]} 早于 {stale_cutoff}，跳过")
                                continue
                            basic_start = (basic_last[ts_code] + timedelta(days=1)).strftime("%Y%m%d")
                        elif args.existing_basic_only:
                            skipped_basic += 1
                            print("  basic: 无历史估值覆盖，跳过")
                            continue
                        else:
                            basic_start = recent_start

                    if basic_start > today:
                        skipped_basic += 1
                        print("  basic: 已是最新，跳过")
                    else:
                        df_basic = fetch_all_chunks(client, "index_dailybasic", ts_code, basic_start)
                        if df_basic.empty:
                            empty_basic += 1
                            print(f"  basic: 无数据 (start={basic_start})")
                        else:
                            n = upsert_basic(conn, df_basic)
                            basic_total += n
                            print(f"  basic: {n} 行")
                except Exception as exc:
                    fail += 1
                    print(f"  basic: FAIL {exc}")

        print(f"\n完成: price={price_total} 行, basic={basic_total} 行")
        print(f"      已是最新跳过={skipped_price}/{skipped_basic}, 空={empty_price}/{empty_basic}, 失败={fail}")

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

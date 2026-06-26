#!/usr/bin/env python3
"""
下载所有成份股的个股 PE/PB 历史数据（Tushare daily_basic）。

按日期批量查询 + 多线程并发：每次调用 daily_basic(trade_date=xxx) 返回全市场
~5000 只股票，过滤出成份股后批量入库。4 个 worker 并发，比单线程快 4 倍。

用法：
    python3 scripts/download_stock_pe_pb.py                   # 增量（跳过已入库的日期）
    python3 scripts/download_stock_pe_pb.py --since 20200101  # 指定起始日期
    python3 scripts/download_stock_pe_pb.py --full            # 全量重跑（从 2006 年）
    python3 scripts/download_stock_pe_pb.py -w 6              # 6 个 worker（默认 4）

写入表：stock_daily_basic（自动建表）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

load_dotenv(ROOT / ".env")

WORKER_SLEEP  = 0.25  # 每个 worker 请求后等待（秒），4 worker 合计 ~10 req/s
START_DATE    = "20060101"
PRINT_LOCK    = threading.Lock()

DB_COLS = [
    "close", "pe", "pe_ttm", "pb",
    "ps", "ps_ttm", "dv_ratio", "dv_ttm",
    "total_mv", "circ_mv",
]

# ── 数据库 ────────────────────────────────────────────────────────────────────

def mysql_config() -> dict:
    return {
        "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER",     "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily_basic (
    ts_code       VARCHAR(20)    NOT NULL COMMENT '股票代码',
    trade_date    DATE           NOT NULL COMMENT '交易日',
    close         DECIMAL(12,4)  NULL     COMMENT '收盘价',
    pe            DECIMAL(12,4)  NULL     COMMENT '市盈率（静态）',
    pe_ttm        DECIMAL(12,4)  NULL     COMMENT '市盈率 TTM',
    pb            DECIMAL(12,4)  NULL     COMMENT '市净率',
    ps            DECIMAL(12,4)  NULL     COMMENT '市销率',
    ps_ttm        DECIMAL(12,4)  NULL     COMMENT '市销率 TTM',
    dv_ratio      DECIMAL(10,4)  NULL     COMMENT '股息率 %',
    dv_ttm        DECIMAL(10,4)  NULL     COMMENT '股息率 TTM %',
    total_mv      DECIMAL(20,2)  NULL     COMMENT '总市值（万元）',
    circ_mv       DECIMAL(20,2)  NULL     COMMENT '流通市值（万元）',
    created_at    TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date),
    KEY idx_sdb_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

def ensure_schema(conn: pymysql.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


import math

def nullify(v):
    if v is None:
        return None
    if isinstance(v, float) and (pd.isna(v) or math.isinf(v)):
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "nan", "nat", "none", "inf", "-inf"):
        return None
    return v

def to_float(v) -> float | None:
    x = nullify(v)
    if x is None:
        return None
    f = float(x)
    # DECIMAL(12,4) 范围：±99999999.9999
    if abs(f) > 99999999:
        return None
    return f

def parse_trade_date(v) -> str | None:
    x = nullify(v)
    if x is None:
        return None
    s = str(int(x)) if isinstance(x, (int, float)) else str(x).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return pd.to_datetime(s).strftime("%Y-%m-%d")


# ── 成份股集合 / 交易日历 ────────────────────────────────────────────────────

def get_constituent_set(conn: pymysql.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT con_code FROM index_constituent")
        return {r[0] for r in cur.fetchall()}


def get_trade_dates(conn: pymysql.Connection, start_date: str) -> list[str]:
    """从 index_daily（沪深300）获取交易日列表（YYYYMMDD）"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT trade_date
            FROM index_daily
            WHERE ts_code = '000300.SH'
              AND trade_date >= %s
            ORDER BY trade_date
        """, (f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}",))
        return [r[0].strftime("%Y%m%d") for r in cur.fetchall()]


def get_dates_in_db(conn: pymysql.Connection) -> set[str]:
    """已入库的交易日期集合（YYYYMMDD）"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date FROM stock_daily_basic ORDER BY trade_date")
        return {r[0].strftime("%Y%m%d") for r in cur.fetchall()}


# ── Worker：Tushare 拉取（线程安全，每个 worker 自带 client）────────────────────

def fetch_one(trade_date: str) -> tuple[str, pd.DataFrame | None, str | None]:
    """单个 worker：拉取一个交易日的全市场 PE/PB，过滤成份股"""
    try:
        client = create_client()
        raw = client.query_http(
            "daily_basic",
            {"trade_date": trade_date},
            timeout=120,
        )
        items  = (raw.get("data") or {}).get("items") or []
        fields = (raw.get("data") or {}).get("fields") or []
        time.sleep(WORKER_SLEEP)
        if not items:
            return trade_date, pd.DataFrame(), None
        return trade_date, pd.DataFrame(items, columns=fields), None
    except Exception as e:
        time.sleep(WORKER_SLEEP)
        return trade_date, None, str(e)


# ── 主线程：数据过滤 + 入库 ──────────────────────────────────────────────────

def prepare_df(df: pd.DataFrame, constituent_set: set[str]) -> pd.DataFrame:
    out = df[df["ts_code"].isin(constituent_set)].copy()
    if out.empty:
        return out
    out["trade_date"] = out["trade_date"].map(parse_trade_date)
    out = out.dropna(subset=["trade_date"])
    keep = ["ts_code", "trade_date"] + DB_COLS
    available = [c for c in keep if c in out.columns]
    return out[available]


def upsert_rows(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = [c for c in DB_COLS if c in df.columns]
    sql = f"""
        INSERT INTO stock_daily_basic
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


# ── 主函数 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="下载成份股 PE/PB（多线程并发）")
    parser.add_argument("--since", default=None, help="起始日期 YYYYMMDD")
    parser.add_argument("--full",  action="store_true", help="全量重跑")
    parser.add_argument("-w", "--workers", type=int, default=4, help="worker 数量（默认 4）")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        ensure_schema(conn)
        constituent_set = get_constituent_set(conn)
        print(f"成份股集合：{len(constituent_set)} 只")

        # 确定起始日期
        if args.full:
            start = START_DATE
        elif args.since:
            start = args.since
        else:
            # 增量：跳过已入库的日期
            dates_in_db = get_dates_in_db(conn)
            all_dates   = set(get_trade_dates(conn, START_DATE))
            missing     = sorted(all_dates - dates_in_db)
            if not missing:
                print("所有交易日已入库，无需处理")
                return
            print(f"增量模式：跳过 {len(dates_in_db)} 个已入库日期，还剩 {len(missing)} 个交易日")
            trade_dates = missing
            _run_concurrent(conn, trade_dates, constituent_set, args.workers)
            return

        trade_dates = get_trade_dates(conn, start)
        today = date.today().strftime("%Y%m%d")
        trade_dates = [d for d in trade_dates if d <= today]

        if not trade_dates:
            print("无需处理（已是最新）")
            return

        _run_concurrent(conn, trade_dates, constituent_set, args.workers)
    finally:
        conn.close()


def _run_concurrent(
    conn: pymysql.Connection,
    trade_dates: list[str],
    constituent_set: set[str],
    n_workers: int,
) -> None:
    print(f"共 {len(trade_dates)} 个交易日，{n_workers} 个 worker（{WORKER_SLEEP}s/worker）")

    total_rows      = 0
    total_stocks    = set()
    errors          = []
    processed_count = 0
    start_time      = time.time()

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        future_map = {
            executor.submit(fetch_one, td): td
            for td in trade_dates
        }

        for future in as_completed(future_map):
            td, df, error = future.result()
            processed_count += 1

            if error:
                errors.append((td, error))
                with PRINT_LOCK:
                    print(f"[{processed_count:4d}/{len(trade_dates)}] {td}: 错误 - {error}")
                continue

            if df is None or df.empty:
                continue

            prep = prepare_df(df, constituent_set)
            n = upsert_rows(conn, prep)
            total_rows += n
            total_stocks.update(prep["ts_code"].tolist())

            # 每 100 个或最后一个，打印进度
            if processed_count % 100 == 0 or processed_count == len(trade_dates):
                elapsed  = time.time() - start_time
                rate     = processed_count / elapsed if elapsed > 0 else 0
                eta_secs = (len(trade_dates) - processed_count) / rate if rate > 0 else 0
                pct      = processed_count / len(trade_dates) * 100
                with PRINT_LOCK:
                    print(f"[{processed_count:4d}/{len(trade_dates)}] {td}: +{n} 行，"
                          f"累计 {total_rows:,} 行 / {len(total_stocks)} 只，"
                          f"速度 {rate:.1f} 日/秒，ETA {eta_secs/60:.0f} 分钟（{pct:.0f}%）")

    elapsed_total = time.time() - start_time
    print(f"\n完成：{total_rows:,} 行，{len(total_stocks)} 只成份股，"
          f"{len(errors)} 个失败日期，耗时 {elapsed_total/60:.1f} 分钟")
    if errors:
        print(f"失败日期（前 10）：{errors[:10]}")


if __name__ == "__main__":
    main()

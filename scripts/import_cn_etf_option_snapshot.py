#!/usr/bin/env python3
"""Import China ETF option contract basics and one daily quote snapshot."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

EXCHANGES = ["SSE", "SZSE"]
BASIC_COLS = [
    "ts_code",
    "exchange",
    "name",
    "per_unit",
    "opt_code",
    "opt_type",
    "call_put",
    "exercise_type",
    "exercise_price",
    "s_month",
    "maturity_date",
    "list_price",
    "list_date",
    "delist_date",
    "last_edate",
    "last_ddate",
    "quote_unit",
    "min_price_chg",
]
DAILY_COLS = [
    "ts_code",
    "trade_date",
    "exchange",
    "pre_settle",
    "pre_close",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "vol",
    "amount",
    "oi",
]


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
    statements = [
        """
        CREATE TABLE IF NOT EXISTS cn_option_basic (
            ts_code varchar(24) NOT NULL,
            exchange varchar(12) NOT NULL,
            name varchar(128) DEFAULT NULL,
            per_unit decimal(18,4) DEFAULT NULL,
            opt_code varchar(32) DEFAULT NULL,
            opt_type varchar(32) DEFAULT NULL,
            call_put varchar(2) DEFAULT NULL,
            exercise_type varchar(16) DEFAULT NULL,
            exercise_price decimal(18,4) DEFAULT NULL,
            s_month varchar(12) DEFAULT NULL,
            maturity_date date DEFAULT NULL,
            list_price decimal(18,4) DEFAULT NULL,
            list_date date DEFAULT NULL,
            delist_date date DEFAULT NULL,
            last_edate date DEFAULT NULL,
            last_ddate date DEFAULT NULL,
            quote_unit varchar(32) DEFAULT NULL,
            min_price_chg decimal(18,6) DEFAULT NULL,
            updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (ts_code),
            KEY idx_cn_option_basic_underlying (opt_code, call_put, maturity_date, exercise_price)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='China ETF option contract basics from Tushare opt_basic'
        """,
        """
        CREATE TABLE IF NOT EXISTS cn_option_daily (
            ts_code varchar(24) NOT NULL,
            trade_date date NOT NULL,
            exchange varchar(12) DEFAULT NULL,
            pre_settle decimal(18,4) DEFAULT NULL,
            pre_close decimal(18,4) DEFAULT NULL,
            open decimal(18,4) DEFAULT NULL,
            high decimal(18,4) DEFAULT NULL,
            low decimal(18,4) DEFAULT NULL,
            close decimal(18,4) DEFAULT NULL,
            settle decimal(18,4) DEFAULT NULL,
            vol decimal(20,2) DEFAULT NULL,
            amount decimal(22,4) DEFAULT NULL,
            oi decimal(20,2) DEFAULT NULL,
            updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (ts_code, trade_date),
            KEY idx_cn_option_daily_date (trade_date),
            KEY idx_cn_option_daily_exchange (exchange, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='China ETF option daily quote snapshots from Tushare opt_daily'
        """,
    ]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
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
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


def fetch_api(client, api_name: str, params: dict[str, str], columns: list[str]) -> pd.DataFrame:
    raw = client.query_http(api_name, params, timeout=120)
    data = raw.get("data") or {}
    items = data.get("items") or []
    fields = data.get("fields") or []
    if not items:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(items, columns=fields)


def fetch_basic(client) -> pd.DataFrame:
    frames = [fetch_api(client, "opt_basic", {"exchange": exchange}, BASIC_COLS) for exchange in EXCHANGES]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=BASIC_COLS)
    if df.empty:
        return df
    for col in ["maturity_date", "list_date", "delist_date", "last_edate", "last_ddate"]:
        df[col] = df[col].map(parse_date)
    return df[BASIC_COLS].drop_duplicates("ts_code")


def fetch_daily(client, trade_date: str) -> pd.DataFrame:
    frames = [
        fetch_api(client, "opt_daily", {"trade_date": trade_date, "exchange": exchange}, DAILY_COLS)
        for exchange in EXCHANGES
    ]
    non_empty = [frame for frame in frames if not frame.empty]
    df = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame(columns=DAILY_COLS)
    if df.empty:
        return df
    df["trade_date"] = df["trade_date"].map(parse_date)
    return df[DAILY_COLS].drop_duplicates(["ts_code", "trade_date"])


def previous_weekday(raw: str) -> str:
    day = date.fromisoformat(parse_date(raw))
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def fetch_latest_daily(client, start_trade_date: str, lookback_days: int) -> tuple[str, pd.DataFrame]:
    trade_date = start_trade_date
    for _attempt in range(max(1, lookback_days)):
        df = fetch_daily(client, trade_date)
        if not df.empty:
            return trade_date, df
        trade_date = previous_weekday(trade_date)
    return start_trade_date, pd.DataFrame(columns=DAILY_COLS)


def upsert_basic(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO cn_option_basic ({', '.join(BASIC_COLS)})
        VALUES ({', '.join(['%s'] * len(BASIC_COLS))})
        ON DUPLICATE KEY UPDATE
            {', '.join(f'{col}=VALUES({col})' for col in BASIC_COLS if col != 'ts_code')}
    """
    rows = []
    for row in df.itertuples(index=False):
        rows.append(
            tuple(
                to_float(getattr(row, col)) if col in {"per_unit", "exercise_price", "list_price", "min_price_chg"}
                else nullify(getattr(row, col))
                for col in BASIC_COLS
            )
        )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_daily(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO cn_option_daily ({', '.join(DAILY_COLS)})
        VALUES ({', '.join(['%s'] * len(DAILY_COLS))})
        ON DUPLICATE KEY UPDATE
            {', '.join(f'{col}=VALUES({col})' for col in DAILY_COLS if col not in {'ts_code', 'trade_date'})}
    """
    numeric = {"pre_settle", "pre_close", "open", "high", "low", "close", "settle", "vol", "amount", "oi"}
    rows = [
        tuple(to_float(getattr(row, col)) if col in numeric else nullify(getattr(row, col)) for col in DAILY_COLS)
        for row in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def print_summary(conn: pymysql.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MIN(list_date), MAX(maturity_date) FROM cn_option_basic")
        print(f"cn_option_basic: rows={cur.fetchone()}")
        cur.execute("SELECT COUNT(DISTINCT ts_code), COUNT(*), MIN(trade_date), MAX(trade_date) FROM cn_option_daily")
        print(f"cn_option_daily: codes,rows,range={cur.fetchone()}")
        cur.execute(
            """
            SELECT b.opt_code, b.call_put, COUNT(*)
            FROM cn_option_basic b
            JOIN cn_option_daily d ON d.ts_code=b.ts_code
            WHERE b.opt_code IN ('OP510300.SH','OP159919.SZ','OP510050.SH','OP510500.SH','OP159922.SZ')
            GROUP BY b.opt_code, b.call_put
            ORDER BY b.opt_code, b.call_put
            """
        )
        for row in cur.fetchall():
            print(f"  {row[0]} {row[1]} rows={row[2]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import China ETF option contract basics and daily snapshot")
    parser.add_argument("--trade-date", help="YYYYMMDD trade date. Default: latest available weekday within lookback.")
    parser.add_argument("--lookback-days", type=int, default=5)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        if args.summary_only:
            print_summary(conn)
            return 0

        client = create_client()
        basic = fetch_basic(client)
        requested_trade_date = args.trade_date or latest_possible_trade_date()
        if args.trade_date:
            trade_date = requested_trade_date
            daily = fetch_daily(client, trade_date)
        else:
            trade_date, daily = fetch_latest_daily(client, requested_trade_date, args.lookback_days)
        n_basic = upsert_basic(conn, basic)
        n_daily = upsert_daily(conn, daily)
        print(f"Imported cn_option_basic rows={n_basic}")
        print(f"Imported cn_option_daily rows={n_daily} trade_date={trade_date}")
        print_summary(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

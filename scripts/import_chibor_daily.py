#!/usr/bin/env python3
"""Import CHIBOR daily rates from akshare (East Money) into MySQL.

Source: ak.rate_interbank(market='中国银行同业拆借市场', symbol='Chibor人民币', indicator=...)
Coverage: 2004-05-24 ~ present (each tenor independently)

Purpose: provide pre-SHIBOR (before 2006-10-08) fallback for V5.0 scorecard
         field `rate_cum_bp_12m` (e.g. 2006 evaluation needs 2005-12-31 vs
         2004-12-31 rates).

Usage:
    python3 scripts/import_chibor_daily.py
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "chibor_daily_schema.sql"
CSV_PATH = DATA_DIR / "chibor_daily.csv"

# (akshare indicator → DB column name)
TENORS: list[tuple[str, str]] = [
    ("隔夜", "rate_on"),
    ("1周",  "rate_1w"),
    ("2周",  "rate_2w"),
    ("1月",  "rate_1m"),
    ("3月",  "rate_3m"),
    ("6月",  "rate_6m"),
    ("9月",  "rate_9m"),
    ("1年",  "rate_1y"),
]

MARKET = "中国银行同业拆借市场"
SYMBOL = "Chibor人民币"
REQUEST_SLEEP = 1.0


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def fetch_all_tenors() -> pd.DataFrame:
    """Pull each tenor independently and outer-join by trade_date."""
    frames: list[pd.DataFrame] = []
    for ind, col in TENORS:
        try:
            df = ak.rate_interbank(market=MARKET, symbol=SYMBOL, indicator=ind)
        except Exception as exc:
            print(f"  {ind} ({col}): fetch failed - {exc}")
            continue
        if df is None or df.empty:
            print(f"  {ind} ({col}): no data")
            continue
        df = df.rename(columns={"报告日": "trade_date", "利率": col})[
            ["trade_date", col]
        ]
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df = df.dropna(subset=["trade_date"])
        print(f"  {ind} ({col}): {len(df)} rows "
              f"({df['trade_date'].min().date()} ~ {df['trade_date'].max().date()})")
        frames.append(df)
        time.sleep(REQUEST_SLEEP)

    if not frames:
        return pd.DataFrame()

    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="trade_date", how="outer")
    return out.sort_values("trade_date").reset_index(drop=True)


def upsert(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    cols = [c for _, c in TENORS if c in df.columns]
    sql = f"""
        INSERT INTO chibor_daily (trade_date, {', '.join(cols)})
        VALUES (%s, {', '.join(['%s'] * len(cols))})
        ON DUPLICATE KEY UPDATE
            {', '.join(f'{c}=VALUES({c})' for c in cols)}
    """
    rows: list[tuple] = []
    for r in df.itertuples(index=False):
        vals: list = [r.trade_date.strftime("%Y-%m-%d")]
        for c in cols:
            v = getattr(r, c, None)
            vals.append(None if pd.isna(v) else float(v))
        rows.append(tuple(vals))

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    warnings.filterwarnings("ignore")
    DATA_DIR.mkdir(exist_ok=True)

    print(f"Fetching CHIBOR ({MARKET} / {SYMBOL})...")
    df = fetch_all_tenors()
    if df.empty:
        print("No data fetched, abort.")
        sys.exit(1)
    df.to_csv(CSV_PATH, index=False)
    print(f"Saved CSV: {CSV_PATH}  ({len(df)} rows)")

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)
        n = upsert(conn, df)
        print(f"Upserted chibor_daily: {n} rows "
              f"({df['trade_date'].min().date()} ~ {df['trade_date'].max().date()})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

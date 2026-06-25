"""Market data helpers for backtests."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "prices"


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    import os

    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def load_listed_etfs(as_of: str, limit: int | None = None) -> pd.DataFrame:
    """Load ETFs listed on or before as_of from MySQL."""
    conn = pymysql.connect(**mysql_config())
    try:
        sql = """
            SELECT ts_code, extname, index_ts_code, index_name, list_date, list_status
            FROM passive_etf
            WHERE list_status = 'L'
              AND list_date IS NOT NULL
              AND list_date <= %s
            ORDER BY list_date, ts_code
        """
        df = pd.read_sql(sql, conn, params=[as_of])
    finally:
        conn.close()
    if limit:
        df = df.head(limit)
    df["list_date"] = pd.to_datetime(df["list_date"])
    return df


def fetch_etf_daily(
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch ETF close prices, wide format indexed by trade_date."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = create_client()
    frames: list[pd.Series] = []

    for code in ts_codes:
        cache_file = CACHE_DIR / f"{code.replace('.', '_')}_{start_date}_{end_date}.csv"
        if use_cache and cache_file.exists():
            s = pd.read_csv(cache_file, parse_dates=["trade_date"]).set_index("trade_date")["close"]
            s.name = code
            frames.append(s)
            continue

        time.sleep(1.2)
        data = client.query_http(
            "fund_daily",
            {"ts_code": code, "start_date": start_date.replace("-", ""), "end_date": end_date.replace("-", "")},
            timeout=90,
        )
        items = data["data"]["items"]
        if not items:
            continue
        fields = data["data"]["fields"]
        df = pd.DataFrame(items, columns=fields)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date")
        df.to_csv(cache_file, index=False)
        s = df.set_index("trade_date")["close"]
        s.name = code
        frames.append(s)

    if not frames:
        return pd.DataFrame()

    prices = pd.concat(frames, axis=1).sort_index()
    return prices

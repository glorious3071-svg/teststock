#!/usr/bin/env python3
"""Import interest rate series from Tushare into MySQL.

APIs (macro-useful):
  - shibor      (doc 149): domestic interbank liquidity, from 2006
  - shibor_lpr  (doc 151): policy lending benchmark, from 2013
  - libor USD   (doc 152): global dollar liquidity

Skipped (not useful for annual macro):
  - shibor_quote (doc 150): per-bank bid/ask, too granular
  - hibor        (doc 153): HK rates; proxy returns no data currently
"""

from __future__ import annotations

import os
import re
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
SCHEMA_FILE = ROOT / "sql" / "interest_rates_schema.sql"

# shibor max 2000 rows/request; libor/lpr max 4000
SHIBOR_START = "20060101"
LPR_START = "20131025"
LIBOR_START = "19900101"
LIBOR_CURR = "USD"
REQUEST_SLEEP = 1.2


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


def parse_date(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(int(value)) if isinstance(value, (int, float)) and not pd.isna(value) else str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none"):
        return None
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


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


def year_chunks(start_y: int, end_y: int) -> list[tuple[str, str]]:
    return [(f"{y}0101", f"{y}1231") for y in range(start_y, end_y + 1)]


def fetch_paged(client, api: str, start_date: str, end_date: str, extra: dict | None = None) -> pd.DataFrame:
    params = {"start_date": start_date, "end_date": end_date}
    if extra:
        params.update(extra)
    data = client.query_http(api, params, timeout=120)
    fields = data["data"]["fields"]
    items = data["data"]["items"]
    if not items:
        return pd.DataFrame(columns=fields)
    return pd.DataFrame(items, columns=fields)


def fetch_shibor(client) -> pd.DataFrame:
    end_y = date.today().year
    frames: list[pd.DataFrame] = []
    for start, end in year_chunks(2006, end_y):
        time.sleep(REQUEST_SLEEP)
        df = fetch_paged(client, "shibor", start, end)
        if not df.empty:
            frames.append(df)
            print(f"  shibor {start[:4]}: {len(df)} rows")
    out = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    out["trade_date"] = out["date"].map(parse_date)
    rename = {
        "on": "rate_on",
        "1w": "rate_1w",
        "2w": "rate_2w",
        "1m": "rate_1m",
        "3m": "rate_3m",
        "6m": "rate_6m",
        "9m": "rate_9m",
        "1y": "rate_1y",
    }
    return out.rename(columns=rename).drop(columns=["date"])


def fetch_lpr(client) -> pd.DataFrame:
    end_y = date.today().year
    frames: list[pd.DataFrame] = []
    for start, end in year_chunks(2013, end_y):
        time.sleep(REQUEST_SLEEP)
        df = fetch_paged(client, "shibor_lpr", start, end)
        if not df.empty:
            frames.append(df)
            print(f"  lpr {start[:4]}: {len(df)} rows")
    out = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    out["trade_date"] = out["date"].map(parse_date)
    return out.rename(columns={"1y": "lpr_1y", "5y": "lpr_5y"}).drop(columns=["date"])


def fetch_libor_usd(client) -> pd.DataFrame:
    end_y = date.today().year
    frames: list[pd.DataFrame] = []
    for start, end in year_chunks(1990, end_y):
        time.sleep(REQUEST_SLEEP)
        df = fetch_paged(client, "libor", start, end, {"curr_type": LIBOR_CURR})
        if not df.empty:
            frames.append(df)
            if int(start[:4]) % 5 == 0:
                print(f"  libor USD {start[:4]}: {len(df)} rows (chunk)")
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "curr_type"])
    out["trade_date"] = out["date"].map(parse_date)
    rename = {
        "on": "rate_on",
        "1w": "rate_1w",
        "1m": "rate_1m",
        "2m": "rate_2m",
        "3m": "rate_3m",
        "6m": "rate_6m",
        "12m": "rate_12m",
    }
    return out.rename(columns=rename).drop(columns=["date"])


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def upsert_shibor(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO shibor_daily
            (trade_date, rate_on, rate_1w, rate_2w, rate_1m, rate_3m, rate_6m, rate_9m, rate_1y)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            rate_on=VALUES(rate_on), rate_1w=VALUES(rate_1w), rate_2w=VALUES(rate_2w),
            rate_1m=VALUES(rate_1m), rate_3m=VALUES(rate_3m), rate_6m=VALUES(rate_6m),
            rate_9m=VALUES(rate_9m), rate_1y=VALUES(rate_1y)
    """
    rows = [
        (
            r.trade_date,
            to_float(r.rate_on),
            to_float(r.rate_1w),
            to_float(r.rate_2w),
            to_float(r.rate_1m),
            to_float(r.rate_3m),
            to_float(r.rate_6m),
            to_float(r.rate_9m),
            to_float(r.rate_1y),
        )
        for r in df.itertuples(index=False)
        if r.trade_date
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_lpr(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO lpr_daily (trade_date, lpr_1y, lpr_5y)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE lpr_1y=VALUES(lpr_1y), lpr_5y=VALUES(lpr_5y)
    """
    rows = [
        (r.trade_date, to_float(r.lpr_1y), to_float(r.lpr_5y))
        for r in df.itertuples(index=False)
        if r.trade_date
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_libor(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO libor_daily
            (trade_date, curr_type, rate_on, rate_1w, rate_1m, rate_2m, rate_3m, rate_6m, rate_12m)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            rate_on=VALUES(rate_on), rate_1w=VALUES(rate_1w), rate_1m=VALUES(rate_1m),
            rate_2m=VALUES(rate_2m), rate_3m=VALUES(rate_3m), rate_6m=VALUES(rate_6m),
            rate_12m=VALUES(rate_12m)
    """
    rows = [
        (
            r.trade_date,
            r.curr_type or LIBOR_CURR,
            to_float(r.rate_on),
            to_float(r.rate_1w),
            to_float(r.rate_1m),
            to_float(r.rate_2m),
            to_float(r.rate_3m),
            to_float(r.rate_6m),
            to_float(r.rate_12m),
        )
        for r in df.itertuples(index=False)
        if r.trade_date
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    print("Fetching SHIBOR...")
    shibor = fetch_shibor(client)
    print(f"  total: {len(shibor)} rows ({shibor['trade_date'].min()} .. {shibor['trade_date'].max()})")
    shibor.to_csv(DATA_DIR / "shibor_daily.csv", index=False)

    print("Fetching LPR...")
    lpr = fetch_lpr(client)
    print(f"  total: {len(lpr)} rows ({lpr['trade_date'].min()} .. {lpr['trade_date'].max()})")
    lpr.to_csv(DATA_DIR / "lpr_daily.csv", index=False)

    print("Fetching LIBOR (USD)...")
    libor = fetch_libor_usd(client)
    print(f"  total: {len(libor)} rows ({libor['trade_date'].min()} .. {libor['trade_date'].max()})")
    libor.to_csv(DATA_DIR / "libor_usd_daily.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n1 = upsert_shibor(conn, shibor)
        print(f"Upserted shibor_daily: {n1}")

        n2 = upsert_lpr(conn, lpr)
        print(f"Upserted lpr_daily: {n2}")

        n3 = upsert_libor(conn, libor)
        print(f"Upserted libor_daily (USD): {n3}")

        # Build annual snapshots after raw data is loaded
        from macro.annual_snapshot import rebuild_annual_snapshots

        n4 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n4} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

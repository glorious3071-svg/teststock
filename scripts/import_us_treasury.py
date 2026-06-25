#!/usr/bin/env python3
"""Import US Treasury rate series from Tushare (docs 219-223) into MySQL.

APIs:
  - us_tycr  (219): nominal yield curve
  - us_trycr (220): real yield curve
  - us_tbr   (221): short-term bill rates
  - us_tltr  (222): long-term rates
  - us_trltr (223): real long-term average
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
SCHEMA_FILE = ROOT / "sql/us_treasury_schema.sql"
REQUEST_SLEEP = 0.8
START_YEAR = 2006

SNAPSHOT_COLS = [
    ("us_rate_date", "DATE NULL COMMENT '美债取数日'"),
    ("us_10y_nominal", "DECIMAL(8,4) NULL COMMENT '美债10Y名义%'"),
    ("us_10y_real", "DECIMAL(8,4) NULL COMMENT '美债10Y实际%'"),
    ("us_tbill_13w", "DECIMAL(8,4) NULL COMMENT '美债13周%'"),
    ("us_10y_real_yoy_bp", "DECIMAL(8,2) NULL COMMENT '10Y实际利率同比bp'"),
    ("global_rate_stance", "VARCHAR(20) NULL COMMENT '全球利率环境'"),
]

API_SPECS: dict[str, dict] = {
    "us_tycr": {
        "table": "us_tycr_daily",
        "cols": ["m1", "m2", "m3", "m4", "m6", "y1", "y2", "y3", "y5", "y7", "y10", "y20", "y30"],
        "csv": "us_tycr_daily.csv",
    },
    "us_trycr": {
        "table": "us_trycr_daily",
        "cols": ["y5", "y7", "y10", "y20", "y30"],
        "csv": "us_trycr_daily.csv",
    },
    "us_tbr": {
        "table": "us_tbr_daily",
        "cols": ["w4_ce", "w8_ce", "w13_ce", "w26_ce", "w52_ce"],
        "csv": "us_tbr_daily.csv",
    },
    "us_tltr": {
        "table": "us_tltr_daily",
        "cols": ["ltc", "cmt", "e_factor"],
        "csv": "us_tltr_daily.csv",
    },
    "us_trltr": {
        "table": "us_trltr_daily",
        "cols": ["ltr_avg"],
        "csv": "us_trltr_daily.csv",
    },
}


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


def fetch_yearly(client, api: str, start_year: int, end_year: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        time.sleep(REQUEST_SLEEP)
        data = client.query_http(
            api,
            {"start_date": f"{year}0101", "end_date": f"{year}1231"},
            timeout=120,
        )
        fields = data["data"]["fields"]
        items = data["data"]["items"]
        if not items:
            continue
        df = pd.DataFrame(items, columns=fields)
        frames.append(df)
        print(f"  {api} {year}: {len(df)} rows")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "date" in out.columns:
        out["trade_date"] = out["date"].map(parse_date)
        out = out.drop(columns=["date"])
    out = out.drop_duplicates("trade_date")
    return out


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


def upsert_table(conn, table: str, df: pd.DataFrame, cols: list[str]) -> int:
    if df.empty:
        return 0
    col_sql = ", ".join(["trade_date"] + cols)
    placeholders = ", ".join(["%s"] * (1 + len(cols)))
    updates = ", ".join(f"{c}=VALUES({c})" for c in cols)
    sql = f"""
        INSERT INTO {table} ({col_sql})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {updates}
    """
    rows = [
        (r.trade_date, *[to_float(getattr(r, c)) for c in cols])
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
    end_year = date.today().year

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        for api, spec in API_SPECS.items():
            print(f"Fetching {api}...")
            df = fetch_yearly(client, api, START_YEAR, end_year)
            if df.empty:
                print(f"  no data returned for {api}")
                continue
            keep = ["trade_date"] + [c for c in spec["cols"] if c in df.columns]
            df = df[keep].sort_values("trade_date").reset_index(drop=True)
            print(f"  total: {len(df)} rows ({df['trade_date'].min()} .. {df['trade_date'].max()})")
            df.to_csv(DATA_DIR / spec["csv"], index=False)
            n = upsert_table(conn, spec["table"], df, spec["cols"])
            print(f"  Upserted {spec['table']}: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

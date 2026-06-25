#!/usr/bin/env python3
"""Import margin trading summary from Tushare margin API (doc_id=58)."""

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
SCHEMA_FILE = ROOT / "sql" / "margin_schema.sql"
REQUEST_SLEEP = 0.8
START_YEAR = 2010

SNAPSHOT_COLS = [
    ("margin_date", "DATE NULL COMMENT '两融取数日'"),
    ("margin_rzye", "DECIMAL(20,2) NULL COMMENT '融资余额合计元(SSE+SZSE)'"),
    ("margin_rqye", "DECIMAL(20,2) NULL COMMENT '融券余额合计元'"),
    ("margin_rzrqye", "DECIMAL(20,2) NULL COMMENT '两融余额合计元'"),
    ("margin_rzrqye_yoy_pct", "DECIMAL(8,2) NULL COMMENT '两融余额同比%'"),
    ("margin_stance", "VARCHAR(20) NULL COMMENT '两融情绪'"),
]

DB_COLS = ["rzye", "rzmre", "rzche", "rqye", "rqmcl", "rzrqye", "rqyl"]


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


def fetch_margin_year(client, year: int) -> pd.DataFrame:
    data = client.query_http(
        "margin",
        {"start_date": f"{year}0101", "end_date": f"{year}1231"},
        timeout=120,
    )
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame(columns=["trade_date", "exchange_id", *DB_COLS])
    return pd.DataFrame(items, columns=fields)


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trade_date"] = out["trade_date"].map(parse_trade_date)
    out = out.dropna(subset=["trade_date", "exchange_id"])
    keep = ["trade_date", "exchange_id", *DB_COLS]
    return out[keep]


def upsert_rows(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO margin_daily
            (trade_date, exchange_id, {", ".join(DB_COLS)})
        VALUES (%s,%s,{",".join(["%s"] * len(DB_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in DB_COLS)}
    """
    rows = [
        (
            r.trade_date,
            r.exchange_id,
            *[to_float(getattr(r, c)) for c in DB_COLS],
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
        end_year = date.today().year
        frames: list[pd.DataFrame] = []
        for year in range(START_YEAR, end_year + 1):
            print(f"Fetching margin {year}...")
            raw = fetch_margin_year(client, year)
            prepared = prepare_df(raw)
            print(f"  rows: {len(prepared)}")
            frames.append(prepared)
            total += upsert_rows(conn, prepared)
            time.sleep(REQUEST_SLEEP)

        frames_non_empty = [f for f in frames if not f.empty]
        if frames_non_empty:
            pd.concat(frames_non_empty, ignore_index=True).to_csv(
                DATA_DIR / "margin_daily.csv", index=False
            )

        from macro.annual_snapshot import rebuild_annual_snapshots

        snap_n = rebuild_annual_snapshots(conn)
        print(f"Upserted margin_daily: {total} rows")
        print(f"Rebuilt macro_annual_snapshot: {snap_n} years")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()

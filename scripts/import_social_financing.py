#!/usr/bin/env python3
"""Import social financing monthly data from Tushare sf_month (doc 310) into MySQL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql/social_financing_schema.sql"

SNAPSHOT_COLS = [
    ("sf_month", "CHAR(6) NULL COMMENT '社融参考月份'"),
    ("sf_inc_cumval", "DECIMAL(14,2) NULL COMMENT '上年社融增量累计(亿元)'"),
    ("sf_stk_endval", "DECIMAL(10,2) NULL COMMENT '社融存量(万亿元)'"),
    ("sf_stk_yoy", "DECIMAL(6,2) NULL COMMENT '社融存量同比%'"),
    ("sf_stance", "VARCHAR(20) NULL COMMENT '信用环境'"),
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


def parse_month(month: str) -> tuple[int, int]:
    return int(month[:4]), int(month[4:6])


def fetch_sf_month(client) -> pd.DataFrame:
    data = client.query_http("sf_month", {}, timeout=120)
    df = pd.DataFrame(data["data"]["items"], columns=data["data"]["fields"])
    years, months = zip(*[parse_month(m) for m in df["month"]])
    df["cal_year"] = years
    df["cal_month"] = months
    return df


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


def upsert_sf(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO sf_monthly (month, cal_year, cal_month, inc_month, inc_cumval, stk_endval)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_month=VALUES(cal_month),
            inc_month=VALUES(inc_month), inc_cumval=VALUES(inc_cumval),
            stk_endval=VALUES(stk_endval)
    """
    rows = [
        (
            r.month, int(r.cal_year), int(r.cal_month),
            to_float(r.inc_month), to_float(r.inc_cumval), to_float(r.stk_endval),
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

    print("Fetching sf_month...")
    sf = fetch_sf_month(client)
    sf_2010 = sf[sf["cal_year"] >= 2010]
    print(f"  total: {len(sf)} rows ({sf['month'].min()} .. {sf['month'].max()})")
    print(f"  from 2010: {len(sf_2010)}")
    sf.to_csv(DATA_DIR / "sf_monthly.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n = upsert_sf(conn, sf)
        print(f"Upserted sf_monthly: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n2 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n2} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

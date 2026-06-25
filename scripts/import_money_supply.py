#!/usr/bin/env python3
"""Import money supply monthly data from Tushare cn_m (doc 242) into MySQL."""

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
SCHEMA_FILE = ROOT / "sql/money_supply_schema.sql"

SNAPSHOT_COLS = [
    ("money_month", "CHAR(6) NULL COMMENT '货币供应参考月份'"),
    ("m1_yoy", "DECIMAL(6,2) NULL COMMENT 'M1同比%'"),
    ("m2_yoy", "DECIMAL(6,2) NULL COMMENT 'M2同比%'"),
    ("m1_m2_scissors", "DECIMAL(6,2) NULL COMMENT 'M1-M2同比增速差'"),
    ("money_stance", "VARCHAR(20) NULL COMMENT '货币环境'"),
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


def fetch_cn_m(client) -> pd.DataFrame:
    data = client.query_http("cn_m", {}, timeout=120)
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


def upsert_cn_m(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_m_monthly (
            month, cal_year, cal_month,
            m0, m0_yoy, m0_mom, m1, m1_yoy, m1_mom, m2, m2_yoy, m2_mom
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_month=VALUES(cal_month),
            m0=VALUES(m0), m0_yoy=VALUES(m0_yoy), m0_mom=VALUES(m0_mom),
            m1=VALUES(m1), m1_yoy=VALUES(m1_yoy), m1_mom=VALUES(m1_mom),
            m2=VALUES(m2), m2_yoy=VALUES(m2_yoy), m2_mom=VALUES(m2_mom)
    """
    rows = [
        (
            r.month, int(r.cal_year), int(r.cal_month),
            to_float(r.m0), to_float(r.m0_yoy), to_float(r.m0_mom),
            to_float(r.m1), to_float(r.m1_yoy), to_float(r.m1_mom),
            to_float(r.m2), to_float(r.m2_yoy), to_float(r.m2_mom),
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

    print("Fetching cn_m...")
    money = fetch_cn_m(client)
    money_2006 = money[money["cal_year"] >= 2006]
    print(f"  total: {len(money)} rows ({money['month'].min()} .. {money['month'].max()})")
    print(f"  backtest range (>=2006): {len(money_2006)}")
    money.to_csv(DATA_DIR / "cn_m_monthly.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n = upsert_cn_m(conn, money)
        print(f"Upserted cn_m_monthly: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n2 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n2} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

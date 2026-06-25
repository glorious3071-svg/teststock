#!/usr/bin/env python3
"""Import GDP quarterly data from Tushare cn_gdp (doc 227) into MySQL."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "cn_gdp_schema.sql"

GDP_COLS = [
    ("gdp_quarter", "VARCHAR(7) NULL COMMENT '参考季度'"),
    ("gdp_yoy", "DECIMAL(6,2) NULL COMMENT 'GDP同比增速%'"),
    ("si_yoy", "DECIMAL(6,2) NULL COMMENT '第二产业同比增速%'"),
    ("ti_yoy", "DECIMAL(6,2) NULL COMMENT '第三产业同比增速%'"),
    ("growth_stance", "VARCHAR(20) NULL COMMENT '增长态势'"),
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


def parse_quarter(quarter: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})Q([1-4])", quarter)
    if not m:
        raise ValueError(f"invalid quarter: {quarter}")
    return int(m.group(1)), int(m.group(2))


def fetch_cn_gdp(client, start_q: str = "1952Q4", end_q: str = "2030Q4") -> pd.DataFrame:
    data = client.query_http("cn_gdp", {"start_q": start_q, "end_q": end_q}, timeout=120)
    df = pd.DataFrame(data["data"]["items"], columns=data["data"]["fields"])
    years, quarters = zip(*[parse_quarter(q) for q in df["quarter"]])
    df["cal_year"] = years
    df["cal_quarter"] = quarters
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
        for col, spec in GDP_COLS:
            if col not in existing:
                cur.execute(f"ALTER TABLE macro_annual_snapshot ADD COLUMN {col} {spec}")
    conn.commit()


def upsert_gdp(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_gdp_quarterly (
            quarter, cal_year, cal_quarter,
            gdp, gdp_yoy, pi, pi_yoy, si, si_yoy, ti, ti_yoy
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_quarter=VALUES(cal_quarter),
            gdp=VALUES(gdp), gdp_yoy=VALUES(gdp_yoy),
            pi=VALUES(pi), pi_yoy=VALUES(pi_yoy),
            si=VALUES(si), si_yoy=VALUES(si_yoy),
            ti=VALUES(ti), ti_yoy=VALUES(ti_yoy)
    """
    rows = [
        (
            r.quarter,
            int(r.cal_year),
            int(r.cal_quarter),
            to_float(r.gdp),
            to_float(r.gdp_yoy),
            to_float(r.pi),
            to_float(r.pi_yoy),
            to_float(r.si),
            to_float(r.si_yoy),
            to_float(r.ti),
            to_float(r.ti_yoy),
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

    print("Fetching cn_gdp...")
    gdp = fetch_cn_gdp(client)
    gdp_2006 = gdp[gdp["cal_year"] >= 2006]
    print(
        f"  total: {len(gdp)} rows ({gdp['quarter'].min()} .. {gdp['quarter'].max()})"
        f" | backtest range (>=2006): {len(gdp_2006)}"
    )
    gdp.to_csv(DATA_DIR / "cn_gdp_quarterly.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n = upsert_gdp(conn, gdp)
        print(f"Upserted cn_gdp_quarterly: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n2 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n2} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

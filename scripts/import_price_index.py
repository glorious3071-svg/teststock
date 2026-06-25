#!/usr/bin/env python3
"""Import CPI/PPI monthly data from Tushare (doc 228, 245) into MySQL."""

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
SCHEMA_FILE = ROOT / "sql" / "price_index_schema.sql"

SNAPSHOT_COLS = [
    ("price_month", "CHAR(6) NULL COMMENT '参考月份'"),
    ("cpi_yoy", "DECIMAL(6,2) NULL COMMENT 'CPI同比%'"),
    ("cpi_accu", "DECIMAL(6,2) NULL COMMENT 'CPI累计同比%'"),
    ("ppi_yoy", "DECIMAL(6,2) NULL COMMENT 'PPI同比%'"),
    ("ppi_accu", "DECIMAL(6,2) NULL COMMENT 'PPI累计同比%'"),
    ("ppi_cpi_spread", "DECIMAL(6,2) NULL COMMENT 'PPI-CPI同比差'"),
    ("inflation_stance", "VARCHAR(20) NULL COMMENT '通胀态势'"),
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


def fetch_api(client, api: str) -> pd.DataFrame:
    data = client.query_http(api, {}, timeout=120)
    return pd.DataFrame(data["data"]["items"], columns=data["data"]["fields"])


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


def prepare_cpi(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    years, months = zip(*[parse_month(m) for m in out["month"]])
    out["cal_year"] = years
    out["cal_month"] = months
    keep = ["month", "cal_year", "cal_month", "nt_yoy", "nt_mom", "nt_accu", "town_yoy", "cnt_yoy"]
    return out[keep]


def prepare_ppi(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    years, months = zip(*[parse_month(m) for m in out["month"]])
    out["cal_year"] = years
    out["cal_month"] = months
    keep = [
        "month", "cal_year", "cal_month",
        "ppi_yoy", "ppi_mp_yoy", "ppi_mp_qm_yoy", "ppi_mp_rm_yoy", "ppi_mp_p_yoy",
        "ppi_cg_yoy", "ppi_mom", "ppi_accu",
    ]
    return out[keep]


def upsert_cpi(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_cpi_monthly
            (month, cal_year, cal_month, nt_yoy, nt_mom, nt_accu, town_yoy, cnt_yoy)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_month=VALUES(cal_month),
            nt_yoy=VALUES(nt_yoy), nt_mom=VALUES(nt_mom), nt_accu=VALUES(nt_accu),
            town_yoy=VALUES(town_yoy), cnt_yoy=VALUES(cnt_yoy)
    """
    rows = [
        (
            r.month, int(r.cal_year), int(r.cal_month),
            to_float(r.nt_yoy), to_float(r.nt_mom), to_float(r.nt_accu),
            to_float(r.town_yoy), to_float(r.cnt_yoy),
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_ppi(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_ppi_monthly (
            month, cal_year, cal_month,
            ppi_yoy, ppi_mp_yoy, ppi_mp_qm_yoy, ppi_mp_rm_yoy, ppi_mp_p_yoy,
            ppi_cg_yoy, ppi_mom, ppi_accu
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_month=VALUES(cal_month),
            ppi_yoy=VALUES(ppi_yoy), ppi_mp_yoy=VALUES(ppi_mp_yoy),
            ppi_mp_qm_yoy=VALUES(ppi_mp_qm_yoy), ppi_mp_rm_yoy=VALUES(ppi_mp_rm_yoy),
            ppi_mp_p_yoy=VALUES(ppi_mp_p_yoy), ppi_cg_yoy=VALUES(ppi_cg_yoy),
            ppi_mom=VALUES(ppi_mom), ppi_accu=VALUES(ppi_accu)
    """
    rows = [
        (
            r.month, int(r.cal_year), int(r.cal_month),
            to_float(r.ppi_yoy), to_float(r.ppi_mp_yoy), to_float(r.ppi_mp_qm_yoy),
            to_float(r.ppi_mp_rm_yoy), to_float(r.ppi_mp_p_yoy), to_float(r.ppi_cg_yoy),
            to_float(r.ppi_mom), to_float(r.ppi_accu),
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

    print("Fetching cn_cpi...")
    cpi_raw = fetch_api(client, "cn_cpi")
    cpi = prepare_cpi(cpi_raw)
    cpi_2006 = cpi[cpi["cal_year"] >= 2006]
    print(f"  total: {len(cpi)} rows ({cpi['month'].min()} .. {cpi['month'].max()})")
    print(f"  backtest range (>=2006): {len(cpi_2006)}")
    cpi.to_csv(DATA_DIR / "cn_cpi_monthly.csv", index=False)

    print("Fetching cn_ppi...")
    ppi_raw = fetch_api(client, "cn_ppi")
    ppi = prepare_ppi(ppi_raw)
    ppi_2006 = ppi[ppi["cal_year"] >= 2006]
    print(f"  total: {len(ppi)} rows ({ppi['month'].min()} .. {ppi['month'].max()})")
    print(f"  backtest range (>=2006): {len(ppi_2006)}")
    ppi.to_csv(DATA_DIR / "cn_ppi_monthly.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n1 = upsert_cpi(conn, cpi)
        print(f"Upserted cn_cpi_monthly: {n1}")

        n2 = upsert_ppi(conn, ppi)
        print(f"Upserted cn_ppi_monthly: {n2}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n3 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n3} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

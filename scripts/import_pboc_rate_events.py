#!/usr/bin/env python3
"""Import PBoC RRR changes and 1Y deposit benchmark rate from local CSV seeds.

Inputs (in repo, version-controlled seeds):
  - data/cn_rrr_changes.csv
  - data/cn_deposit_rate.csv

Tables created (if not exist):
  - cn_rrr_changes      (sql/cn_rrr_changes_schema.sql)
  - cn_deposit_rate     (sql/cn_deposit_rate_schema.sql)

Both feed V5.0 backtest/scorecard.py:
  - rrr_cum_pp_12m  = SUM(rrr_change_pp) over 12m, inst_type IN ('large','all')
  - deposit_1y_rate = latest rate_after_pct as of snapshot_date

Usage:
    python3 scripts/import_pboc_rate_events.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

DATA_DIR = ROOT / "data"
RRR_CSV = DATA_DIR / "cn_rrr_changes.csv"
DEPOSIT_CSV = DATA_DIR / "cn_deposit_rate.csv"
RRR_SCHEMA = ROOT / "sql" / "cn_rrr_changes_schema.sql"
DEPOSIT_SCHEMA = ROOT / "sql" / "cn_deposit_rate_schema.sql"


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


def apply_schema(conn: pymysql.connections.Connection, schema_path: Path) -> None:
    sql = schema_path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def to_native(value):
    """Convert NaN → None, pandas Timestamp → 'YYYY-MM-DD'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return value


def upsert_rrr(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_rrr_changes
            (effective_date, inst_type, rrr_change_pp, rrr_after_pp,
             direction, announce_date, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            rrr_change_pp = VALUES(rrr_change_pp),
            rrr_after_pp  = VALUES(rrr_after_pp),
            direction     = VALUES(direction),
            announce_date = VALUES(announce_date),
            note          = VALUES(note)
    """
    rows = [
        (
            to_native(r.effective_date),
            r.inst_type,
            float(r.rrr_change_pp),
            to_native(r.rrr_after_pp),
            r.direction,
            to_native(r.announce_date),
            to_native(r.note),
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_deposit(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_deposit_rate
            (effective_date, rate_after_pct, rate_change_pp,
             direction, announce_date, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            rate_after_pct = VALUES(rate_after_pct),
            rate_change_pp = VALUES(rate_change_pp),
            direction      = VALUES(direction),
            announce_date  = VALUES(announce_date),
            note           = VALUES(note)
    """
    rows = [
        (
            to_native(r.effective_date),
            float(r.rate_after_pct),
            to_native(r.rate_change_pp),
            r.direction,
            to_native(r.announce_date),
            to_native(r.note),
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def load_csv(path: Path, date_cols: list[str]) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Seed CSV not found: {path}")
    df = pd.read_csv(path)
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def main() -> None:
    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schemas...")
        apply_schema(conn, RRR_SCHEMA)
        apply_schema(conn, DEPOSIT_SCHEMA)

        print(f"Loading {RRR_CSV.name} ...")
        rrr_df = load_csv(RRR_CSV, ["effective_date", "announce_date"])
        n1 = upsert_rrr(conn, rrr_df)
        print(f"  upserted cn_rrr_changes: {n1} rows "
              f"({rrr_df['effective_date'].min().date()} ~ "
              f"{rrr_df['effective_date'].max().date()})")

        print(f"Loading {DEPOSIT_CSV.name} ...")
        dep_df = load_csv(DEPOSIT_CSV, ["effective_date", "announce_date"])
        n2 = upsert_deposit(conn, dep_df)
        print(f"  upserted cn_deposit_rate: {n2} rows "
              f"({dep_df['effective_date'].min().date()} ~ "
              f"{dep_df['effective_date'].max().date()})")

        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

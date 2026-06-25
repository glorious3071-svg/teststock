#!/usr/bin/env python3
"""Import ETF benchmark indices and passive ETFs into MySQL via Tushare."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
SCHEMA_SQL = ROOT / "sql" / "schema.sql"


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
    if isinstance(value, (int, float)) and not pd.isna(value):
        text = str(int(value))
    else:
        text = str(value).strip()
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


def fetch_etf_index(client) -> pd.DataFrame:
    data = client.query_http("etf_index", {}, timeout=120)
    df = pd.DataFrame(data["data"]["items"], columns=data["data"]["fields"])
    df["pub_date"] = df["pub_date"].map(parse_date)
    df["base_date"] = df["base_date"].map(parse_date)
    return df


def fetch_etf_basic(client) -> pd.DataFrame:
    data = client.query_http("etf_basic", {}, timeout=120)
    df = pd.DataFrame(data["data"]["items"], columns=data["data"]["fields"])
    df["setup_date"] = df["setup_date"].map(parse_date)
    df["list_date"] = df["list_date"].map(parse_date)
    return df


def filter_passive_etfs(df: pd.DataFrame) -> pd.DataFrame:
    """Keep index-tracking ETFs on SH/SZ, exclude money market and enhanced."""
    out = df.copy()
    out = out[out["index_code"].notna() & out["index_name"].notna()]
    out = out[out["exchange"].isin(["SH", "SZ"])]
    out = out[~out["extname"].str.contains("货币|保证金", na=False, regex=True)]
    out["extname"] = out["extname"].fillna(out["csname"])
    out = out[out["extname"].notna()]
    out["is_enhanced"] = out["extname"].str.contains("增强", na=False).astype(int)
    out = out[out["is_enhanced"] == 0]
    out = out.drop_duplicates("ts_code")
    return out


def supplement_benchmark_indices(index_df: pd.DataFrame, etf_df: pd.DataFrame) -> pd.DataFrame:
    """Add index codes referenced by ETFs but missing from etf_index."""
    known = set(index_df["ts_code"])
    missing = etf_df[~etf_df["index_code"].isin(known)][["index_code", "index_name"]].drop_duplicates()
    if missing.empty:
        return index_df
    stubs = pd.DataFrame(
        {
            "ts_code": missing["index_code"],
            "indx_name": missing["index_name"],
            "indx_csname": None,
            "pub_party_name": None,
            "pub_date": None,
            "base_date": None,
            "bp": None,
            "adj_circle": None,
        }
    )
    return pd.concat([index_df, stubs], ignore_index=True).drop_duplicates("ts_code")


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        cur.execute("DROP TABLE IF EXISTS passive_etf")
        cur.execute("DROP TABLE IF EXISTS etf_benchmark_index")
        cur.execute("DROP TABLE IF EXISTS market_index")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def insert_benchmark_indices(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO etf_benchmark_index
            (ts_code, indx_name, indx_csname, pub_party_name, pub_date, base_date, base_point, adj_circle)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = [
        (
            r.ts_code,
            r.indx_name,
            nullify(r.indx_csname),
            nullify(r.pub_party_name),
            nullify(r.pub_date),
            nullify(r.base_date),
            None if pd.isna(r.bp) else float(r.bp),
            nullify(r.adj_circle),
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def row_value(value, as_float: bool = False):
    if as_float:
        return None if value is None or (isinstance(value, float) and pd.isna(value)) else float(value)
    return nullify(value)


def insert_passive_etfs(conn, df: pd.DataFrame, valid_index_codes: set[str]) -> int:
    sql = """
        INSERT INTO passive_etf
            (ts_code, extname, cname, index_ts_code, index_name, setup_date, list_date,
             list_status, exchange, mgr_name, custod_name, mgt_fee, etf_type, is_enhanced)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = []
    for _, r in df.iterrows():
        index_code = row_value(r.get("index_code"))
        if index_code not in valid_index_codes:
            index_code = None
        rows.append(
            (
                row_value(r["ts_code"]),
                row_value(r["extname"]),
                row_value(r.get("cname")),
                index_code,
                row_value(r.get("index_name")),
                row_value(r.get("setup_date")),
                row_value(r.get("list_date")),
                row_value(r.get("list_status")),
                row_value(r.get("exchange")),
                row_value(r.get("mgr_name")),
                row_value(r.get("custod_name")),
                row_value(r.get("mgt_fee"), as_float=True),
                row_value(r.get("etf_type")),
                int(r.get("is_enhanced") or 0),
            )
        )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    print("Fetching etf_index (benchmark indices)...")
    indices = fetch_etf_index(client)
    print(f"  etf_index rows: {len(indices)}")

    print("Fetching etf_basic (ETF list)...")
    etfs_raw = fetch_etf_basic(client)
    print(f"  etf_basic rows: {len(etfs_raw)}")

    etfs = filter_passive_etfs(etfs_raw)
    print(f"  passive index ETFs (SH/SZ, non-enhanced): {len(etfs)}")

    indices = supplement_benchmark_indices(indices, etfs)
    print(f"  benchmark indices (with ETF stubs): {len(indices)}")

    indices.to_csv(DATA_DIR / "etf_benchmark_index.csv", index=False)
    etfs.to_csv(DATA_DIR / "passive_etf_etf_basic.csv", index=False)

    cfg = mysql_config()
    conn = pymysql.connect(**cfg)
    try:
        print("Applying schema...")
        apply_schema(conn)

        n_idx = insert_benchmark_indices(conn, indices)
        print(f"Inserted etf_benchmark_index: {n_idx}")

        valid_codes = set(indices["ts_code"])
        n_etf = insert_passive_etfs(conn, etfs, valid_codes)
        print(f"Inserted passive_etf: {n_etf}")
        print(f"  list_date coverage: {etfs['list_date'].notna().sum()}/{len(etfs)}")
        print(f"  before 2021: {(pd.to_datetime(etfs['list_date']) < '2021-01-01').sum()}")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT list_status, COUNT(*) FROM passive_etf GROUP BY list_status ORDER BY COUNT(*) DESC
                """
            )
            print("\nETF by status:")
            for status, cnt in cur.fetchall():
                print(f"  {status}: {cnt}")

            cur.execute(
                """
                SELECT e.ts_code, e.extname, e.index_ts_code, i.indx_name, e.list_date
                FROM passive_etf e
                LEFT JOIN etf_benchmark_index i ON e.index_ts_code = i.ts_code
                WHERE e.ts_code IN (
                    '510050.SH','510300.SH','510500.SH','159901.SZ','159915.SZ','512100.SH'
                )
                ORDER BY e.ts_code
                """
            )
            print("\nKey ETFs:")
            for row in cur.fetchall():
                print(f"  {row[0]} {row[1]} -> {row[2]} {row[3]} ({row[4]})")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

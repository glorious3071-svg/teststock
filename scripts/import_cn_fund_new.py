#!/usr/bin/env python3.11
"""Import 月度新发公募基金 数据到 MySQL.

数据源：
  1) akshare fund_new_found_em  → cn_fund_new_monthly   (2023-至今，精确)
  2) data/cn_fund_new_yearly.csv → cn_fund_new_yearly   (2002-2023 年度兜底)

评分卡读取约定（详见 docs/v50_scorecard_spec.md）：
  - 优先取 cn_fund_new_monthly.new_fund_billion
  - 缺失则回退到 cn_fund_new_yearly.new_fund_billion / 12

用法：
    python3.11 scripts/import_cn_fund_new.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

DATA_DIR = ROOT / "data"
YEARLY_CSV = DATA_DIR / "cn_fund_new_yearly.csv"
MONTHLY_SCHEMA = ROOT / "sql" / "cn_fund_new_monthly_schema.sql"
YEARLY_SCHEMA = ROOT / "sql" / "cn_fund_new_yearly_schema.sql"

# akshare 接口给出的「基金类型」字段值粒度很细，统一映射到 6 类
TYPE_MAP = {
    "equity": ("股票型",),
    "mixed": ("混合型",),
    "index": ("指数型",),
    "bond": ("债券型",),
    "qdii": ("QDII",),
    "fof": ("FOF", "基金型"),
    "money": ("货币",),
    "commodity": ("商品", "黄金"),
}

# 进入 active_billion / 评分卡情绪打分的窄口径
ACTIVE_BUCKETS = ("equity", "mixed", "index")


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


def normalize_type(raw: str | None) -> str:
    """Map akshare 细类型 → 规范桶名"""
    if not raw:
        return "other"
    text = str(raw)
    for bucket, keywords in TYPE_MAP.items():
        for kw in keywords:
            if kw in text:
                return bucket
    return "other"


def fetch_monthly_from_akshare() -> pd.DataFrame:
    import akshare as ak

    df = ak.fund_new_found_em()
    df = df.dropna(subset=["成立日期", "募集份额"]).copy()
    df["成立日期"] = pd.to_datetime(df["成立日期"], errors="coerce")
    df = df.dropna(subset=["成立日期"])
    df["募集份额"] = pd.to_numeric(df["募集份额"], errors="coerce")
    df = df.dropna(subset=["募集份额"])

    df["ntype"] = df["基金类型"].map(normalize_type)
    df["month"] = df["成立日期"].dt.strftime("%Y%m")

    rows: list[dict] = []
    for month, g in df.groupby("month"):
        by_type = {
            bucket: round(float(g.loc[g["ntype"] == bucket, "募集份额"].sum()), 2)
            for bucket in set(TYPE_MAP) | {"other"}
            if (g["ntype"] == bucket).any()
        }
        rows.append({
            "month":            month,
            "cal_year":         int(month[:4]),
            "cal_month":        int(month[4:6]),
            "new_fund_count":   int(len(g)),
            "new_fund_billion": round(float(g["募集份额"].sum()), 2),
            "active_billion":   round(
                float(g.loc[g["ntype"].isin(ACTIVE_BUCKETS), "募集份额"].sum()), 2
            ),
            "bond_billion":     round(
                float(g.loc[g["ntype"] == "bond", "募集份额"].sum()), 2
            ),
            "qdii_billion":     round(
                float(g.loc[g["ntype"] == "qdii", "募集份额"].sum()), 2
            ),
            "by_type_json":     json.dumps(by_type, ensure_ascii=False),
            "source":           "em",
            "source_url":       "https://fund.eastmoney.com/data/xinfound.html",
            "notes":            None,
        })
    return pd.DataFrame(rows).sort_values("month").reset_index(drop=True)


def upsert_monthly(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO cn_fund_new_monthly
            (month, cal_year, cal_month, new_fund_count, new_fund_billion,
             active_billion, bond_billion, qdii_billion, by_type_json,
             source, source_url, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            new_fund_count   = VALUES(new_fund_count),
            new_fund_billion = VALUES(new_fund_billion),
            active_billion   = VALUES(active_billion),
            bond_billion     = VALUES(bond_billion),
            qdii_billion     = VALUES(qdii_billion),
            by_type_json     = VALUES(by_type_json),
            source           = VALUES(source),
            source_url       = VALUES(source_url),
            notes            = VALUES(notes)
    """
    rows = [
        (
            r.month, r.cal_year, r.cal_month, r.new_fund_count,
            r.new_fund_billion, r.active_billion, r.bond_billion,
            r.qdii_billion, r.by_type_json,
            r.source, r.source_url, r.notes,
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_yearly(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_fund_new_yearly
            (cal_year, new_fund_count, new_fund_billion, active_billion,
             source, source_url, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            new_fund_count   = VALUES(new_fund_count),
            new_fund_billion = VALUES(new_fund_billion),
            active_billion   = VALUES(active_billion),
            source           = VALUES(source),
            source_url       = VALUES(source_url),
            notes            = VALUES(notes)
    """

    def to_int(v):
        try:
            return None if pd.isna(v) else int(v)
        except (TypeError, ValueError):
            return None

    def to_float(v):
        try:
            return None if pd.isna(v) else float(v)
        except (TypeError, ValueError):
            return None

    def to_str(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return str(v)

    rows = [
        (
            int(r.cal_year),
            to_int(r.new_fund_count),
            float(r.new_fund_billion),
            to_float(r.active_billion),
            r.source,
            to_str(r.source_url),
            to_str(r.notes),
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import 月度新发公募基金")
    parser.add_argument("--skip-monthly", action="store_true",
                        help="跳过 akshare 月度抓取（仅刷新年度兜底）")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schemas...")
        apply_schema(conn, MONTHLY_SCHEMA)
        apply_schema(conn, YEARLY_SCHEMA)

        if not args.skip_monthly:
            print("Fetching akshare fund_new_found_em ...")
            monthly = fetch_monthly_from_akshare()
            n1 = upsert_monthly(conn, monthly)
            print(f"  upserted cn_fund_new_monthly: {n1} rows "
                  f"({monthly['month'].min()} ~ {monthly['month'].max()})")

        print(f"Loading {YEARLY_CSV.name} ...")
        yearly = pd.read_csv(YEARLY_CSV)
        n2 = upsert_yearly(conn, yearly)
        print(f"  upserted cn_fund_new_yearly: {n2} rows "
              f"({int(yearly['cal_year'].min())} ~ "
              f"{int(yearly['cal_year'].max())})")

        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Import PMI monthly data from Tushare cn_pmi (doc 325) into MySQL."""

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
SCHEMA_FILE = ROOT / "sql/pmi_schema.sql"
REQUEST_SLEEP = 0.8

PMI_COL_MAP = {
    "month": "month",
    "pmi010000": "pmi_mfg",
    "pmi010400": "pmi_production",
    "pmi010500": "pmi_new_order",
    "pmi020100": "pmi_non_mfg",
    "pmi030000": "pmi_composite",
}

SNAPSHOT_COLS = [
    ("pmi_month", "CHAR(6) NULL COMMENT 'PMI参考月份'"),
    ("pmi_mfg", "DECIMAL(5,2) NULL COMMENT '制造业PMI'"),
    ("pmi_non_mfg", "DECIMAL(5,2) NULL COMMENT '非制造业PMI'"),
    ("pmi_composite", "DECIMAL(5,2) NULL COMMENT '综合PMI'"),
    ("pmi_stance", "VARCHAR(20) NULL COMMENT '景气度'"),
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


def fetch_cn_pmi(client, start_year: int = 2005, end_year: int | None = None) -> pd.DataFrame:
    if end_year is None:
        end_year = date.today().year

    frames: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        time.sleep(REQUEST_SLEEP)
        data = client.query_http(
            "cn_pmi",
            {"start_m": f"{year}01", "end_m": f"{year}12"},
            timeout=120,
        )
        fields = [f.lower() for f in data["data"]["fields"]]
        items = data["data"]["items"]
        if not items:
            continue
        raw = pd.DataFrame(items, columns=fields)
        frames.append(raw)
        print(f"  cn_pmi {year}: {len(raw)} rows")

    if not frames:
        return pd.DataFrame(columns=list(PMI_COL_MAP.values()) + ["cal_year", "cal_month"])

    out = pd.concat(frames, ignore_index=True)
    rename = {k: v for k, v in PMI_COL_MAP.items() if k in out.columns}
    out = out.rename(columns=rename)
    out = out[list(PMI_COL_MAP.values())].copy()
    out = out[out["month"].notna()]
    out = out.drop_duplicates("month")
    years, months = zip(*[parse_month(m) for m in out["month"]])
    out["cal_year"] = years
    out["cal_month"] = months
    return out.sort_values("month").reset_index(drop=True)


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


def upsert_pmi(conn, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_pmi_monthly (
            month, cal_year, cal_month,
            pmi_mfg, pmi_production, pmi_new_order, pmi_non_mfg, pmi_composite
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            cal_year=VALUES(cal_year), cal_month=VALUES(cal_month),
            pmi_mfg=VALUES(pmi_mfg), pmi_production=VALUES(pmi_production),
            pmi_new_order=VALUES(pmi_new_order), pmi_non_mfg=VALUES(pmi_non_mfg),
            pmi_composite=VALUES(pmi_composite)
    """
    rows = [
        (
            r.month, int(r.cal_year), int(r.cal_month),
            to_float(r.pmi_mfg), to_float(r.pmi_production), to_float(r.pmi_new_order),
            to_float(r.pmi_non_mfg), to_float(r.pmi_composite),
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

    print("Fetching cn_pmi...")
    pmi = fetch_cn_pmi(client)
    pmi_2006 = pmi[pmi["cal_year"] >= 2006]
    print(
        f"  total: {len(pmi)} rows ({pmi['month'].min()} .. {pmi['month'].max()})"
        f" | backtest range (>=2006): {len(pmi_2006)}"
    )
    pmi.to_csv(DATA_DIR / "cn_pmi_monthly.csv", index=False)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema...")
        apply_schema(conn)

        n = upsert_pmi(conn, pmi)
        print(f"Upserted cn_pmi_monthly: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        n2 = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {n2} years")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3.11
"""拉取全球四大央行（Fed/ECB/BoE/BoJ）政策利率决议事件，入库 global_cb_rate_events。

数据源：akshare
  - macro_bank_usa_interest_rate      （Fed, 1982 至今）
  - macro_bank_euro_interest_rate     （ECB, 1999 至今）
  - macro_bank_english_interest_rate  （BoE, 1970 至今）
  - macro_bank_japan_interest_rate    （BoJ, 2008 至今）

入库规则：
  - 「今值」为 NaN 的预告记录（决议未公布）跳过
  - 同日同央行重复记录保留 LAST（akshare 偶有同日多条修正）
  - direction = hike / cut / hold 依据 rate_change_pp 推导
  - rate_before_pct 优先用 akshare「前值」；缺失则用上一条 rate_after_pct
  - 幂等：ON DUPLICATE KEY UPDATE

用法：
  python3.11 scripts/import_global_cb_rates.py [--cb FED ECB BOE BOJ] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# 央行 → akshare 接口 + 起止 sanity
CB_REGISTRY: dict[str, dict] = {
    "FED": {"fetcher": ak.macro_bank_usa_interest_rate,     "min_date": "1982-01-01"},
    "ECB": {"fetcher": ak.macro_bank_euro_interest_rate,    "min_date": "1999-01-01"},
    "BOE": {"fetcher": ak.macro_bank_english_interest_rate, "min_date": "1970-01-01"},
    "BOJ": {"fetcher": ak.macro_bank_japan_interest_rate,   "min_date": "2008-01-01"},
}

DIRECTION_TOLERANCE_PP = 0.001  # 小于此幅度判 hold（避免浮点误差）


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


def classify_direction(change_pp: float) -> str:
    if change_pp > DIRECTION_TOLERANCE_PP:
        return "hike"
    if change_pp < -DIRECTION_TOLERANCE_PP:
        return "cut"
    return "hold"


def normalize(cb_code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """akshare 原始 → 标准列；过滤未公布；同日去重保留 last；补 rate_before。"""
    df = raw.copy()
    df["effective_date"] = pd.to_datetime(df["日期"]).dt.date
    df["rate_after_pct"] = pd.to_numeric(df["今值"], errors="coerce")
    df["rate_before_raw"] = pd.to_numeric(df["前值"], errors="coerce")

    # 过滤未公布
    df = df.dropna(subset=["rate_after_pct"])
    # 过滤过早数据（小于 min_date）
    min_d = pd.to_datetime(CB_REGISTRY[cb_code]["min_date"]).date()
    df = df[df["effective_date"] >= min_d]
    # 同日去重，保留 LAST（最新修正）
    df = df.sort_values("effective_date").drop_duplicates("effective_date", keep="last")

    # rate_before: 优先用「前值」，缺失则向前回溯填充
    df["rate_before_pct"] = df["rate_before_raw"].fillna(
        df["rate_after_pct"].shift(1)
    )
    df["rate_change_pp"] = df["rate_after_pct"] - df["rate_before_pct"]
    # 首行可能没有 before → change=NaN → 视为 hold（首记录）
    df["rate_change_pp"] = df["rate_change_pp"].fillna(0)
    df["direction"] = df["rate_change_pp"].apply(classify_direction)

    df["cb_code"] = cb_code
    df["source"] = "akshare"
    cols = ["cb_code", "effective_date", "rate_before_pct",
            "rate_after_pct", "rate_change_pp", "direction", "source"]
    return df[cols].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO global_cb_rate_events
    (cb_code, effective_date, rate_before_pct, rate_after_pct,
     rate_change_pp, direction, source)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    rate_before_pct = VALUES(rate_before_pct),
    rate_after_pct  = VALUES(rate_after_pct),
    rate_change_pp  = VALUES(rate_change_pp),
    direction       = VALUES(direction),
    source          = VALUES(source);
"""


def upsert(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    # MySQL 不接受 float NaN，统一转 None
    cleaned: list[tuple] = []
    for row in rows:
        cleaned.append(tuple(
            None if (isinstance(v, float) and v != v) else v
            for v in row
        ))
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, cleaned)
    conn.commit()
    return len(cleaned)


def summary(df: pd.DataFrame, cb: str) -> None:
    n = len(df)
    n_hike = int((df["direction"] == "hike").sum())
    n_cut = int((df["direction"] == "cut").sum())
    n_hold = int((df["direction"] == "hold").sum())
    print(f"  [{cb}] {n} 条  {df['effective_date'].min()} ~ {df['effective_date'].max()}  "
          f"hike={n_hike} / cut={n_cut} / hold={n_hold}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cb", nargs="+", default=list(CB_REGISTRY.keys()),
                    choices=list(CB_REGISTRY.keys()),
                    help="只拉指定央行（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 summary，不写库")
    args = ap.parse_args()

    print(f"拉取央行: {args.cb}")
    all_rows: list[tuple] = []
    for cb in args.cb:
        try:
            raw = CB_REGISTRY[cb]["fetcher"]()
        except Exception as exc:
            print(f"  ✗ [{cb}] akshare 拉取失败: {exc}", file=sys.stderr)
            continue
        df = normalize(cb, raw)
        summary(df, cb)
        all_rows.extend(df.itertuples(index=False, name=None))

    if args.dry_run:
        print(f"\n[dry-run] 共 {len(all_rows)} 条，未入库")
        return 0

    conn = pymysql.connect(**mysql_config())
    try:
        n = upsert(conn, all_rows)
    finally:
        conn.close()
    print(f"\n入库完成：upsert {n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

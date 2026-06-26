#!/usr/bin/env python3.11
"""拉取 CBOE VIX 隐含波动率指数日频数据，入库 cboe_vix_daily。

数据源：CBOE 官方公开 CSV
  https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
  - 1990-01-02 至今，全量 9000+ 行
  - 字段：DATE, OPEN, HIGH, LOW, CLOSE
  - 1990-91 仅 CLOSE 字段（OHLC 同值），1992 起 OHLC 完整

入库规则：
  - close NaN/缺失 行跳过
  - 同日重复保留 LAST
  - 幂等：ON DUPLICATE KEY UPDATE

用法：
  python3.11 scripts/import_cboe_vix.py [--dry-run]
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

VIX_CSV_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)
HTTP_TIMEOUT_SECONDS = 60
USER_AGENT = "Mozilla/5.0 (compatible; teststock-vix-fetcher/1.0)"


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


def fetch_vix_csv() -> pd.DataFrame:
    """从 CBOE 拉取 VIX 历史 CSV 并解析为 DataFrame。"""
    req = urllib.request.Request(VIX_CSV_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read()
    return pd.read_csv(io.BytesIO(raw))


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """CBOE 原始 → 标准列；过滤 CLOSE 缺失；同日去重保留 LAST。"""
    df = raw.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    # 标准化：CBOE 列名是 DATE, OPEN, HIGH, LOW, CLOSE
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["close"])
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return df[["trade_date", "open", "high", "low", "close"]].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO cboe_vix_daily (trade_date, open, high, low, close)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    open  = VALUES(open),
    high  = VALUES(high),
    low   = VALUES(low),
    close = VALUES(close);
"""


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if pd.isna(value):
        return None
    return value


def upsert(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    cleaned = [tuple(_clean(v) for v in row) for row in rows]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, cleaned)
    conn.commit()
    return len(cleaned)


def summary(df: pd.DataFrame) -> None:
    print(f"  CBOE VIX  {len(df)} 条  "
          f"{df['trade_date'].min()} ~ {df['trade_date'].max()}  "
          f"close: min={df['close'].min():.2f} max={df['close'].max():.2f} "
          f"median={df['close'].median():.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 summary，不写库")
    args = ap.parse_args()

    print(f"拉取 CBOE VIX: {VIX_CSV_URL}")
    try:
        raw = fetch_vix_csv()
    except Exception as exc:
        print(f"  ✗ CBOE 拉取失败: {exc}", file=sys.stderr)
        return 1
    df = normalize(raw)
    summary(df)
    rows = list(df.itertuples(index=False, name=None))

    if args.dry_run:
        print(f"\n[dry-run] 共 {len(rows)} 条，未入库")
        return 0

    conn = pymysql.connect(**mysql_config())
    try:
        n = upsert(conn, rows)
    finally:
        conn.close()
    print(f"\n入库完成：upsert {n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

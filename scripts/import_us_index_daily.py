#!/usr/bin/env python3.11
"""拉取美股三大指数（SPX / IXIC / DJI）日频行情，入库 us_index_daily。

数据源：akshare
  - index_us_stock_sina(symbol='.INX')   → 标普 500
  - index_us_stock_sina(symbol='.IXIC')  → 纳斯达克综合
  - index_us_stock_sina(symbol='.DJI')   → 道琼斯工业

入库规则：
  - close 为 NaN 的行（未公布）跳过
  - 同日同 ts_code 重复保留 LAST
  - amount=0（2024 后新浪未回填）允许入库，不影响 monthly_pct
  - 幂等：ON DUPLICATE KEY UPDATE

用法：
  python3.11 scripts/import_us_index_daily.py [--symbols SPX IXIC DJI] [--dry-run]
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

# 内部 ts_code → akshare symbol
SYMBOL_REGISTRY: dict[str, dict] = {
    "SPX":  {"sina_symbol": ".INX",  "ts_code": "SPX.US",  "name": "标普 500"},
    "IXIC": {"sina_symbol": ".IXIC", "ts_code": "IXIC.US", "name": "纳斯达克综合"},
    "DJI":  {"sina_symbol": ".DJI",  "ts_code": "DJI.US",  "name": "道琼斯工业"},
}


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


def normalize(ts_code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """akshare 原始 → 标准列；过滤 close=NaN；同日去重保留 LAST。"""
    df = raw.copy()
    df["ts_code"] = ts_code
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    for col in ("open", "high", "low", "close", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")

    df = df.dropna(subset=["close"])
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")

    cols = ["ts_code", "trade_date", "open", "high", "low",
            "close", "volume", "amount"]
    return df[cols].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO us_index_daily
    (ts_code, trade_date, open, high, low, close, volume, amount)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    open   = VALUES(open),
    high   = VALUES(high),
    low    = VALUES(low),
    close  = VALUES(close),
    volume = VALUES(volume),
    amount = VALUES(amount);
"""


def _clean(value):
    """MySQL 不接受 float NaN / pandas NA，统一转 None。"""
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


def summary(df: pd.DataFrame, sym: str, name: str) -> None:
    n = len(df)
    print(f"  [{sym}] {name:>10}  {n} 条  "
          f"{df['trade_date'].min()} ~ {df['trade_date'].max()}  "
          f"close: min={df['close'].min():.2f} max={df['close'].max():.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOL_REGISTRY.keys()),
                    choices=list(SYMBOL_REGISTRY.keys()),
                    help="只拉指定指数（默认全部）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 summary，不写库")
    args = ap.parse_args()

    print(f"拉取美股指数: {args.symbols}")
    all_rows: list[tuple] = []
    for sym in args.symbols:
        meta = SYMBOL_REGISTRY[sym]
        try:
            raw = ak.index_us_stock_sina(symbol=meta["sina_symbol"])
        except Exception as exc:
            print(f"  ✗ [{sym}] akshare 拉取失败: {exc}", file=sys.stderr)
            continue
        df = normalize(meta["ts_code"], raw)
        summary(df, sym, meta["name"])
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

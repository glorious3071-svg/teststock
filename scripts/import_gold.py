#!/usr/bin/env python3.11
"""拉取黄金日频价格（SGE Au99.99 + COMEX GC）入库 gold_daily。

数据源（akshare）：
  - AU9999.SGE  上海黄金交易所 Au99.99 现货   spot_hist_sge('Au99.99')        CNY/克   2016-12-19 起
  - GC.FOREIGN  COMEX 黄金期货连续合约 (sina)  futures_foreign_hist('GC')      USD/盎司 2016-06-27 起

入库规则：
  - close NaN/缺失 行跳过
  - 同 (symbol, trade_date) 重复保留 LAST
  - 幂等：ON DUPLICATE KEY UPDATE

用法：
  python3.11 scripts/import_gold.py [--symbols AU9999.SGE GC.FOREIGN] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

REQUEST_SLEEP_SECONDS = 1.0


@dataclass(frozen=True)
class GoldSeries:
    """一个黄金价格序列的元信息 + 拉取函数。"""
    symbol:    str       # 入库 symbol 标识
    currency:  str       # CNY / USD
    unit:      str       # gram / troy_oz
    source:   str        # 来源标识
    fetch:     Callable[[], pd.DataFrame]


SERIES: dict[str, GoldSeries] = {
    "AU9999.SGE": GoldSeries(
        symbol="AU9999.SGE",
        currency="CNY",
        unit="gram",
        source="akshare_sge",
        fetch=lambda: ak.spot_hist_sge(symbol="Au99.99"),
    ),
    "GC.FOREIGN": GoldSeries(
        symbol="GC.FOREIGN",
        currency="USD",
        unit="troy_oz",
        source="akshare_sina_foreign",
        fetch=lambda: ak.futures_foreign_hist(symbol="GC"),
    ),
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


def normalize(raw: pd.DataFrame, series: GoldSeries) -> pd.DataFrame:
    """akshare 原始 DataFrame → 标准列；过滤 close 缺失；同日去重保留 LAST。"""
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=["symbol", "trade_date", "currency", "unit",
                     "open", "high", "low", "close", "volume", "source"]
        )
    df = raw.copy()
    df.columns = [c.strip().lower() for c in df.columns]

    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce") if col in df.columns else None

    # volume：sina foreign 全 0，归一为 NULL；SGE 接口无该字段
    if "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce")
        df["volume"] = vol.where(vol > 0)  # 0 视为缺失
    else:
        df["volume"] = None

    df = df.dropna(subset=["close"])
    df["symbol"] = series.symbol
    df["currency"] = series.currency
    df["unit"] = series.unit
    df["source"] = series.source

    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return df[["symbol", "trade_date", "currency", "unit",
               "open", "high", "low", "close", "volume", "source"]].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO gold_daily
    (symbol, trade_date, currency, unit, open, high, low, close, volume, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    currency = VALUES(currency),
    unit     = VALUES(unit),
    open     = VALUES(open),
    high     = VALUES(high),
    low      = VALUES(low),
    close    = VALUES(close),
    volume   = VALUES(volume),
    source   = VALUES(source);
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


def summary(df: pd.DataFrame, series: GoldSeries) -> None:
    print(
        f"  {series.symbol:<12} {len(df):>5} 条  "
        f"{df['trade_date'].min()} ~ {df['trade_date'].max()}  "
        f"close({series.currency}/{series.unit}): "
        f"min={df['close'].min():.2f} max={df['close'].max():.2f} "
        f"median={df['close'].median():.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbols",
        nargs="+",
        choices=sorted(SERIES.keys()),
        default=sorted(SERIES.keys()),
        help="限定拉取的 symbol（默认全部）",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 summary，不写库")
    args = ap.parse_args()

    print(f"目标 symbols: {args.symbols}")

    all_rows: list[tuple] = []
    for sym in args.symbols:
        series = SERIES[sym]
        print(f"\n拉取 {sym}（{series.source}）...")
        try:
            raw = series.fetch()
        except Exception as exc:
            print(f"  ✗ {sym} 拉取失败: {exc}", file=sys.stderr)
            continue
        df = normalize(raw, series)
        if df.empty:
            print(f"  ✗ {sym} 标准化后为空")
            continue
        summary(df, series)
        all_rows.extend(df.itertuples(index=False, name=None))
        time.sleep(REQUEST_SLEEP_SECONDS)

    if args.dry_run:
        print(f"\n[dry-run] 合计 {len(all_rows)} 条，未入库")
        return 0

    if not all_rows:
        print("\n无数据可入库", file=sys.stderr)
        return 1

    conn = pymysql.connect(**mysql_config())
    try:
        n = upsert(conn, all_rows)
    finally:
        conn.close()
    print(f"\n入库完成：upsert {n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

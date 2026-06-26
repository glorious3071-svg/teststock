#!/usr/bin/env python3.11
"""akshare 兜底：拉取美债名义曲线 (us_tycr_daily) 的 y2/y5/y10/y30 四个 tenor。

背景：
  仓库主路径 scripts/import_us_treasury.py 走 Tushare（teajoin 代理），但代理对
  us_tycr 接口返回 code=0 但 items=[]（无数据权限）。本脚本走 akshare 公开通道
  作为兜底，主要服务 10Y-2Y 倒挂分析（核心 tenor 都齐全）。

数据源：
  akshare.bond_zh_us_rate()
    1990-12-19 至今约 9000+ 行
    返回列：日期, 中国国债收益率(2年/5年/10年/30年/10年-2年), 中国GDP年增率,
            美国国债收益率(2年/5年/10年/30年/10年-2年), 美国GDP年增率

字段映射：
  美国国债收益率2年   → us_tycr_daily.y2
  美国国债收益率5年   → us_tycr_daily.y5
  美国国债收益率10年  → us_tycr_daily.y10
  美国国债收益率30年  → us_tycr_daily.y30
  其余 tenor (m1, m2, m3, m4, m6, y1, y3, y7, y20) 不在源数据中 → 保留 NULL

入库规则：
  - 4 个 tenor 全部为 NaN 的行跳过（无信息量）
  - 同日重复保留 LAST
  - 幂等：ON DUPLICATE KEY UPDATE（仅覆盖 y2/y5/y10/y30 四列，其它 tenor 列若已被
    Tushare 路径填过则保留 — Tushare 与 akshare 互补共存）

用法：
  python3.11 scripts/import_us_tycr_akshare.py [--start 2006-01-01] [--dry-run]
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

DEFAULT_START_DATE = "2006-01-01"

AKSHARE_TO_DB_COLS = {
    "美国国债收益率2年":  "y2",
    "美国国债收益率5年":  "y5",
    "美国国债收益率10年": "y10",
    "美国国债收益率30年": "y30",
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


def fetch_akshare() -> pd.DataFrame:
    """直接调用 akshare 接口，返回原始 DataFrame。"""
    return ak.bond_zh_us_rate()


def normalize(raw: pd.DataFrame, start_date: str) -> pd.DataFrame:
    """原始 → 标准列；过滤 4 tenor 全空；按 start_date 截断；同日去重。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["trade_date", "y2", "y5", "y10", "y30"])

    missing = [c for c in AKSHARE_TO_DB_COLS if c not in raw.columns]
    if missing:
        raise RuntimeError(f"akshare 返回字段缺失: {missing}")

    df = raw[["日期", *AKSHARE_TO_DB_COLS.keys()]].copy()
    df = df.rename(columns={"日期": "trade_date", **AKSHARE_TO_DB_COLS})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    tenor_cols = list(AKSHARE_TO_DB_COLS.values())
    for col in tenor_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=tenor_cols, how="all")
    df = df[df["trade_date"] >= pd.to_datetime(start_date).date()]
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return df[["trade_date", *tenor_cols]].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO us_tycr_daily (trade_date, y2, y5, y10, y30)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    y2  = COALESCE(VALUES(y2),  y2),
    y5  = COALESCE(VALUES(y5),  y5),
    y10 = COALESCE(VALUES(y10), y10),
    y30 = COALESCE(VALUES(y30), y30);
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
    print(
        f"  us_tycr (akshare) {len(df)} 条  "
        f"{df['trade_date'].min()} ~ {df['trade_date'].max()}"
    )
    for col in ("y2", "y5", "y10", "y30"):
        vals = df[col].dropna()
        if vals.empty:
            print(f"    {col}: 全空")
            continue
        print(f"    {col}: 非空 {len(vals)} 条  min={vals.min():.4f} max={vals.max():.4f} median={vals.median():.4f}")
    inv = (df["y10"] - df["y2"]).dropna()
    inv_negative = (inv < 0).sum()
    if not inv.empty:
        print(f"    10Y-2Y: 总 {len(inv)} 条, 倒挂 {inv_negative} 条 ({inv_negative / len(inv) * 100:.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=DEFAULT_START_DATE,
                    help=f"起始日期（默认 {DEFAULT_START_DATE}）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 summary，不写库")
    args = ap.parse_args()

    print("拉取 akshare bond_zh_us_rate ...")
    try:
        raw = fetch_akshare()
    except Exception as exc:
        print(f"  ✗ akshare 拉取失败: {exc}", file=sys.stderr)
        return 1

    df = normalize(raw, args.start)
    if df.empty:
        print("  ✗ 标准化后为空", file=sys.stderr)
        return 1
    summary(df)

    rows = list(df.itertuples(index=False, name=None))
    if args.dry_run:
        print(f"\n[dry-run] {len(rows)} 条未入库")
        return 0

    conn = pymysql.connect(**mysql_config())
    try:
        n = upsert(conn, rows)
    finally:
        conn.close()
    print(f"\n入库完成：upsert {n} 条（y2/y5/y10/y30 字段；其它 tenor 保留 NULL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Import global market & sentiment indicators into macro_monthly (long format).

数据源（全部 AkShare，无需 Tushare 积分）：
  - 美联储基准利率   → macro_bank_usa_interest_rate   事件驱动 → 月频展开
  - S&P500 收盘价    → index_us_stock_sina('.INX')    日频 → 月末快照
  - 纳斯达克收盘价   → index_us_stock_sina('.IXIC')   日频 → 月末快照
  - SOX 半导体指数   → macro_global_sox_index()        日频 → 月末快照
  - 黄金价格(SGE)    → spot_golden_benchmark_sge()     日频 → 月末快照，2016 年起
  - 两融余额(沪+深)  → macro_china_market_margin_sh/sz() 日频 → 月末合计
  - 北向资金累计净买  → stock_hsgt_hist_em('北向资金')  日频 → 月末快照

新增指标（写入 macro_monthly）：
  fed_rate          美联储基准利率 %（按生效月展开）
  sp500_close       S&P500 月末收盘
  nasdaq_close      纳斯达克月末收盘
  sox_close         SOX 半导体月末收盘
  gold_sge_close    SGE 黄金月末晚盘价（元/克，2016 年起）
  margin_balance    沪深两融余额合计月末（亿元）
  hsgt_cum_buy      北向资金累计净买入月末（亿元，2014 年起）

幂等：ON DUPLICATE KEY UPDATE，重跑安全。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv

SCHEMA_FILE = ROOT / "sql" / "macro_monthly_schema.sql"
GLOBAL_START_YEAR = 2000


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


def to_period(d: date) -> date:
    """任意日期 → 当月 1 日（用作 macro_monthly.period）。"""
    return date(d.year, d.month, 1)


def month_end_snapshot(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    indicator: str,
    date_fmt: str = "%Y-%m-%d",
) -> list[tuple[str, date, float | None]]:
    """日频 df → 每月最后一个交易日收盘价，存为月末快照。"""
    df = df.copy()
    df["_date"] = pd.to_datetime(df[date_col], format=date_fmt, errors="coerce")
    df = df.dropna(subset=["_date"])
    df["_period"] = df["_date"].dt.to_period("M")
    df["_val"] = pd.to_numeric(df[value_col], errors="coerce")
    # 每月取最后一个交易日的值
    monthly = (
        df.sort_values("_date")
        .groupby("_period", sort=True)
        .last()
        .reset_index()
    )
    rows = []
    for _, r in monthly.iterrows():
        period = date(r["_period"].year, r["_period"].month, 1)
        if period.year < GLOBAL_START_YEAR:
            continue
        val = to_float(r["_val"])
        if val is not None:
            rows.append((indicator, period, val))
    return rows


# ─────────────────────────────────────────────
# 各指标拉取函数
# ─────────────────────────────────────────────

def fetch_fed_rate() -> list[tuple[str, date, float | None]]:
    """美联储基准利率（决议事件）→ 按生效月展开为月频。"""
    df = ak.macro_bank_usa_interest_rate()
    # 字段：商品 / 日期(YYYY-MM-DD) / 今值 / 预测值 / 前值
    events: list[tuple[date, float]] = []
    for _, r in df.iterrows():
        raw = nullify(r.get("日期"))
        if raw is None:
            continue
        try:
            d = pd.to_datetime(str(raw)).date()
        except Exception:
            continue
        val = to_float(r.get("今值"))
        if val is not None:
            events.append((d, val))

    if not events:
        return []

    events.sort(key=lambda x: x[0])

    rows = []
    today = date.today()
    for i, (eff_date, val) in enumerate(events):
        next_eff = events[i + 1][0] if i + 1 < len(events) else today + timedelta(days=31)
        cur = date(eff_date.year, eff_date.month, 1)
        end = date(next_eff.year, next_eff.month, 1)
        while cur < end:
            if cur.year >= GLOBAL_START_YEAR:
                rows.append(("fed_rate", cur, val))
            cur = date(cur.year, cur.month + 1, 1) if cur.month < 12 else date(cur.year + 1, 1, 1)
    return rows


def fetch_sp500() -> list[tuple[str, date, float | None]]:
    """S&P500 日频 → 月末收盘快照。"""
    df = ak.index_us_stock_sina(symbol=".INX")
    return month_end_snapshot(df, "date", "close", "sp500_close")


def fetch_nasdaq() -> list[tuple[str, date, float | None]]:
    """纳斯达克综合指数 日频 → 月末收盘快照。"""
    df = ak.index_us_stock_sina(symbol=".IXIC")
    return month_end_snapshot(df, "date", "close", "nasdaq_close")


def fetch_sox() -> list[tuple[str, date, float | None]]:
    """SOX 费城半导体指数 日频 → 月末收盘快照。"""
    df = ak.macro_global_sox_index()
    # 字段：日期 / 最新值 / 涨跌幅 / ...
    return month_end_snapshot(df, "日期", "最新值", "sox_close")


def fetch_gold_sge() -> list[tuple[str, date, float | None]]:
    """上海黄金交易所黄金基准价 日频 → 月末晚盘价（元/克），2016 年起。"""
    df = ak.spot_golden_benchmark_sge()
    # 字段：交易时间 / 晚盘价 / 早盘价
    return month_end_snapshot(df, "交易时间", "晚盘价", "gold_sge_close")


def fetch_margin_balance() -> list[tuple[str, date, float | None]]:
    """沪深两融余额合计 月末（亿元），2010 年起。"""
    df_sh = ak.macro_china_market_margin_sh()
    df_sz = ak.macro_china_market_margin_sz()

    def _parse(df: pd.DataFrame, date_col: str, val_col: str) -> pd.Series:
        df = df.copy()
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce")
        df["_val"] = pd.to_numeric(df[val_col], errors="coerce")
        return df.dropna(subset=["_date", "_val"]).set_index("_date")["_val"]

    # 沪：融资余额（元）；深：融资余额（元）→ 合计后转亿元
    sh = _parse(df_sh, "日期", "融资余额")
    sz = _parse(df_sz, "日期", "融资余额")
    merged = sh.add(sz, fill_value=0).rename("total")
    merged.index = pd.to_datetime(merged.index)

    # 月末快照
    monthly = merged.resample("M").last()
    rows = []
    for period_end, val in monthly.items():
        period = date(period_end.year, period_end.month, 1)
        if period.year < GLOBAL_START_YEAR:
            continue
        v = to_float(val)
        if v is not None:
            rows.append(("margin_balance", period, round(v / 1e8, 2)))  # 元 → 亿元
    return rows


def fetch_hsgt_cum_buy() -> list[tuple[str, date, float | None]]:
    """北向资金历史累计净买入 月末快照（亿元），2014 年起。"""
    df = ak.stock_hsgt_hist_em(symbol="北向资金")
    # 字段：日期 / 当日成交净买额 / 买入成交额 / 卖出成交额 / 历史累计净买额 / ...
    return month_end_snapshot(df, "日期", "历史累计净买额", "hsgt_cum_buy")


# ─────────────────────────────────────────────
# 入库
# ─────────────────────────────────────────────

def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def upsert_rows(conn, rows: list[tuple[str, date, float | None]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO macro_monthly (indicator, period, value)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE value = VALUES(value)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema (idempotent)...")
        apply_schema(conn)

        fetchers = [
            ("美联储基准利率",     fetch_fed_rate),
            ("S&P500 月末",        fetch_sp500),
            ("纳斯达克月末",       fetch_nasdaq),
            ("SOX 半导体月末",     fetch_sox),
            ("黄金 SGE 月末",      fetch_gold_sge),
            ("两融余额合计月末",   fetch_margin_balance),
            ("北向资金累计净买月末", fetch_hsgt_cum_buy),
        ]

        total = 0
        for label, fn in fetchers:
            print(f"\n{label} ...")
            rows = fn()
            indicators = {r[0] for r in rows}
            print(f"  {len(rows)} 条记录，指标: {sorted(indicators)}")
            if rows:
                n = upsert_rows(conn, rows)
                total += n

        # 汇总校验
        with conn.cursor() as cur:
            cur.execute("""
                SELECT indicator, COUNT(*) AS months,
                       MIN(period), MAX(period)
                FROM macro_monthly
                GROUP BY indicator
                ORDER BY indicator
            """)
            print("\nmacro_monthly 完整覆盖：")
            for r in cur.fetchall():
                print(f"  {r[0]:<25} {r[1]:>4} 月  {r[2]} ~ {r[3]}")

        print(f"\nTotal upserted: {total}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

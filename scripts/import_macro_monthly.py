#!/usr/bin/env python3
"""Import monthly macro indicators into macro_monthly (long format).

数据源混合策略（Tushare 积分不足时降级到 AkShare）：
  - PMI 制造业/非制造业   → AkShare macro_china_pmi
  - PPI 同比/环比         → Tushare cn_ppi
  - CPI 同比/环比         → Tushare cn_cpi
  - M2/M1 同比余额        → AkShare macro_china_money_supply
  - 社融月增量            → Tushare sf_month
  - 工业增加值 YoY        → AkShare macro_china_industrial_production_yoy
  - 存款准备金率          → AkShare macro_china_reserve_requirement_ratio
                           （事件驱动，按生效月份展开为月频）

落表：macro_monthly (indicator, period, value)
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

from tushare_client import create_client

SCHEMA_FILE = ROOT / "sql" / "macro_monthly_schema.sql"

# 全局拉取起点（2000 年前宏观数据用于行业框架价值有限）
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


def parse_cn_month(text: str) -> date | None:
    """'2026年05月份' → date(2026, 5, 1)"""
    v = nullify(text)
    if v is None:
        return None
    m = re.match(r"(\d{4})年(\d{2})月", str(v))
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return None


def parse_yyyymm(text: str) -> date | None:
    """'202412' → date(2024, 12, 1)"""
    v = nullify(text)
    if v is None:
        return None
    s = str(v).strip()
    if len(s) == 6 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), 1)
    return None


def parse_cn_date(text: str) -> date | None:
    """'2025年05月15日' → date(2025, 5, 15)"""
    v = nullify(text)
    if v is None:
        return None
    m = re.match(r"(\d{4})年(\d{2})月(\d{2})日", str(v))
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ─────────────────────────────────────────────
# 各指标拉取函数，返回 list[(indicator, period, value)]
# ─────────────────────────────────────────────

def fetch_pmi() -> list[tuple[str, date, float | None]]:
    """AkShare: 制造业 + 非制造业 PMI，月频。"""
    df = ak.macro_china_pmi()
    rows = []
    for _, r in df.iterrows():
        period = parse_cn_month(r["月份"])
        if period is None or period.year < GLOBAL_START_YEAR:
            continue
        mfg = to_float(r["制造业-指数"])
        non = to_float(r["非制造业-指数"])
        if mfg is not None:
            rows.append(("pmi_mfg", period, mfg))
        if non is not None:
            rows.append(("pmi_non_mfg", period, non))
    return rows


def fetch_ppi(client) -> list[tuple[str, date, float | None]]:
    """Tushare: PPI 同比 / 环比。"""
    data = client.query_http("cn_ppi", {}, timeout=60)
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    df = pd.DataFrame(items, columns=fields)
    rows = []
    for _, r in df.iterrows():
        period = parse_yyyymm(r.get("month"))
        if period is None or period.year < GLOBAL_START_YEAR:
            continue
        yoy = to_float(r.get("ppi_yoy"))
        mom = to_float(r.get("ppi_mom"))
        if yoy is not None:
            rows.append(("ppi_yoy", period, yoy))
        if mom is not None:
            rows.append(("ppi_mom", period, mom))
    return rows


def fetch_cpi(client) -> list[tuple[str, date, float | None]]:
    """Tushare: CPI 同比 / 环比（全国）。"""
    data = client.query_http("cn_cpi", {}, timeout=60)
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    df = pd.DataFrame(items, columns=fields)
    rows = []
    for _, r in df.iterrows():
        period = parse_yyyymm(r.get("month"))
        if period is None or period.year < GLOBAL_START_YEAR:
            continue
        yoy = to_float(r.get("nt_yoy"))
        mom = to_float(r.get("nt_mom"))
        if yoy is not None:
            rows.append(("cpi_yoy", period, yoy))
        if mom is not None:
            rows.append(("cpi_mom", period, mom))
    return rows


def fetch_m2() -> list[tuple[str, date, float | None]]:
    """AkShare: M2/M1 同比增速 + M2 余额，月频。"""
    df = ak.macro_china_money_supply()
    rows = []
    for _, r in df.iterrows():
        period = parse_cn_month(r["月份"])
        if period is None or period.year < GLOBAL_START_YEAR:
            continue
        m2_yoy = to_float(r.get("货币和准货币(M2)-同比增长"))
        m2_bal = to_float(r.get("货币和准货币(M2)-数量(亿元)"))
        m1_yoy = to_float(r.get("货币(M1)-同比增长"))
        if m2_yoy is not None:
            rows.append(("m2_yoy", period, m2_yoy))
        if m2_bal is not None:
            rows.append(("m2_balance", period, m2_bal))
        if m1_yoy is not None:
            rows.append(("m1_yoy", period, m1_yoy))
    return rows


def fetch_sf(client) -> list[tuple[str, date, float | None]]:
    """Tushare: 社会融资规模月增量。"""
    data = client.query_http("sf_month", {}, timeout=60)
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    df = pd.DataFrame(items, columns=fields)
    rows = []
    for _, r in df.iterrows():
        period = parse_yyyymm(r.get("month"))
        if period is None or period.year < GLOBAL_START_YEAR:
            continue
        inc = to_float(r.get("inc_month"))
        if inc is not None:
            rows.append(("sf_inc_month", period, inc))
    return rows


def fetch_iva() -> list[tuple[str, date, float | None]]:
    """AkShare: 规模以上工业增加值 YoY %，月频。"""
    df = ak.macro_china_industrial_production_yoy()
    rows = []
    for _, r in df.iterrows():
        raw_date = nullify(r.get("日期"))
        if raw_date is None:
            continue
        try:
            d = pd.to_datetime(str(raw_date)).date()
        except Exception:
            continue
        period = date(d.year, d.month, 1)
        if period.year < GLOBAL_START_YEAR:
            continue
        val = to_float(r.get("今值"))
        if val is not None:
            rows.append(("iva_yoy", period, val))
    return rows


def fetch_rrr() -> list[tuple[str, date, float | None]]:
    """AkShare: 存款准备金率（大型金融机构），按生效月展开为月频。

    事件驱动接口 → 将每次调整的「调整后」值从生效月起展开到下次调整前，
    写入 rrr_large indicator，每月存一条。
    """
    df = ak.macro_china_reserve_requirement_ratio()
    events: list[tuple[date, float]] = []
    for _, r in df.iterrows():
        effective_date = parse_cn_date(r.get("生效时间", ""))
        if effective_date is None:
            continue
        val = to_float(r.get("大型金融机构-调整后"))
        if val is None:
            continue
        events.append((effective_date, val))

    if not events:
        return []

    # 按生效日期升序排列
    events.sort(key=lambda x: x[0])

    rows = []
    today = date.today()
    for i, (eff_date, val) in enumerate(events):
        next_eff = events[i + 1][0] if i + 1 < len(events) else today + timedelta(days=31)
        # 生效月 1 日到下次调整月前一月 1 日，每月写一条
        cur = date(eff_date.year, eff_date.month, 1)
        end = date(next_eff.year, next_eff.month, 1)
        while cur < end:
            if cur.year >= GLOBAL_START_YEAR:
                rows.append(("rrr_large", cur, val))
            # 下个月 1 日
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
    return rows


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
    client = create_client()
    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema (idempotent)...")
        apply_schema(conn)

        fetchers = [
            ("PMI 制造业/非制造业", lambda: fetch_pmi()),
            ("PPI 同比/环比",       lambda: fetch_ppi(client)),
            ("CPI 同比/环比",       lambda: fetch_cpi(client)),
            ("M2/M1 同比余额",      lambda: fetch_m2()),
            ("社融月增量",          lambda: fetch_sf(client)),
            ("工业增加值 YoY",      lambda: fetch_iva()),
            ("存款准备金率",        lambda: fetch_rrr()),
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

        # 校验汇总
        with conn.cursor() as cur:
            cur.execute("""
                SELECT indicator, COUNT(*) AS months,
                       MIN(period), MAX(period)
                FROM macro_monthly
                GROUP BY indicator
                ORDER BY indicator
            """)
            print("\nmacro_monthly 当前覆盖：")
            for r in cur.fetchall():
                print(f"  {r[0]:<20} {r[1]} 月  {r[2]} ~ {r[3]}")

        print(f"\nTotal upserted: {total}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

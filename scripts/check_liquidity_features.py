#!/usr/bin/env python3
"""Sanity check: V5.0 scorecard liquidity inputs over benchmark years.

Computes for snapshot_date = (year - 1)-12-31:
  - rate_cum_bp_12m   via SHIBOR 3M primary, CHIBOR 3M fallback
  - rrr_cum_pp_12m    = SUM(rrr_change_pp) over 12m, inst_type IN ('large','all')
  - deposit_1y_rate   two-stage:
      ≤ 2015-10-24  → cn_deposit_rate (PBoC benchmark step)
      >  2015-10-24  → SHIBOR 1Y, 30-day average (post-benchmark-freeze proxy)

Useful as a smoke test after seeding cn_rrr_changes, cn_deposit_rate,
chibor_daily.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv


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


def latest_rate_3m(cur, table: str, as_of: date) -> tuple[date | None, float | None]:
    cur.execute(
        f"SELECT trade_date, rate_3m FROM {table} "
        "WHERE trade_date <= %s AND rate_3m IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        (as_of,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return row[0], float(row[1])


def rate_cum_bp_12m(cur, as_of: date) -> tuple[float | None, str]:
    """SHIBOR primary; if either point missing, fall back to CHIBOR."""
    src = "shibor"
    d_now, r_now = latest_rate_3m(cur, "shibor_daily", as_of)
    d_yr,  r_yr  = latest_rate_3m(cur, "shibor_daily", as_of - timedelta(days=365))

    if r_now is None or r_yr is None:
        d_now, r_now = latest_rate_3m(cur, "chibor_daily", as_of)
        d_yr,  r_yr  = latest_rate_3m(cur, "chibor_daily", as_of - timedelta(days=365))
        src = "chibor"

    if r_now is None or r_yr is None:
        return None, "missing"
    return round((r_now - r_yr) * 100, 2), f"{src}  now={r_now}@{d_now}  prior={r_yr}@{d_yr}"


def rrr_cum_pp_12m(cur, as_of: date) -> float:
    cur.execute(
        "SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes "
        "WHERE effective_date > %s AND effective_date <= %s "
        "AND inst_type IN ('large','all')",
        (as_of - timedelta(days=365), as_of),
    )
    return float(cur.fetchone()[0] or 0)


PBOC_DEPOSIT_FREEZE = date(2015, 10, 24)   # 央行最后一次基准利率调整
SHIBOR_PROXY_WINDOW_DAYS = 30              # 2015 后用 SHIBOR 1Y 30 日均值


def deposit_1y_rate(cur, as_of: date) -> tuple[float | None, str]:
    """Two-stage:
        ≤ 2015-10-24 → cn_deposit_rate.rate_after_pct (latest before as_of)
        >  2015-10-24 → AVG(shibor_daily.rate_1y) in [as_of-30d, as_of]
    Returns (rate_pct, source_description).
    """
    if as_of <= PBOC_DEPOSIT_FREEZE:
        cur.execute(
            "SELECT effective_date, rate_after_pct FROM cn_deposit_rate "
            "WHERE effective_date <= %s ORDER BY effective_date DESC LIMIT 1",
            (as_of,),
        )
        row = cur.fetchone()
        if not row:
            return None, "missing"
        return float(row[1]), f"cn_deposit_rate@{row[0]} (PBoC 基准)"

    window_start = as_of - timedelta(days=SHIBOR_PROXY_WINDOW_DAYS)
    cur.execute(
        "SELECT AVG(rate_1y), MIN(trade_date), MAX(trade_date), COUNT(*) "
        "FROM shibor_daily "
        "WHERE rate_1y IS NOT NULL AND trade_date BETWEEN %s AND %s",
        (window_start, as_of),
    )
    avg, mn, mx, n = cur.fetchone()
    if avg is None:
        return None, "missing"
    return float(avg), f"SHIBOR 1Y {SHIBOR_PROXY_WINDOW_DAYS}d 均值 [{mn}~{mx}, n={n}] (后基准代理)"


def main() -> None:
    apply_years = [2006, 2008, 2010, 2012, 2015, 2016, 2018, 2020, 2022, 2024, 2026]
    conn = pymysql.connect(**mysql_config())
    try:
        with conn.cursor() as cur:
            print(f"{'年份':<6}{'snap_date':<12}{'rate_cum_bp':>13}  {'rrr_cum_pp':>11}  {'deposit_1y':>10}  来源")
            print("-" * 130)
            for yr in apply_years:
                snap = date(yr - 1, 12, 31)
                bp, src = rate_cum_bp_12m(cur, snap)
                rrr = rrr_cum_pp_12m(cur, snap)
                dep, dep_src = deposit_1y_rate(cur, snap)
                bp_s   = f"{bp:+8.1f} bp" if bp is not None else "    n/a"
                dep_s  = f"{dep:5.2f}%" if dep is not None else "  n/a"
                print(f"{yr:<6}{snap}  {bp_s:>13}  {rrr:>+8.2f} pp  {dep_s:>10}  "
                      f"rate={src} | dep={dep_src}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3.11
"""sentiment 规则候选方向 — 离线探索分析（不改 scorecard.py）

对 2008-2025 18 个 snapshot 月，分别用 4 套规则给信号，
看哪种规则的「方向命中率」最高（无伪信号、不错向）。

规则候选：
  v1 旧规则 (絶对阈值)
    new_fund_billion > 1500 → +1 (过热)
    new_fund_billion < 200  → -1 (机会)
    fund_doubling_6m == True → +1
  v2 动态分位 (trailing 24M)
    new_fund_billion > P75(24M) → +1
    new_fund_billion < P25(24M) → -1
    fund_doubling_6m == True → +1
  v3 v2 + 健康度过滤
    若 snapshot 月 fund_count < 5 → 整个 sentiment 跳过（避免监管伪信号）
  v4 极简（只用 fund_doubling_6m）
    fund_doubling_6m == True → +1
    其余跳过

判定指标：方向命中率（信号正确指向次年涨/跌）+ 错向次数 + 总触发次数
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DOUBLING_LAG_MIN = 100.0
HEALTH_MIN_COUNT = 5


def _conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )


def load_data():
    conn = _conn()
    monthly = pd.read_sql(
        """
        SELECT month, new_fund_count, new_fund_billion
        FROM cn_fund_new_monthly
        WHERE month >= '200401'
        ORDER BY month
        """,
        conn,
    )
    cs300 = pd.read_sql(
        """
        SELECT trade_date, close
        FROM index_daily
        WHERE ts_code='000300.SH'
        ORDER BY trade_date
        """,
        conn, parse_dates=["trade_date"], index_col="trade_date",
    )
    conn.close()
    monthly["date"] = pd.to_datetime(monthly["month"], format="%Y%m")
    monthly = monthly.set_index("date").sort_index()
    monthly["new_fund_billion"] = monthly["new_fund_billion"].astype(float)
    monthly["new_fund_count"] = monthly["new_fund_count"].astype(int)
    return monthly, cs300


# ── 规则候选 ────────────────────────────────────────────────
def rule_v1_legacy(snap_row, lag_row, _trailing):
    s = 0; items = []
    nb = snap_row["new_fund_billion"]
    if nb > 1500: s += 1; items.append("月发>1500")
    if nb < 200: s -= 1; items.append("月发<200")
    if (lag_row is not None and lag_row["new_fund_billion"] >= DOUBLING_LAG_MIN
            and nb >= 2.0 * lag_row["new_fund_billion"]):
        s += 1; items.append("6M翻倍")
    return s, items


def rule_v2_dynamic(snap_row, lag_row, trailing):
    s = 0; items = []
    nb = snap_row["new_fund_billion"]
    p75 = trailing.quantile(0.75)
    p25 = trailing.quantile(0.25)
    if nb > p75: s += 1; items.append(f"月发>P75({p75:.0f})")
    if nb < p25: s -= 1; items.append(f"月发<P25({p25:.0f})")
    if (lag_row is not None and lag_row["new_fund_billion"] >= DOUBLING_LAG_MIN
            and nb >= 2.0 * lag_row["new_fund_billion"]):
        s += 1; items.append("6M翻倍")
    return s, items


def rule_v3_dynamic_health(snap_row, lag_row, trailing):
    if snap_row["new_fund_count"] < HEALTH_MIN_COUNT:
        return 0, ["健康度过滤<5只→跳过"]
    return rule_v2_dynamic(snap_row, lag_row, trailing)


def rule_v4_doubling_only(snap_row, lag_row, _trailing):
    s = 0; items = []
    nb = snap_row["new_fund_billion"]
    if (lag_row is not None and lag_row["new_fund_billion"] >= DOUBLING_LAG_MIN
            and nb >= 2.0 * lag_row["new_fund_billion"]):
        s += 1; items.append("6M翻倍")
    return s, items


RULES = [
    ("v1 旧规则 (绝对1500/200)", rule_v1_legacy),
    ("v2 动态分位 (24M P75/P25)", rule_v2_dynamic),
    ("v3 动态分位+健康度过滤", rule_v3_dynamic_health),
    ("v4 极简(仅 6M翻倍)", rule_v4_doubling_only),
]


def main():
    monthly, cs300 = load_data()

    # 每年 1-1 评估 snapshot = (year-1, 12-31) 对应的最近一个月
    samples = []
    for year in range(2008, 2026):
        # snapshot 月 = year-1 的 12 月（取实际有数据的月）
        snap_m = f"{year - 1}12"
        if snap_m not in monthly.index.strftime("%Y%m").values:
            continue
        snap_dt = pd.Timestamp(snap_m + "01")
        snap_row = monthly.loc[snap_dt]
        # 6 月前
        lag_dt = snap_dt - pd.DateOffset(months=6)
        lag_m = lag_dt.strftime("%Y%m")
        lag_row = monthly.loc[lag_dt] if lag_dt in monthly.index else None
        # trailing 24M
        trailing = monthly.loc[
            (monthly.index >= snap_dt - pd.DateOffset(months=24))
            & (monthly.index < snap_dt)
        ]["new_fund_billion"]
        # 次年沪深300 涨跌
        cs_y_open = cs300.loc[cs300.index >= f"{year}-01-01"].iloc[0]["close"]
        cs_y_close = cs300.loc[cs300.index <= f"{year}-12-31"].iloc[-1]["close"]
        cs_ret = (cs_y_close / cs_y_open - 1) * 100
        samples.append({
            "year": year,
            "snap_m": snap_m,
            "snap_billion": snap_row["new_fund_billion"],
            "snap_count": int(snap_row["new_fund_count"]),
            "lag_m": lag_m,
            "lag_row": lag_row,
            "trailing": trailing,
            "cs_ret": cs_ret,
        })

    # 跑每个规则
    print(f"{'年':<5}{'CS300%':>8}{'月发亿':>9}{'只数':>5}", end="")
    for label, _ in RULES:
        print(f"{label[:15]:>17}", end="")
    print()
    print("-" * (27 + 17 * len(RULES)))

    rule_results = {label: {"signals": [], "items": [], "rets": []} for label, _ in RULES}
    for s in samples:
        print(f"{s['year']:<5}{s['cs_ret']:>7.1f}%{s['snap_billion']:>8.0f}{s['snap_count']:>5}", end="")
        snap_row = {"new_fund_billion": s["snap_billion"], "new_fund_count": s["snap_count"]}
        for label, fn in RULES:
            sig, items = fn(snap_row, s["lag_row"], s["trailing"])
            rule_results[label]["signals"].append(sig)
            rule_results[label]["items"].append(items)
            rule_results[label]["rets"].append(s["cs_ret"])
            print(f"{sig:>+5d} ({len(items):>2}项)   ", end="")
        print()

    # 评价
    print(f"\n{'规则':<32}{'触发次':>8}{'正确向':>8}{'错向':>8}{'命中率':>10}")
    print("-" * 66)
    for label, _ in RULES:
        r = rule_results[label]
        sigs = r["signals"]
        rets = r["rets"]
        n_trigger = sum(1 for s in sigs if s != 0)
        n_correct = sum(
            1 for s, ret in zip(sigs, rets)
            if (s > 0 and ret < 0) or (s < 0 and ret > 0)
        )
        n_wrong = sum(
            1 for s, ret in zip(sigs, rets)
            if (s > 0 and ret > 0) or (s < 0 and ret < 0)
        )
        hit_rate = n_correct / n_trigger * 100 if n_trigger else 0
        print(f"{label:<32}{n_trigger:>8}{n_correct:>8}{n_wrong:>8}{hit_rate:>9.1f}%")

    # 详细错向案例
    print(f"\n{'错向年':<6}{'CS300%':>8}", end="")
    for label, _ in RULES:
        print(f"{label[:14]:>16}", end="")
    print()
    for i, s in enumerate(samples):
        row = []
        for label, _ in RULES:
            sig = rule_results[label]["signals"][i]
            wrong = (sig > 0 and s["cs_ret"] > 0) or (sig < 0 and s["cs_ret"] < 0)
            row.append("❌" if wrong else ("✓" if sig != 0 else "-"))
        if "❌" in row:
            print(f"{s['year']:<6}{s['cs_ret']:>7.1f}%", end="")
            for r in row:
                print(f"{r:>16}", end="")
            print()


if __name__ == "__main__":
    main()

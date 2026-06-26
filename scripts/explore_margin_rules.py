#!/usr/bin/env python3.11
"""margin_growth_pct 规则探索分析

候选规则（同 sentiment v5 思路）:
  v1 旧规则: >50 +1 / <-30 -1
  v2 仅冰点: <-30 → -1（删除过热信号）
  v3 仅冰点放宽: <-20 → -1
  v4 仅冰点更宽: <-15 → -1
  v5 仅过热: >50 → +1（验证是否结构性错向）

数据：macro_annual_snapshot.margin_rzrqye_yoy_pct (2012-2026, 15 年)
"""

import os
import pymysql
import pandas as pd
from dotenv import load_dotenv

load_dotenv('.env')


def main():
    conn = pymysql.connect(host='127.0.0.1', user='teststock',
                           password='teststock', database='teststock')
    snap = pd.read_sql("""
        SELECT apply_year, snapshot_date, margin_rzrqye_yoy_pct
        FROM macro_annual_snapshot
        WHERE margin_rzrqye_yoy_pct IS NOT NULL
        ORDER BY apply_year
    """, conn)
    cs = pd.read_sql("""
        SELECT trade_date, close FROM index_daily
        WHERE ts_code='000300.SH' ORDER BY trade_date
    """, conn, parse_dates=['trade_date'], index_col='trade_date')
    conn.close()

    samples = []
    for _, r in snap.iterrows():
        year = int(r['apply_year'])
        if year > 2025:
            continue  # 2026/2027 还没完整
        cs_open = cs.loc[cs.index >= f"{year}-01-01"].iloc[0]['close']
        cs_close = cs.loc[cs.index <= f"{year}-12-31"].iloc[-1]['close']
        ret = (float(cs_close) / float(cs_open) - 1) * 100
        samples.append({
            'year': year,
            'yoy': float(r['margin_rzrqye_yoy_pct']),
            'cs_ret': ret,
        })

    rules = {
        'v1 旧规则 (>50/<-30)': lambda y: 1 if y > 50 else (-1 if y < -30 else 0),
        'v2 仅冰点 <-30':       lambda y: -1 if y < -30 else 0,
        'v3 仅冰点 <-20':       lambda y: -1 if y < -20 else 0,
        'v4 仅冰点 <-15':       lambda y: -1 if y < -15 else 0,
        'v5 仅过热 >50':        lambda y: 1 if y > 50 else 0,
    }

    print(f"{'年':<5}{'CS300%':>9}{'yoy%':>9}", end="")
    for label in rules:
        print(f"{label[:14]:>16}", end="")
    print()
    print('-' * (23 + 16 * len(rules)))

    results = {label: [] for label in rules}
    for s in samples:
        print(f"{s['year']:<5}{s['cs_ret']:>8.1f}%{s['yoy']:>8.1f}%", end="")
        for label, fn in rules.items():
            sig = fn(s['yoy'])
            results[label].append((sig, s['cs_ret']))
            print(f"{sig:>+16d}", end="")
        print()

    print(f"\n{'规则':<22}{'触发':>6}{'对方向':>8}{'错向':>6}{'命中率':>10}")
    print('-' * 56)
    for label, pairs in results.items():
        trig = [p for p in pairs if p[0] != 0]
        correct = sum(1 for s, r in trig if (s > 0 and r < 0) or (s < 0 and r > 0))
        wrong = sum(1 for s, r in trig if (s > 0 and r > 0) or (s < 0 and r < 0))
        rate = correct / len(trig) * 100 if trig else 0
        print(f"{label:<22}{len(trig):>6}{correct:>8}{wrong:>6}{rate:>9.1f}%")


if __name__ == '__main__':
    main()

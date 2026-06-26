#!/usr/bin/env python3.11
"""PE 动态分位化探索 — 不改 main，离线分析

旧规则 (绝对阈值):
  PE>50/+2, PE>40/+1, PE>30/+1, PE<20/-1, PE<15/-2

候选新规则 (滚动 60 月分位):
  PE > P95 → +2
  PE > P80 → +1
  PE < P20 → -1
  PE < P05 → -2

比较:
  - 触发频次（避免过稀疏或过密）
  - 与次月/次年 CS300 收益的命中率
  - Spearman ρ
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

WINDOW_MONTHS = 60  # 5 年滚动窗口


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_data():
    conn = db()
    # 月末 PE
    pe = pd.read_sql(
        """SELECT trade_date, pe_ttm, pb FROM index_dailybasic
           WHERE ts_code='000300.SH' ORDER BY trade_date""",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    pe['pe_ttm'] = pe['pe_ttm'].astype(float)
    pe['pb'] = pe['pb'].astype(float)
    pe_m = pe.resample('ME').last()

    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    cs['close'] = cs['close'].astype(float)
    cs_m = cs['close'].resample('ME').last()
    ret_1m = cs_m.pct_change().shift(-1) * 100
    ret_12m = cs_m.pct_change(12).shift(-12) * 100
    conn.close()

    df = pd.DataFrame({'pe': pe_m['pe_ttm'], 'pb': pe_m['pb'],
                        'ret_1m': ret_1m, 'ret_12m': ret_12m})
    return df


def score_pe_legacy(pe):
    """旧规则绝对阈值"""
    if pd.isna(pe): return 0
    if pe > 50: return 2
    if pe > 40: return 1
    if pe > 30: return 1
    if pe < 15: return -2
    if pe < 20: return -1
    return 0


def score_pe_dynamic(pe, p05, p20, p80, p95):
    """动态分位"""
    if pd.isna(pe) or pd.isna(p05): return 0
    if pe > p95: return 2
    if pe > p80: return 1
    if pe < p05: return -2
    if pe < p20: return -1
    return 0


def spearman(xs, ys):
    a = pd.Series(xs).rank()
    b = pd.Series(ys).rank()
    return a.corr(b)


def main():
    df = load_data()
    print(f'PE 数据: {df.index[0]:%Y-%m} ~ {df.index[-1]:%Y-%m}, {len(df)} 月')

    # 滚动分位
    df['pe_p05'] = df['pe'].rolling(WINDOW_MONTHS, min_periods=24).quantile(0.05)
    df['pe_p20'] = df['pe'].rolling(WINDOW_MONTHS, min_periods=24).quantile(0.20)
    df['pe_p80'] = df['pe'].rolling(WINDOW_MONTHS, min_periods=24).quantile(0.80)
    df['pe_p95'] = df['pe'].rolling(WINDOW_MONTHS, min_periods=24).quantile(0.95)

    # 算两个 score
    df['score_legacy'] = df['pe'].apply(score_pe_legacy)
    df['score_dynamic'] = df.apply(
        lambda r: score_pe_dynamic(r['pe'], r['pe_p05'], r['pe_p20'], r['pe_p80'], r['pe_p95']),
        axis=1,
    )

    sub = df.dropna(subset=['pe', 'pe_p05', 'ret_1m']).copy()
    print(f'\n有效样本（含分位数据）: {len(sub)} 月')

    # 触发频次
    for col in ('score_legacy', 'score_dynamic'):
        print(f'\n{col} 分布:')
        print(sub[col].value_counts().sort_index().to_string())
        n_trig = (sub[col] != 0).sum()
        print(f'  触发占比: {n_trig/len(sub)*100:.0f}%')

    # 命中率 + ρ
    print(f'\n=== 与次 1 月 CS300 收益的关联 ===')
    for col in ('score_legacy', 'score_dynamic'):
        s = sub[col].values
        y = sub['ret_1m'].values
        rho = spearman(s, y)
        # 方向命中（score<0 是机会，期望 ret>0；score>0 是风险，期望 ret<0）
        trig = sub[sub[col] != 0]
        if len(trig) > 0:
            correct = sum(1 for sc, rt in zip(trig[col], trig['ret_1m'])
                          if (sc < 0 and rt > 0) or (sc > 0 and rt < 0))
            hit = correct / len(trig) * 100
        else:
            hit = 0
        print(f'  {col:18s}: ρ={rho:+.3f}, 触发 {len(trig)} 月, 命中率 {hit:.0f}%')

    print(f'\n=== 与次 12 月 CS300 收益的关联 ===')
    sub2 = df.dropna(subset=['pe','pe_p05','ret_12m']).copy()
    sub2['score_legacy'] = sub2['pe'].apply(score_pe_legacy)
    sub2['score_dynamic'] = sub2.apply(
        lambda r: score_pe_dynamic(r['pe'], r['pe_p05'], r['pe_p20'], r['pe_p80'], r['pe_p95']),
        axis=1,
    )
    for col in ('score_legacy', 'score_dynamic'):
        s = sub2[col].values
        y = sub2['ret_12m'].values
        rho = spearman(s, y)
        trig = sub2[sub2[col] != 0]
        if len(trig) > 0:
            correct = sum(1 for sc, rt in zip(trig[col], trig['ret_12m'])
                          if (sc < 0 and rt > 0) or (sc > 0 and rt < 0))
            hit = correct / len(trig) * 100
        else:
            hit = 0
        print(f'  {col:18s}: ρ={rho:+.3f}, 触发 {len(trig)} 月, 命中率 {hit:.0f}%')

    # 看不同年代触发分布
    print(f'\n=== 各年触发数对比 ===')
    sub['year'] = sub.index.year
    by_year = sub.groupby('year').agg(
        legacy_trig=('score_legacy', lambda x: (x != 0).sum()),
        dynamic_trig=('score_dynamic', lambda x: (x != 0).sum()),
        pe_mean=('pe', 'mean'),
    ).round(1)
    print(by_year.to_string())


if __name__ == '__main__':
    main()

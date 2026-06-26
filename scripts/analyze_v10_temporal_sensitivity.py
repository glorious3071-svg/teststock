#!/usr/bin/env python3.11
"""v10-R1 评分卡时间敏感性分析

用户提出原则：评分卡应对近期更敏感，早期可衰减。
对比 v8 vs v10-R1 在三个时期的 ρ：
  - 早期 2008-2013
  - 中期 2014-2019
  - 近期 2020-2025

理想：v10-R1 近期 ρ 显著强于早期/中期；如果反过来或近期未改善，则不符合用户预期。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(ROOT / '.env')


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def spearman(xs, ys):
    a = pd.Series(xs); b = pd.Series(ys)
    sub = pd.concat([a, b], axis=1).dropna()
    if len(sub) < 5: return np.nan
    return sub.iloc[:, 0].rank().corr(sub.iloc[:, 1].rank())


def main():
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['pmi_non_mfg'] = ml['pmi_non_mfg']
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['v8_total'] = score['total_score'] + score['v8_extra']

    def r1_extra(r):
        v = r['pmi_non_mfg']
        if pd.isna(v): return 0
        if v > 55: return +1
        if v < 50: return -1
        return 0
    score['r1_extra'] = score.apply(r1_extra, axis=1)
    score['v10_total'] = score['v8_total'] + score['r1_extra']

    # CS300 月收益
    conn = db()
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    cs_m = cs['close'].astype(float).resample('ME').last()
    cs_m.index = cs_m.index.to_period('M').to_timestamp('M')
    for n in (1, 3, 6, 12):
        score[f'ret_{n}m'] = cs_m.pct_change(n).shift(-n) * 100

    # ── 三段对比 ──────────────────────────────────────────
    print('='*92)
    print('v8 vs v10-R1 三段时期对比（ρ 越负越好）')
    print('='*92)
    periods = [
        ('早期 2008-2013', '2008-01-01', '2013-12-31'),
        ('中期 2014-2019', '2014-01-01', '2019-12-31'),
        ('近期 2020-2025', '2020-01-01', '2025-12-31'),
    ]
    print(f'{"区间":<18}{"":>5}|{"v8 ρ_1m":>10}{"v10 ρ_1m":>10}{"Δ":>8}|{"v8 ρ_12m":>10}{"v10 ρ_12m":>10}{"Δ":>8}')
    print('-'*92)
    for name, start, end in periods:
        sub = score.loc[start:end]
        v8_1 = spearman(sub['v8_total'].tolist(), sub['ret_1m'].tolist())
        v10_1 = spearman(sub['v10_total'].tolist(), sub['ret_1m'].tolist())
        v8_12 = spearman(sub['v8_total'].tolist(), sub['ret_12m'].tolist())
        v10_12 = spearman(sub['v10_total'].tolist(), sub['ret_12m'].tolist())
        print(f'{name:<18}{len(sub):>5} | {v8_1:>+9.3f}{v10_1:>+10.3f}{v10_1-v8_1:>+8.3f} | '
              f'{v8_12:>+9.3f}{v10_12:>+10.3f}{v10_12-v8_12:>+8.3f}')

    # ── 用户期望诊断 ─────────────────────────────────────
    print(f'\n{"="*92}')
    print('用户期望：评分卡近期 ρ 应显著强于早期/中期')
    print('='*92)
    sub_early = score.loc['2008-01-01':'2013-12-31']
    sub_mid = score.loc['2014-01-01':'2019-12-31']
    sub_late = score.loc['2020-01-01':'2025-12-31']
    for label, col in [('v8 total', 'v8_total'), ('v10-R1 total', 'v10_total')]:
        e1 = spearman(sub_early[col].tolist(), sub_early['ret_1m'].tolist())
        m1 = spearman(sub_mid[col].tolist(), sub_mid['ret_1m'].tolist())
        l1 = spearman(sub_late[col].tolist(), sub_late['ret_1m'].tolist())
        e12 = spearman(sub_early[col].tolist(), sub_early['ret_12m'].tolist())
        m12 = spearman(sub_mid[col].tolist(), sub_mid['ret_12m'].tolist())
        l12 = spearman(sub_late[col].tolist(), sub_late['ret_12m'].tolist())
        # 时间敏感度判定
        # 理想：|l| > |m| > |e|，且 l 是负的（评分卡设计的方向）
        verdict_1m = '✓ 近期最强' if abs(l1) > abs(m1) and abs(l1) > abs(e1) else (
            '⚠ 中期最强' if abs(m1) > abs(l1) else '❌ 早期最强（旧式）')
        verdict_12m = '✓ 近期最强' if abs(l12) > abs(m12) and abs(l12) > abs(e12) else (
            '⚠ 中期最强' if abs(m12) > abs(l12) else '❌ 早期最强（旧式）')
        print(f'\n  {label}:')
        print(f'    ρ_1m:   早 {e1:+.3f} | 中 {m1:+.3f} | 近 {l1:+.3f}  → {verdict_1m}')
        print(f'    ρ_12m:  早 {e12:+.3f} | 中 {m12:+.3f} | 近 {l12:+.3f}  → {verdict_12m}')

    # ── 滚动 5 年看是否近期有趋势性提升 ─────────────────
    print(f'\n{"="*92}')
    print('滚动 5 年 ρ_1m 演化 — v8 vs v10-R1（关键年份）')
    print('='*92)
    win = 60
    print(f'  {"截至":>10}{"v8 ρ_1m":>12}{"v10 ρ_1m":>12}{"Δ":>10}')
    for end_idx_year in (2013, 2016, 2019, 2022, 2025):
        end_ts = pd.Timestamp(f'{end_idx_year}-12-31')
        end_ts_norm = end_ts.to_period('M').to_timestamp('M')
        if end_ts_norm not in score.index: continue
        start_pos = max(0, score.index.get_loc(end_ts_norm) - win)
        end_pos = score.index.get_loc(end_ts_norm) + 1
        sub = score.iloc[start_pos:end_pos]
        v8_r = spearman(sub['v8_total'].tolist(), sub['ret_1m'].tolist())
        v10_r = spearman(sub['v10_total'].tolist(), sub['ret_1m'].tolist())
        print(f'  {end_ts_norm:%Y-%m}{v8_r:>+12.3f}{v10_r:>+12.3f}{v10_r-v8_r:>+10.3f}')


if __name__ == '__main__':
    main()

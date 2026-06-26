#!/usr/bin/env python3.11
"""v11 外部宏观补强 simulate

候选规则:
  R-Ext1: gold_yoy > 25 → -1 (89% 命中)
  R-Ext2: vix_30d_avg > 30 → -1 (80% 命中)
  R-Ext3: fed_rate >= 4.5 → -1 (反转, 71% 命中)
  R-Ext4: 删除 global_recession +2 (反向规则)

测：
  - v10 baseline (含 R1 pmi_non_mfg)
  - v11-G: + R-Ext1 gold
  - v11-V: + R-Ext2 vix
  - v11-F: + R-Ext3 fed_high
  - v11-N: - R-Ext4 删除衰退规则
  - v11-ALL: G+V+F+N 全组合
"""

from __future__ import annotations

import os, sys
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


INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10


def score_to_eq(s):
    if s <= -10: return 95
    if s <= -7: return 90
    if s <= -4: return 85
    if s <= -1: return 80
    if s == 0: return 75
    if s <= 3: return 70
    if s <= 6: return 60
    if s <= 9: return 50
    if s <= 12: return 30
    return 20


def gate_eq(row, score_col):
    eq = score_to_eq(int(row[score_col]))
    if eq < 80: return float(eq)
    hits = 0
    if row.get('pboc') == 'loose': hits += 1
    if row.get('cmt') == 'expansionary': hits += 1
    if row.get('fun_score', 0) < 0: hits += 1
    return float(eq) if hits >= 2 else 75.0


def backtest(eq, ret):
    df = pd.DataFrame({'eq': eq, 'ret': ret}).dropna()
    df['eq_prev'] = df['eq'].shift(1).fillna(75.0)
    df['turn'] = (df['eq'] - df['eq_prev']).abs()
    cash_m = CASH_ANNUAL_RATE / 12 * 100
    df['net'] = df['eq']/100*df['ret'] + (1-df['eq']/100)*cash_m - df['turn']/100*COST_PCT
    df['nav'] = (1 + df['net']/100).cumprod() * INITIAL_CAPITAL
    n = len(df)
    cum = (df['nav'].iloc[-1]/INITIAL_CAPITAL - 1)*100
    peak = df['nav'].cummax()
    dd = (df['nav']/peak - 1).min() * 100
    return cum, dd


def main():
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    # v8 + R1 (pmi_non_mfg) = v10 baseline
    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['pmi_non_mfg'] = ml['pmi_non_mfg']
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    def r1_extra(r):
        v = r['pmi_non_mfg']
        if pd.isna(v): return 0
        if v > 55: return +1
        if v < 50: return -1
        return 0
    score['r1_extra'] = score.apply(r1_extra, axis=1)
    score['v10_total'] = score['total_score'] + score['v8_extra'] + score['r1_extra']

    # 外部数据
    conn = db()
    vix = pd.read_sql(
        "SELECT trade_date, close FROM cboe_vix_daily ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    vix_m = vix['close'].astype(float).resample('ME').mean()
    vix_m.index = vix_m.index.to_period('M').to_timestamp('M')

    gold = pd.read_sql(
        "SELECT trade_date, close FROM gold_daily WHERE symbol='GC.FOREIGN' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    gold_m = gold['close'].astype(float).resample('ME').last()
    gold_m.index = gold_m.index.to_period('M').to_timestamp('M')
    gold_yoy = gold_m.pct_change(12) * 100

    fed = pd.read_sql(
        "SELECT effective_date AS dt, rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' ORDER BY effective_date",
        conn, parse_dates=['dt'], index_col='dt',
    )
    fed_m = fed['rate_after_pct'].astype(float).resample('ME').last().ffill()
    fed_m.index = fed_m.index.to_period('M').to_timestamp('M')

    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    cs_m = cs['close'].astype(float).resample('ME').last()
    cs_m.index = cs_m.index.to_period('M').to_timestamp('M')
    ret_for_pnl = cs_m.pct_change() * 100
    next_ret = cs_m.pct_change().shift(-1) * 100
    conn.close()

    # 加 extra
    score['vix_m'] = vix_m.reindex(score.index)
    score['gold_yoy'] = gold_yoy.reindex(score.index)
    score['fed_m'] = fed_m.reindex(score.index)

    score['ext_g_extra'] = score['gold_yoy'].apply(lambda x: -1 if pd.notna(x) and x > 25 else 0)
    score['ext_v_extra'] = score['vix_m'].apply(lambda x: -1 if pd.notna(x) and x > 30 else 0)
    score['ext_f_extra'] = score['fed_m'].apply(lambda x: -1 if pd.notna(x) and x >= 4.5 else 0)
    # R-Ext4: 删除 global_recession 旧规则
    # 现有 ext_score 已含 recession +2，需要计算撤回值
    # 简化：假设 global_recession 在 2008/2020 期间触发 +2，撤回 = -2
    # 但 ext_score 来自 monthly_scorecard_series.csv，不知道具体每月是否触发
    # 不做 R-Ext4 simulate（需要重跑 adapter）；本次只测 G/V/F 三种

    # 5 个版本
    versions = [
        ('v10 baseline',       lambda r: 0),
        ('v11-G (gold)',       lambda r: r['ext_g_extra']),
        ('v11-V (vix)',        lambda r: r['ext_v_extra']),
        ('v11-F (fed_high)',   lambda r: r['ext_f_extra']),
        ('v11-GV',             lambda r: r['ext_g_extra'] + r['ext_v_extra']),
        ('v11-GVF',            lambda r: r['ext_g_extra'] + r['ext_v_extra'] + r['ext_f_extra']),
    ]

    print(f'{"版本":<24}{"触发月":>8}{"P&L":>10}{"vs base":>10}{"DD":>10}{"vs base":>10}{"ρ_1m":>10}{"ρ_12m":>10}')
    print('-'*100)
    base_cum = base_dd = base_r12 = None
    for name, fn in versions:
        score['_extra'] = score.apply(fn, axis=1)
        score['_total'] = score['v10_total'] + score['_extra']
        score['_eq'] = score.apply(lambda r: gate_eq(r, '_total'), axis=1)
        eq_held = score['_eq'].shift(1).fillna(75.0)
        cum, dd = backtest(eq_held, ret_for_pnl)
        r1m = spearman(score['_total'].tolist(), next_ret.reindex(score.index).tolist())
        r12 = spearman(score['_total'].tolist(),
                        cs_m.pct_change(12).shift(-12).reindex(score.index).tolist())
        n_trig = (score['_extra'] != 0).sum()
        if base_cum is None:
            base_cum, base_dd, base_r12 = cum, dd, r12
            d_cum_str = ''
            d_dd_str = ''
        else:
            d_cum_str = f'{cum-base_cum:>+10.2f}'
            d_dd_str = f'{dd-base_dd:>+10.2f}'
        print(f'{name:<24}{n_trig:>8}{cum:>+9.2f}%{d_cum_str}{dd:>+9.2f}%{d_dd_str}{r1m:>+10.3f}{r12:>+10.3f}')

    # 红线判定
    print('\n=== 严格红线 3/3 (vs v10 baseline) ===')
    for name, fn in versions[1:]:
        score['_extra'] = score.apply(fn, axis=1)
        score['_total'] = score['v10_total'] + score['_extra']
        score['_eq'] = score.apply(lambda r: gate_eq(r, '_total'), axis=1)
        eq_held = score['_eq'].shift(1).fillna(75.0)
        cum, dd = backtest(eq_held, ret_for_pnl)
        r12 = spearman(score['_total'].tolist(),
                        cs_m.pct_change(12).shift(-12).reindex(score.index).tolist())
        c1 = cum >= base_cum
        c2 = dd >= base_dd - 3.0
        c3 = r12 <= base_r12
        flag = ''.join(['✓' if c else '✗' for c in (c1, c2, c3)])
        passed = sum([c1, c2, c3])
        verdict = 'ADOPT' if passed == 3 else 'REJECT'
        print(f'  {name:<24} {flag} ({passed}/3) {verdict}')


if __name__ == '__main__':
    main()

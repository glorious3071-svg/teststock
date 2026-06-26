#!/usr/bin/env python3.11
"""外部宏观信号 ρ 单变量分析

候选信号：
  - VIX 月均（30 日均值）
  - US 10Y 名义利率
  - FED rate 绝对水平
  - 黄金 YoY（避险情绪）
  - 中美 10Y 利差（中美资本流向锚）
  - SPX YoY 与月环比
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


def main():
    conn = db()
    # CS300 月收益
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    cs_m = cs['close'].astype(float).resample('ME').last()

    # VIX 月均
    vix = pd.read_sql(
        "SELECT trade_date, close FROM cboe_vix_daily ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    vix_m = vix['close'].astype(float).resample('ME').mean()

    # US 10Y 名义
    us10y = pd.read_sql(
        "SELECT trade_date, y10 FROM us_tycr_daily WHERE y10 IS NOT NULL ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    us10y_m = us10y['y10'].astype(float).resample('ME').last()

    # SPX 月环比 + YoY
    spx = pd.read_sql(
        "SELECT trade_date, close FROM us_index_daily WHERE ts_code='SPX.US' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    spx_m = spx['close'].astype(float).resample('ME').last()
    spx_yoy = spx_m.pct_change(12) * 100
    spx_mom = spx_m.pct_change() * 100

    # 黄金 YoY (GC.FOREIGN)
    gold = pd.read_sql(
        "SELECT trade_date, close FROM gold_daily WHERE symbol='GC.FOREIGN' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    gold_m = gold['close'].astype(float).resample('ME').last()
    gold_yoy = gold_m.pct_change(12) * 100

    # FED rate（事件表 → 每月末持有的 rate）
    fed = pd.read_sql(
        "SELECT effective_date AS dt, rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' ORDER BY effective_date",
        conn, parse_dates=['dt'], index_col='dt',
    )
    fed['rate_after_pct'] = fed['rate_after_pct'].astype(float)
    # 重采样到月末，前向填充（每月末取该月或更早最近一次决议后的 rate）
    fed_m = fed['rate_after_pct'].resample('ME').last().ffill()

    conn.close()

    # 对齐月末索引
    idx = cs_m.resample('ME').last().index
    df = pd.DataFrame(index=idx)
    df['cs_ret_1m'] = cs_m.pct_change().shift(-1) * 100
    df['cs_ret_3m'] = cs_m.pct_change(3).shift(-3) * 100
    df['cs_ret_6m'] = cs_m.pct_change(6).shift(-6) * 100
    df['cs_ret_12m'] = cs_m.pct_change(12).shift(-12) * 100

    df['vix_avg'] = vix_m.reindex(idx)
    df['vix_yoy'] = vix_m.pct_change(12).reindex(idx) * 100
    df['us10y'] = us10y_m.reindex(idx)
    df['us10y_chg_12m'] = us10y_m.diff(12).reindex(idx)
    df['spx_yoy'] = spx_yoy.reindex(idx)
    df['spx_mom'] = spx_mom.reindex(idx)
    df['gold_yoy'] = gold_yoy.reindex(idx)
    df['fed_rate'] = fed_m.reindex(idx)
    df['fed_rate_chg_12m'] = fed_m.diff(12).reindex(idx)

    # 只看 2008+
    df = df[df.index >= '2008-01-01']

    feats = ['vix_avg', 'vix_yoy', 'us10y', 'us10y_chg_12m', 'spx_yoy', 'spx_mom',
             'gold_yoy', 'fed_rate', 'fed_rate_chg_12m']

    print(f'{"特征":<20}{"n":>5}{"ρ_1m":>9}{"ρ_3m":>9}{"ρ_6m":>9}{"ρ_12m":>9}{"|ρ_12m|":>9}')
    print('-'*75)
    rows = []
    for f in feats:
        n = df[f].notna().sum()
        r1 = spearman(df[f].tolist(), df['cs_ret_1m'].tolist())
        r3 = spearman(df[f].tolist(), df['cs_ret_3m'].tolist())
        r6 = spearman(df[f].tolist(), df['cs_ret_6m'].tolist())
        r12 = spearman(df[f].tolist(), df['cs_ret_12m'].tolist())
        rows.append({'特征': f, 'n': n, 'r1': r1, 'r3': r3, 'r6': r6, 'r12': r12, '|r12|': abs(r12)})
        print(f'{f:<20}{int(n):>5}{r1:>+9.3f}{r3:>+9.3f}{r6:>+9.3f}{r12:>+9.3f}{abs(r12):>+9.3f}')

    # 排序 top
    print('\n按 |ρ_12m| 排序：')
    for r in sorted(rows, key=lambda x: -x['|r12|']):
        sig = '+' if r['r12'] > 0 else '-'
        meaning = ''
        if r['特征'] == 'fed_rate':
            meaning = f'(rate {sig}, 高=A股{"涨" if r["r12"]>0 else "跌"})'
        elif 'vix' in r['特征']:
            meaning = '(恐慌, 高=A股' + ('涨' if r['r12']>0 else '跌') + ')'
        elif 'us10y' in r['特征']:
            meaning = '(美债, 高=A股' + ('涨' if r['r12']>0 else '跌') + ')'
        elif 'spx' in r['特征']:
            meaning = '(美股, 高=A股' + ('涨' if r['r12']>0 else '跌') + ')'
        elif 'gold' in r['特征']:
            meaning = '(避险, 高=A股' + ('涨' if r['r12']>0 else '跌') + ')'
        print(f'  {r["特征"]:<20} ρ_12m={r["r12"]:+.3f}, n={int(r["n"])}  {meaning}')

    # ── 极端阈值触发分析 ───────────────────────────────
    print(f'\n{"="*78}')
    print('候选阈值触发分析（看哪个阈值最有效）')
    print('='*78)

    thresholds = [
        ('vix_avg > 25 (恐慌)',     df['vix_avg'] > 25, 'long'),
        ('vix_avg > 30 (极端恐慌)', df['vix_avg'] > 30, 'long'),
        ('vix_avg < 14 (过度乐观)', df['vix_avg'] < 14, 'short'),
        ('us10y > 4.5 (美债吸金)',   df['us10y'] > 4.5, 'short'),
        ('us10y < 2 (QE 周期)',     df['us10y'] < 2, 'long'),
        ('fed_rate >= 4.5 (紧缩末段)', df['fed_rate'] >= 4.5, 'short'),
        ('fed_rate <= 0.25 (零利率)', df['fed_rate'] <= 0.25, 'long'),
        ('spx_yoy < -10 (美股熊)',   df['spx_yoy'] < -10, 'short'),
        ('spx_yoy > 25 (美股牛)',    df['spx_yoy'] > 25, 'long'),
        ('gold_yoy > 25 (避险)',     df['gold_yoy'] > 25, 'long'),
    ]
    print(f'{"阈值":<28}{"触发月":>8}{"期望":<8}{"次1m平均%":>11}{"次12m平均%":>12}{"次12m命中":>10}')
    print('-'*85)
    for name, mask, expected in thresholds:
        trig = df[mask]
        if len(trig) < 3: continue
        avg_1m = trig['cs_ret_1m'].mean()
        avg_12m = trig['cs_ret_12m'].mean()
        # 期望 long: 次月/年涨；short: 次月/年跌
        if expected == 'long':
            hit_12m = (trig['cs_ret_12m'] > 0).sum()
        else:
            hit_12m = (trig['cs_ret_12m'] < 0).sum()
        n_12m = trig['cs_ret_12m'].notna().sum()
        hr_str = f'{hit_12m}/{n_12m} = {hit_12m/n_12m*100:.0f}%' if n_12m else '?'
        print(f'{name:<28}{len(trig):>8}{expected:<8}{avg_1m:>+10.2f}%{avg_12m:>+11.2f}%{hr_str:>10}')


if __name__ == '__main__':
    main()

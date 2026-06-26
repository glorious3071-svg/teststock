#!/usr/bin/env python3.11
"""模拟 P1: 新增 cb_cuts_6m >= 3 → -1 规则

baseline v1: 现有 total_score（已含 v5+v6+v3.4.9）
candidate v8: v1 + (cb_cuts_6m >= 3 → -1)

红线 3/3:
  - 累计回报 v8 ≥ v1
  - 最大回撤 Δ ≤ +3pp
  - Spearman ρ v8 ≤ v1（更负）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs,
    policy_triple_gate,
    score_to_target_equity,
)

load_dotenv(ROOT / ".env")

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_cs300_monthly_ret():
    conn = db()
    df = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    monthly = df['close'].astype(float).resample('ME').last()
    return monthly.pct_change() * 100


def apply_gate_eq(row, score_col='total_score') -> float:
    eq, _ = score_to_target_equity(int(row[score_col]))
    if eq < 80:
        return float(eq)
    inp = ScorecardInputs(
        pboc_tone=row['pboc'] if pd.notna(row['pboc']) else None,
        central_meeting_tone=row['cmt'] if pd.notna(row['cmt']) else None,
        ppi_yoy_change='turn_positive' if row['fun_score'] < 0 else 'flat',
    )
    passed, _ = policy_triple_gate(inp)
    return float(eq) if passed else 75.0


def spearman(xs, ys):
    if len(xs) < 2: return 0.0
    def avg_rank(a):
        n = len(a); idx = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n; i = 0
        while i < n:
            j = i
            while j + 1 < n and a[idx[j + 1]] == a[idx[i]]:
                j += 1
            avg = (i + j + 2) / 2.0
            for k in range(i, j + 1): r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = avg_rank(xs), avg_rank(ys)
    mx = sum(rx) / len(rx); my = sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def backtest(eq_series, ret_series, name='strategy'):
    df = pd.DataFrame({'eq': eq_series, 'ret': ret_series}).dropna()
    df['eq_prev'] = df['eq'].shift(1).fillna(75.0)
    df['turn'] = (df['eq'] - df['eq_prev']).abs()
    cash_m = CASH_ANNUAL_RATE / 12 * 100
    df['gross_ret'] = df['eq'] / 100 * df['ret'] + (1 - df['eq'] / 100) * cash_m
    df['cost'] = df['turn'] / 100 * COST_PCT
    df['net_ret'] = df['gross_ret'] - df['cost']
    df['nav'] = (1 + df['net_ret'] / 100).cumprod() * INITIAL_CAPITAL
    n = len(df)
    cum = (df['nav'].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    ann = ((df['nav'].iloc[-1] / INITIAL_CAPITAL) ** (12 / n) - 1) * 100
    vol = df['net_ret'].std() * (12 ** 0.5)
    peak = df['nav'].cummax()
    dd = (df['nav'] / peak - 1).min() * 100
    n_trade = (df['turn'] > 0).sum()
    return {'name': name, 'cum': cum, 'ann': ann, 'vol': vol, 'dd': dd, 'n_trade': n_trade}


def main():
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    # 合并 cb_cuts_6m
    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['v1_score'] = score['total_score']

    # v8: 加 cb_cuts_6m >= 3 → -1
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['v8_score'] = score['v1_score'] + score['v8_extra']

    n_trig = (score['v8_extra'] != 0).sum()
    print(f'cb_cuts_6m >= 3 → -1 触发: {n_trig} / {len(score)} 月')

    # 仓位
    score['v1_equity'] = score.apply(lambda r: apply_gate_eq(r, 'v1_score'), axis=1)
    s2 = score.copy()
    s2['total_score'] = s2['v8_score']
    score['v8_equity'] = s2.apply(lambda r: apply_gate_eq(r, 'total_score'), axis=1)

    n_eq_diff = (score['v1_equity'] != score['v8_equity']).sum()
    print(f'v8 vs v1 仓位变化: {n_eq_diff} 月')
    if n_eq_diff > 0:
        diff = score[score['v1_equity'] != score['v8_equity']][['cb_cuts_6m','v1_score','v8_score','v1_equity','v8_equity']]
        print('变化样本:')
        print(diff.to_string())

    # 回测
    ret = load_cs300_monthly_ret()
    ret.index = ret.index.to_period('M').to_timestamp('M')
    base_eq = pd.Series(75.0, index=score.index)
    v1_eq = score['v1_equity'].shift(1).fillna(75.0)
    v8_eq = score['v8_equity'].shift(1).fillna(75.0)

    r_base = backtest(base_eq, ret, 'baseline 75%')
    r_v1 = backtest(v1_eq, ret, 'v1')
    r_v8 = backtest(v8_eq, ret, 'v8')

    print('\n=== 月度回测对比 ===')
    print(f"{'指标':<24}{'基准75%':>12}{'v1':>12}{'v8':>12}{'Δ(v8-v1)':>10}")
    print('-' * 70)
    for label, key in [
        ('累计回报 (%)', 'cum'),
        ('年化收益 (%)', 'ann'),
        ('年化波动 (%)', 'vol'),
        ('最大回撤 (%)', 'dd'),
        ('调仓次数', 'n_trade'),
    ]:
        b = r_base[key]; v1 = r_v1[key]; v8 = r_v8[key]
        if isinstance(v1, int):
            print(f"{label:<24}{b:>12d}{v1:>12d}{v8:>12d}{v8-v1:>+10d}")
        else:
            print(f"{label:<24}{b:>12.2f}{v1:>12.2f}{v8:>12.2f}{v8-v1:>+10.2f}")

    # Spearman ρ
    next_ret = ret.shift(-1).reindex(score.index)
    merged = pd.DataFrame({'v1': score['v1_score'].values, 'v8': score['v8_score'].values,
                            'ret_next': next_ret.values}).dropna()
    rho_v1 = spearman(merged['v1'].tolist(), merged['ret_next'].tolist())
    rho_v8 = spearman(merged['v8'].tolist(), merged['ret_next'].tolist())
    print(f"\nSpearman ρ(score, next_month_ret):")
    print(f"  v1: {rho_v1:+.3f}  |  v8: {rho_v8:+.3f}  |  Δ: {rho_v8-rho_v1:+.3f}")

    # 严格红线 3/3
    print('\n=== 严格红线 3/3 (v8 vs v1) ===')
    cond = {
        '累计回报 v8 ≥ v1': r_v8['cum'] >= r_v1['cum'],
        '回撤可控 (Δ ≤ +3pp)': r_v8['dd'] >= r_v1['dd'] - 3.0,
        'Spearman ρ v8 ≤ v1（更负）': rho_v8 <= rho_v1,
    }
    for c, v in cond.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    passed = sum(cond.values())
    decision = 'ADOPT' if passed == 3 else 'REJECT'
    print(f"\n→ 决策：{decision} ({passed}/3)")


if __name__ == '__main__':
    main()

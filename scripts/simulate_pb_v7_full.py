#!/usr/bin/env python3.11
"""模拟 PB 提权 v7 在完整月度评分卡上的边际效果

逻辑：
  - 基于 monthly_scorecard_series.csv 的 total_score（已含 v5+v6+v3.4.9 完整规则）
  - 模拟 PB 提权：每月 v7_score = v1_score + extra (PB<2 → -1, PB>3 → +1)
  - 重新映射档位 + 三重门 → 重新算 P&L

红线 3/3:
  - 累计回报 ≥ baseline (v1)
  - 最大回撤 Δ ≤ +3pp
  - Spearman ρ 更负
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
    return {
        'name': name,
        'cum': cum, 'ann': ann, 'vol': vol, 'dd': dd, 'n_trade': n_trade,
        'df': df,
    }


def main():
    # 读 main 月度评分序列
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    # v1 baseline: 当前 total_score (含 PB±1)
    score['v1_score'] = score['total_score']

    # v7 提权: 加 PB extra
    def pb_extra(pb):
        if pd.isna(pb): return 0
        if pb > 3: return 1
        if pb < 2: return -1
        return 0
    score['pb_extra'] = score['pb'].apply(pb_extra)
    score['v7_score'] = score['v1_score'] + score['pb_extra']

    # 重新 apply gate
    score['v1_equity'] = score.apply(lambda r: apply_gate_eq(r, 'v1_score'), axis=1)
    score_v7 = score.copy()
    score_v7['total_score'] = score_v7['v7_score']
    score['v7_equity'] = score_v7.apply(lambda r: apply_gate_eq(r, 'total_score'), axis=1)

    # 仓位变化
    n_diff = (score['v1_equity'] != score['v7_equity']).sum()
    print(f'PB 提权造成仓位变化的月数: {n_diff} / {len(score)}')
    if n_diff > 0:
        print('变化样本:')
        diff = score[score['v1_equity'] != score['v7_equity']][['pe','pb','v1_score','v7_score','v1_equity','v7_equity']].head(10)
        print(diff.to_string())

    # CS300 月收益
    ret = load_cs300_monthly_ret()
    ret.index = ret.index.to_period('M').to_timestamp('M')

    # 回测: baseline (always 75%), v1, v7
    base_eq = pd.Series(75.0, index=score.index)
    v1_eq = score['v1_equity'].shift(1).fillna(75.0)
    v7_eq = score['v7_equity'].shift(1).fillna(75.0)

    r_base = backtest(base_eq, ret, 'baseline 75%')
    r_v1 = backtest(v1_eq, ret, 'v1 (PB±1)')
    r_v7 = backtest(v7_eq, ret, 'v7 (PB±2)')

    print('\n=== 月度回测对比（含三重门）===')
    print(f"{'指标':<24}{'基准75%':>12}{'v1 PB±1':>12}{'v7 PB±2':>12}{'Δ(v7-v1)':>10}")
    print('-' * 70)
    for label, key in [
        ('累计回报 (%)', 'cum'),
        ('年化收益 (%)', 'ann'),
        ('年化波动 (%)', 'vol'),
        ('最大回撤 (%)', 'dd'),
        ('调仓次数', 'n_trade'),
    ]:
        b = r_base[key]; v1 = r_v1[key]; v7 = r_v7[key]
        if isinstance(v1, int):
            print(f"{label:<24}{b:>12d}{v1:>12d}{v7:>12d}{v7-v1:>+10d}")
        else:
            print(f"{label:<24}{b:>12.2f}{v1:>12.2f}{v7:>12.2f}{v7-v1:>+10.2f}")

    # Spearman ρ
    merged = pd.DataFrame({'v1': score['v1_score'].values, 'v7': score['v7_score'].values,
                            'ret_next': ret.reindex(score.index).shift(-1).values}).dropna()
    rho_v1 = spearman(merged['v1'].tolist(), merged['ret_next'].tolist())
    rho_v7 = spearman(merged['v7'].tolist(), merged['ret_next'].tolist())
    print(f"\nSpearman ρ(score, next_month_ret):")
    print(f"  v1: {rho_v1:+.3f}  |  v7: {rho_v7:+.3f}  |  Δ: {rho_v7-rho_v1:+.3f}")

    # 严格红线 3/3
    print('\n=== 严格红线 3/3 (v7 vs v1) ===')
    cond = {
        '累计回报 v7 ≥ v1': r_v7['cum'] >= r_v1['cum'],
        '回撤可控 (Δ ≤ +3pp)': r_v7['dd'] >= r_v1['dd'] - 3.0,
        'Spearman ρ v7 ≤ v1（更负）': rho_v7 <= rho_v1,
    }
    for c, v in cond.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    passed = sum(cond.values())
    decision = 'ADOPT' if passed == 3 else 'REJECT'
    print(f"\n→ 决策：{decision} ({passed}/3)")


if __name__ == '__main__':
    main()

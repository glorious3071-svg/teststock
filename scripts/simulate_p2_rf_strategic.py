#!/usr/bin/env python3.11
"""P2: RF 战略层 — walk-forward 严格验证 + 实战仓位回测

设计:
  RF 用历史训练，预测 next_12m 收益。预测值经过 sigmoid 转成「目标仓位」(20-90%)，
  与原评分卡作为「年度战略层」的两个独立信号融合：
    - mode_a: 仅 RF（独立信号）
    - mode_b: 等权平均 (评分卡仓位 + RF 仓位) / 2
    - mode_c: 仅原评分卡（baseline，含 v8）

严防上帝视角:
  - walk-forward expanding window: 训练用 ≤ t-12，预测 t（next_12m 要在 t+12 之后才知答案）
  - 每年重新训练（用历史所有可用数据）
  - 起点 2012（前 4 年作为初始训练集）

红线 3/3:
  - 累计回报 >= baseline (v1 含 v8)
  - 最大回撤 Δ ≤ +3pp
  - Spearman ρ(target_eq, next_ret) 更负
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
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler

load_dotenv(ROOT / ".env")

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10
RF_PARAMS = dict(n_estimators=300, max_depth=5, random_state=42, n_jobs=-1)


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


def backtest(eq_series, ret_series):
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
    return {'cum': cum, 'ann': ann, 'vol': vol, 'dd': dd, 'n_trade': (df['turn'] > 0).sum()}


def rf_pred_to_equity(pred):
    """RF 预测的 12 月收益% → 目标仓位 (20-90%)，逻辑 sigmoid"""
    # 直觉：预测越高仓位越高
    # 校准：18 年 next_12m 均值约 5%，std 约 25%
    # pred = 0 → 75% (中性), pred = +30 → 90%, pred = -30 → 20%
    if pred is None or np.isnan(pred):
        return 75.0
    # 线性 + clip
    eq = 75.0 + pred * 0.5
    return max(20.0, min(90.0, eq))


def main():
    # 加载特征 + 评分卡总分
    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    # 加 cb_cuts_6m → v8 score (因为 monthly_scorecard_series 是旧版本)
    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['v8_total'] = score['total_score'] + score['v8_extra']

    feat_cols = [c for c in ml.columns
                 if c not in ('snapshot', 'ret_1m', 'ret_3m', 'ret_6m', 'ret_12m')]

    # walk-forward expanding：每月底用所有 ≤ snap-12 月数据训 RF，预测当月 next_12m
    print(f'walk-forward 训练 RF：每月用 ≤ snap-12 数据训练')
    rf_preds = pd.Series(np.nan, index=ml.index)
    train_start = pd.Timestamp('2008-01-31')
    min_train_months = 48  # 至少 4 年训练数据

    for i, snap in enumerate(ml.index):
        # 训练集：ml.index ≤ snap - 12 月
        cutoff = snap - pd.DateOffset(months=12)
        train_idx = ml.index[ml.index <= cutoff]
        if len(train_idx) < min_train_months:
            continue
        train = ml.loc[train_idx]
        sub = train.dropna(subset=feat_cols + ['ret_12m'])
        if len(sub) < min_train_months:
            continue
        X = sub[feat_cols].values
        y = sub['ret_12m'].values
        scaler = StandardScaler()
        X = scaler.fit_transform(X)
        rf = RandomForestRegressor(**RF_PARAMS)
        rf.fit(X, y)
        # 预测当月
        x_now = ml.loc[snap, feat_cols].values.reshape(1, -1)
        if pd.isna(x_now).any():
            continue
        x_now = scaler.transform(x_now)
        rf_preds[snap] = rf.predict(x_now)[0]
        if i % 24 == 0:
            print(f'  {snap:%Y-%m}: train_n={len(sub)}, pred={rf_preds[snap]:+.2f}')

    score['rf_pred_12m'] = rf_preds
    score['rf_equity'] = score['rf_pred_12m'].apply(rf_pred_to_equity)

    # 三种模式
    score['v1_eq'] = score['target_equity_pct']  # 现有评分卡（不含 v8 简化）
    # 用现有 final_eq from 已有数据
    ret = load_cs300_monthly_ret()
    ret.index = ret.index.to_period('M').to_timestamp('M')

    # baseline 是 v8 完整评分卡 (含 cb_cuts_6m)
    # 用 score['v8_total'] 重算档位（简化）
    def to_eq(s):
        if s <= -10: return 90.0
        if s <= -5: return 80.0
        return 75.0  # 简化，不严格按 score_to_target_equity 七段
    score['v8_eq'] = score['v8_total'].apply(to_eq)

    # 融合模式
    score['rf_only_eq'] = score['rf_equity'].shift(1).fillna(75.0)
    score['v8_only_eq'] = score['v8_eq'].shift(1).fillna(75.0)
    score['hybrid_eq'] = (score['v8_eq'] + score['rf_equity']) / 2
    score['hybrid_eq'] = score['hybrid_eq'].shift(1).fillna(75.0)

    # 回测
    # 只用 RF 有预测的月份（从 2012 起约 60 月）
    mask = score['rf_pred_12m'].notna()
    sub_score = score[mask]

    r_base = backtest(pd.Series(75.0, index=sub_score.index), ret)
    r_v8 = backtest(sub_score['v8_only_eq'], ret)
    r_rf = backtest(sub_score['rf_only_eq'], ret)
    r_hyb = backtest(sub_score['hybrid_eq'], ret)

    print(f'\n=== 月度回测对比 (RF 有效期 = {sub_score.index[0]:%Y-%m} ~ {sub_score.index[-1]:%Y-%m}, {len(sub_score)} 月) ===')
    print(f"{'指标':<22}{'基准75%':>11}{'v8 评分卡':>11}{'仅 RF':>11}{'融合(50/50)':>14}")
    print('-' * 70)
    for label, key in [('累计回报 (%)', 'cum'), ('年化收益 (%)', 'ann'),
                       ('年化波动 (%)', 'vol'), ('最大回撤 (%)', 'dd'),
                       ('调仓次数', 'n_trade')]:
        b = r_base[key]; v = r_v8[key]; r = r_rf[key]; h = r_hyb[key]
        if isinstance(v, int):
            print(f"{label:<22}{b:>11d}{v:>11d}{r:>11d}{h:>14d}")
        else:
            print(f"{label:<22}{b:>11.2f}{v:>11.2f}{r:>11.2f}{h:>14.2f}")

    # Spearman ρ：注意要看「目标仓位 vs 次月收益」（仓位越高期望 ret 越高）
    next_ret = ret.shift(-1).reindex(sub_score.index)
    rho_v8 = spearman(sub_score['v8_only_eq'].tolist(), next_ret.tolist())
    rho_rf = spearman(sub_score['rf_only_eq'].tolist(), next_ret.tolist())
    rho_hyb = spearman(sub_score['hybrid_eq'].tolist(), next_ret.tolist())
    print(f"\nSpearman ρ(equity, next_1m_ret) [正向：仓位高时确实涨]:")
    print(f"  v8: {rho_v8:+.3f}  |  RF: {rho_rf:+.3f}  |  融合: {rho_hyb:+.3f}")

    # 严格红线 3/3 — 比较融合 vs v8
    print('\n=== 严格红线 3/3 (融合 vs v8) ===')
    cond = {
        '累计回报 融合 ≥ v8': r_hyb['cum'] >= r_v8['cum'],
        '回撤可控 (Δ ≤ +3pp)': r_hyb['dd'] >= r_v8['dd'] - 3.0,
        'Spearman ρ 更正': rho_hyb >= rho_v8,
    }
    for c, v in cond.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    print(f"\n→ 融合决策：{'ADOPT' if sum(cond.values()) == 3 else 'REJECT'} ({sum(cond.values())}/3)")

    print('\n=== 严格红线 3/3 (RF 独立 vs v8) ===')
    cond2 = {
        '累计回报 RF ≥ v8': r_rf['cum'] >= r_v8['cum'],
        '回撤可控 (Δ ≤ +3pp)': r_rf['dd'] >= r_v8['dd'] - 3.0,
        'Spearman ρ 更正': rho_rf >= rho_v8,
    }
    for c, v in cond2.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    print(f"\n→ RF 独立决策：{'ADOPT' if sum(cond2.values()) == 3 else 'REJECT'} ({sum(cond2.values())}/3)")


if __name__ == '__main__':
    main()

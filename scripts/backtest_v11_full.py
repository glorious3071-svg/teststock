#!/usr/bin/env python3.11
"""v11 完整月度回测 — 含 v8 + R1(pmi_non_mfg) + R-GVF 全外部规则

对比 4 个版本（在主干 v3.4.11 档位 + 三重门基础上）：
  - 基准 75%（不调仓）
  - v8        (含 cb_cuts_6m, sentiment v5/v6, PMI 反转 v9)
  - v10       (v8 + R1 pmi_non_mfg)
  - v11       (v10 + Gold/VIX/FED_high)

完整指标：累计回报、年化、波动、Sharpe、最大回撤、ρ_1m/3m/6m/12m、方向命中率
"""

from __future__ import annotations

import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs, policy_triple_gate, score_to_target_equity,
)

load_dotenv(ROOT / '.env')
fm.FontProperties(fname='/System/Library/Fonts/PingFang.ttc')
plt.rcParams['font.family'] = 'PingFang HK'

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10

OUT_PNG = ROOT / 'docs' / 'assets' / 'v11_backtest_comparison.png'
OUT_CSV = ROOT / 'data' / 'v11_backtest_series.csv'


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def spearman(xs, ys):
    a = pd.Series(xs); b = pd.Series(ys)
    sub = pd.concat([a, b], axis=1).dropna()
    if len(sub) < 5: return np.nan
    return sub.iloc[:, 0].rank().corr(sub.iloc[:, 1].rank())


def apply_gate(row, score_col):
    eq, _ = score_to_target_equity(int(row[score_col]))
    if eq < 80:
        return float(eq)
    inp = ScorecardInputs(
        pboc_tone=row.get('pboc') if pd.notna(row.get('pboc')) else None,
        central_meeting_tone=row.get('cmt') if pd.notna(row.get('cmt')) else None,
        ppi_yoy_change='turn_positive' if row.get('fun_score', 0) < 0 else 'flat',
    )
    passed, _ = policy_triple_gate(inp)
    return float(eq) if passed else 75.0


def backtest(eq_series, ret_series):
    df = pd.DataFrame({'eq': eq_series, 'ret': ret_series}).dropna()
    df['eq_prev'] = df['eq'].shift(1).fillna(75.0)
    df['turn'] = (df['eq'] - df['eq_prev']).abs()
    cash_m = CASH_ANNUAL_RATE / 12 * 100
    df['gross'] = df['eq']/100*df['ret'] + (1-df['eq']/100)*cash_m
    df['cost'] = df['turn']/100*COST_PCT
    df['net'] = df['gross'] - df['cost']
    df['nav'] = (1 + df['net']/100).cumprod() * INITIAL_CAPITAL
    n = len(df)
    cum = (df['nav'].iloc[-1]/INITIAL_CAPITAL - 1)*100
    ann = ((df['nav'].iloc[-1]/INITIAL_CAPITAL) ** (12/n) - 1)*100
    vol = df['net'].std() * (12**0.5)
    sharpe = (ann - CASH_ANNUAL_RATE*100) / vol if vol > 0 else 0
    peak = df['nav'].cummax()
    dd = (df['nav']/peak - 1).min() * 100
    n_trade = (df['turn'] > 0).sum()
    return {'cum': cum, 'ann': ann, 'vol': vol, 'sharpe': sharpe, 'dd': dd,
            'n_trade': n_trade, 'nav': df['nav']}


def main():
    # 旧月度评分（含 v3.4.11 档位、v5/v6 sentiment、v9 PMI 反转等已合入 main 的改动）
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')
    score['pmi_non_mfg'] = ml['pmi_non_mfg']
    score['cb_cuts_6m'] = ml['cb_cuts_6m']

    # v11 新数据
    conn = db()
    ext = pd.read_sql(
        "SELECT month, gold_yoy_pct, vix_30d_avg, fed_rate_level FROM external_macro_monthly ORDER BY month",
        conn,
    )
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    ext['date'] = pd.to_datetime(ext['month'], format='%Y%m')
    ext = ext.set_index('date').sort_index()
    score['gold_yoy_pct'] = ext['gold_yoy_pct']
    score['vix_30d_avg'] = ext['vix_30d_avg']
    score['fed_rate_level'] = ext['fed_rate_level']

    cs_m = cs['close'].astype(float).resample('ME').last()
    cs_m.index = cs_m.index.to_period('M').to_timestamp('M')
    ret_curr = cs_m.pct_change() * 100

    # 构造 4 个评分版本
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['r1_extra'] = score['pmi_non_mfg'].apply(
        lambda v: 1 if (pd.notna(v) and v > 55) else (-1 if (pd.notna(v) and v < 50) else 0))
    score['gold_extra'] = score['gold_yoy_pct'].apply(lambda x: -1 if pd.notna(x) and x > 25 else 0)
    score['vix_extra'] = score['vix_30d_avg'].apply(lambda x: -1 if pd.notna(x) and x > 30 else 0)
    score['fed_extra'] = score['fed_rate_level'].apply(lambda x: -1 if pd.notna(x) and x >= 4.5 else 0)

    score['v8_total'] = score['total_score'] + score['v8_extra']
    score['v10_total'] = score['v8_total'] + score['r1_extra']
    score['v11_total'] = score['v10_total'] + score['gold_extra'] + score['vix_extra'] + score['fed_extra']

    versions = [
        ('baseline 75%', None),
        ('v8',  'v8_total'),
        ('v10', 'v10_total'),
        ('v11', 'v11_total'),
    ]
    colors = {'baseline 75%': '#6b7280', 'v8': '#2563eb', 'v10': '#16a34a', 'v11': '#dc2626'}

    results = {}
    next_ret = cs_m.pct_change().shift(-1) * 100
    ret_3m = cs_m.pct_change(3).shift(-3) * 100
    ret_6m = cs_m.pct_change(6).shift(-6) * 100
    ret_12m = cs_m.pct_change(12).shift(-12) * 100

    for name, col in versions:
        if col is None:
            eq_held = pd.Series(75.0, index=score.index)
            scores_for_rho = pd.Series(0.0, index=score.index)
        else:
            eq_now = score.apply(lambda r, c=col: apply_gate(r, c), axis=1)
            eq_held = eq_now.shift(1).fillna(75.0)
            scores_for_rho = score[col]
        m = backtest(eq_held, ret_curr)
        m['rho_1m'] = spearman(scores_for_rho.tolist(), next_ret.reindex(score.index).tolist())
        m['rho_3m'] = spearman(scores_for_rho.tolist(), ret_3m.reindex(score.index).tolist())
        m['rho_6m'] = spearman(scores_for_rho.tolist(), ret_6m.reindex(score.index).tolist())
        m['rho_12m'] = spearman(scores_for_rho.tolist(), ret_12m.reindex(score.index).tolist())
        results[name] = m

    print('='*90)
    print('v11 评分卡完整 18 年回测 (2008-01 ~ 2025-12)')
    print('='*90)
    print(f"{'指标':<22}{'基准75%':>11}{'v8':>11}{'v10(+R1)':>11}{'v11(+GVF)':>11}{'Δ(v11-v8)':>11}")
    print('-'*90)
    metrics_table = [
        ('累计回报 (%)', 'cum'), ('年化收益 (%)', 'ann'),
        ('年化波动 (%)', 'vol'), ('Sharpe', 'sharpe'),
        ('最大回撤 (%)', 'dd'), ('调仓次数', 'n_trade'),
        ('Spearman ρ_1m', 'rho_1m'), ('Spearman ρ_3m', 'rho_3m'),
        ('Spearman ρ_6m', 'rho_6m'), ('Spearman ρ_12m', 'rho_12m'),
    ]
    for label, k in metrics_table:
        b = results['baseline 75%'][k]
        v8 = results['v8'][k]
        v10 = results['v10'][k]
        v11 = results['v11'][k]
        delta = v11 - v8
        if isinstance(b, int):
            print(f"{label:<22}{b:>11d}{v8:>11d}{v10:>11d}{v11:>11d}{delta:>+11d}")
        else:
            print(f"{label:<22}{b:>11.3f}{v8:>11.3f}{v10:>11.3f}{v11:>11.3f}{delta:>+11.3f}")

    # 红线判定 v11 vs v8
    print('\n=== 严格红线 3/3 (v11 vs v8 基准) ===')
    cond = {
        '累计回报 v11 ≥ v8': results['v11']['cum'] >= results['v8']['cum'],
        '回撤可控 (Δ ≤ +3pp)': results['v11']['dd'] >= results['v8']['dd'] - 3.0,
        'Spearman ρ_12m 更负': results['v11']['rho_12m'] <= results['v8']['rho_12m'],
    }
    for c, v in cond.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    print(f"\n→ 决策：{'ADOPT' if sum(cond.values()) == 3 else 'REJECT'} ({sum(cond.values())}/3)")

    # 输出 csv
    score['v11_eq'] = score.apply(lambda r: apply_gate(r, 'v11_total'), axis=1)
    out_df = score[['total_score', 'v8_total', 'v10_total', 'v11_total',
                     'gold_yoy_pct', 'vix_30d_avg', 'fed_rate_level',
                     'v11_eq']].copy()
    out_df.to_csv(OUT_CSV)
    print(f'\n月度评分序列已保存：{OUT_CSV}')

    # ── 可视化 ──────────────────────────────────────────────
    print('生成可视化...')
    fig = plt.figure(figsize=(18, 13))
    fig.patch.set_facecolor('white')
    fig.suptitle('v11 完整评分卡 18 年回测对比（baseline / v8 / v10 / v11）',
                 fontsize=14, fontweight='bold', y=0.99)

    def st(ax):
        ax.set_facecolor('#f8f9fa')
        ax.tick_params(labelsize=8)
        ax.grid(color='#dee2e6', alpha=0.7)
        for s in ax.spines.values(): s.set_color('#dee2e6')

    # ① 净值
    ax1 = fig.add_axes([0.06, 0.56, 0.92, 0.36])
    st(ax1)
    for name, _ in versions:
        nav = results[name]['nav'] / 1e6
        ax1.plot(nav.index, nav.values, color=colors[name], linewidth=1.4,
                 label=f"{name} (年化{results[name]['ann']:.2f}% / 回撤{results[name]['dd']:.1f}%)")
    ax1.set_ylabel('累计净值（百万元）', fontsize=9)
    ax1.set_title('① 累计净值（初始 100 万）', fontsize=11, fontweight='bold', pad=6)
    ax1.legend(loc='upper left', fontsize=9, framealpha=0.92)
    ax1.xaxis.set_major_locator(mdates.YearLocator(2))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    # ② ρ_12m 条形图
    ax2 = fig.add_axes([0.06, 0.10, 0.42, 0.36])
    st(ax2)
    names = [n for n, _ in versions]
    rho_values = [results[n]['rho_12m'] for n in names]
    bar_colors = [colors[n] for n in names]
    bars = ax2.barh(names, rho_values, color=bar_colors, alpha=0.85)
    ax2.axvline(0, color='#1a1a2e', linewidth=0.6)
    ax2.set_xlabel('Spearman ρ_12m（评分 vs 次 12 月收益，越负越好）', fontsize=9)
    ax2.set_title('② 长期预测能力 ρ_12m 对比', fontsize=11, fontweight='bold', pad=6)
    for bar, v in zip(bars, rho_values):
        ax2.text(v + (0.005 if v > 0 else -0.005), bar.get_y() + bar.get_height()/2,
                  f'{v:+.3f}', va='center', ha='left' if v > 0 else 'right',
                  fontsize=10, fontweight='bold')
    ax2.invert_yaxis()

    # ③ 关键指标条形对比
    ax3 = fig.add_axes([0.55, 0.10, 0.42, 0.36])
    st(ax3)
    width = 0.20
    x = np.arange(3)  # 累计回报, 最大回撤*-1, Sharpe*30
    for i, name in enumerate(names):
        m = results[name]
        vals = [m['cum'], -m['dd'], m['sharpe'] * 30]
        ax3.bar(x + i * width - 1.5 * width, vals, width, color=colors[name],
                alpha=0.85, label=name)
    ax3.set_xticks(x)
    ax3.set_xticklabels(['累计回报 (%)', '抗回撤 (-DD %)', 'Sharpe × 30'])
    ax3.set_title('③ 风险收益指标对比', fontsize=11, fontweight='bold', pad=6)
    ax3.legend(loc='upper left', fontsize=8, framealpha=0.92)

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PNG, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    print(f'已保存：{OUT_PNG}')


if __name__ == '__main__':
    main()

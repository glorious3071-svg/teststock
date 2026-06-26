#!/usr/bin/env python3.11
"""月度评分卡 P&L 回测 — 三策略对比

策略：
  A. baseline    : 全程 75% 平衡仓位（不调）
  B. annual      : 年初按上年 12 月评分定仓位（年度调仓 18 次）
  C. monthly     : 每月底按当月评分定仓位（月度调仓 ≤216 次）

P&L 模型：
  月收益% = equity_pct/100 × cs300_month_ret_pct + cash_pct/100 × (annual_cash_rate/12 × 100)
  交易成本：每次 |equity_pct change| × 0.10%（万一双边）
  月复利累加

输出：
  - 各策略累计回报、年化、波动、Sharpe、最大回撤
  - 评分/仓位/净值时序图（docs/assets/monthly_scorecard_backtest.png）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import ScorecardInputs, policy_triple_gate

load_dotenv(ROOT / ".env")

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PNG = ROOT / "docs" / "assets" / "monthly_scorecard_backtest.png"
CACHE = ROOT / "data" / "monthly_scorecard_series.csv"

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PER_TRADE_PCT = 0.10  # 单边千分之一（约券商佣金）


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_cs300_monthly():
    conn = db()
    df = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    df['close'] = df['close'].astype(float)
    monthly = df['close'].resample('ME').last()
    monthly_ret = monthly.pct_change() * 100  # %
    return monthly, monthly_ret


def load_score_series():
    df = pd.read_csv(CACHE)
    df['snapshot'] = pd.to_datetime(df['snapshot'])
    df = df.set_index('snapshot').sort_index()
    df['ym'] = df.index.strftime('%Y-%m')
    return df


def apply_gate(row) -> float:
    """三重门校验，未通过则降到 75%"""
    if row['target_equity_pct'] < 80:
        return float(row['target_equity_pct'])
    inp = ScorecardInputs(
        pboc_tone=row['pboc'] if pd.notna(row['pboc']) else None,
        central_meeting_tone=row['cmt'] if pd.notna(row['cmt']) else None,
        ppi_yoy_change='turn_positive' if row['fun_score'] < 0 else 'flat',
    )
    passed, _ = policy_triple_gate(inp)
    return float(row['target_equity_pct']) if passed else 75.0


def backtest_strategy(equity_series: pd.Series, monthly_ret: pd.Series,
                      cost_pct: float = COST_PER_TRADE_PCT) -> pd.DataFrame:
    """对齐 equity_series 和 monthly_ret，月度复利。
    equity_series 索引应该是 month-end 日期，与 monthly_ret 同频对齐。
    """
    df = pd.DataFrame({'equity_pct': equity_series, 'cs_ret': monthly_ret}).dropna()
    df['equity_pct_prev'] = df['equity_pct'].shift(1).fillna(75.0)
    df['turnover'] = (df['equity_pct'] - df['equity_pct_prev']).abs()
    # 月收益% = equity * cs_ret + cash * (cash_rate/12)
    cash_rate_month = CASH_ANNUAL_RATE / 12 * 100
    df['gross_ret_pct'] = df['equity_pct'] / 100 * df['cs_ret'] + (1 - df['equity_pct'] / 100) * cash_rate_month
    df['cost_pct'] = df['turnover'] / 100 * cost_pct
    df['net_ret_pct'] = df['gross_ret_pct'] - df['cost_pct']
    df['nav'] = (1 + df['net_ret_pct'] / 100).cumprod() * INITIAL_CAPITAL
    return df


def metrics(df: pd.DataFrame) -> dict:
    n_months = len(df)
    years = n_months / 12
    cum = (df['nav'].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    ann = ((df['nav'].iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
    vol = df['net_ret_pct'].std() * (12 ** 0.5)
    sharpe = (ann - CASH_ANNUAL_RATE * 100) / vol if vol > 0 else 0
    peak = df['nav'].cummax()
    dd = (df['nav'] / peak - 1).min() * 100
    n_trade = (df['turnover'] > 0).sum()
    total_cost = df['cost_pct'].sum()
    return {
        'cumulative_return_pct': cum,
        'annualized_return_pct': ann,
        'annualized_vol_pct': vol,
        'sharpe': sharpe,
        'max_drawdown_pct': dd,
        'n_trades': n_trade,
        'total_cost_pct': total_cost,
    }


def main():
    score = load_score_series()
    score['final_equity'] = score.apply(apply_gate, axis=1)

    _, monthly_ret = load_cs300_monthly()
    monthly_ret.index = monthly_ret.index.to_period('M').to_timestamp('M')

    # 对齐三策略的 equity series（均索引到 month-end）
    score.index = score.index.to_period('M').to_timestamp('M')

    # A. baseline: 全程 75%
    baseline_eq = pd.Series(75.0, index=score.index)
    # B. annual: 年初看上年 12 月评分
    annual_eq = score['final_equity'].copy()
    # 把每年 1 月持有去年 12 月信号的仓位
    annual_year = annual_eq.copy()
    annual_year[:] = 0.0
    # 用上年 12 月信号填充当年 1-12 月
    for y in range(2009, 2026):
        snap = f"{y - 1}-12"
        snap_idx = score.index[score.index.strftime('%Y-%m') == snap]
        if len(snap_idx):
            target = score.loc[snap_idx[0], 'final_equity']
            mask = (score.index.year == y)
            annual_year.loc[mask] = target
    # 2008 用 2007 评分（没数据 → 默认 75）
    annual_year.loc[annual_year == 0.0] = 75.0
    annual_eq = annual_year
    # C. monthly: 当月底信号 → 下月 持有
    monthly_eq = score['final_equity'].shift(1).fillna(75.0)

    # 回测
    res_base = backtest_strategy(baseline_eq, monthly_ret, cost_pct=0)
    res_ann = backtest_strategy(annual_eq, monthly_ret)
    res_mon = backtest_strategy(monthly_eq, monthly_ret)

    m_base = metrics(res_base)
    m_ann = metrics(res_ann)
    m_mon = metrics(res_mon)

    print('=' * 78)
    print('月度评分卡 P&L 回测 (2008-01 ~ 2025-12, 216 月)')
    print('=' * 78)
    print(f"{'指标':<22}{'A 基准75%':>14}{'B 年度评分':>14}{'C 月度评分':>14}")
    print('-' * 78)
    for label, key in [
        ('累计回报 (%)', 'cumulative_return_pct'),
        ('年化收益 (%)', 'annualized_return_pct'),
        ('年化波动 (%)', 'annualized_vol_pct'),
        ('Sharpe', 'sharpe'),
        ('最大回撤 (%)', 'max_drawdown_pct'),
        ('调仓次数', 'n_trades'),
        ('累计交易成本 (%)', 'total_cost_pct'),
    ]:
        b = m_base[key]; a = m_ann[key]; c = m_mon[key]
        if isinstance(b, float):
            print(f"{label:<22}{b:>14.2f}{a:>14.2f}{c:>14.2f}")
        else:
            print(f"{label:<22}{b:>14d}{a:>14d}{c:>14d}")

    # 决策标准
    print('\n=== 月度 vs 年度对比 ===')
    delta = {
        '累计回报 ↑': m_mon['cumulative_return_pct'] - m_ann['cumulative_return_pct'],
        '年化收益 ↑': m_mon['annualized_return_pct'] - m_ann['annualized_return_pct'],
        'Sharpe ↑':   m_mon['sharpe'] - m_ann['sharpe'],
        '最大回撤 ↓ (负数好)': m_mon['max_drawdown_pct'] - m_ann['max_drawdown_pct'],
        '波动 ↓ (负数好)': m_mon['annualized_vol_pct'] - m_ann['annualized_vol_pct'],
    }
    for k, v in delta.items():
        print(f"  {k:>20s}: {v:+.2f}")

    # ── 可视化 ─────────────────────────────────────────────
    print('\n生成可视化...')
    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor("white")
    fig.suptitle("月度评分卡 P&L 回测：基准 vs 年度 vs 月度（2008-2025）",
                 fontsize=15, fontweight="bold", color="#1a1a2e", y=0.99)

    DARK_BG, GRID_COL = "#f8f9fa", "#dee2e6"
    def st(ax):
        ax.set_facecolor(DARK_BG)
        ax.tick_params(colors="#495057", labelsize=8)
        for s in ax.spines.values():
            s.set_color(GRID_COL)
        ax.grid(color=GRID_COL, linewidth=0.6, alpha=0.7)

    ax1 = fig.add_axes([0.06, 0.74, 0.92, 0.18])  # 净值
    ax2 = fig.add_axes([0.06, 0.51, 0.92, 0.18])  # 仓位
    ax3 = fig.add_axes([0.06, 0.29, 0.92, 0.18])  # 评分分维度
    ax4 = fig.add_axes([0.06, 0.07, 0.92, 0.18])  # 评分 vs CS300

    # ① 净值曲线
    st(ax1)
    ax1.plot(res_base.index, res_base['nav']/1e6, color="#6b7280",
             linewidth=1.2, label=f"A 基准75% (年化{m_base['annualized_return_pct']:.2f}%)")
    ax1.plot(res_ann.index, res_ann['nav']/1e6, color="#2563eb",
             linewidth=1.2, label=f"B 年度评分 (年化{m_ann['annualized_return_pct']:.2f}%, MDD{m_ann['max_drawdown_pct']:.1f}%)")
    ax1.plot(res_mon.index, res_mon['nav']/1e6, color="#dc2626",
             linewidth=1.4, label=f"C 月度评分 (年化{m_mon['annualized_return_pct']:.2f}%, MDD{m_mon['max_drawdown_pct']:.1f}%)")
    ax1.set_ylabel("净值（百万元，初始 100）", fontsize=9)
    ax1.set_title("① 累计净值曲线（初始资金 100 万元）",
                  fontsize=10, fontweight="bold", pad=6)
    ax1.legend(loc="upper left", fontsize=8, framealpha=0.92)
    ax1.xaxis.set_major_locator(mdates.YearLocator(2))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ② 仓位时序
    st(ax2)
    ax2.fill_between(score.index, 0, [75]*len(score), color="#6b7280",
                     alpha=0.3, label="A 基准 75%")
    ax2.step(annual_eq.index, annual_eq.values, color="#2563eb",
             linewidth=1.2, label="B 年度", where='post')
    ax2.step(monthly_eq.index, monthly_eq.values, color="#dc2626",
             linewidth=1.0, label="C 月度", where='post', alpha=0.85)
    ax2.set_ylim(20, 95)
    ax2.set_ylabel("目标股票仓位 (%)", fontsize=9)
    ax2.set_title("② 目标仓位时序（三重门已过滤）",
                  fontsize=10, fontweight="bold", pad=6)
    ax2.legend(loc="lower left", fontsize=8, framealpha=0.92)
    ax2.xaxis.set_major_locator(mdates.YearLocator(2))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ③ 评分分维度堆叠
    st(ax3)
    dims = [
        ('val_score', '估值', '#dc2626'),
        ('liq_score', '流动性', '#0891b2'),
        ('fun_score', '基本面', '#16a34a'),
        ('sen_score', '情绪', '#f59e0b'),
        ('ext_score', '外部', '#7c3aed'),
        ('pol_score', '政策', '#6b7280'),
    ]
    # 拆正负
    for col, label, color in dims:
        ax3.bar(score.index, score[col], width=22,
                color=color, alpha=0.55, label=label)
    ax3.plot(score.index, score['total_score'], color="#1a1a2e",
             linewidth=1.4, label="总分", alpha=0.85)
    ax3.axhline(0, color="#1a1a2e", linewidth=0.5)
    ax3.axhline(-5, color="#16a34a", linewidth=0.6, linestyle='--', alpha=0.6)
    ax3.text(score.index[5], -5.5, "score≤-5 进加仓档", fontsize=6.5, color="#16a34a")
    ax3.axhline(3, color="#dc2626", linewidth=0.6, linestyle='--', alpha=0.6)
    ax3.text(score.index[5], 3.5, "score>3 进减仓档", fontsize=6.5, color="#dc2626")
    ax3.set_ylabel("评分", fontsize=9)
    ax3.set_title("③ 月度评分时序（含各维度分项贡献）",
                  fontsize=10, fontweight="bold", pad=6)
    ax3.legend(loc="upper right", fontsize=7, framealpha=0.92, ncol=4)
    ax3.xaxis.set_major_locator(mdates.YearLocator(2))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ④ 总分 vs CS300
    st(ax4)
    ax4.bar(score.index, score['total_score'], width=22,
            color=["#dc2626" if s > 0 else "#16a34a" for s in score['total_score']],
            alpha=0.55, label="月度总分")
    ax4.set_ylabel("评分", fontsize=9, color="#1a1a2e")
    ax42 = ax4.twinx()
    cs_dates = monthly_ret.cumsum().index
    cs_nav = (1 + monthly_ret / 100).cumprod() * 100
    ax42.plot(cs_nav.index, cs_nav.values, color="#1d4ed8",
              linewidth=1.4, label="沪深 300 累计净值")
    ax42.set_ylabel("CS300 累计净值", color="#1d4ed8", fontsize=9)
    ax42.tick_params(colors="#1d4ed8", labelsize=8)
    ax42.spines['right'].set_color("#1d4ed8")
    ax4.set_title("④ 月度评分 vs 沪深 300 累计净值",
                  fontsize=10, fontweight="bold", pad=6)
    ax4.legend(loc="upper left", fontsize=8, framealpha=0.92)
    ax42.legend(loc="upper right", fontsize=8, framealpha=0.92)
    ax4.xaxis.set_major_locator(mdates.YearLocator(2))
    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    summary = (
        f"  18 年 (216 月) 回测对比： A 基准 累计 {m_base['cumulative_return_pct']:+.1f}% "
        f"| B 年度 {m_ann['cumulative_return_pct']:+.1f}% (回撤 {m_ann['max_drawdown_pct']:.1f}%) "
        f"| C 月度 {m_mon['cumulative_return_pct']:+.1f}% (回撤 {m_mon['max_drawdown_pct']:.1f}%)\n"
        f"  调仓次数：B {m_ann['n_trades']} 次 | C {m_mon['n_trades']} 次  "
        f"｜ 累计交易成本 B {m_ann['total_cost_pct']:.2f}% | C {m_mon['total_cost_pct']:.2f}%\n"
        f"  ΔSharpe (C-A) = {m_mon['sharpe']-m_base['sharpe']:+.2f}  "
        f"｜ Δ年化回报 (C-A) = {m_mon['annualized_return_pct']-m_base['annualized_return_pct']:+.2f}%"
    )
    fig.text(0.5, 0.03, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5", edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据：cn_*_monthly + index_daily + macro_annual_snapshot ｜评分代码：backtest/scorecard.py（v3.4.1 + v5 + v6 + v3.4.9）",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PNG}")


if __name__ == '__main__':
    main()

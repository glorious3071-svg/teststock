#!/usr/bin/env python3.11
"""企业景气/信心指数可视化

① 景气 + 信心指数时序（含 100 荣枯线 + 历史事件）
② 与 CS300 季度涨跌对比（看同步性）
③ 各阶段均值柱状图（量化不同时期的企业信心水平）
④ 景气指数 YoY 趋势（感知加速/减速）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path('/Users/jingxuan/workspace/teststock')
sys.path.insert(0, str(ROOT))

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(ROOT / '.env')
fm.FontProperties(fname='/System/Library/Fonts/PingFang.ttc')
plt.rcParams['font.family'] = 'PingFang HK'

OUT_PATH = Path(__file__).resolve().parent.parent / 'docs' / 'assets' / 'enterprise_boom_analysis.png'
WORKTREE = Path(__file__).resolve().parent.parent
OUT_PATH_LOCAL = WORKTREE / 'docs' / 'assets' / 'enterprise_boom_analysis.png'

DARK_BG, GRID = '#f8f9fa', '#dee2e6'
BOOM_COL = '#2563eb'
CONF_COL = '#dc2626'
CS300_COL = '#16a34a'

EVENTS = [
    ('2008-09', '雷曼', '#dc2626'),
    ('2009-03', '四万亿', '#16a34a'),
    ('2015-06', '股灾', '#dc2626'),
    ('2018-04', '贸易战', '#dc2626'),
    ('2020-01', 'COVID', '#dc2626'),
    ('2021-12', '信心数据停', '#6b7280'),
    ('2022-04', '上海封控', '#dc2626'),
    ('2024-09', '924反转', '#16a34a'),
]


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_data():
    conn = db()
    boom = pd.read_sql(
        """SELECT quarter_date, cal_year, cal_quarter,
                  boom_index, confidence_index, boom_yoy
           FROM cn_enterprise_boom_quarterly ORDER BY quarter_date""",
        conn, parse_dates=['quarter_date'], index_col='quarter_date',
    )
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()

    boom['boom_index'] = boom['boom_index'].astype(float)
    boom['confidence_index'] = boom['confidence_index'].astype(float)
    boom['boom_yoy'] = boom['boom_yoy'].astype(float)

    cs_q = cs['close'].astype(float).resample('QE').last()
    cs_q_ret = cs_q.pct_change() * 100  # 当季涨跌

    return boom, cs_q_ret


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors='#495057', labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.7)


def fmt_xaxis(ax: plt.Axes, year_step: int = 2) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))


def mark_events(ax: plt.Axes, y_top: float, y_range: float) -> None:
    for date_str, label, color in EVENTS:
        dt = pd.Timestamp(date_str)
        ax.axvline(dt, color=color, alpha=0.25, linewidth=0.8, linestyle='--')
        ax.text(dt, y_top + y_range * 0.02, label,
                rotation=90, fontsize=6, color=color, alpha=0.85,
                va='bottom', ha='right')


def main():
    boom, cs_q_ret = load_data()

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor('white')
    fig.suptitle('企业景气指数 + 企业家信心指数（国家统计局，2005Q1-2026Q1）',
                 fontsize=15, fontweight='bold', y=0.99)

    ax1 = fig.add_axes([0.06, 0.72, 0.92, 0.22])  # 景气+信心时序
    ax2 = fig.add_axes([0.06, 0.49, 0.92, 0.18])  # CS300 对比
    ax3 = fig.add_axes([0.06, 0.27, 0.42, 0.17])  # 各阶段均值
    ax4 = fig.add_axes([0.55, 0.27, 0.42, 0.17])  # YoY 趋势
    ax5 = fig.add_axes([0.06, 0.07, 0.92, 0.15])  # 信心指数散点 vs CS300

    # ① 景气 + 信心指数时序
    style_ax(ax1)
    ax1.plot(boom.index, boom['boom_index'], color=BOOM_COL, linewidth=1.8,
             label='企业景气指数', marker='o', markersize=3)
    ax1.plot(boom.dropna(subset=['confidence_index']).index,
             boom['confidence_index'].dropna(), color=CONF_COL, linewidth=1.8,
             label='企业家信心指数（2021Q4后停止发布）', marker='s', markersize=3)
    ax1.axhline(100, color='#1a1a2e', linewidth=1.0, linestyle='--', alpha=0.7)
    ax1.text(boom.index[2], 101, '荣枯线 100（企业景气/不景气分界）',
             fontsize=7.5, color='#1a1a2e', alpha=0.85)
    ax1.fill_between(boom.index, 100, boom['boom_index'],
                     where=boom['boom_index'] >= 100,
                     color=BOOM_COL, alpha=0.08, label='景气区')
    ax1.fill_between(boom.index, 100, boom['boom_index'],
                     where=boom['boom_index'] < 100,
                     color='#dc2626', alpha=0.15, label='不景气区')
    mark_events(ax1, boom['boom_index'].max(), boom['boom_index'].max() - 90)
    fmt_xaxis(ax1)
    ax1.set_xlim(boom.index[0], boom.index[-1])
    ax1.set_ylabel('指数点位', fontsize=9)
    ax1.set_title('① 企业景气指数 + 企业家信心指数（季度，荣枯线=100）',
                  fontsize=11, fontweight='bold', pad=6)
    ax1.legend(loc='lower left', fontsize=8, framealpha=0.92)

    # ② CS300 季度涨跌对比
    style_ax(ax2)
    cs_reindexed = cs_q_ret.reindex(boom.index)
    pos = cs_reindexed.where(cs_reindexed >= 0, 0)
    neg = cs_reindexed.where(cs_reindexed < 0, 0)
    ax2.bar(boom.index, pos.values, width=60, color=CS300_COL, alpha=0.7, label='CS300 季度涨')
    ax2.bar(boom.index, neg.values, width=60, color='#dc2626', alpha=0.7, label='CS300 季度跌')
    # 叠加景气指数归一化（视觉对比）
    ax2b = ax2.twinx()
    boom_norm = (boom['boom_index'] - 100) / boom['boom_index'].std()
    ax2b.plot(boom.index, boom_norm.values, color=BOOM_COL, linewidth=1.4,
              alpha=0.6, label='景气指数（标准化）')
    ax2b.set_ylabel('景气指数（标准化）', color=BOOM_COL, fontsize=8)
    ax2b.tick_params(colors=BOOM_COL, labelsize=7)
    ax2b.spines['right'].set_color(BOOM_COL)
    ax2.axhline(0, color='#1a1a2e', linewidth=0.6)
    fmt_xaxis(ax2)
    ax2.set_xlim(boom.index[0], boom.index[-1])
    ax2.set_ylabel('CS300 季度涨跌 %', fontsize=9)
    ax2.set_title('② CS300 季度涨跌 vs 企业景气指数（同步性验证）',
                  fontsize=11, fontweight='bold', pad=6)
    ax2.legend(loc='lower left', fontsize=8, framealpha=0.92)
    ax2b.legend(loc='lower right', fontsize=8, framealpha=0.92)

    # ③ 各阶段均值柱状图
    style_ax(ax3)
    phases = [
        ('2005-2007', '2005Q1', '2007Q4', '高增长期'),
        ('2008-2009', '2008Q1', '2009Q4', '金融危机'),
        ('2010-2014', '2010Q1', '2014Q4', '后危机-复苏'),
        ('2015-2016', '2015Q1', '2016Q4', '股灾-熔断'),
        ('2017-2019', '2017Q1', '2019Q4', '慢牛-贸易战'),
        ('2020-2021', '2020Q1', '2021Q4', '疫情-反弹'),
        ('2022-2023', '2022Q1', '2023Q4', '封控-下行'),
        ('2024-2026', '2024Q1', '2026Q1', '924反转'),
    ]
    labels = [p[0] for p in phases]
    boom_avgs = []
    for _, s, e, _ in phases:
        sub = boom.loc[s:e]['boom_index'].dropna()
        boom_avgs.append(sub.mean() if not sub.empty else np.nan)

    colors = ['#16a34a' if v > 120 else '#f59e0b' if v > 110 else '#dc2626'
              for v in boom_avgs]
    bars = ax3.bar(range(len(labels)), boom_avgs, color=colors, alpha=0.80)
    ax3.axhline(100, color='#1a1a2e', linewidth=0.8, linestyle='--', alpha=0.7)
    ax3.axhline(120, color='#16a34a', linewidth=0.6, linestyle=':', alpha=0.5)
    for bar, val in zip(bars, boom_avgs):
        if not np.isnan(val):
            ax3.text(bar.get_x() + bar.get_width()/2, val + 0.5,
                     f'{val:.1f}', ha='center', fontsize=7.5, fontweight='bold')
    ax3.set_xticks(range(len(labels)))
    ax3.set_xticklabels(labels, rotation=35, ha='right', fontsize=7.5)
    ax3.set_ylabel('企业景气指数均值', fontsize=9)
    ax3.set_ylim(85, 150)
    ax3.set_title('③ 各历史阶段景气指数均值', fontsize=10, fontweight='bold', pad=6)

    # ④ YoY 趋势
    style_ax(ax4)
    yoy_clean = boom['boom_yoy'].dropna()
    pos_y = yoy_clean.where(yoy_clean >= 0, 0)
    neg_y = yoy_clean.where(yoy_clean < 0, 0)
    ax4.bar(yoy_clean.index, pos_y.values, width=60, color=BOOM_COL, alpha=0.75, label='同比升')
    ax4.bar(yoy_clean.index, neg_y.values, width=60, color='#dc2626', alpha=0.75, label='同比降')
    ax4.axhline(0, color='#1a1a2e', linewidth=0.6)
    ax4.plot(yoy_clean.index, yoy_clean.rolling(4).mean().values,
             color='#7c3aed', linewidth=1.4, label='4 季度滚动均')
    fmt_xaxis(ax4)
    ax4.set_xlim(boom.index[0], boom.index[-1])
    ax4.set_ylabel('同比变化 pp', fontsize=9)
    ax4.set_title('④ 景气指数 YoY 同比（加速/减速趋势）',
                  fontsize=10, fontweight='bold', pad=6)
    ax4.legend(loc='upper right', fontsize=7.5, framealpha=0.92)

    # ⑤ 企业景气 vs CS300 次4季散点
    style_ax(ax5)
    merged = pd.DataFrame({
        'boom': boom['boom_index'],
        'cs_ret_4q': cs_q_ret.shift(-4).reindex(boom.index),
    }).dropna()
    years = merged.index.year
    sc = ax5.scatter(merged['boom'], merged['cs_ret_4q'],
                     c=years, cmap='RdYlGn', s=40, alpha=0.7, edgecolors='white', linewidth=0.5)
    plt.colorbar(sc, ax=ax5, label='年份', pad=0.01)
    # 线性拟合
    z = np.polyfit(merged['boom'].dropna(), merged['cs_ret_4q'].dropna(), 1)
    p = np.poly1d(z)
    x_range = np.linspace(merged['boom'].min(), merged['boom'].max(), 100)
    ax5.plot(x_range, p(x_range), color='#1a1a2e', linewidth=1.2, linestyle='--', alpha=0.6)
    ax5.axhline(0, color='#495057', linewidth=0.6, alpha=0.5)
    ax5.axvline(100, color='#495057', linewidth=0.6, alpha=0.5)
    ax5.set_xlabel('企业景气指数', fontsize=9)
    ax5.set_ylabel('次 4 季 CS300 涨跌 %', fontsize=9)
    ax5.set_title('⑤ 企业景气指数 vs 次 4 季 CS300 涨跌（散点，颜色=年份）',
                  fontsize=10, fontweight='bold', pad=6)

    # 摘要
    boom_cur = boom['boom_index'].iloc[-1]
    conf_cur = boom['confidence_index'].dropna().iloc[-1] if boom['confidence_index'].dropna().any() else None
    conf_str = f'{conf_cur:.1f}（2021Q4起停发）' if conf_cur is None else f'{conf_cur:.1f}'
    summary = (
        f'  数据覆盖：{boom.index[0]:%Y}Q{boom["cal_quarter"].iloc[0]} ~ {boom.index[-1]:%Y}Q{boom["cal_quarter"].iloc[-1]}，共 {len(boom)} 季度  |  '
        f'最新景气指数：{boom_cur:.1f}（2026Q1）  |  企业家信心：{conf_str}\n'
        f'  荣枯线：100  |  历史均值（全样本）景气 {boom["boom_index"].mean():.1f}  |  '
        f'2022Q1-2022Q3 跌破 100（历史罕见，仅 2009Q1 曾短暂破线）'
    )
    fig.text(0.5, 0.03, summary, ha='center', va='top',
             fontsize=8.5, color='#495057',
             bbox=dict(boxstyle='round,pad=0.4', facecolor='#f1f3f5', edgecolor=GRID))
    fig.text(0.5, 0.005,
             '数据来源：国家统计局企业景气调查 via akshare macro_china_enterprise_boom_index | teststock MySQL cn_enterprise_boom_quarterly',
             ha='center', fontsize=7, color='#adb5bd')

    OUT_PATH_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH_LOCAL, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    print(f'已保存：{OUT_PATH_LOCAL}')
    print(f'  最新景气指数（2026Q1）：{boom_cur:.1f}')
    print(f'  2022Q2 最低点：{boom.loc["2022-06-30", "boom_index"]:.1f}（历史极值）')


if __name__ == '__main__':
    main()

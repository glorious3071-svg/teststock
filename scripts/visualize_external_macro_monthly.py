#!/usr/bin/env python3.11
"""外部宏观月度特征数据可视化 — 验证数据正确性

布局（6 子图）:
  ① VIX 时序 + 30 阈值 + 触发月标记
  ② Gold YoY 时序 + 25% 阈值 + 触发月标记
  ③ FED rate 时序 + 4.5% 阈值 + 触发月标记
  ④ US 10Y vs FED rate 双线
  ⑤ SPX 累计 vs CS300 累计（对照）
  ⑥ 三个触发信号的分布直方图

目的：人眼校验数据是否合理（无异常值、时间对齐对、关键历史事件可见）
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

fm.FontProperties(fname='/System/Library/Fonts/PingFang.ttc')
plt.rcParams['font.family'] = 'PingFang HK'

OUT_PNG = ROOT / 'docs' / 'assets' / 'external_macro_monthly.png'


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_data():
    conn = db()
    ext = pd.read_sql(
        "SELECT * FROM external_macro_monthly ORDER BY month",
        conn,
    )
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    ext['date'] = pd.to_datetime(ext['month'], format='%Y%m')
    ext = ext.set_index('date').sort_index()
    for c in ('vix_30d_avg', 'vix_30d_max', 'gold_close', 'gold_yoy_pct',
              'fed_rate_level', 'us10y_yield', 'spx_close', 'spx_yoy_pct'):
        ext[c] = ext[c].astype(float)
    cs['close'] = cs['close'].astype(float)
    return ext, cs


# ── 关键历史事件 ────────────────────────────────────────────
EVENTS = [
    ('2000-03', '互联网泡沫顶', 'red'),
    ('2008-09', '雷曼破产', 'red'),
    ('2011-08', '美债降级', 'red'),
    ('2014-12', 'QE3 结束', 'orange'),
    ('2018-04', '中美贸易战', 'red'),
    ('2020-03', 'COVID 冲击', 'red'),
    ('2022-02', '俄乌战争', 'red'),
    ('2024-09', 'FED 首次降息', 'green'),
    ('2025-04', '关税战 VIX 52', 'red'),
]


def style_ax(ax):
    ax.set_facecolor('#f8f9fa')
    ax.tick_params(colors='#495057', labelsize=8)
    for s in ax.spines.values():
        s.set_color('#dee2e6')
    ax.grid(color='#dee2e6', linewidth=0.6, alpha=0.7)


def fmt_xaxis(ax, year_step=2):
    ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))


def mark_events(ax, idx, y_top):
    for date_str, label, color in EVENTS:
        dt = pd.Timestamp(date_str)
        if dt < idx.min() or dt > idx.max():
            continue
        c = '#dc2626' if color == 'red' else ('#16a34a' if color == 'green' else '#f59e0b')
        ax.axvline(dt, color=c, alpha=0.25, linewidth=0.8, linestyle='--')
        ax.text(dt, y_top, label, rotation=90, fontsize=6, color=c, alpha=0.8,
                va='top', ha='right', fontweight='bold')


def main():
    ext, cs = load_data()

    fig = plt.figure(figsize=(18, 18))
    fig.patch.set_facecolor('white')
    fig.suptitle('外部宏观月度特征（external_macro_monthly）— 数据校验',
                 fontsize=15, fontweight='bold', y=0.99)

    # 6 子图 3x2
    axes = [fig.add_axes([0.06 + (i % 2) * 0.48, 0.71 - (i // 2) * 0.31, 0.42, 0.23])
            for i in range(6)]
    ax1, ax2, ax3, ax4, ax5, ax6 = axes

    # ① VIX 时序
    style_ax(ax1)
    ax1.plot(ext.index, ext['vix_30d_avg'], color='#dc2626', linewidth=1.0, label='VIX 月均')
    ax1.fill_between(ext.index, ext['vix_30d_min'], ext['vix_30d_max'],
                      color='#dc2626', alpha=0.15, label='VIX 月内范围')
    ax1.axhline(30, color='#16a34a', linewidth=1.0, linestyle='--', label='v11 阈值 30')
    trigs = ext[ext['trig_vix_30plus'] == 1]
    ax1.scatter(trigs.index, trigs['vix_30d_avg'], color='#16a34a', s=30, zorder=5,
                edgecolor='white', linewidth=0.6, label=f'触发 {len(trigs)} 月')
    mark_events(ax1, ext.index, 85)
    fmt_xaxis(ax1, 3)
    ax1.set_ylim(5, 90)
    ax1.set_ylabel('VIX', fontsize=9)
    ax1.set_title(f'① VIX 恐慌指数（1990-2026，{len(trigs)} 月触发 >30）',
                  fontsize=10, fontweight='bold', pad=6)
    ax1.legend(loc='upper left', fontsize=7, framealpha=0.92)

    # ② Gold YoY
    style_ax(ax2)
    gd = ext.dropna(subset=['gold_yoy_pct'])
    ax2.bar(gd.index, gd['gold_yoy_pct'],
            width=22,
            color=['#dc2626' if v > 25 else '#9ca3af' for v in gd['gold_yoy_pct']],
            alpha=0.75)
    ax2.axhline(25, color='#16a34a', linewidth=1.0, linestyle='--', label='v11 阈值 25%')
    ax2.axhline(0, color='#1a1a2e', linewidth=0.6)
    trigs = ext[ext['trig_gold_yoy25'] == 1]
    mark_events(ax2, ext.index, 75)
    fmt_xaxis(ax2)
    ax2.set_xlim(ext.dropna(subset=['gold_yoy_pct']).index.min(), ext.index.max())
    ax2.set_ylim(-30, 80)
    ax2.set_ylabel('黄金 YoY %', fontsize=9)
    ax2.set_title(f'② 黄金 YoY（{len(trigs)} 月触发 >25%）',
                  fontsize=10, fontweight='bold', pad=6)
    ax2.legend(loc='upper left', fontsize=7, framealpha=0.92)

    # ③ FED rate
    style_ax(ax3)
    fd = ext.dropna(subset=['fed_rate_level'])
    ax3.plot(fd.index, fd['fed_rate_level'], color='#1d4ed8', linewidth=1.2, label='FED 利率水平')
    ax3.fill_between(fd.index, 0, fd['fed_rate_level'], color='#1d4ed8', alpha=0.10)
    ax3.axhline(4.5, color='#16a34a', linewidth=1.0, linestyle='--', label='v11 阈值 4.5%')
    trigs = ext[ext['trig_fed_45plus'] == 1]
    mark_events(ax3, ext.index, 9)
    fmt_xaxis(ax3, 3)
    ax3.set_xlim(fd.index.min(), fd.index.max())
    ax3.set_ylim(0, 10)
    ax3.set_ylabel('FED rate %', fontsize=9)
    ax3.set_title(f'③ FED 利率（{len(trigs)} 月 ≥4.5%）',
                  fontsize=10, fontweight='bold', pad=6)
    ax3.legend(loc='upper right', fontsize=7, framealpha=0.92)

    # ④ US 10Y vs FED rate
    style_ax(ax4)
    sub = ext.dropna(subset=['us10y_yield'])
    ax4.plot(sub.index, sub['us10y_yield'], color='#7c3aed', linewidth=1.2, label='US 10Y 名义')
    ax4.plot(fd.index, fd['fed_rate_level'], color='#1d4ed8', linewidth=1.0,
             alpha=0.8, label='FED 利率')
    ax4.fill_between(sub.index, sub['us10y_yield'], fd['fed_rate_level'].reindex(sub.index),
                      where=(sub['us10y_yield'] - fd['fed_rate_level'].reindex(sub.index)) > 0,
                      color='#dc2626', alpha=0.20, label='正期限利差')
    ax4.fill_between(sub.index, sub['us10y_yield'], fd['fed_rate_level'].reindex(sub.index),
                      where=(sub['us10y_yield'] - fd['fed_rate_level'].reindex(sub.index)) <= 0,
                      color='#16a34a', alpha=0.20, label='倒挂（衰退信号）')
    fmt_xaxis(ax4)
    ax4.set_xlim(sub.index.min(), sub.index.max())
    ax4.set_ylim(0, 8)
    ax4.set_ylabel('利率 %', fontsize=9)
    ax4.set_title('④ US 10Y vs FED rate（看期限利差/倒挂）',
                  fontsize=10, fontweight='bold', pad=6)
    ax4.legend(loc='upper right', fontsize=7, framealpha=0.92)

    # ⑤ SPX 累计 vs CS300 累计
    style_ax(ax5)
    spx = ext.dropna(subset=['spx_close']).copy()
    if not spx.empty:
        spx_norm = spx['spx_close'] / spx['spx_close'].iloc[0] * 100
        ax5.plot(spx.index, spx_norm, color='#1d4ed8', linewidth=1.4, label='SPX (rebase 100)')
    cs_m = cs['close'].resample('ME').last()
    cs_aligned = cs_m[cs_m.index >= spx.index.min()] if not spx.empty else cs_m
    if not cs_aligned.empty:
        cs_norm = cs_aligned / cs_aligned.iloc[0] * 100
        ax5.plot(cs_aligned.index, cs_norm, color='#dc2626', linewidth=1.4, label='CS300 (rebase 100)')
    fmt_xaxis(ax5, 3)
    ax5.set_ylabel('归一化 100', fontsize=9)
    ax5.set_title('⑤ SPX vs CS300 累计净值',
                  fontsize=10, fontweight='bold', pad=6)
    ax5.legend(loc='upper left', fontsize=7, framealpha=0.92)

    # ⑥ 三个触发信号的分布直方图
    style_ax(ax6)
    rows = []
    rows.append(('VIX > 30', int(ext['trig_vix_30plus'].sum()),
                 ext.loc[ext['trig_vix_30plus']==1, 'vix_30d_avg'].mean() if ext['trig_vix_30plus'].sum() else 0))
    rows.append(('Gold YoY > 25%', int(ext['trig_gold_yoy25'].sum()),
                 ext.loc[ext['trig_gold_yoy25']==1, 'gold_yoy_pct'].mean() if ext['trig_gold_yoy25'].sum() else 0))
    rows.append(('FED ≥ 4.5%', int(ext['trig_fed_45plus'].sum()),
                 ext.loc[ext['trig_fed_45plus']==1, 'fed_rate_level'].mean() if ext['trig_fed_45plus'].sum() else 0))
    labels = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    means = [r[2] for r in rows]
    colors_b = ['#dc2626', '#f59e0b', '#1d4ed8']
    bars = ax6.bar(labels, counts, color=colors_b, alpha=0.8)
    for bar, c, m in zip(bars, counts, means):
        ax6.text(bar.get_x() + bar.get_width()/2, c + 3,
                  f'{c} 月\n触发值均: {m:.1f}',
                  ha='center', fontsize=8, fontweight='bold')
    ax6.set_ylabel('触发月数', fontsize=9)
    ax6.set_title('⑥ v11 三规则触发汇总（全样本 1990-2026, 438 月）',
                  fontsize=10, fontweight='bold', pad=6)
    ax6.set_ylim(0, max(counts) * 1.25 if counts else 100)

    fig.text(0.5, 0.04,
             f'数据源校验：vix={ext["vix_30d_avg"].notna().sum()} 月  '
             f'gold={ext["gold_yoy_pct"].notna().sum()} 月  '
             f'fed={ext["fed_rate_level"].notna().sum()} 月  '
             f'us10y={ext["us10y_yield"].notna().sum()} 月  '
             f'spx={ext["spx_close"].notna().sum()} 月',
             ha='center', fontsize=9, color='#495057',
             bbox=dict(boxstyle='round,pad=0.4', facecolor='#f1f3f5', edgecolor='#dee2e6'))

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PNG, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    print(f'已保存：{OUT_PNG}')


if __name__ == '__main__':
    main()

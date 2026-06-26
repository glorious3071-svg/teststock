#!/usr/bin/env python3.11
"""数据探索：deposit_1y_rate 两段拼接特征（2002-2026）

四块布局：
  ① 主时序：两段拼接后的 deposit_1y + 评分卡阈值带 + 重要事件
  ② 分布直方图：两段口径分别的频次分布（凸显系统性偏差）
  ③ 双源对照：cn_deposit_rate 阶梯 vs SHIBOR 1Y 日频/30d 均值，
     高亮 2015-10-24 之后 SHIBOR 相对央行末值（1.50%）的溢价
  ④ 评分时序：每月 deposit_1y 触发的评分卡分数 (+1 / 0 / -1)
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_PNG = ROOT / "docs" / "assets" / "cn_deposit_1y_explore.png"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

# ── 常量 ────────────────────────────────────────────────
FREEZE = pd.Timestamp("2015-10-24")  # 央行最后一次基准利率调整
START  = pd.Timestamp("2002-01-01")
END    = pd.Timestamp("2026-06-30")
SHIBOR_WINDOW = "30D"
PROXY_FROZEN_RATE = 1.50             # 2015-10-24 后央行事实基准
TH_RISK_HIGH = 3.5
TH_OPPORTUNITY_LOW = 2.5

# ── 数据加载 ────────────────────────────────────────────
load_dotenv(ROOT / ".env")
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "teststock"),
    password=os.getenv("MYSQL_PASSWORD", "teststock"),
    database=os.getenv("MYSQL_DATABASE", "teststock"),
)

events = pd.read_sql(
    "SELECT effective_date, rate_after_pct, rate_change_pp, direction "
    "FROM cn_deposit_rate ORDER BY effective_date",
    conn, parse_dates=["effective_date"],
)
events = events.set_index("effective_date")

shibor = pd.read_sql(
    "SELECT trade_date, rate_1y FROM shibor_daily "
    "WHERE rate_1y IS NOT NULL ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")["rate_1y"].astype(float)
conn.close()

# 央行基准事件 → 日频 step（ffill）
daily_idx = pd.date_range(START, END, freq="D")
pboc_step = events["rate_after_pct"].astype(float).reindex(daily_idx, method="ffill")

# SHIBOR 1Y 30 日均值
shibor_30d = shibor.rolling(SHIBOR_WINDOW, min_periods=5).mean().reindex(daily_idx).ffill()

# 两段拼接
combined = pboc_step.copy()
combined.loc[combined.index > FREEZE] = shibor_30d.loc[shibor_30d.index > FREEZE]
combined = combined.dropna()

# 月末抽样用于评分
monthly = combined.resample("M").last().dropna()
def score(rate: float) -> int:
    if rate > TH_RISK_HIGH:
        return 1
    if rate < TH_OPPORTUNITY_LOW:
        return -1
    return 0
monthly_score = monthly.apply(score)

# ── 绘图 ────────────────────────────────────────────────
fig = plt.figure(figsize=(18, 14))
fig.patch.set_facecolor("white")
fig.suptitle(
    "数据探索  1Y 定存利率特征（央行基准 + SHIBOR 1Y 30d 代理，2002–2026）",
    fontsize=15, fontweight="bold", color="#1a1a2e", y=0.985,
)

GRID = "#dee2e6"
BG = "#f8f9fa"
COL_PBOC = "#7c3aed"        # 紫 — 央行基准
COL_PROXY = "#0891b2"       # 青 — SHIBOR 代理
COL_SHIBOR_RAW = "#94a3b8"  # 灰 — SHIBOR 日频原始
COL_RISK = "#dc2626"
COL_OPP = "#16a34a"
COL_NEU = "#9ca3af"

ax1      = fig.add_axes([0.05, 0.66, 0.62, 0.25])  # 主时序
ax1_dist = fig.add_axes([0.71, 0.66, 0.25, 0.25])  # 分布
ax2      = fig.add_axes([0.05, 0.36, 0.91, 0.22])  # 双源对照
ax3      = fig.add_axes([0.05, 0.10, 0.91, 0.18])  # 评分时序

events_marks = [
    ("2007-12-21", "末次加息 4.14%", "red"),
    ("2008-11-27", "雷曼后大降息", "green"),
    ("2010-10-20", "重启加息", "red"),
    ("2014-11-22", "重启降息", "green"),
    ("2015-10-24", "基准冻结 1.50%", "black"),
    ("2017-12-31", "金融去杠杆顶", "red"),
    ("2020-02-04", "疫情低点", "green"),
    ("2024-09-24", "924政策", "green"),
]


def style(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)


def mark_events(ax, y_top):
    for d_str, lab, col in events_marks:
        dt = pd.Timestamp(d_str)
        if dt < START or dt > END:
            continue
        ec = {"red": COL_RISK, "green": COL_OPP, "black": "#1a1a2e"}[col]
        ax.axvline(dt, color=ec, alpha=0.30, linewidth=0.8, linestyle="--")
        ax.text(dt, y_top, lab, rotation=90, fontsize=6.5,
                color=ec, alpha=0.85, va="top", ha="right")


def fmt_x(ax):
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(START, END)


# ── ① 主时序 ────────────────────────────────────────────
style(ax1)
pre = combined[combined.index <= FREEZE]
post = combined[combined.index > FREEZE]
ax1.plot(pre.index, pre.values, color=COL_PBOC, linewidth=1.6,
         label="央行 1Y 基准 (≤2015-10-24)")
ax1.plot(post.index, post.values, color=COL_PROXY, linewidth=1.4,
         label="SHIBOR 1Y 30d 均 (>2015-10-24，代理)")

# 评分卡阈值带
ax1.axhspan(TH_RISK_HIGH, 5.5, alpha=0.08, color=COL_RISK)
ax1.axhspan(0.5, TH_OPPORTUNITY_LOW, alpha=0.08, color=COL_OPP)
ax1.axhline(TH_RISK_HIGH, color=COL_RISK, linewidth=0.9, linestyle=":")
ax1.axhline(TH_OPPORTUNITY_LOW, color=COL_OPP, linewidth=0.9, linestyle=":")
ax1.text(START, TH_RISK_HIGH + 0.05, f"风险阈值 {TH_RISK_HIGH:.1f}%",
         color=COL_RISK, fontsize=7, alpha=0.85)
ax1.text(START, TH_OPPORTUNITY_LOW - 0.18, f"机会阈值 {TH_OPPORTUNITY_LOW:.1f}%",
         color=COL_OPP, fontsize=7, alpha=0.85)

# 冻结点
ax1.axvline(FREEZE, color="#1a1a2e", linewidth=1.0, alpha=0.6)
ax1.annotate(
    f"  央行基准冻结于 1.50%\n  之后改用 SHIBOR 1Y 30d 代理",
    xy=(FREEZE, 4.2),
    xytext=(15, 0), textcoords="offset points",
    color="#1a1a2e", fontsize=7.5, fontweight="bold",
    va="center",
)

mark_events(ax1, 5.4)
fmt_x(ax1)
ax1.set_ylim(0.5, 5.5)
ax1.set_ylabel("1Y 利率 (%)", fontsize=9, color="#1a1a2e")
ax1.set_title("① 两段拼接后的 deposit_1y_rate（评分卡实际输入）",
              fontsize=10, color="#1a1a2e", fontweight="bold", pad=6)
ax1.legend(loc="upper right", fontsize=7.5, framealpha=0.9)

# ── ① 右侧分布直方图 ────────────────────────────────────
style(ax1_dist)
bins = np.arange(0.5, 5.5, 0.15)
ax1_dist.hist(pre.values, bins=bins, alpha=0.65, color=COL_PBOC,
              orientation="horizontal", label=f"基准段 n={len(pre):,}")
ax1_dist.hist(post.values, bins=bins, alpha=0.65, color=COL_PROXY,
              orientation="horizontal", label=f"代理段 n={len(post):,}")
ax1_dist.axhline(TH_RISK_HIGH, color=COL_RISK, linewidth=0.9, linestyle=":")
ax1_dist.axhline(TH_OPPORTUNITY_LOW, color=COL_OPP, linewidth=0.9, linestyle=":")
# 中位数
ax1_dist.axhline(pre.median(), color=COL_PBOC, linewidth=1.2,
                 linestyle="-", alpha=0.85)
ax1_dist.axhline(post.median(), color=COL_PROXY, linewidth=1.2,
                 linestyle="-", alpha=0.85)
ax1_dist.text(ax1_dist.get_xlim()[1] * 0.95, pre.median(),
              f" 基准中位 {pre.median():.2f}%", color=COL_PBOC,
              fontsize=7, ha="right", va="bottom", fontweight="bold")
ax1_dist.text(ax1_dist.get_xlim()[1] * 0.95, post.median(),
              f" 代理中位 {post.median():.2f}%", color=COL_PROXY,
              fontsize=7, ha="right", va="top", fontweight="bold")
ax1_dist.set_ylim(0.5, 5.5)
ax1_dist.set_title("② 两段口径分布（频次 vs 利率）",
                   fontsize=10, color="#1a1a2e", fontweight="bold", pad=6)
ax1_dist.set_xlabel("天数", fontsize=8, color="#495057")
ax1_dist.tick_params(axis="y", labelleft=False)
ax1_dist.legend(loc="upper right", fontsize=7, framealpha=0.9)

# ── ② 双源对照 ──────────────────────────────────────────
style(ax2)
ax2.plot(pboc_step.index, pboc_step.values, color=COL_PBOC,
         linewidth=1.6, label="cn_deposit_rate (央行基准, step)")
ax2.plot(shibor.index, shibor.values, color=COL_SHIBOR_RAW,
         linewidth=0.6, alpha=0.7, label="SHIBOR 1Y 日频 (原始)")
ax2.plot(shibor_30d.index, shibor_30d.values, color=COL_PROXY,
         linewidth=1.4, label="SHIBOR 1Y 30d 均")

# 高亮 2015-10-24 后 SHIBOR vs 1.50% 的溢价
post_idx = shibor_30d.index > FREEZE
ax2.fill_between(
    shibor_30d.index[post_idx],
    shibor_30d.values[post_idx],
    PROXY_FROZEN_RATE,
    where=shibor_30d.values[post_idx] > PROXY_FROZEN_RATE,
    color="#fbbf24", alpha=0.20,
    label=f"SHIBOR 相对央行末值 1.50% 的溢价",
)
ax2.axhline(PROXY_FROZEN_RATE, color="#92400e", linewidth=0.9,
            linestyle="--", alpha=0.7)
ax2.text(pd.Timestamp("2016-06-01"), PROXY_FROZEN_RATE - 0.18,
         "央行冻结 1.50%", color="#92400e", fontsize=7, alpha=0.85)

ax2.axvline(FREEZE, color="#1a1a2e", linewidth=1.0, alpha=0.5)
mark_events(ax2, 6.5)
fmt_x(ax2)
ax2.set_ylim(1, 6.7)
ax2.set_ylabel("利率 (%)", fontsize=9, color="#1a1a2e")
ax2.set_title("③ 双源对照：央行基准 vs SHIBOR 1Y（同业批发系统性高于零售存款 70-150 bp）",
              fontsize=10, color="#1a1a2e", fontweight="bold", pad=6)
ax2.legend(loc="upper right", fontsize=7.5, ncol=2, framealpha=0.9)

# ── ③ 评分时序 ──────────────────────────────────────────
style(ax3)
# 月度 score 转面积
colors_map = {1: COL_RISK, 0: COL_NEU, -1: COL_OPP}
for s, c in colors_map.items():
    mask = monthly_score == s
    if mask.any():
        ax3.fill_between(monthly_score.index, 0, monthly_score,
                         where=monthly_score == s, color=c, alpha=0.7,
                         step="mid")

# 实际 deposit 利率（参考线）
ax3b = ax3.twinx()
ax3b.set_facecolor("none")
ax3b.plot(combined.index, combined.values, color="#1a1a2e",
          linewidth=0.7, alpha=0.6)
ax3b.set_ylabel("deposit_1y (%)", fontsize=8, color="#495057")
ax3b.set_ylim(0.5, 5.5)
ax3b.tick_params(axis="y", labelsize=7, colors="#495057")

ax3.axhline(0, color="#495057", linewidth=0.5)
ax3.set_ylim(-1.4, 1.4)
ax3.set_yticks([-1, 0, 1])
ax3.set_yticklabels(["−1 机会", "0 中性", "+1 风险"], fontsize=8)
mark_events(ax3, 1.3)
fmt_x(ax3)

# 统计文字
n_risk = (monthly_score == 1).sum()
n_opp = (monthly_score == -1).sum()
n_neu = (monthly_score == 0).sum()
n_total = len(monthly_score)
stat = (f"月度命中：风险+1 = {n_risk} 月 ({n_risk/n_total:.1%})  ·  "
        f"中性 0 = {n_neu} 月 ({n_neu/n_total:.1%})  ·  "
        f"机会-1 = {n_opp} 月 ({n_opp/n_total:.1%})  ·  "
        f"全样本 {n_total} 月（{monthly_score.index[0]:%Y-%m} ~ {monthly_score.index[-1]:%Y-%m}）")
ax3.set_title(f"④ 评分卡视角：deposit_1y 月度命中（{stat}）",
              fontsize=10, color="#1a1a2e", fontweight="bold", pad=6)

# 图例
patches = [
    mpatches.Patch(color=COL_RISK, alpha=0.7, label="风险+1 (>3.5%)"),
    mpatches.Patch(color=COL_NEU, alpha=0.7, label="中性 0"),
    mpatches.Patch(color=COL_OPP, alpha=0.7, label="机会-1 (<2.5%)"),
]
ax3.legend(handles=patches, loc="lower right", fontsize=7.5, framealpha=0.9)

fig.text(0.5, 0.045,
         "数据来源：cn_deposit_rate (PBoC 历史公告 seed) + shibor_daily (Tushare)  ·  "
         "拼接逻辑：scripts/check_liquidity_features.py::deposit_1y_rate",
         ha="center", fontsize=7, color="#adb5bd")

OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT_PNG}")

# ── 文字探索摘要 ────────────────────────────────────────
print("\n" + "=" * 70)
print("数据探索摘要")
print("=" * 70)
print(f"\n样本总览：{len(combined):,} 天，{START.date()} ~ {END.date()}")
print(f"  · 央行基准段：{len(pre):,} 天 (2002-01-01 ~ 2015-10-24)")
print(f"  · SHIBOR 代理段：{len(post):,} 天 (2015-10-25 ~ {END.date()})")

print(f"\n两段口径统计（mean / median / std）：")
print(f"  · 央行基准段：{pre.mean():.2f}% / {pre.median():.2f}% / {pre.std():.2f}")
print(f"  · SHIBOR 代理段：{post.mean():.2f}% / {post.median():.2f}% / {post.std():.2f}")

# 切换点系统性偏差
last_pboc = pre.iloc[-1]
first_proxy = post.iloc[0]
print(f"\n切换点系统性偏差（2015-10-24）：")
print(f"  · 央行末值：{last_pboc:.2f}%")
print(f"  · SHIBOR 30d 首值：{first_proxy:.2f}%")
print(f"  · 跳变：{first_proxy - last_pboc:+.2f} pp（口径差异，非真实利率变动）")

# 评分卡命中
print(f"\n评分卡月度命中（{n_total} 个月）：")
print(f"  · 风险+1：{n_risk} 月 ({n_risk/n_total:.1%})")
print(f"  · 中性 0：{n_neu} 月 ({n_neu/n_total:.1%})")
print(f"  · 机会-1：{n_opp} 月 ({n_opp/n_total:.1%})")

# 当前状态
print(f"\n当前快照：")
print(f"  · 最新 deposit_1y_rate: {combined.iloc[-1]:.2f}% @ {combined.index[-1].date()}")
print(f"  · 评分: {score(combined.iloc[-1]):+d}")

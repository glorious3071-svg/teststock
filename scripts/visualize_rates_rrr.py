#!/usr/bin/env python3.11
"""可视化：央行加息 / 加准 / 定存基准利率（2005-2026）

三联图：
  ① 利率走势：SHIBOR 3M（主源）/ CHIBOR 3M（pre-SHIBOR 兜底）/ 1Y 定存基准 / LPR 1Y
  ② 大型机构存款准备金率（阶梯线）+ 每次调整事件柱形
  ③ 评分卡视角：过去 12 月累计加息（bp）& 累计加准（pp），叠加阈值带
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
OUT_PNG = ROOT / "docs" / "assets" / "cn_rate_rrr_history.png"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

# ── 数据加载 ────────────────────────────────────────────────
load_dotenv(ROOT / ".env")
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "teststock"),
    password=os.getenv("MYSQL_PASSWORD", "teststock"),
    database=os.getenv("MYSQL_DATABASE", "teststock"),
)

START_DATE = "2005-01-01"

shibor = pd.read_sql(
    "SELECT trade_date, rate_3m FROM shibor_daily "
    "WHERE trade_date >= %s AND rate_3m IS NOT NULL ORDER BY trade_date",
    conn, params=(START_DATE,), parse_dates=["trade_date"],
).set_index("trade_date")

chibor = pd.read_sql(
    "SELECT trade_date, rate_3m FROM chibor_daily "
    "WHERE trade_date >= %s AND rate_3m IS NOT NULL ORDER BY trade_date",
    conn, params=(START_DATE,), parse_dates=["trade_date"],
).set_index("trade_date")

lpr = pd.read_sql(
    "SELECT trade_date, lpr_1y FROM lpr_daily "
    "WHERE trade_date >= %s ORDER BY trade_date",
    conn, params=(START_DATE,), parse_dates=["trade_date"],
).set_index("trade_date")

deposit = pd.read_sql(
    "SELECT effective_date, rate_after_pct FROM cn_deposit_rate "
    "ORDER BY effective_date",
    conn, parse_dates=["effective_date"],
).set_index("effective_date")

rrr = pd.read_sql(
    "SELECT effective_date, rrr_change_pp, rrr_after_pp, direction "
    "FROM cn_rrr_changes WHERE inst_type IN ('large','all') "
    "ORDER BY effective_date",
    conn, parse_dates=["effective_date"],
).set_index("effective_date")

conn.close()

# 把存款基准向前补齐到 2026-06，便于阶梯线绘制
DEP_END = pd.Timestamp("2026-06-30")
if deposit.index.max() < DEP_END:
    deposit.loc[DEP_END] = deposit["rate_after_pct"].iloc[-1]
RRR_END = DEP_END
if rrr.index.max() < RRR_END:
    rrr.loc[RRR_END] = [0.0, rrr["rrr_after_pp"].dropna().iloc[-1], "hold"]

# ── 评分卡视角：滚动 12 月累计加息 / 加准 ────────────────
def rolling_rate_cum_bp(s: pd.Series) -> pd.Series:
    """每日：rate − rate(t−365d)，单位 bp。"""
    aligned = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="D")).ffill()
    yr_ago = aligned.shift(365)
    return ((aligned - yr_ago) * 100).dropna()


# 拼接 CHIBOR + SHIBOR 用于过去 1 年差分
rate_combined = pd.concat([chibor["rate_3m"].rename("rate_3m"), shibor["rate_3m"]])
rate_combined = rate_combined[~rate_combined.index.duplicated(keep="last")].sort_index()
rate_cum_bp = rolling_rate_cum_bp(rate_combined)

# 滚动 12 月累计 RRR：事件求和
def rolling_rrr_cum_pp(events: pd.Series) -> pd.Series:
    daily = pd.Series(0.0, index=pd.date_range(events.index.min(), events.index.max(), freq="D"))
    for dt, val in events.items():
        if dt in daily.index:
            daily.loc[dt] += val
    return daily.rolling("365D").sum()


rrr_events = rrr["rrr_change_pp"].dropna()
rrr_events = rrr_events[rrr_events != 0]   # 排除虚拟终点
rrr_cum_pp = rolling_rrr_cum_pp(rrr_events)

# ── 重要事件 ────────────────────────────────────────────
events = [
    ("2007-10-16", "6124点顶部", "red"),
    ("2008-09-15", "雷曼破产", "red"),
    ("2008-11-09", "四万亿", "green"),
    ("2011-07-07", "末次加息", "red"),
    ("2015-06-12", "5178点", "red"),
    ("2015-08-26", "降准降息", "green"),
    ("2018-12-28", "2440点", "green"),
    ("2020-02-04", "疫情低点", "green"),
    ("2024-09-24", "924政策", "green"),
]

# ── 绘图 ────────────────────────────────────────────────
fig = plt.figure(figsize=(18, 15))
fig.patch.set_facecolor("white")
fig.suptitle(
    "中国央行 利率 · 准备金 · 定存基准  历史走势（2005–2026）",
    fontsize=16, fontweight="bold", color="#1a1a2e", y=0.99,
)

GRID = "#dee2e6"
BG = "#f8f9fa"
COL_SHIBOR  = "#2563eb"   # 蓝
COL_CHIBOR  = "#0ea5e9"   # 浅蓝
COL_DEPOSIT = "#7c3aed"   # 紫
COL_LPR     = "#0f766e"   # 深绿
COL_RRR     = "#0891b2"   # 青
COL_HIKE    = "#dc2626"   # 红（加准/加息）
COL_CUT     = "#16a34a"   # 绿（降准/降息）

ax1 = fig.add_axes([0.06, 0.69, 0.90, 0.24])  # 利率
ax2 = fig.add_axes([0.06, 0.40, 0.90, 0.24])  # RRR
ax3 = fig.add_axes([0.06, 0.10, 0.90, 0.24])  # 评分卡累计


def style(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)


def mark_events(ax, y_top):
    for date_str, label, color in events:
        dt = pd.Timestamp(date_str)
        ec = "#dc2626" if color == "red" else "#16a34a"
        ax.axvline(dt, color=ec, alpha=0.25, linewidth=0.8, linestyle="--")
        ax.text(dt, y_top, label, rotation=90, fontsize=6,
                color=ec, alpha=0.8, va="top", ha="right")


def fmt_x(ax):
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(pd.Timestamp(START_DATE), pd.Timestamp("2026-12-31"))


# ── ① 利率走势 ─────────────────────────────────────────
style(ax1)
ax1.plot(chibor.index, chibor["rate_3m"], color=COL_CHIBOR,
         linewidth=0.9, alpha=0.85, label="CHIBOR 3M")
ax1.plot(shibor.index, shibor["rate_3m"], color=COL_SHIBOR,
         linewidth=1.0, label="SHIBOR 3M")
ax1.step(deposit.index, deposit["rate_after_pct"], where="post",
         color=COL_DEPOSIT, linewidth=1.6, label="1Y 定存基准")
ax1.step(lpr.index, lpr["lpr_1y"], where="post",
         color=COL_LPR, linewidth=1.4, label="LPR 1Y")

# 重要利率阈值线
for v, lab, c in [(2.0, "宽松区 2%", "#16a34a"),
                  (4.0, "偏紧 4%",  "#ea580c"),
                  (5.0, "紧缩 5%",  "#dc2626")]:
    ax1.axhline(v, color=c, alpha=0.35, linewidth=0.8, linestyle=":")
    ax1.text(pd.Timestamp(START_DATE), v + 0.06, lab,
             color=c, fontsize=6.5, alpha=0.8)

mark_events(ax1, ax1.get_ylim()[1] if False else 7.5)
fmt_x(ax1)
ax1.set_ylabel("利率 (%)", fontsize=9, color="#1a1a2e")
ax1.set_title("① 货币市场利率 vs 政策基准利率", fontsize=10,
              color="#1a1a2e", pad=6, fontweight="bold")
ax1.legend(loc="upper right", fontsize=7.5, ncol=4, framealpha=0.9)

# ── ② RRR 阶梯线 + 调整事件柱 ────────────────────────
style(ax2)
ax2.step(rrr.index, rrr["rrr_after_pp"], where="post",
         color=COL_RRR, linewidth=1.5, label="大型机构 RRR")
ax2.fill_between(rrr.index, rrr["rrr_after_pp"], step="post",
                 alpha=0.10, color=COL_RRR)

# 调整事件柱（双 Y 轴）
ax2b = ax2.twinx()
ax2b.set_facecolor("none")
for dt, ev in rrr.iterrows():
    delta = ev["rrr_change_pp"]
    if pd.isna(delta) or delta == 0:
        continue
    c = COL_HIKE if delta > 0 else COL_CUT
    ax2b.bar(dt, delta, width=30, color=c, alpha=0.75, edgecolor="none")
ax2b.axhline(0, color="#495057", linewidth=0.5)
ax2b.set_ylabel("单次调整幅度 (pp)", fontsize=9, color="#495057")
ax2b.tick_params(axis="y", labelsize=7, colors="#495057")
ax2b.set_ylim(-1.6, 1.6)

mark_events(ax2, 21.5)
fmt_x(ax2)
ax2.set_ylabel("RRR 水平 (%)", fontsize=9, color=COL_RRR)
ax2.set_ylim(5, 22)
ax2.set_title("② 大型存款类机构准备金率：阶梯水平 + 历次调整事件 "
              "（红=加准 / 绿=降准）", fontsize=10,
              color="#1a1a2e", pad=6, fontweight="bold")

# ── ③ 评分卡：滚动 12 月累计加息 / 累计加准 ───────────
style(ax3)
# 累计加息 bp（左 Y）
ax3.fill_between(rate_cum_bp.index, rate_cum_bp.values, 0,
                 where=rate_cum_bp.values >= 0,
                 alpha=0.20, color=COL_HIKE)
ax3.fill_between(rate_cum_bp.index, rate_cum_bp.values, 0,
                 where=rate_cum_bp.values < 0,
                 alpha=0.20, color=COL_CUT)
ax3.plot(rate_cum_bp.index, rate_cum_bp.values,
         color="#1a1a2e", linewidth=1.0, label="累计加息 12M (bp，左)")
ax3.axhline(0, color="#495057", linewidth=0.5)
# 评分卡阈值
for v, lab, c in [(150, "+150 风险+2", "#dc2626"),
                  (100, "+100 风险+1", "#ea580c"),
                  (-100, "-100 机会-2", "#16a34a")]:
    ax3.axhline(v, color=c, alpha=0.45, linewidth=0.8, linestyle=":")
    ax3.text(pd.Timestamp("2006-01-01"), v + 6, lab,
             color=c, fontsize=6.5, alpha=0.85)

ax3.set_ylabel("累计加息 12M (bp)", fontsize=9, color="#1a1a2e")
ax3.set_ylim(-450, 450)

# 累计加准 pp（右 Y）
ax3b = ax3.twinx()
ax3b.set_facecolor("none")
ax3b.plot(rrr_cum_pp.index, rrr_cum_pp.values,
          color=COL_RRR, linewidth=1.4, label="累计加准 12M (pp，右)")
for v, lab, c in [(3, "+3 风险+1", "#dc2626"),
                  (-1, "-1 机会-1", "#16a34a")]:
    ax3b.axhline(v, color=c, alpha=0.35, linewidth=0.7, linestyle="-.")
ax3b.set_ylabel("累计加准 12M (pp)", fontsize=9, color=COL_RRR)
ax3b.tick_params(axis="y", labelsize=7, colors=COL_RRR)
ax3b.set_ylim(-4, 6)

mark_events(ax3, 420)
fmt_x(ax3)
ax3.set_title("③ 评分卡视角：滚动 12 月累计加息 / 累计加准（V5.0 流动性维度输入）",
              fontsize=10, color="#1a1a2e", pad=6, fontweight="bold")

# 合并图例
h1, l1 = ax3.get_legend_handles_labels()
h2, l2 = ax3b.get_legend_handles_labels()
ax3.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=7.5, framealpha=0.9)

# 底部数据来源
fig.text(0.5, 0.045,
         "数据来源：shibor_daily / chibor_daily(akshare) / lpr_daily / "
         "cn_deposit_rate / cn_rrr_changes（teststock MySQL）",
         ha="center", fontsize=7, color="#adb5bd")

OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT_PNG}")

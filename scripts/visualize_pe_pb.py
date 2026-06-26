#!/usr/bin/env python3.11
"""沪深300 PE / PB 历史可视化（2006-2026）"""

import pymysql
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# 使用系统中文字体
_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
from matplotlib.patches import Patch
import numpy as np
import os

# ── 数据库连接 ────────────────────────────────────────────────
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", 3306)),
    user=os.getenv("MYSQL_USER", "teststock"),
    password=os.getenv("MYSQL_PASSWORD", "teststock"),
    database=os.getenv("MYSQL_DATABASE", "teststock"),
)

df = pd.read_sql(
    """
    SELECT trade_date, pe_ttm, pb
    FROM index_dailybasic
    WHERE ts_code = '000300.SH'
      AND trade_date >= '2006-01-01'
    ORDER BY trade_date
    """,
    conn,
    parse_dates=["trade_date"],
    index_col="trade_date",
)
conn.close()

# ── 统计量 ────────────────────────────────────────────────────
def quantile_bands(series):
    return {
        "p10": series.quantile(0.10),
        "p25": series.quantile(0.25),
        "median": series.median(),
        "p75": series.quantile(0.75),
        "p90": series.quantile(0.90),
        "mean": series.mean(),
    }

pe_stat = quantile_bands(df["pe_ttm"])
pb_stat = quantile_bands(df["pb"])

# ── 重要市场事件 ──────────────────────────────────────────────
events = [
    ("2007-10-16", "6124点顶部", "red"),
    ("2008-10-28", "1664点底部", "green"),
    ("2015-06-12", "5178点顶部", "red"),
    ("2016-01-27", "熔断底", "green"),
    ("2018-12-28", "2440点底", "green"),
    ("2020-02-04", "疫情低点", "green"),
    ("2021-02-10", "抱团瓦解", "red"),
    ("2024-09-24", "924政策反转", "green"),
]

# ── 绘图 ──────────────────────────────────────────────────────
fig = plt.figure(figsize=(18, 14))
fig.patch.set_facecolor("white")

fig.suptitle(
    "沪深300  PE_TTM & PB  历史走势（2006–2026）",
    fontsize=16, fontweight="bold", color="#1a1a2e",
    y=0.98,
)

ax_pe      = fig.add_axes([0.06, 0.56, 0.66, 0.36])
ax_pb      = fig.add_axes([0.06, 0.12, 0.66, 0.36])
ax_pe_dist = fig.add_axes([0.76, 0.56, 0.21, 0.36])
ax_pb_dist = fig.add_axes([0.76, 0.12, 0.21, 0.36])

DARK_BG    = "#f8f9fa"
GRID_COL   = "#dee2e6"
PE_COL     = "#2563eb"   # 宝蓝
PB_COL     = "#dc2626"   # 红
FILL_ALPHA = 0.08

def style_ax(ax):
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.8)

def draw_hbands(ax, stat, color, y_label=True, fmt=".1f"):
    """绘制百分位横线及色带"""
    ax.axhspan(stat["p10"], stat["p25"], alpha=0.07, color="#16a34a")
    ax.axhspan(stat["p75"], stat["p90"], alpha=0.07, color="#dc2626")
    for label, val, ls, alpha in [
        ("P10",  stat["p10"],    "--", 0.45),
        ("P25",  stat["p25"],    ":",  0.45),
        ("中位", stat["median"], "-",  0.65),
        ("P75",  stat["p75"],    ":",  0.45),
        ("P90",  stat["p90"],    "--", 0.45),
    ]:
        ax.axhline(val, color=color, alpha=alpha, linewidth=0.9, linestyle=ls)
        if y_label:
            ax.text(
                df.index[-1], val, f" {label}={val:{fmt}}",
                color=color, fontsize=7, va="center", alpha=0.75,
            )

def mark_events(ax, y_min, y_max):
    for date_str, label, color in events:
        dt = pd.Timestamp(date_str)
        if dt < df.index[0] or dt > df.index[-1]:
            continue
        ec = "#16a34a" if color == "green" else "#dc2626"
        ax.axvline(dt, color=ec, alpha=0.30, linewidth=0.9, linestyle="--")
        ax.text(
            dt, y_max * 0.97, label,
            rotation=90, fontsize=6, color=ec, alpha=0.75,
            va="top", ha="right",
        )

def fmt_xaxis(ax):
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

# ── PE_TTM 时序 ───────────────────────────────────────────────
style_ax(ax_pe)
ax_pe.plot(df.index, df["pe_ttm"], color=PE_COL, linewidth=0.9, label="PE_TTM")
ax_pe.fill_between(df.index, df["pe_ttm"], pe_stat["median"],
                   where=df["pe_ttm"] > pe_stat["median"],
                   alpha=0.12, color="red", label="高于中位")
ax_pe.fill_between(df.index, df["pe_ttm"], pe_stat["median"],
                   where=df["pe_ttm"] < pe_stat["median"],
                   alpha=0.12, color="green", label="低于中位")

draw_hbands(ax_pe, pe_stat, PE_COL)

# 评分卡阈值
for val, label, col in [
    (15, "PE=15 (机会-2)", "lime"),
    (20, "PE=20 (机会-1)", "limegreen"),
    (30, "PE=30 (风险+1)", "orange"),
    (40, "PE=40 (风险+1)", "tomato"),
    (50, "PE=50 (风险+2)", "red"),
]:
    ax_pe.axhline(val, color=col, alpha=0.55, linewidth=1.0, linestyle=":")
    ax_pe.text(df.index[10], val + 0.5, label, color=col, fontsize=6.5, alpha=0.8)

mark_events(ax_pe, df["pe_ttm"].min(), df["pe_ttm"].max())
fmt_xaxis(ax_pe)
ax_pe.set_xlim(df.index[0], df.index[-1])
ax_pe.set_ylabel("PE_TTM", color="#1a1a2e", fontsize=9)
ax_pe.tick_params(axis="x", labelbottom=False)

# PE 阈值色带
for val, label, col in [
    (15, "PE=15 (机会-2)", "#15803d"),
    (20, "PE=20 (机会-1)", "#65a30d"),
    (30, "PE=30 (风险+1)", "#d97706"),
    (40, "PE=40 (风险+1)", "#ea580c"),
    (50, "PE=50 (风险+2)", "#dc2626"),
]:
    ax_pe.axhline(val, color=col, alpha=0.6, linewidth=1.1, linestyle=":")
    ax_pe.text(df.index[10], val + 0.5, label, color=col, fontsize=6.5, alpha=0.85)
ax_pe.set_title("PE_TTM（市盈率·滚动12月）", color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")

# 当前值标注
cur_pe = df["pe_ttm"].iloc[-1]
ax_pe.annotate(
    f"当前 {cur_pe:.1f}",
    xy=(df.index[-1], cur_pe),
    xytext=(-55, 14), textcoords="offset points",
    color=PE_COL, fontsize=8.5, fontweight="bold",
    arrowprops=dict(arrowstyle="->", color=PE_COL, lw=1.0),
)

# ── PB 时序 ───────────────────────────────────────────────────
style_ax(ax_pb)
ax_pb.plot(df.index, df["pb"], color=PB_COL, linewidth=0.9, label="PB")
ax_pb.fill_between(df.index, df["pb"], pb_stat["median"],
                   where=df["pb"] > pb_stat["median"],
                   alpha=0.12, color="red")
ax_pb.fill_between(df.index, df["pb"], pb_stat["median"],
                   where=df["pb"] < pb_stat["median"],
                   alpha=0.12, color="green")

draw_hbands(ax_pb, pb_stat, PB_COL)

for val, label, col in [
    (2.0, "PB=2 (机会-1)", "limegreen"),
    (3.0, "PB=3 (风险+1)", "tomato"),
]:
    ax_pb.axhline(val, color=col, alpha=0.55, linewidth=1.0, linestyle=":")
    ax_pb.text(df.index[10], val + 0.04, label, color=col, fontsize=6.5, alpha=0.8)

mark_events(ax_pb, df["pb"].min(), df["pb"].max())
fmt_xaxis(ax_pb)
ax_pb.set_xlim(df.index[0], df.index[-1])
ax_pb.set_ylabel("PB", color="#1a1a2e", fontsize=9)
ax_pb.set_title("PB（市净率）", color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")

cur_pb = df["pb"].iloc[-1]
ax_pb.annotate(
    f"当前 {cur_pb:.2f}",
    xy=(df.index[-1], cur_pb),
    xytext=(-55, 14), textcoords="offset points",
    color=PB_COL, fontsize=8.5, fontweight="bold",
    arrowprops=dict(arrowstyle="->", color=PB_COL, lw=1.0),
)

# ── PE 分布（横向直方图）────────────────────────────────────────
style_ax(ax_pe_dist)
pe_vals = df["pe_ttm"].dropna()
ax_pe_dist.hist(pe_vals, bins=60, orientation="horizontal",
                color=PE_COL, alpha=0.7, edgecolor="none")
for val, col in [(15, "lime"), (20, "limegreen"), (30, "orange"), (40, "tomato"), (50, "red")]:
    ax_pe_dist.axhline(val, color=col, alpha=0.6, linewidth=0.9, linestyle=":")
ax_pe_dist.axhline(pe_stat["median"], color=PE_COL, linewidth=1.2)
ax_pe_dist.axhline(cur_pe, color="white", linewidth=1.5, linestyle="-")
ax_pe_dist.set_title("PE_TTM 分布", color="#1a1a2e", fontsize=9, pad=4, fontweight="bold")
ax_pe_dist.set_xlabel("频次", color="#495057", fontsize=8)
ax_pe_dist.tick_params(axis="y", labelleft=False)

# 分位数文字
for label, val in [("P10", pe_stat["p10"]), ("P25", pe_stat["p25"]),
                   ("中位", pe_stat["median"]), ("P75", pe_stat["p75"]),
                   ("P90", pe_stat["p90"])]:
    ax_pe_dist.text(
        ax_pe_dist.get_xlim()[1] * 0.95, val,
        f"{label}={val:.1f}", color=PE_COL, fontsize=6.5,
        ha="right", va="center", alpha=0.85,
    )
ax_pe_dist.text(
    ax_pe_dist.get_xlim()[1] * 0.95, cur_pe,
    f"现={cur_pe:.1f}", color="#1a1a2e", fontsize=7,
    ha="right", va="center", fontweight="bold",
)

# 当前分位
pct_rank_pe = (pe_vals < cur_pe).mean() * 100
ax_pe_dist.set_xlabel(f"当前分位 {pct_rank_pe:.0f}%", color="#495057", fontsize=8)

# ── PB 分布 ────────────────────────────────────────────────────
style_ax(ax_pb_dist)
pb_vals = df["pb"].dropna()
ax_pb_dist.hist(pb_vals, bins=60, orientation="horizontal",
                color=PB_COL, alpha=0.7, edgecolor="none")
for val, col in [(2.0, "limegreen"), (3.0, "tomato")]:
    ax_pb_dist.axhline(val, color=col, alpha=0.6, linewidth=0.9, linestyle=":")
ax_pb_dist.axhline(pb_stat["median"], color=PB_COL, linewidth=1.2)
ax_pb_dist.axhline(cur_pb, color="white", linewidth=1.5, linestyle="-")
ax_pb_dist.set_title("PB 分布", color="#1a1a2e", fontsize=9, pad=4, fontweight="bold")
ax_pb_dist.tick_params(axis="y", labelleft=False)

for label, val in [("P10", pb_stat["p10"]), ("P25", pb_stat["p25"]),
                   ("中位", pb_stat["median"]), ("P75", pb_stat["p75"]),
                   ("P90", pb_stat["p90"])]:
    ax_pb_dist.text(
        ax_pb_dist.get_xlim()[1] * 0.95, val,
        f"{label}={val:.2f}", color=PB_COL, fontsize=6.5,
        ha="right", va="center", alpha=0.85,
    )
ax_pb_dist.text(
    ax_pb_dist.get_xlim()[1] * 0.95, cur_pb,
    f"现={cur_pb:.2f}", color="#1a1a2e", fontsize=7,
    ha="right", va="center", fontweight="bold",
)

pct_rank_pb = (pb_vals < cur_pb).mean() * 100
ax_pb_dist.set_xlabel(f"当前分位 {pct_rank_pb:.0f}%", color="#495057", fontsize=8)

# ── 统计摘要文字 ──────────────────────────────────────────────
summary = (
    f"  PE_TTM  最小={pe_vals.min():.1f}  P10={pe_stat['p10']:.1f}  "
    f"P25={pe_stat['p25']:.1f}  中位={pe_stat['median']:.1f}  "
    f"P75={pe_stat['p75']:.1f}  P90={pe_stat['p90']:.1f}  最大={pe_vals.max():.1f}  "
    f"当前={cur_pe:.1f}（历史{pct_rank_pe:.0f}%分位）\n"
    f"  PB          最小={pb_vals.min():.2f}  P10={pb_stat['p10']:.2f}  "
    f"P25={pb_stat['p25']:.2f}  中位={pb_stat['median']:.2f}  "
    f"P75={pb_stat['p75']:.2f}  P90={pb_stat['p90']:.2f}  最大={pb_vals.max():.2f}  "
    f"当前={cur_pb:.2f}（历史{pct_rank_pb:.0f}%分位）"
)
fig.text(0.5, 0.065, summary, ha="center", va="top",
         fontsize=8, color="#495057",
         bbox=dict(boxstyle="round,pad=0.4", facecolor="#f1f3f5", edgecolor=GRID_COL))

fig.text(0.5, 0.005, "数据来源：teststock MySQL · index_dailybasic · 000300.SH",
         ha="center", fontsize=7, color="#adb5bd")

out = "/Users/jingxuan/workspace/teststock/docs/assets/cs300_pe_pb_analysis.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{out}")
plt.show()

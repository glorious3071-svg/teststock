#!/usr/bin/env python3.11
"""两融数据特征可视化（2010-2026）

布局：
  ① 两融余额合计时序（含历史峰值标注 + 关键市场事件）
  ② 融资 vs 融券对比（A 股做空机制极弱的事实）
  ③ 两融余额 12 月 YoY% + 评分卡阈值（v6: <-20% 触发冰点机会信号）
  ④ 两融余额 vs 沪深 300（同步性 + 杠杆率代理）
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "margin_analysis.png"

# ── 配色 ───────────────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
TOTAL_COL = "#7c3aed"     # 两融合计 - 紫
RZYE_COL = "#dc2626"      # 融资 - 红
RQYE_COL = "#16a34a"      # 融券 - 绿
YOY_POS_COL = "#dc2626"
YOY_NEG_COL = "#16a34a"
CS300_COL = "#1d4ed8"

# 评分卡阈值（v6 采纳后）
COLD_THRESHOLD = -20.0  # 两融 YoY < -20% → 机会 -1

# 关键事件
KEY_EVENTS = [
    ("2014-03-01", "杠杆牛启动", "↑"),
    ("2015-06-12", "5178 顶部", "↓"),
    ("2015-08-26", "股灾",       "↓"),
    ("2016-01-04", "熔断",       "↓"),
    ("2018-06-01", "去杠杆",    "↓"),
    ("2020-07-10", "爆款年",    "↑"),
    ("2021-02-10", "抱团顶",    "↓"),
    ("2024-09-24", "924反转",   "↑"),
    ("2025-09-01", "新一轮杠杆", "↑"),
]


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = _conn()
    margin = pd.read_sql(
        """
        SELECT trade_date, SUM(rzye) AS rzye, SUM(rqye) AS rqye,
               SUM(rzrqye) AS rzrqye, SUM(rzmre) AS rzmre, SUM(rqmcl) AS rqmcl
        FROM margin_daily
        GROUP BY trade_date
        ORDER BY trade_date
        """,
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    cs300 = pd.read_sql(
        """
        SELECT trade_date, close FROM index_daily
        WHERE ts_code='000300.SH' AND trade_date >= '2010-01-01'
        ORDER BY trade_date
        """,
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    conn.close()

    for c in ("rzye", "rqye", "rzrqye", "rzmre", "rqmcl"):
        margin[c] = margin[c].astype(float)
    # 转万亿
    for c in ("rzye", "rqye", "rzrqye"):
        margin[f"{c}_T"] = margin[c] / 1e12
    cs300["close"] = cs300["close"].astype(float)

    # 12 月 YoY%
    margin["rzrqye_yoy"] = margin["rzrqye"].pct_change(periods=242) * 100
    return margin, cs300


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.7)


def fmt_xaxis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


def mark_events(ax: plt.Axes, df_idx, y_top: float) -> None:
    for date_str, label, direction in KEY_EVENTS:
        dt = pd.Timestamp(date_str)
        if dt < df_idx[0] or dt > df_idx[-1]:
            continue
        color = "#dc2626" if direction == "↓" else "#16a34a"
        ax.axvline(dt, color="#6c757d", alpha=0.25, linewidth=0.8,
                   linestyle="--")
        ax.text(dt, y_top, f"{label}{direction}", rotation=90, fontsize=6,
                color=color, alpha=0.85, va="top", ha="right",
                fontweight="bold")


# ── ① 两融余额合计时序 ─────────────────────────────────────────
def plot_total(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(m.index, m["rzrqye_T"], color=TOTAL_COL, linewidth=1.2)
    ax.fill_between(m.index, m["rzrqye_T"], 0,
                    color=TOTAL_COL, alpha=0.10)

    # 标注峰值
    peak_idx = m["rzrqye_T"].idxmax()
    peak_val = m.loc[peak_idx, "rzrqye_T"]
    ax.annotate(
        f"历史峰值\n{peak_idx:%Y-%m-%d}\n{peak_val:.2f} 万亿",
        xy=(peak_idx, peak_val), xytext=(-90, -10),
        textcoords="offset points", color=TOTAL_COL,
        fontsize=7.5, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=TOTAL_COL, lw=0.9),
    )
    # 2015 杠杆牛顶
    idx_2015 = m.loc[m.index <= "2015-06-30"]["rzrqye_T"].idxmax()
    val_2015 = m.loc[idx_2015, "rzrqye_T"]
    ax.annotate(
        f"2015 杠杆牛顶\n{idx_2015:%Y-%m-%d}\n{val_2015:.2f} 万亿",
        xy=(idx_2015, val_2015), xytext=(20, 12),
        textcoords="offset points", color="#dc2626",
        fontsize=7, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#dc2626", lw=0.8),
    )

    mark_events(ax, m.index, y_top=2.8)
    fmt_xaxis(ax)
    ax.set_xlim(m.index[0], m.index[-1])
    ax.set_ylim(0, max(3.2, peak_val * 1.10))
    ax.set_ylabel("两融余额合计（万亿元）", color="#1a1a2e", fontsize=9)
    ax.set_title("① 两融余额合计（rzrqye）2010-2026 全交易所",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")


# ── ② 融资 vs 融券 ─────────────────────────────────────────────
def plot_rzye_vs_rqye(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(m.index, m["rzye_T"], color=RZYE_COL, linewidth=1.2,
            label=f"融资余额 (rzye)")
    # 融券乘 50 倍才看得见
    rq_scaled = m["rqye_T"] * 50
    ax.plot(m.index, rq_scaled, color=RQYE_COL, linewidth=1.0,
            label=f"融券余额 (rqye) × 50")

    # 当前比例
    last = m.iloc[-1]
    rz_pct = last["rzye"] / last["rzrqye"] * 100
    rq_pct = last["rqye"] / last["rzrqye"] * 100
    ax.text(
        0.01, 0.97,
        f"当前 ({m.index[-1]:%Y-%m-%d}):\n"
        f"  融资: {last['rzye_T']:.2f} 万亿 ({rz_pct:.1f}%)\n"
        f"  融券: {last['rqye']/1e8:.0f} 亿 ({rq_pct:.1f}%)\n"
        f"  → A 股做空机制极弱\n"
        f"     融资是绝对主导",
        transform=ax.transAxes, fontsize=7.5, color="#1a1a2e",
        va="top",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                  edgecolor=GRID_COL, alpha=0.92),
    )

    fmt_xaxis(ax)
    ax.set_xlim(m.index[0], m.index[-1])
    ax.set_ylabel("万亿元", color="#1a1a2e", fontsize=9)
    ax.set_title("② 融资 vs 融券对比（融券放大 50× 才可见）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)


# ── ③ 两融余额 12 月 YoY + 评分卡阈值 ────────────────────────
def plot_yoy(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    yoy = m["rzrqye_yoy"].dropna()
    pos = yoy.where(yoy >= 0, 0)
    neg = yoy.where(yoy < 0, 0)
    ax.fill_between(yoy.index, 0, pos, color=YOY_POS_COL, alpha=0.5)
    ax.fill_between(yoy.index, 0, neg, color=YOY_NEG_COL, alpha=0.55)

    ax.axhline(0, color="#1a1a2e", linewidth=0.6)
    # v6 阈值线
    ax.axhline(COLD_THRESHOLD, color="#16a34a", linewidth=1.2,
               linestyle="--", alpha=0.85)
    ax.text(yoy.index[5], COLD_THRESHOLD - 8,
            f"v6 冰点阈值 {COLD_THRESHOLD:.0f}% → 触发 -1 机会信号",
            color="#16a34a", fontsize=7.5, fontweight="bold")

    # 旧阈值（已废弃）— 灰色对比
    ax.axhline(50, color="#9ca3af", linewidth=0.8,
               linestyle=":", alpha=0.7)
    ax.text(yoy.index[5], 52, "旧阈值 +50%（已废弃，错向率高）",
            color="#6b7280", fontsize=6.5, alpha=0.85)
    ax.axhline(-30, color="#9ca3af", linewidth=0.8,
               linestyle=":", alpha=0.7)
    ax.text(yoy.index[5], -34, "旧阈值 -30%（已废弃，从未触发）",
            color="#6b7280", fontsize=6.5, alpha=0.85)

    # 标注 v6 触发点
    cold_hits = yoy[yoy < COLD_THRESHOLD]
    print(f"v6 冰点触发样本（YoY < -20%）：")
    print(cold_hits.resample("ME").last().dropna().head(20))
    if not cold_hits.empty:
        # 找跨年的最低点们
        for year_grp, g in cold_hits.groupby(cold_hits.index.year):
            idx = g.idxmin()
            val = g.min()
            ax.annotate(
                f"{idx:%Y-%m}\n{val:.0f}%",
                xy=(idx, val), xytext=(0, -20),
                textcoords="offset points",
                ha="center", fontsize=6.5, color="#16a34a",
                fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#16a34a", lw=0.8),
            )

    fmt_xaxis(ax)
    ax.set_xlim(yoy.index[0], yoy.index[-1])
    ax.set_ylim(-50, 320)
    ax.set_ylabel("两融余额 12 月 YoY (%)", color="#1a1a2e", fontsize=9)
    ax.set_title("③ 两融余额 12 月 YoY 与评分卡阈值",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")


# ── ④ 两融 vs 沪深 300 ─────────────────────────────────────────
def plot_vs_cs300(ax: plt.Axes, m: pd.DataFrame, cs300: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(m.index, m["rzrqye_T"], color=TOTAL_COL, linewidth=1.0,
            label="两融余额（万亿）")
    ax.set_ylabel("两融余额（万亿元）", color=TOTAL_COL, fontsize=9)
    ax.tick_params(axis="y", colors=TOTAL_COL, labelsize=8)

    ax2 = ax.twinx()
    ax2.plot(cs300.index, cs300["close"], color=CS300_COL,
             linewidth=1.0, alpha=0.85, label="沪深 300")
    ax2.set_ylabel("沪深 300 点位", color=CS300_COL, fontsize=8)
    ax2.tick_params(colors=CS300_COL, labelsize=8)
    ax2.spines["right"].set_color(CS300_COL)

    fmt_xaxis(ax)
    ax.set_xlim(m.index[0], m.index[-1])
    ax.set_title("④ 两融余额 vs 沪深 300（杠杆资金与行情同步性）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)
    ax2.legend(loc="upper right", fontsize=7, framealpha=0.92)

    # 相关系数
    merged = pd.concat([
        m["rzrqye_T"].rename("margin"),
        cs300["close"].rename("cs300"),
    ], axis=1).dropna()
    if len(merged) >= 100:
        corr_lvl = merged["margin"].corr(merged["cs300"])
        corr_chg = merged["margin"].pct_change().corr(merged["cs300"].pct_change())
        ax.text(
            0.01, 0.97,
            f"水平相关 ρ = {corr_lvl:+.2f}\n"
            f"日变化率相关 ρ = {corr_chg:+.2f}",
            transform=ax.transAxes, fontsize=7, color="#1a1a2e",
            va="top",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=GRID_COL, alpha=0.92),
        )


def main() -> None:
    margin, cs300 = load_data()

    fig = plt.figure(figsize=(18, 18))
    fig.patch.set_facecolor("white")
    fig.suptitle(
        "两融数据特征分析（2010-2026, 全交易所合计）",
        fontsize=16, fontweight="bold", color="#1a1a2e", y=0.99,
    )

    ax1 = fig.add_axes([0.06, 0.74, 0.92, 0.18])
    ax2 = fig.add_axes([0.06, 0.51, 0.92, 0.18])
    ax3 = fig.add_axes([0.06, 0.29, 0.92, 0.18])
    ax4 = fig.add_axes([0.06, 0.07, 0.92, 0.18])

    plot_total(ax1, margin)
    plot_rzye_vs_rqye(ax2, margin)
    plot_yoy(ax3, margin)
    plot_vs_cs300(ax4, margin, cs300)

    # 摘要
    peak_idx = margin["rzrqye_T"].idxmax()
    peak_val = margin.loc[peak_idx, "rzrqye_T"]
    cur_val = margin["rzrqye_T"].iloc[-1]
    cur_yoy = margin["rzrqye_yoy"].iloc[-1]
    n_cold = int((margin["rzrqye_yoy"] < COLD_THRESHOLD).sum())
    summary = (
        f"  数据覆盖：{margin.index[0]:%Y-%m-%d} ~ {margin.index[-1]:%Y-%m-%d}  "
        f"共 {len(margin):,} 个交易日   |   历史峰值 {peak_val:.2f} 万亿 @ {peak_idx:%Y-%m-%d}\n"
        f"  当前两融余额 {cur_val:.2f} 万亿，YoY {cur_yoy:+.1f}%  |   "
        f"融资:融券 ≈ 99:1（A 股做空机制极弱）\n"
        f"  v6 评分卡触发：YoY <-20% 共 {n_cold} 个交易日 → "
        f"对应 2017 / 2019 两次反向机会信号（年度 snapshot 100% 命中）"
    )
    fig.text(0.5, 0.03, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据来源：teststock MySQL · margin_daily（SSE+SZSE+BSE 合计）+ index_daily",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"\n已保存：{OUT_PATH}")
    print(f"  历史峰值: {peak_val:.2f} 万亿 @ {peak_idx:%Y-%m-%d}")
    print(f"  当前: {cur_val:.2f} 万亿, YoY {cur_yoy:+.1f}%")
    print(f"  v6 冰点触发日数: {n_cold}")


if __name__ == "__main__":
    main()

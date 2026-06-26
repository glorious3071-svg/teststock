#!/usr/bin/env python3.11
"""月发新基特征可视化与探索分析（2002-2026）

布局：
  ① 历史年度合计 + 月度精确叠加（长期趋势）
  ② 月度分类型堆叠（active/bond/qdii/other），2023+
  ③ 月发新基 vs 沪深 300（情绪-行情同步性）
  ④ 月度金额分布 + 评分卡阈值（>1500 / <200）
"""

from __future__ import annotations

import json
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

OUT_PATH = ROOT / "docs" / "assets" / "fund_new_analysis.png"

# ── 配色 ───────────────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
YEARLY_COL = "#7c3aed"   # 年度柱 - 紫
MONTHLY_COL = "#2563eb"  # 月度精确 - 蓝
EQUITY_COL = "#dc2626"   # 主动权益 - 红
BOND_COL = "#059669"     # 债券 - 翠绿
QDII_COL = "#f59e0b"     # QDII - 橙
OTHER_COL = "#6b7280"    # 其他 - 灰
INDEX_COL = "#1d4ed8"    # 沪深 300 - 深蓝

# 评分卡阈值
HOT_THRESHOLD = 1500.0   # 月发 > 1500 亿 → 情绪过热 +1
COLD_THRESHOLD = 200.0   # 月发 < 200 亿 → 情绪冰点 -1


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )


def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    conn = _conn()
    monthly = pd.read_sql(
        """
        SELECT month, new_fund_count, new_fund_billion,
               active_billion, bond_billion, qdii_billion, by_type_json
        FROM cn_fund_new_monthly
        ORDER BY month
        """,
        conn,
    )
    yearly = pd.read_sql(
        """
        SELECT cal_year, new_fund_count, new_fund_billion, active_billion
        FROM cn_fund_new_yearly
        ORDER BY cal_year
        """,
        conn,
    )
    cs300 = pd.read_sql(
        """
        SELECT trade_date, close
        FROM index_daily
        WHERE ts_code = '000300.SH' AND trade_date >= '2020-01-01'
        ORDER BY trade_date
        """,
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    conn.close()

    monthly["date"] = pd.to_datetime(monthly["month"], format="%Y%m")
    for c in ("new_fund_billion", "active_billion",
              "bond_billion", "qdii_billion"):
        monthly[c] = monthly[c].astype(float)
    monthly["other_billion"] = (
        monthly["new_fund_billion"]
        - monthly[["active_billion", "bond_billion", "qdii_billion"]].sum(axis=1)
    ).clip(lower=0)

    # 标注当前月数据不完整（最后一行）
    monthly["partial"] = False
    today = pd.Timestamp.today()
    cur_month = today.strftime("%Y%m")
    monthly.loc[monthly["month"] == cur_month, "partial"] = True

    cs300["close"] = cs300["close"].astype(float)
    return monthly, yearly, cs300


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.7)


def fmt_money(v: float, _pos=None) -> str:
    return f"{v:,.0f}"


# ── ① 长期年度趋势 + 月度精确叠加 ──────────────────────────────
def plot_long_term(ax: plt.Axes, yearly: pd.DataFrame,
                   monthly: pd.DataFrame) -> None:
    style_ax(ax)
    years = yearly["cal_year"].astype(int).values
    totals = yearly["new_fund_billion"].astype(float).values
    actives = yearly["active_billion"].astype(float).values

    # 年度柱：全口径（紫）+ 主动口径（深紫 overlay）
    ax.bar(years, totals, width=0.7, color=YEARLY_COL, alpha=0.45,
           edgecolor="white", label="年度合计（全口径）")
    ax.bar(years, actives, width=0.7, color="#4c1d95", alpha=0.85,
           edgecolor="white", label="年度合计（主动+指数）")

    # 标注历史峰值
    for label, year in [("2007 牛市", 2007), ("2015 杠杆牛", 2015),
                        ("2020 爆款年", 2020), ("2021 核心资产", 2021)]:
        if year in years:
            i = list(years).index(year)
            ax.text(year, totals[i] + 800, f"{label}\n{totals[i]:.0f}",
                    ha="center", fontsize=7, color="#4c1d95",
                    fontweight="bold")

    # 月度精确叠加为另一系列（月聚到年看是否吻合）
    m_yearly = monthly.groupby(monthly["date"].dt.year)["new_fund_billion"].sum()
    m_yearly = m_yearly[m_yearly.index >= 2023]
    ax.scatter(m_yearly.index, m_yearly.values,
               marker="D", s=70, color=MONTHLY_COL, zorder=5,
               edgecolor="white", linewidth=1.2,
               label="akshare 实测合计（2023+）")
    for y, v in m_yearly.items():
        ax.text(y, v + 600, f"{v:.0f}", ha="center",
                fontsize=7, color=MONTHLY_COL, fontweight="bold")

    ax.set_xlim(2001.5, 2026.5)
    ax.set_xticks(range(2002, 2027, 2))
    ax.set_ylabel("亿元", color="#1a1a2e", fontsize=9)
    ax.set_title("① 公募新发募集 — 年度长期趋势（2002-2026）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_money))
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.92)


# ── ② 月度分类型堆叠 ───────────────────────────────────────────
def plot_monthly_stack(ax: plt.Axes, monthly: pd.DataFrame) -> None:
    style_ax(ax)
    m = monthly[~monthly["partial"]].copy()
    dates = m["date"].values
    width = 22  # days

    bottom = pd.Series(0.0, index=range(len(m)))
    series = [
        ("主动+混合+指数", m["active_billion"], EQUITY_COL),
        ("债券型",        m["bond_billion"],   BOND_COL),
        ("QDII",          m["qdii_billion"],   QDII_COL),
        ("其他/FOF/货币", m["other_billion"],  OTHER_COL),
    ]
    for label, vals, color in series:
        ax.bar(dates, vals.values, width=width,
               bottom=bottom.values, color=color, alpha=0.85,
               edgecolor="none", label=label)
        bottom = bottom + vals.reset_index(drop=True)

    ax.axhline(HOT_THRESHOLD, color="#dc2626", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(dates[0], HOT_THRESHOLD + 60,
            f"过热阈值 {HOT_THRESHOLD:.0f} 亿",
            color="#dc2626", fontsize=7, alpha=0.85)
    ax.axhline(COLD_THRESHOLD, color="#16a34a", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(dates[0], COLD_THRESHOLD + 60,
            f"冰点阈值 {COLD_THRESHOLD:.0f} 亿",
            color="#16a34a", fontsize=7, alpha=0.85)

    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("亿元", color="#1a1a2e", fontsize=9)
    ax.set_title("② 月度精确数据 — 分类型堆叠（2023-2026，akshare 源）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_money))
    ax.legend(loc="upper right", fontsize=7, framealpha=0.92, ncol=2)


# ── ③ 月发新基 vs 沪深 300 ─────────────────────────────────────
def plot_vs_cs300(ax: plt.Axes, monthly: pd.DataFrame,
                  cs300: pd.DataFrame) -> None:
    style_ax(ax)
    m = monthly[~monthly["partial"]].copy()

    # 月发柱图
    ax.bar(m["date"].values, m["new_fund_billion"].values, width=22,
           color=MONTHLY_COL, alpha=0.55, edgecolor="none",
           label="月发新基（亿元）")

    # 沪深 300 月线收盘（取每月末点位）
    cs_monthly = cs300["close"].resample("ME").last()
    cs_monthly = cs_monthly[cs_monthly.index >= m["date"].min()]

    ax2 = ax.twinx()
    ax2.plot(cs_monthly.index, cs_monthly.values, color=INDEX_COL,
             linewidth=1.6, label="沪深 300 月末收盘")
    ax2.set_ylabel("沪深 300 点位", color=INDEX_COL, fontsize=9)
    ax2.tick_params(colors=INDEX_COL, labelsize=8)
    ax2.spines["right"].set_color(INDEX_COL)

    # 标注两个对比点：2024-06 月发巅峰、2024-02 行情底部
    for label, ms, ann_dy in [
        ("月发巅峰\n2024-06\n2543亿", "2024-06", 200),
        ("924反转后\n月发起飞", "2024-10", -180),
    ]:
        match = m[m["month"] == ms.replace("-", "")]
        if not match.empty:
            row = match.iloc[0]
            ax.annotate(
                label,
                xy=(row["date"], row["new_fund_billion"]),
                xytext=(0, ann_dy), textcoords="offset points",
                ha="center", color="#1a1a2e", fontsize=6.8,
                fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#1a1a2e",
                                lw=0.7, alpha=0.7),
            )

    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("月发亿元", color="#1a1a2e", fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_money))
    ax.set_title("③ 月发新基 vs 沪深 300（情绪 vs 行情同步性）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)
    ax2.legend(loc="upper right", fontsize=7, framealpha=0.92)

    # 相关系数（同月）
    merged = pd.DataFrame({
        "fund": m.set_index("date")["new_fund_billion"]
                   .resample("ME").sum(),
        "cs300": cs_monthly,
    }).dropna()
    if len(merged) >= 6:
        corr_lvl = merged["fund"].corr(merged["cs300"])
        corr_chg = merged["fund"].pct_change().corr(
            merged["cs300"].pct_change()
        )
        ax.text(
            0.01, 0.97,
            f"水平相关 ρ = {corr_lvl:+.2f}\n月变化率相关 ρ = {corr_chg:+.2f}",
            transform=ax.transAxes, fontsize=7, color="#1a1a2e",
            va="top",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=GRID_COL, alpha=0.9),
        )


# ── ④ 月度分布 + 阈值 ──────────────────────────────────────────
def plot_distribution(ax: plt.Axes, monthly: pd.DataFrame,
                      yearly: pd.DataFrame) -> dict:
    style_ax(ax)
    m = monthly[~monthly["partial"]].copy()
    vals = m["new_fund_billion"].values

    n, bins, patches = ax.hist(
        vals, bins=18, color=MONTHLY_COL, alpha=0.7, edgecolor="white",
        linewidth=0.6,
    )
    # 给超阈值的桶上色
    for p, edge in zip(patches, bins[:-1]):
        if edge >= HOT_THRESHOLD:
            p.set_facecolor("#dc2626")
            p.set_alpha(0.75)
        elif edge < COLD_THRESHOLD:
            p.set_facecolor("#16a34a")
            p.set_alpha(0.75)

    ax.axvline(HOT_THRESHOLD, color="#dc2626", linewidth=1.0,
               linestyle="--", alpha=0.85,
               label=f"过热阈值 {HOT_THRESHOLD:.0f}")
    ax.axvline(COLD_THRESHOLD, color="#16a34a", linewidth=1.0,
               linestyle="--", alpha=0.85,
               label=f"冰点阈值 {COLD_THRESHOLD:.0f}")
    ax.axvline(vals.mean(), color="#1a1a2e", linewidth=1.2,
               linestyle=":", alpha=0.85,
               label=f"均值 {vals.mean():.0f}")
    ax.axvline(pd.Series(vals).median(), color="#7c3aed",
               linewidth=1.2, linestyle=":", alpha=0.85,
               label=f"中位 {pd.Series(vals).median():.0f}")

    n_hot = int((vals >= HOT_THRESHOLD).sum())
    n_cold = int((vals < COLD_THRESHOLD).sum())
    n_total = len(vals)

    # 年度兜底的月均值（参考线）
    y2002_2022 = yearly[yearly["cal_year"] < 2023]
    monthly_avg_pre23 = (y2002_2022["new_fund_billion"].sum()
                         / (len(y2002_2022) * 12))
    ax.axvline(monthly_avg_pre23, color="#9333ea", linewidth=1.0,
               linestyle="-.", alpha=0.7,
               label=f"2002-22 月均 {monthly_avg_pre23:.0f}（兜底参考）")

    ax.set_xlabel("月发新基（亿元）", color="#1a1a2e", fontsize=9)
    ax.set_ylabel("月份数", color="#1a1a2e", fontsize=9)
    ax.set_title(f"④ 月度金额分布（2023-当前完整月，n={n_total}）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper right", fontsize=7, framealpha=0.92)

    # 文本框：触发次数统计
    ax.text(
        0.02, 0.95,
        f"过热 (≥{HOT_THRESHOLD:.0f}): {n_hot} 月 / {n_hot/n_total*100:.0f}%\n"
        f"冰点 (<{COLD_THRESHOLD:.0f}): {n_cold} 月 / {n_cold/n_total*100:.0f}%\n"
        f"中位 ≈ {pd.Series(vals).median():.0f} 亿\n"
        f"近期均值 ≈ {vals.mean():.0f} 亿",
        transform=ax.transAxes, fontsize=7.5, color="#1a1a2e",
        va="top",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                  edgecolor=GRID_COL, alpha=0.92),
    )

    return {
        "n_total":  n_total,
        "n_hot":    n_hot,
        "n_cold":   n_cold,
        "mean":     vals.mean(),
        "median":   pd.Series(vals).median(),
        "peak":     vals.max(),
        "peak_month": m.loc[m["new_fund_billion"].idxmax(), "month"],
    }


# ── 主流程 ────────────────────────────────────────────────────
def main() -> None:
    monthly, yearly, cs300 = load_data()

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor("white")
    fig.suptitle(
        "公募新发募集（月发新基）特征分析（2002-2026）",
        fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985,
    )

    # 上半：跨满宽的长期趋势
    ax1 = fig.add_axes([0.06, 0.64, 0.92, 0.28])
    # 中部：月度分类型堆叠（左）+ vs 沪深300（右）
    ax2 = fig.add_axes([0.06, 0.36, 0.42, 0.22])
    ax3 = fig.add_axes([0.55, 0.36, 0.42, 0.22])
    # 下部：分布 + 阈值（满宽偏中）
    ax4 = fig.add_axes([0.20, 0.10, 0.60, 0.20])

    plot_long_term(ax1, yearly, monthly)
    plot_monthly_stack(ax2, monthly)
    plot_vs_cs300(ax3, monthly, cs300)
    stats = plot_distribution(ax4, monthly, yearly)

    # 底部摘要
    full_period_total = yearly["new_fund_billion"].sum() + \
        monthly.loc[monthly["date"].dt.year >= 2024, "new_fund_billion"].sum()
    summary = (
        f"  2002-2026 累计新发募集约 {full_period_total/10000:.1f} 万亿元 "
        f"｜历史月度峰值 {stats['peak']:.0f} 亿 @ {stats['peak_month']}（akshare 实测）"
        f"｜历史年度峰值 31,400 亿 @ 2020（爆款年）\n"
        f"  近 3 年月度统计（n={stats['n_total']}）：中位 {stats['median']:.0f} 亿、"
        f"均值 {stats['mean']:.0f} 亿；触发过热 +1 共 {stats['n_hot']} 月、"
        f"触发冰点 -1 共 {stats['n_cold']} 月\n"
        f"  口径说明：年度数据 2002-2023 来自 Wind/基金报公开整理；"
        f"月度精确数据 2023-至今 来自 akshare 东财源"
    )
    fig.text(0.5, 0.04, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据来源：teststock MySQL · cn_fund_new_monthly + cn_fund_new_yearly",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")
    print(f"  月度统计 n={stats['n_total']}, 过热={stats['n_hot']}, "
          f"冰点={stats['n_cold']}, 月峰={stats['peak']:.0f}@{stats['peak_month']}")


if __name__ == "__main__":
    main()

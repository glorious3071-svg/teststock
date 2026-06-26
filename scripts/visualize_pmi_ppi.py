#!/usr/bin/env python3.11
"""PMI ↔ PPI 联动可视化与特征探索（2005-2026）

四张子图：
  ① PPI yoy 主时序 + 跨零标注 + 极值标注 + 历史事件
  ② PMI 制造业 vs PPI yoy 双轴对比（直观领先关系）
  ③ PPI 生产资料 vs 生活资料 剪刀差
  ④ PMI(新订单, t) ↔ PPI yoy(t+lag) 滞后相关曲线
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

_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "pmi_ppi_analysis.png"

DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
PPI_COL = "#dc2626"        # PPI 红
PMI_COL = "#2563eb"        # PMI 蓝
MP_COL = "#7c3aed"         # 生产资料 紫
CG_COL = "#16a34a"         # 生活资料 绿
LAG_COL = "#f59e0b"        # 滞后曲线 橙
PEAK_LAG = 7               # PMI 新订单领先 PPI ≈7 月（数据驱动）

# 关键事件
EVENTS = [
    ("2008-08", "金融危机起"),
    ("2009-07", "PPI谷底"),
    ("2011-07", "大宗顶"),
    ("2015-08", "通缩深底"),
    ("2016-09", "供给侧改革"),
    ("2020-02", "新冠冲击"),
    ("2021-10", "PPI峰值"),
    ("2024-09", "924反转"),
]


def load_data() -> pd.DataFrame:
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )
    pmi = pd.read_sql(
        "SELECT month, pmi_mfg, pmi_production, pmi_new_order "
        "FROM cn_pmi_monthly WHERE month >= '200501' ORDER BY month",
        conn,
    )
    ppi = pd.read_sql(
        "SELECT month, ppi_yoy, ppi_mp_yoy, ppi_cg_yoy "
        "FROM cn_ppi_monthly WHERE month >= '200501' ORDER BY month",
        conn,
    )
    conn.close()
    df = pmi.merge(ppi, on="month", how="inner")
    df["date"] = pd.to_datetime(df["month"], format="%Y%m")
    for c in ("pmi_mfg", "pmi_production", "pmi_new_order",
              "ppi_yoy", "ppi_mp_yoy", "ppi_cg_yoy"):
        df[c] = df[c].astype(float)
    df["mp_cg_diff"] = df["ppi_mp_yoy"] - df["ppi_cg_yoy"]
    return df.set_index("date")


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.8)


def fmt_xaxis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


def mark_events(ax: plt.Axes, y_pos: float) -> None:
    for date_str, label in EVENTS:
        dt = pd.Timestamp(date_str)
        ax.axvline(dt, color="#6c757d", alpha=0.30, linewidth=0.8, linestyle="--")
        ax.text(dt, y_pos, label, rotation=90, fontsize=6,
                color="#495057", alpha=0.75, va="top", ha="right")


# ── 子图 ① PPI yoy 主时序 ─────────────────────────────────────
def plot_ppi_timeseries(ax: plt.Axes, df: pd.DataFrame) -> dict:
    style_ax(ax)
    ax.plot(df.index, df["ppi_yoy"], color=PPI_COL, linewidth=1.0, label="PPI 同比")
    ax.fill_between(df.index, df["ppi_yoy"], 0,
                    where=df["ppi_yoy"] > 0, alpha=0.15, color="#dc2626",
                    label="通胀（>0）")
    ax.fill_between(df.index, df["ppi_yoy"], 0,
                    where=df["ppi_yoy"] < 0, alpha=0.15, color="#16a34a",
                    label="通缩（<0）")
    ax.axhline(0, color="#1a1a2e", linewidth=0.9, linestyle="-")

    # 极值标注
    idx_min = df["ppi_yoy"].idxmin()
    idx_max = df["ppi_yoy"].idxmax()
    for idx, color, dy in [(idx_min, "#16a34a", -16), (idx_max, "#dc2626", 14)]:
        v = df.loc[idx, "ppi_yoy"]
        ax.annotate(
            f"{idx.strftime('%Y-%m')}\n{v:+.1f}%",
            xy=(idx, v), xytext=(-30, dy), textcoords="offset points",
            color=color, fontsize=7, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=color, lw=0.8),
        )

    # 跨零事件
    sign_chg = (df["ppi_yoy"] >= 0).astype(int).diff()
    crossings = df[sign_chg.fillna(0) != 0]
    for ts, _ in crossings.iterrows():
        ax.scatter(ts, 0, s=20, color="#fbbf24", edgecolor="#1a1a2e",
                   linewidth=0.5, zorder=5)

    mark_events(ax, y_pos=12.5)
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(-10, 15)
    ax.set_ylabel("PPI 同比 (%)", color="#1a1a2e", fontsize=9)
    ax.set_title(f"① PPI 同比时序（黄点=跨零 {len(crossings)} 次）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    return {"min": (idx_min, df.loc[idx_min, "ppi_yoy"]),
            "max": (idx_max, df.loc[idx_max, "ppi_yoy"]),
            "crossings": len(crossings)}


# ── 子图 ② PMI vs PPI 双轴对比 ────────────────────────────────
def plot_pmi_vs_ppi(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(df.index, df["pmi_new_order"], color=PMI_COL, linewidth=1.0,
            label="PMI 新订单", alpha=0.85)
    ax.axhline(50, color=PMI_COL, linewidth=0.7, linestyle=":", alpha=0.5)
    ax.set_ylabel("PMI 新订单", color=PMI_COL, fontsize=9)
    ax.set_ylim(30, 65)

    ax2 = ax.twinx()
    ax2.plot(df.index, df["ppi_yoy"], color=PPI_COL, linewidth=1.0,
             label="PPI 同比", alpha=0.85)
    ax2.axhline(0, color=PPI_COL, linewidth=0.7, linestyle=":", alpha=0.5)
    ax2.set_ylabel("PPI 同比 (%)", color=PPI_COL, fontsize=9)
    ax2.set_ylim(-10, 15)
    ax2.tick_params(colors="#495057", labelsize=8)
    ax2.spines["right"].set_color(GRID_COL)

    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_title(f"② PMI 新订单 vs PPI 同比 — 新订单领先 PPI 约 {PEAK_LAG} 个月",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    # 联合图例
    l1, lab1 = ax.get_legend_handles_labels()
    l2, lab2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lab1 + lab2, loc="upper right", fontsize=7, framealpha=0.9)


# ── 子图 ③ 生产资料 vs 生活资料 剪刀差 ────────────────────────
def plot_mp_cg_divergence(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(df.index, df["ppi_mp_yoy"], color=MP_COL, linewidth=0.9,
            label="生产资料 PPI", alpha=0.85)
    ax.plot(df.index, df["ppi_cg_yoy"], color=CG_COL, linewidth=0.9,
            label="生活资料 PPI", alpha=0.85)

    ax2 = ax.twinx()
    ax2.fill_between(df.index, 0, df["mp_cg_diff"],
                     where=df["mp_cg_diff"] > 0, alpha=0.20, color=MP_COL,
                     label="生产>生活（上游推动）")
    ax2.fill_between(df.index, 0, df["mp_cg_diff"],
                     where=df["mp_cg_diff"] < 0, alpha=0.20, color=CG_COL,
                     label="生活>生产（消费驱动）")
    ax2.axhline(0, color="#1a1a2e", linewidth=0.5)
    ax2.set_ylabel("剪刀差 (pp)", color="#495057", fontsize=8)
    ax2.tick_params(colors="#495057", labelsize=7)
    ax2.spines["right"].set_color(GRID_COL)
    ax2.set_ylim(-12, 20)

    # 标 2021 剪刀差极值
    idx_max = df["mp_cg_diff"].idxmax()
    v = df.loc[idx_max, "mp_cg_diff"]
    ax2.annotate(
        f"{idx_max.strftime('%Y-%m')}\nΔ={v:+.1f}pp\n(限电+大宗)",
        xy=(idx_max, v), xytext=(-90, -20),
        textcoords="offset points", color=MP_COL,
        fontsize=7, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=MP_COL, lw=0.8),
    )

    ax.axhline(0, color="#1a1a2e", linewidth=0.7, linestyle="-", alpha=0.5)
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(-12, 20)
    ax.set_ylabel("PPI 同比 (%)", color="#1a1a2e", fontsize=9)
    ax.set_title("③ PPI 生产资料 vs 生活资料（剪刀差揭示通胀结构）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    l1, lab1 = ax.get_legend_handles_labels()
    l2, lab2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lab1 + lab2, loc="upper left", fontsize=7, framealpha=0.9)


# ── 子图 ④ 滞后相关曲线 ─────────────────────────────────────
def plot_lag_correlation(ax: plt.Axes, df: pd.DataFrame) -> tuple[int, float]:
    style_ax(ax)
    lags = range(0, 13)
    corrs = []
    for lag in lags:
        if lag == 0:
            c = df["pmi_new_order"].corr(df["ppi_yoy"])
        else:
            a = df["pmi_new_order"][:-lag].reset_index(drop=True)
            b = df["ppi_yoy"][lag:].reset_index(drop=True)
            c = a.corr(b)
        corrs.append(c)
    peak_idx = corrs.index(max(corrs))
    peak_val = corrs[peak_idx]

    bars = ax.bar(list(lags), corrs, width=0.7, color=LAG_COL, alpha=0.7,
                  edgecolor="#1a1a2e", linewidth=0.5)
    # 高亮峰值
    bars[peak_idx].set_color(PPI_COL)
    bars[peak_idx].set_alpha(0.85)
    ax.axvline(peak_idx, color=PPI_COL, alpha=0.4, linestyle="--", linewidth=0.9)
    ax.text(peak_idx, peak_val + 0.03, f"peak: lag={peak_idx}\nρ={peak_val:.3f}",
            color=PPI_COL, ha="center", fontsize=8, fontweight="bold")

    ax.axhline(0, color="#1a1a2e", linewidth=0.7)
    ax.axhline(0.3, color="#495057", linewidth=0.6, linestyle=":")
    ax.axhline(0.5, color="#495057", linewidth=0.6, linestyle=":")
    ax.text(12.2, 0.30, "弱相关线", fontsize=6, color="#495057", va="center")
    ax.text(12.2, 0.50, "中度相关线", fontsize=6, color="#495057", va="center")

    ax.set_xticks(list(lags))
    ax.set_xlabel("PMI 新订单领先 PPI 的月数（lag）", color="#1a1a2e", fontsize=9)
    ax.set_ylabel("Pearson ρ", color="#1a1a2e", fontsize=9)
    ax.set_xlim(-0.5, 12.5)
    ax.set_ylim(0, 0.65)
    ax.set_title("④ PMI 新订单 → PPI 滞后相关（找最佳领先期）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    return peak_idx, peak_val


# ── 主流程 ────────────────────────────────────────────────────
def main() -> None:
    df = load_data()
    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor("white")
    fig.suptitle("PMI ↔ PPI 联动分析（2005-2026）",
                 fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985)

    ax1 = fig.add_axes([0.06, 0.56, 0.42, 0.36])
    ax2 = fig.add_axes([0.55, 0.56, 0.42, 0.36])
    ax3 = fig.add_axes([0.06, 0.10, 0.42, 0.36])
    ax4 = fig.add_axes([0.55, 0.10, 0.42, 0.36])

    ppi_stats = plot_ppi_timeseries(ax1, df)
    plot_pmi_vs_ppi(ax2, df)
    plot_mp_cg_divergence(ax3, df)
    peak_lag, peak_corr = plot_lag_correlation(ax4, df)

    ppi = df["ppi_yoy"]
    summary = (
        f"  样本：{len(df)} 月（{df.index[0]:%Y-%m} ~ {df.index[-1]:%Y-%m}）"
        f"｜PPI 同比：均值={ppi.mean():+.2f}%  σ={ppi.std():.2f}%  "
        f"中位={ppi.median():+.2f}%\n"
        f"  PPI 极值：min={ppi_stats['min'][1]:+.2f}% @ {ppi_stats['min'][0]:%Y-%m}（金融危机）"
        f"｜max={ppi_stats['max'][1]:+.2f}% @ {ppi_stats['max'][0]:%Y-%m}"
        f"（限电+大宗）｜跨零次数={ppi_stats['crossings']} 次\n"
        f"  PMI→PPI 领先：最佳滞后={peak_lag} 月，ρ={peak_corr:.3f} "
        f"（PMI 新订单是 PPI 的领先指标，预测窗口约半年）"
    )
    fig.text(0.5, 0.06, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.012,
             "数据来源：teststock MySQL · cn_pmi_monthly / cn_ppi_monthly · Tushare",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")


if __name__ == "__main__":
    main()

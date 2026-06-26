#!/usr/bin/env python3.11
"""中国 PMI 历史可视化与异常检测（2005-2026）"""

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

OUT_PATH = ROOT / "docs" / "assets" / "pmi_analysis.png"

# ── 颜色 & 样式 ───────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
MFG_COL = "#2563eb"        # 制造业 - 蓝
NON_MFG_COL = "#16a34a"    # 非制造业 - 绿
COMP_COL = "#9333ea"       # 综合 - 紫
PROD_COL = "#dc2626"       # 生产 - 红
ORDER_COL = "#f59e0b"      # 新订单 - 橙

# 关键阈值
THRESHOLD_BOOM = 52.0     # 景气
THRESHOLD_PIVOT = 50.0    # 荣枯线
THRESHOLD_WEAK = 48.0     # 临界偏弱
SHOCK_DELTA = 5.0         # 单月跳变 ≥ 此值视为冲击
DIVERGENCE_DELTA = 3.0    # 子项背离阈值

# ── 重要市场/经济事件（标注用）─────────────────────────────────
EVENTS = [
    ("2008-09", "雷曼破产"),
    ("2008-11", "四万亿出台"),
    ("2015-08", "汇改+股灾"),
    ("2018-04", "中美贸易战"),
    ("2020-02", "新冠冲击"),
    ("2022-04", "上海封控"),
    ("2022-12", "防控放开"),
    ("2024-09", "924政策反转"),
]


# ── 数据加载 ──────────────────────────────────────────────────
def load_pmi() -> pd.DataFrame:
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )
    df = pd.read_sql(
        """
        SELECT month, pmi_mfg, pmi_production, pmi_new_order,
               pmi_non_mfg, pmi_composite
        FROM cn_pmi_monthly
        ORDER BY month
        """,
        conn,
    )
    conn.close()
    df["date"] = pd.to_datetime(df["month"], format="%Y%m")
    for col in ("pmi_mfg", "pmi_production", "pmi_new_order",
                "pmi_non_mfg", "pmi_composite"):
        df[col] = df[col].astype(float)
    df = df.set_index("date").drop(columns=["month"])
    df["mfg_delta"] = df["pmi_mfg"].diff()
    df["po_diff"] = df["pmi_production"] - df["pmi_new_order"]
    df["mn_diff"] = df["pmi_mfg"] - df["pmi_non_mfg"]
    return df


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


def draw_threshold_lines(ax: plt.Axes) -> None:
    for val, label, col in [
        (THRESHOLD_BOOM, f"{THRESHOLD_BOOM} 景气线", "#16a34a"),
        (THRESHOLD_PIVOT, f"{THRESHOLD_PIVOT} 荣枯线", "#1a1a2e"),
        (THRESHOLD_WEAK, f"{THRESHOLD_WEAK} 临界偏弱", "#dc2626"),
    ]:
        ax.axhline(val, color=col, alpha=0.7, linewidth=1.0, linestyle=":")
        ax.text(0.01, val, f" {label}", color=col, fontsize=6.5,
                va="center", alpha=0.85, ha="left",
                transform=ax.get_yaxis_transform())


# ── 子图 1：制造业 PMI 主时序 ────────────────────────────────
def plot_mfg_timeseries(ax: plt.Axes, df: pd.DataFrame) -> dict:
    style_ax(ax)
    ax.plot(df.index, df["pmi_mfg"], color=MFG_COL, linewidth=1.0,
            label="制造业 PMI")
    ax.fill_between(df.index, df["pmi_mfg"], THRESHOLD_PIVOT,
                    where=df["pmi_mfg"] > THRESHOLD_PIVOT,
                    alpha=0.12, color="#16a34a", interpolate=True,
                    label="荣枯线上方")
    ax.fill_between(df.index, df["pmi_mfg"], THRESHOLD_PIVOT,
                    where=df["pmi_mfg"] < THRESHOLD_PIVOT,
                    alpha=0.12, color="#dc2626", interpolate=True,
                    label="荣枯线下方")

    # 极端值标注
    idx_min = df["pmi_mfg"].idxmin()
    idx_max = df["pmi_mfg"].idxmax()
    for idx, color, dy in [(idx_min, "#dc2626", 8), (idx_max, "#16a34a", -16)]:
        val = df.loc[idx, "pmi_mfg"]
        ax.annotate(
            f"{idx.strftime('%Y-%m')}\n{val:.1f}",
            xy=(idx, val), xytext=(-30, dy), textcoords="offset points",
            color=color, fontsize=7, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=color, lw=0.8),
        )

    draw_threshold_lines(ax)
    mark_events(ax, y_pos=58.8)
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(33, 61)
    ax.set_ylabel("制造业 PMI", color="#1a1a2e", fontsize=9)
    ax.set_title("① 制造业 PMI 时序（2005-2026）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="lower left", fontsize=7, framealpha=0.9)

    return {
        "min": (idx_min, df.loc[idx_min, "pmi_mfg"]),
        "max": (idx_max, df.loc[idx_max, "pmi_mfg"]),
        "median": df["pmi_mfg"].median(),
        "below_pivot_pct": (df["pmi_mfg"] < THRESHOLD_PIVOT).mean() * 100,
    }


# ── 子图 2：三大 PMI 对比 ────────────────────────────────────
def plot_three_pmi(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    ax.plot(df.index, df["pmi_mfg"], color=MFG_COL, linewidth=0.9,
            label="制造业", alpha=0.85)
    ax.plot(df.index, df["pmi_non_mfg"], color=NON_MFG_COL, linewidth=0.9,
            label="非制造业", alpha=0.85)
    ax.plot(df.index, df["pmi_composite"], color=COMP_COL, linewidth=1.0,
            label="综合 PMI", alpha=0.85)

    ax.axhline(THRESHOLD_PIVOT, color="#1a1a2e", alpha=0.7,
               linewidth=1.0, linestyle=":")
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylabel("指数点位", color="#1a1a2e", fontsize=9)
    ax.set_title("② 制造业 / 非制造业 / 综合 PMI 对比",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="lower left", fontsize=7, framealpha=0.9)

    # 标注 2020-02 服务业崩跌
    idx = pd.Timestamp("2020-02-01")
    if idx in df.index and not pd.isna(df.loc[idx, "pmi_non_mfg"]):
        val = df.loc[idx, "pmi_non_mfg"]
        ax.annotate(
            f"非制造业 {val:.1f}\n(史上首跌穿)",
            xy=(idx, val), xytext=(15, 6),
            textcoords="offset points", color=NON_MFG_COL,
            fontsize=7, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=NON_MFG_COL, lw=0.8),
        )


# ── 子图 3：单月跳变 ─────────────────────────────────────────
def plot_monthly_delta(ax: plt.Axes, df: pd.DataFrame) -> list:
    style_ax(ax)
    deltas = df["mfg_delta"].dropna()
    pos = deltas.where(deltas >= 0, 0)
    neg = deltas.where(deltas < 0, 0)
    ax.bar(deltas.index, pos, width=20, color="#16a34a", alpha=0.7,
           label=f"≥0 (n={int((deltas >= 0).sum())})")
    ax.bar(deltas.index, neg, width=20, color="#dc2626", alpha=0.7,
           label=f"<0 (n={int((deltas < 0).sum())})")

    ax.axhline(SHOCK_DELTA, color="#dc2626", alpha=0.6,
               linewidth=0.9, linestyle="--")
    ax.axhline(-SHOCK_DELTA, color="#dc2626", alpha=0.6,
               linewidth=0.9, linestyle="--")
    ax.axhline(0, color="#1a1a2e", linewidth=0.6)

    # 标注 |Δ| ≥ SHOCK_DELTA 的冲击点
    shocks = df[df["mfg_delta"].abs() >= SHOCK_DELTA]
    for idx, row in shocks.iterrows():
        ax.text(idx, row["mfg_delta"] + (0.4 if row["mfg_delta"] > 0 else -1.2),
                f"{idx.strftime('%Y-%m')}\nΔ{row['mfg_delta']:+.1f}",
                fontsize=6, color="#1a1a2e", ha="center",
                fontweight="bold")

    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(-17, 18)
    ax.set_ylabel("制造业 PMI 单月 Δ", color="#1a1a2e", fontsize=9)
    ax.set_title(f"③ 制造业 PMI 单月跳变（红虚线 = ±{SHOCK_DELTA} 冲击阈值）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9)
    return shocks.index.tolist()


# ── 子图 4：生产 - 新订单 背离 ────────────────────────────────
def plot_production_order_div(ax: plt.Axes, df: pd.DataFrame) -> int:
    style_ax(ax)
    ax.plot(df.index, df["pmi_production"], color=PROD_COL, linewidth=0.8,
            label="生产指数", alpha=0.7)
    ax.plot(df.index, df["pmi_new_order"], color=ORDER_COL, linewidth=0.8,
            label="新订单指数", alpha=0.7)

    # 背离色带 (用第二个 y 轴叠加差值带)
    ax2 = ax.twinx()
    ax2.fill_between(df.index, 0, df["po_diff"], where=df["po_diff"] > 0,
                     alpha=0.20, color="#dc2626",
                     label="生产>订单 (库存积累/被动生产)")
    ax2.fill_between(df.index, 0, df["po_diff"], where=df["po_diff"] < 0,
                     alpha=0.20, color="#16a34a",
                     label="订单>生产 (需求强劲)")
    ax2.axhline(DIVERGENCE_DELTA, color="#dc2626", alpha=0.6,
                linewidth=0.7, linestyle="--")
    ax2.axhline(-DIVERGENCE_DELTA, color="#16a34a", alpha=0.6,
                linewidth=0.7, linestyle="--")
    ax2.set_ylabel("生产-订单 差值", color="#495057", fontsize=8)
    ax2.tick_params(colors="#495057", labelsize=7)
    ax2.spines["right"].set_color(GRID_COL)
    ax2.set_ylim(-8, 8)

    ax.axhline(THRESHOLD_PIVOT, color="#1a1a2e", alpha=0.5,
               linewidth=0.8, linestyle=":")
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(30, 70)
    ax.set_ylabel("子项指数", color="#1a1a2e", fontsize=9)
    ax.set_title(f"④ 生产 vs 新订单 背离（|Δ|>{DIVERGENCE_DELTA} 警戒）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    ax2.legend(loc="lower right", fontsize=7, framealpha=0.9)

    return int((df["po_diff"].abs() > DIVERGENCE_DELTA).sum())


# ── 主流程 ────────────────────────────────────────────────────
def main() -> None:
    df = load_pmi()

    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor("white")
    fig.suptitle("中国 PMI 历史走势与异常检测（2005-2026）",
                 fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985)

    ax1 = fig.add_axes([0.06, 0.56, 0.42, 0.36])
    ax2 = fig.add_axes([0.55, 0.56, 0.42, 0.36])
    ax3 = fig.add_axes([0.06, 0.10, 0.42, 0.36])
    ax4 = fig.add_axes([0.55, 0.10, 0.42, 0.36])

    stats = plot_mfg_timeseries(ax1, df)
    plot_three_pmi(ax2, df)
    shock_dates = plot_monthly_delta(ax3, df)
    divergence_count = plot_production_order_div(ax4, df)

    # ── 底部统计摘要 ──────────────────────────────────────────
    mfg = df["pmi_mfg"]
    summary = (
        f"  样本：{len(df)} 月（{df.index[0]:%Y-%m} ~ {df.index[-1]:%Y-%m}）"
        f"｜均值={mfg.mean():.2f}  中位={stats['median']:.2f}  "
        f"P10={mfg.quantile(0.10):.2f}  P90={mfg.quantile(0.90):.2f}  "
        f"σ={mfg.std():.2f}\n"
        f"  极值：min={stats['min'][1]:.1f} @ {stats['min'][0]:%Y-%m}"
        f"（疫情）｜max={stats['max'][1]:.1f} @ {stats['max'][0]:%Y-%m}"
        f"（次贷危机前过热）｜"
        f"荣枯线下方占比={stats['below_pivot_pct']:.1f}%\n"
        f"  异常：单月跳变 |Δ|≥{SHOCK_DELTA:.0f} 共 {len(shock_dates)} 次"
        f"｜生产-订单 |Δ|>{DIVERGENCE_DELTA:.0f} 共 {divergence_count} 月"
        f"｜2020-02 制造业 PMI {stats['min'][1]:.1f} 系建表以来唯一一次跌破 40"
    )
    fig.text(0.5, 0.06, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.012,
             "数据来源：teststock MySQL · cn_pmi_monthly · Tushare cn_pmi (doc 325)",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")


if __name__ == "__main__":
    main()

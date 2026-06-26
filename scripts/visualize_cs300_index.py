#!/usr/bin/env python3.11
"""沪深300 指数过去 20 年走势可视化（2006-2026）

布局：
  ① 收盘点位时序（标注顶部/底部/关键事件 + 牛熊色带）
  ② 月度成交额
  ③ 滚动 1Y 收益（年化）
  ④ 历史峰值回撤（drawdown）
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "cs300_index_20y.png"

# ── 配色 ───────────────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
PRICE_COL = "#1d4ed8"      # 主线 - 深蓝
BULL_COL = "#dc2626"       # 牛市色带（A 股审美：红涨）
BEAR_COL = "#16a34a"       # 熊市色带（绿跌）
VOL_COL = "#0891b2"        # 成交额 - 青
RET_POS_COL = "#dc2626"
RET_NEG_COL = "#16a34a"
DD_COL = "#7c3aed"         # 回撤 - 紫

# ── 关键事件 & 顶底 ───────────────────────────────────────────
KEY_POINTS = [
    # date, price (None=用实际收盘), label, color
    ("2007-10-16", None, "6124 顶部", "#dc2626"),
    ("2008-11-04", None, "1664 底部", "#16a34a"),
    ("2009-08-04", None, "3803 反弹高点", "#dc2626"),
    ("2015-06-12", None, "5178 顶部", "#dc2626"),
    ("2016-01-28", None, "熔断底 2853", "#16a34a"),
    ("2018-01-26", None, "4403 抱团顶", "#dc2626"),
    ("2019-01-04", None, "2935 底", "#16a34a"),
    ("2021-02-10", None, "5807 抱团高点", "#dc2626"),
    ("2024-02-05", None, "3097 底部", "#16a34a"),
    ("2024-10-08", None, "924 反转高点", "#dc2626"),
]

# 牛熊周期（粗粒度，用于色带）
BULL_BEAR_PHASES = [
    ("2006-01-04", "2007-10-16", "bull", "06-07 大牛市"),
    ("2007-10-16", "2008-11-04", "bear", "次贷熊"),
    ("2008-11-04", "2009-08-04", "bull", "四万亿反弹"),
    ("2014-07-01", "2015-06-12", "bull", "杠杆牛"),
    ("2015-06-12", "2016-01-28", "bear", "股灾"),
    ("2019-01-04", "2021-02-10", "bull", "核心资产牛"),
    ("2021-02-10", "2024-02-05", "bear", "三年阴跌"),
    ("2024-09-24", "2024-10-08", "bull", "924反转"),
]


def load_index() -> pd.DataFrame:
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )
    df = pd.read_sql(
        """
        SELECT trade_date, open, high, low, close, vol, amount, pct_chg
        FROM index_daily
        WHERE ts_code = '000300.SH'
        ORDER BY trade_date
        """,
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    conn.close()
    for c in ("open", "high", "low", "close", "vol", "amount", "pct_chg"):
        df[c] = df[c].astype(float)
    # 衍生
    df["log_ret"] = np.log(df["close"]).diff()
    df["roll_high"] = df["close"].cummax()
    df["drawdown"] = df["close"] / df["roll_high"] - 1.0
    df["ret_1y"] = df["close"].pct_change(periods=242)  # 约 1 年交易日
    return df


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.8)


def fmt_xaxis(ax: plt.Axes, year_step: int = 2) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


def shade_phases(ax: plt.Axes, df: pd.DataFrame) -> None:
    for start, end, kind, _ in BULL_BEAR_PHASES:
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        if e < df.index[0] or s > df.index[-1]:
            continue
        color = BULL_COL if kind == "bull" else BEAR_COL
        ax.axvspan(s, e, alpha=0.07, color=color)


def annotate_key_points(ax: plt.Axes, df: pd.DataFrame) -> None:
    for date_str, price, label, color in KEY_POINTS:
        dt = pd.Timestamp(date_str)
        if dt not in df.index:
            # 取最近一个交易日
            pos = df.index.searchsorted(dt)
            pos = min(pos, len(df.index) - 1)
            dt = df.index[pos]
        p = float(df.loc[dt, "close"]) if price is None else price
        is_top = "顶" in label or "高" in label
        dy = -22 if is_top else 22
        ax.scatter([dt], [p], s=22, color=color, zorder=5,
                   edgecolor="white", linewidth=0.6)
        ax.annotate(
            f"{label}\n{p:.0f}",
            xy=(dt, p), xytext=(0, dy),
            textcoords="offset points", ha="center",
            color=color, fontsize=6.8, fontweight="bold",
            arrowprops=dict(arrowstyle="-", color=color,
                            lw=0.6, alpha=0.6),
        )


# ── 子图 ────────────────────────────────────────────────────────
def plot_price(ax: plt.Axes, df: pd.DataFrame) -> dict:
    style_ax(ax)
    shade_phases(ax, df)
    ax.plot(df.index, df["close"], color=PRICE_COL, linewidth=1.0)
    ax.fill_between(df.index, df["close"], 0,
                    color=PRICE_COL, alpha=0.05)
    annotate_key_points(ax, df)

    # 当前点位
    cur = df.iloc[-1]
    ax.annotate(
        f"当前 {cur['close']:.0f}\n{cur.name:%Y-%m-%d}",
        xy=(cur.name, cur["close"]), xytext=(-65, 16),
        textcoords="offset points", color=PRICE_COL,
        fontsize=8, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=PRICE_COL, lw=0.9),
    )

    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(0, df["close"].max() * 1.10)
    ax.set_ylabel("收盘点位", color="#1a1a2e", fontsize=9)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.set_title("① 沪深 300 收盘点位（红=牛市段 / 绿=熊市段）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")

    # 收益率
    start, end = df.iloc[0]["close"], cur["close"]
    years = (df.index[-1] - df.index[0]).days / 365.25
    cagr = (end / start) ** (1.0 / years) - 1
    return {
        "start": (df.index[0], start),
        "end": (df.index[-1], end),
        "years": years,
        "cagr": cagr,
        "max_dd": df["drawdown"].min(),
    }


def plot_volume(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    monthly = df["amount"].resample("ME").sum() / 1e8 / 1000  # 千元 → 万亿
    ax.bar(monthly.index, monthly.values, width=22,
           color=VOL_COL, alpha=0.65, edgecolor="none")
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylabel("月成交额（万亿元）", color="#1a1a2e", fontsize=9)
    ax.set_title("② 月度成交额（沪深 300 成分股）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    # 标注成交高峰
    top3 = monthly.nlargest(3)
    for ts, v in top3.items():
        ax.text(ts, v, f" {ts:%Y-%m}\n {v:.1f}", fontsize=6.5,
                color="#0e7490", fontweight="bold", va="bottom", ha="center")


def plot_rolling_return(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    r = df["ret_1y"].dropna() * 100
    pos = r.where(r >= 0, 0)
    neg = r.where(r < 0, 0)
    ax.fill_between(r.index, 0, pos, color=RET_POS_COL, alpha=0.5)
    ax.fill_between(r.index, 0, neg, color=RET_NEG_COL, alpha=0.5)
    ax.axhline(0, color="#1a1a2e", linewidth=0.6)
    for level, col in [(50, "#dc2626"), (-30, "#16a34a")]:
        ax.axhline(level, color=col, linewidth=0.7,
                   linestyle="--", alpha=0.7)
        ax.text(df.index[10], level + 1.5,
                f"±{abs(level)}% 阈值",
                color=col, fontsize=6.5, alpha=0.85)
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylabel("滚动 1Y 收益 (%)", color="#1a1a2e", fontsize=9)
    ax.set_title("③ 滚动 1 年累计收益（衡量短期牛熊强度）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")


def plot_drawdown(ax: plt.Axes, df: pd.DataFrame) -> None:
    style_ax(ax)
    dd = df["drawdown"] * 100
    ax.fill_between(dd.index, 0, dd.values, color=DD_COL, alpha=0.45)
    ax.plot(dd.index, dd.values, color=DD_COL, linewidth=0.8)
    ax.axhline(0, color="#1a1a2e", linewidth=0.6)
    for lvl in (-20, -40, -60):
        ax.axhline(lvl, color="#7c3aed", alpha=0.3,
                   linewidth=0.6, linestyle=":")
        ax.text(df.index[10], lvl + 1, f"{lvl}%",
                color="#7c3aed", fontsize=6.5, alpha=0.8)
    # 最大回撤点
    idx_dd = dd.idxmin()
    val_dd = dd.loc[idx_dd]
    ax.annotate(
        f"最大回撤 {val_dd:.1f}%\n{idx_dd:%Y-%m-%d}",
        xy=(idx_dd, val_dd), xytext=(20, 8),
        textcoords="offset points", color="#7c3aed",
        fontsize=7.5, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#7c3aed", lw=0.9),
    )
    fmt_xaxis(ax)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_ylim(-75, 5)
    ax.set_ylabel("距历史峰值回撤 (%)", color="#1a1a2e", fontsize=9)
    ax.set_title("④ 从历史峰值的回撤（drawdown）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")


def main() -> None:
    df = load_index()

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor("white")
    fig.suptitle(f"沪深 300 指数 20 年走势（{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}）",
                 fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985)

    ax1 = fig.add_axes([0.06, 0.58, 0.92, 0.34])
    ax2 = fig.add_axes([0.06, 0.40, 0.92, 0.13])
    ax3 = fig.add_axes([0.06, 0.24, 0.92, 0.13])
    ax4 = fig.add_axes([0.06, 0.08, 0.92, 0.13])

    stats = plot_price(ax1, df)
    # 隐藏中间子图的 x 轴标签，避免重复
    for ax_top in (ax1, ax2, ax3):
        ax_top.tick_params(axis="x", labelbottom=False)
    plot_volume(ax2, df)
    plot_rolling_return(ax3, df)
    plot_drawdown(ax4, df)

    # 底部摘要
    bull_years = sum((pd.Timestamp(e) - pd.Timestamp(s)).days
                     for s, e, k, _ in BULL_BEAR_PHASES if k == "bull") / 365.25
    bear_years = sum((pd.Timestamp(e) - pd.Timestamp(s)).days
                     for s, e, k, _ in BULL_BEAR_PHASES if k == "bear") / 365.25
    summary = (
        f"  起点：{stats['start'][0]:%Y-%m-%d} {stats['start'][1]:.0f}  →  "
        f"当前：{stats['end'][0]:%Y-%m-%d} {stats['end'][1]:.0f}  "
        f"｜跨度 {stats['years']:.1f} 年  ｜年化收益 (CAGR) = {stats['cagr']*100:.2f}%\n"
        f"  历史最高收盘 5877 (2007-10-16)  ｜历史最低 924 (2006-01-04)  "
        f"｜最大回撤 {stats['max_dd']*100:.1f}%  "
        f"｜牛市累计 {bull_years:.1f} 年 / 熊市累计 {bear_years:.1f} 年\n"
        f"  3 轮主要顶部：2007/10 6124、2015/06 5178、2021/02 5807   "
        f"｜4 轮主要底部：2008/11 1664、2016/01 2853、2019/01 2935、2024/02 3097"
    )
    fig.text(0.5, 0.045, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据来源：teststock MySQL · index_daily · 000300.SH（Tushare index_daily）",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")
    print(f"  CAGR={stats['cagr']*100:.2f}%  最大回撤={stats['max_dd']*100:.1f}%")


if __name__ == "__main__":
    main()

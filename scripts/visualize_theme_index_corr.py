#!/usr/bin/env python3
"""可视化各题材关联指数的历史走势，叠加政策信号背景，分析关联性。

图表结构：
  - 每个题材一个子图，2列布局
  - 背景色：政策信号强度（深红=强，橙=中，灰=弱/无）
  - 折线：该题材强关联申万指数，以2006-Q1为基点归一化
  - 数据按季度重采样（指数数据本身稀疏，~每月4-12条）

Usage:
  python scripts/visualize_theme_index_corr.py
  python scripts/visualize_theme_index_corr.py --out /tmp/theme_corr.png
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import argparse
from collections import defaultdict
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pymysql
from dotenv import load_dotenv

# ── 配置 ────────────────────────────────────────────────────────────
PLOT_START = date(2006, 1, 1)
PLOT_END   = date(2026, 6, 30)
BASE_DATE  = date(2006, 6, 30)   # 归一化基点（取有数据的最早一个季末）

# 每个题材展示的代表性指数（优先选 2006 年前就有数据的，代码有意义的）
THEME_REPR: dict[str, list[str]] = {
    "消费/内需":        ["801125.SI", "801127.SI", "801219.SI", "801993.SI"],
    "科技创新/自主创新":  ["801103.SI", "801104.SI", "801080.SI", "801081.SI"],
    "新能源/光伏储能":   ["801056.SI", "801735.SI", "801736.SI", "801737.SI"],
    "先进制造/产业升级": ["801078.SI", "801116.SI", "801072.SI", "801074.SI"],
    "节能环保/绿色低碳": ["801970.SI", "801971.SI", "801972.SI", "801115.SI"],
    "基建/城镇化":      ["801077.SI", "801179.SI", "801738.SI", "801710.SI"],
    "房地产/城投化债":   ["801183.SI", "801180.SI", "801713.SI", "801722.SI"],
    "对外开放/出海":    ["801992.SI", "801131.SI", "801170.SI", "801202.SI"],
}

SIGNAL_COLORS = {"强": "#d62728", "中": "#ff7f0e", "弱": "#aec7e8"}
SIGNAL_ALPHA  = {"强": 0.18,      "中": 0.12,      "弱": 0.06}

LINE_COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b",
               "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def fetch_signals(conn) -> dict[tuple[str, str], str]:
    """返回 {(as_of_date_str, theme): signal_strength}。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT as_of_date, theme, signal_strength FROM annual_sector_signals ORDER BY as_of_date"
        )
        return {(str(r[0]), r[1]): r[2] for r in cur.fetchall()}


def fetch_index_names(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT ts_code, index_name FROM theme_index_map WHERE ts_code LIKE '%.SI'")
        return {r[0]: r[1] for r in cur.fetchall()}


def fetch_prices(conn, codes: list[str]) -> dict[str, list[tuple[date, float]]]:
    """返回 {ts_code: [(date, close)]}，按日期升序。"""
    result: dict[str, list] = defaultdict(list)
    for code in codes:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
                "AND trade_date >= %s AND trade_date <= %s AND close IS NOT NULL "
                "ORDER BY trade_date",
                (code, PLOT_START, PLOT_END),
            )
            for td, cl in cur.fetchall():
                result[code].append((td, float(cl)))
    return result


def quarterly_resample(series: list[tuple[date, float]]) -> dict[str, float]:
    """按自然季度取最后一个有效收盘价，返回 {'YYYY-QN': close}。"""
    quarters: dict[str, float] = {}
    for td, cl in series:
        q = f"{td.year}-Q{(td.month - 1) // 3 + 1}"
        quarters[q] = cl   # 同季度内越晚越覆盖 → 季末价
    return quarters


def normalize(quarters: dict[str, float], base_q: str) -> dict[str, float]:
    """以 base_q 季度为 100 归一化；若该季度无数据则取最早可用季度。"""
    if not quarters:
        return {}
    sorted_qs = sorted(quarters)
    if base_q in quarters:
        base_val = quarters[base_q]
    else:
        # 取最早季度
        base_val = quarters[sorted_qs[0]]
    if base_val == 0:
        return {}
    return {q: v / base_val * 100 for q, v in quarters.items()}


def quarter_to_date(q: str) -> date:
    """'2006-Q2' → 2006-06-30。"""
    yr, qn = q.split("-Q")
    month_end = int(qn) * 3
    day = 31 if month_end in (3, 12) else 30
    return date(int(yr), month_end, day)


def build_quarter_list() -> list[str]:
    qs = []
    for yr in range(2006, 2027):
        for qi in range(1, 5):
            q = f"{yr}-Q{qi}"
            if quarter_to_date(q) > PLOT_END:
                break
            qs.append(q)
    return qs


def draw_signal_background(ax, all_quarters: list[str],
                            signals: dict[tuple[str, str], str], theme: str):
    """在 ax 上涂抹政策信号背景色。"""
    for i, q in enumerate(all_quarters[:-1]):
        sig = signals.get((f"{quarter_to_date(q).year}-01-01", theme), None)
        # as_of_date 是年初，对应全年，按年染色
        yr = int(q.split("-")[0])
        as_of_key = f"{yr}-01-01"
        sig = signals.get((as_of_key, theme), None)
        if sig and sig in SIGNAL_COLORS:
            x0 = quarter_to_date(q)
            x1 = quarter_to_date(all_quarters[i + 1])
            ax.axvspan(x0, x1, color=SIGNAL_COLORS[sig], alpha=SIGNAL_ALPHA[sig], linewidth=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/assets/theme_index_corr.png")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        signals   = fetch_signals(conn)
        name_map  = fetch_index_names(conn)
        all_codes = list({c for codes in THEME_REPR.values() for c in codes})
        prices    = fetch_prices(conn, all_codes)
    finally:
        conn.close()

    all_quarters = build_quarter_list()
    base_q = "2006-Q2"   # 取 Q2 让早期数据更稳定

    nthemes = len(THEME_REPR)
    ncols   = 2
    nrows   = (nthemes + 1) // ncols

    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(22, nrows * 5),
                             sharex=True)
    axes = axes.flatten()

    plt.rcParams["font.family"] = ["STHeiti", "PingFang SC", "Arial Unicode MS",
                                   "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    for ax_idx, (theme, codes) in enumerate(THEME_REPR.items()):
        ax = axes[ax_idx]

        draw_signal_background(ax, all_quarters, signals, theme)

        has_any = False
        for ci, code in enumerate(codes):
            series = prices.get(code, [])
            if not series:
                continue
            q_prices = quarterly_resample(series)
            normed   = normalize(q_prices, base_q)
            if not normed:
                continue

            xs = [quarter_to_date(q) for q in sorted(normed)]
            ys = [normed[q] for q in sorted(normed)]

            label = name_map.get(code, code).replace("申万", "").replace("Ⅱ", "")
            lw = 2.0 if series[0][0] <= BASE_DATE else 1.4
            ax.plot(xs, ys, color=LINE_COLORS[ci % len(LINE_COLORS)],
                    linewidth=lw, label=label, marker=".", markersize=2, alpha=0.85)
            has_any = True

        if not has_any:
            ax.text(0.5, 0.5, "数据不足", transform=ax.transAxes,
                    ha="center", va="center", fontsize=12, color="gray")

        ax.axhline(100, color="black", linewidth=0.5, linestyle="--", alpha=0.4)
        ax.set_title(theme, fontsize=13, fontweight="bold", pad=6)
        ax.set_ylabel("归一化（2006-Q2=100）", fontsize=9)
        ax.legend(loc="upper left", fontsize=8, ncol=2, framealpha=0.7)
        ax.grid(axis="y", alpha=0.3)
        ax.set_xlim(date(2006, 1, 1), PLOT_END)

    # 隐藏多余子图
    for ax_idx in range(nthemes, len(axes)):
        axes[ax_idx].set_visible(False)

    # 统一 x 轴格式
    import matplotlib.dates as mdates
    for ax in axes[:nthemes]:
        ax.xaxis.set_major_locator(mdates.YearLocator(2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=8)

    # 图例 patch
    legend_patches = [
        mpatches.Patch(color=SIGNAL_COLORS["强"], alpha=0.5, label="政策信号：强"),
        mpatches.Patch(color=SIGNAL_COLORS["中"], alpha=0.5, label="政策信号：中"),
    ]
    fig.legend(handles=legend_patches, loc="lower center",
               ncol=2, fontsize=10, framealpha=0.8, bbox_to_anchor=(0.5, 0.01))

    fig.suptitle("各题材申万指数走势 vs 政策信号（2006-2026）\n"
                 "背景色 = 年度政策信号强度，折线 = 归一化收盘（2006-Q2=100）",
                 fontsize=14, y=1.01)

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=[0, 0.04, 1, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"已保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

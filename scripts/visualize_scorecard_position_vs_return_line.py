#!/usr/bin/env python3.11
"""可视化 V5.0 评分卡 21 年「目标仓位 vs 当年沪深 300 涨幅」时序折线图

读取 data/backtests/scorecard_20y_simulation.json，按年份画双 Y 轴折线：
  - 左 Y 轴：评分卡年初目标仓位 (%)
  - 右 Y 轴：沪深 300 当年涨跌幅 (%)
  - X 轴：年份（2006–2026）
  - 0% 与 75% 参考线，标注 2008 / 2007 / 2015 等关键节点
  - 顶部条形带：每年决策超额（vs 满仓 CS300），绿=赚到，红=拖后腿

输出：docs/assets/scorecard_position_vs_return_line.png
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "backtests" / "scorecard_20y_simulation.json"
OUT_PNG = ROOT / "docs" / "assets" / "scorecard_position_vs_return_line.png"

NEUTRAL_POSITION_PCT = 75.0
ZERO_RETURN_PCT = 0.0
EXCESS_HIGHLIGHT_THRESHOLD_PP = 5.0   # |超额| 超过此值才在年份标签上做强调
KEY_YEARS = {
    2007: "牛市顶（评分卡未减仓）",
    2008: "GFC（减仓到 30% 救场）",
    2015: "股灾",
    2018: "贸易战",
    2024: "924 反转",
}

COL_POSITION = "#0891b2"     # 仓位线
COL_RETURN = "#dc2626"       # CS300 涨跌线
COL_WIN = "#16a34a"
COL_LOSS = "#dc2626"
COL_NEUTRAL = "#9ca3af"
COL_ANNOTATE = "#1a1a2e"
GRID = "#dee2e6"
BG = "#f8f9fa"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"


def _excess_color(excess_pp: float) -> str:
    if abs(excess_pp) < 1.0:
        return COL_NEUTRAL
    return COL_WIN if excess_pp > 0 else COL_LOSS


def load_records() -> list[dict]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"找不到回测结果 {DATA_PATH}；请先运行 scripts/simulate_scorecard_20y.py"
        )
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))["yearly"]


def render(records: list[dict]) -> None:
    years = np.array([r["year"] for r in records])
    positions = np.array([r["target_equity_pct"] for r in records])
    returns = np.array([r["cs300_return_pct"] for r in records])
    excesses = np.array([r["annual_pnl_pct"] - r["cs300_return_pct"] for r in records])

    fig, (ax_excess, ax_main) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={"height_ratios": [1, 4], "hspace": 0.08},
        sharex=True,
    )
    fig.patch.set_facecolor("white")

    # ── 顶部：每年超额条形（vs 满仓 CS300） ─────
    ax_excess.set_facecolor(BG)
    ax_excess.grid(color=GRID, linewidth=0.5, alpha=0.7, axis="y")
    for spine in ax_excess.spines.values():
        spine.set_color(GRID)
    colors = [_excess_color(e) for e in excesses]
    ax_excess.bar(years, excesses, color=colors, alpha=0.85, edgecolor="none",
                  width=0.7)
    ax_excess.axhline(0, color="#475569", linewidth=0.6)
    for year, excess in zip(years, excesses):
        if abs(excess) < EXCESS_HIGHLIGHT_THRESHOLD_PP:
            continue
        ax_excess.text(year, excess + (1.2 if excess > 0 else -1.2),
                       f"{excess:+.0f}", fontsize=8,
                       ha="center", va="bottom" if excess > 0 else "top",
                       color=COL_ANNOTATE)
    ax_excess.set_ylabel("超额 (pp)\nvs 满仓 CS300", fontsize=9, color="#1f2937")
    ax_excess.set_title(
        f"V5.0 评分卡 21 年时序：年初目标仓位 vs 沪深 300 当年涨幅  "
        f"({years[0]}–{years[-1]})",
        fontsize=13, fontweight="bold", color="#1a1a2e", pad=10,
    )
    ax_excess.tick_params(colors="#495057", labelsize=8)

    # ── 主图：双 Y 轴折线 ────────────────────────
    ax_main.set_facecolor(BG)
    ax_main.grid(color=GRID, linewidth=0.5, alpha=0.7)
    for spine in ax_main.spines.values():
        spine.set_color(GRID)

    line_position, = ax_main.plot(
        years, positions, color=COL_POSITION, linewidth=2.2,
        marker="o", markersize=7, markerfacecolor="white",
        markeredgewidth=1.8, markeredgecolor=COL_POSITION,
        label="评分卡目标仓位 (%, 左轴)",
        zorder=4,
    )
    ax_main.axhline(NEUTRAL_POSITION_PCT, color=COL_POSITION,
                    linewidth=0.7, linestyle="--", alpha=0.5,
                    label=f"中性档位 {NEUTRAL_POSITION_PCT:.0f}%")
    ax_main.set_ylabel("评分卡目标仓位 (%)", fontsize=11, color=COL_POSITION)
    ax_main.set_ylim(0, 110)
    ax_main.tick_params(axis="y", colors=COL_POSITION, labelsize=9)
    ax_main.tick_params(axis="x", colors="#495057", labelsize=9)

    ax_right = ax_main.twinx()
    line_return, = ax_right.plot(
        years, returns, color=COL_RETURN, linewidth=2.0,
        marker="s", markersize=6, markerfacecolor=COL_RETURN,
        markeredgecolor="white", markeredgewidth=1.2,
        alpha=0.85,
        label="CS300 当年涨跌幅 (%, 右轴)",
        zorder=3,
    )
    ax_right.axhline(ZERO_RETURN_PCT, color=COL_RETURN,
                     linewidth=0.7, linestyle=":", alpha=0.6)
    ax_right.set_ylabel("沪深 300 当年涨跌幅 (%)", fontsize=11, color=COL_RETURN)
    # 设置对称范围：让 0% 大致落在中性档对应的位置
    return_abs_max = max(abs(returns.min()), abs(returns.max())) * 1.15
    ax_right.set_ylim(-return_abs_max, return_abs_max)
    ax_right.tick_params(axis="y", colors=COL_RETURN, labelsize=9)
    for spine in ax_right.spines.values():
        spine.set_color(GRID)

    # ── 关键年份注释 ──────────────────────────────
    for year, label in KEY_YEARS.items():
        if year not in years:
            continue
        idx = np.where(years == year)[0][0]
        ax_main.annotate(
            label,
            xy=(year, positions[idx]),
            xytext=(0, 22),
            textcoords="offset points",
            fontsize=8, color=COL_ANNOTATE, ha="center",
            arrowprops=dict(arrowstyle="-", color="#94a3b8",
                            linewidth=0.7, alpha=0.7),
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=GRID, alpha=0.95),
        )

    # ── X 轴：所有年份都显示 ─────────────────────
    ax_main.set_xticks(years)
    ax_main.set_xticklabels([str(y) for y in years], rotation=0)
    ax_main.set_xlabel("年份", fontsize=11, color="#1f2937")

    # ── 图例：合并双轴 ──────────────────────────
    handles = [line_position, line_return]
    labels = [h.get_label() for h in handles]
    ax_main.legend(handles, labels, loc="upper right",
                   fontsize=10, framealpha=0.95, edgecolor=GRID)

    # ── 底部数据来源 ─────────────────────────────
    fig.text(0.5, 0.005,
             f"数据源：{DATA_PATH.relative_to(ROOT)}  ·  "
             "顶部条形=评分卡组合年收益 − 满仓CS300年收益（pp）；绿=救到/赚到，红=拖后腿",
             ha="center", fontsize=8, color="#94a3b8")

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout(rect=(0, 0.02, 1, 1))
    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"已保存：{OUT_PNG}")


def print_alignment_summary(records: list[dict]) -> None:
    """方向一致性摘要：仓位 vs 中性档 与 CS300 vs 0% 的同号率。"""
    aligned, total = 0, 0
    misses = []
    for r in records:
        pos_signal = r["target_equity_pct"] - NEUTRAL_POSITION_PCT  # >0=超配, <0=减仓, =0=平
        ret_signal = r["cs300_return_pct"]
        if abs(pos_signal) < 0.1:
            continue
        total += 1
        if pos_signal * ret_signal > 0:
            aligned += 1
        else:
            misses.append((r["year"], pos_signal, ret_signal,
                           r["annual_pnl_pct"] - ret_signal))

    print("\n" + "═" * 78)
    print("仓位偏离中性档（±75%）与 CS300 涨跌方向是否同号")
    print("═" * 78)
    print(f"  有偏离的年份：{total}  |  方向一致：{aligned}  "
          f"({aligned/total*100 if total else 0:.0f}%)")
    if misses:
        print(f"\n  方向相反的年份（仓位偏离方向 ≠ CS300 涨跌方向）：")
        for year, ps, rs, excess in misses:
            direction = "超配" if ps > 0 else "减仓"
            cs_dir = "涨" if rs > 0 else "跌"
            print(f"    {year}  {direction} {abs(ps):+.0f}pp   CS300 {cs_dir} {rs:+.1f}%   "
                  f"超额 {excess:+.1f}pp")


def main() -> int:
    records = load_records()
    render(records)
    print_alignment_summary(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

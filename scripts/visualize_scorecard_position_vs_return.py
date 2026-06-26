#!/usr/bin/env python3.11
"""可视化 V5.0 评分卡 21 年「目标仓位 vs 当年沪深 300 涨幅」关系图

读取 data/backtests/scorecard_20y_simulation.json 后绘制单张散点图：
  - X 轴：当年沪深 300 涨跌幅 (%)
  - Y 轴：评分卡年初指示的目标仓位 (%)
  - 点颜色：相对"满仓 CS300"的超额收益（绿=救/赚到，红=拖后腿）
  - 点大小：超额收益绝对值（更大 = 决策影响更显著）
  - 每点附年份标签
  - 四象限参考线：x=0（CS300 涨跌分界）、y=75（评分卡中性档仓位）
  - 四象限注释：
      右上 = 涨且重仓（理想）
      右下 = 涨却轻仓（少赚）
      左上 = 跌却重仓（踩雷）
      左下 = 跌且轻仓（理想，最有价值）

输出：docs/assets/scorecard_position_vs_return.png
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "backtests" / "scorecard_20y_simulation.json"
OUT_PNG = ROOT / "docs" / "assets" / "scorecard_position_vs_return.png"

NEUTRAL_POSITION_PCT = 75.0          # 评分卡中性档位（评分 0 → 75% 仓位）
ZERO_RETURN_PCT = 0.0                # CS300 涨跌分界
EXCESS_SIZE_SCALE = 14.0             # 点大小：基础 + 系数 × |超额收益|
EXCESS_SIZE_BASE = 80.0
LABEL_OFFSET = (6, 6)                # 年份文字偏移（避免压住点）

# ── 颜色 ────────────────────────────────────────────
COL_WIN = "#16a34a"        # 决策正向（救到或赚到 vs 满仓）
COL_LOSS = "#dc2626"       # 决策负向（拖后腿 vs 满仓）
COL_NEUTRAL = "#9ca3af"    # 决策中性（|超额| < 1pp）
COL_NEUTRAL_THRESHOLD = 1.0
GRID = "#dee2e6"
BG = "#f8f9fa"
QUADRANT_LABEL_COLOR = "#475569"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"


def _classify_color(excess_pct: float) -> str:
    if abs(excess_pct) < COL_NEUTRAL_THRESHOLD:
        return COL_NEUTRAL
    return COL_WIN if excess_pct > 0 else COL_LOSS


def _excess_vs_fullstock(target_equity_pct: float, cs_return_pct: float,
                          cash_pnl_pct: float, annual_pnl_pct: float) -> float:
    """评分卡组合年收益 − 满仓 CS300 年收益。

    满仓基准 ≡ cs_return_pct（仓位 100%、现金贡献 0）；
    评分卡组合 = annual_pnl_pct（已含 equity_pnl + cash_pnl）。
    """
    return annual_pnl_pct - cs_return_pct


def load_records() -> list[dict]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"找不到回测结果 {DATA_PATH}；请先运行 scripts/simulate_scorecard_20y.py"
        )
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return payload["yearly"]


def render(records: list[dict]) -> None:
    years = [r["year"] for r in records]
    positions = [r["target_equity_pct"] for r in records]
    returns = [r["cs300_return_pct"] for r in records]
    excesses = [
        _excess_vs_fullstock(
            r["target_equity_pct"], r["cs300_return_pct"],
            r["cash_pnl_pct"], r["annual_pnl_pct"],
        )
        for r in records
    ]

    fig, ax = plt.subplots(figsize=(14, 9))
    fig.patch.set_facecolor("white")
    ax.set_facecolor(BG)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.85)
    for spine in ax.spines.values():
        spine.set_color(GRID)

    # ── 四象限参考线 ──────────────────────────────
    ax.axvline(ZERO_RETURN_PCT, color="#475569", linewidth=0.9, alpha=0.7)
    neutral_line = ax.axhline(
        NEUTRAL_POSITION_PCT, color="#475569", linewidth=0.9,
        linestyle="--", alpha=0.7,
        label=f"评分卡中性档位 {NEUTRAL_POSITION_PCT:.0f}%",
    )

    # ── 散点（颜色 = 超额方向，大小 = 超额绝对值）─
    sizes = [EXCESS_SIZE_BASE + EXCESS_SIZE_SCALE * abs(e) for e in excesses]
    colors = [_classify_color(e) for e in excesses]
    ax.scatter(returns, positions, s=sizes, c=colors,
               alpha=0.78, edgecolor="white", linewidth=1.2, zorder=3)

    # ── 年份标签 ──────────────────────────────────
    for x_val, y_val, year, excess in zip(returns, positions, years, excesses):
        label = f"{year}\n({excess:+.0f}pp)"
        ax.annotate(label, xy=(x_val, y_val), xytext=LABEL_OFFSET,
                    textcoords="offset points",
                    fontsize=8, color="#1f2937",
                    ha="left", va="bottom")

    # ── 四象限注释 ────────────────────────────────
    x_min, x_max = min(returns) - 8, max(returns) + 12
    y_min, y_max = -5, 105
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)

    pad_x = (x_max - x_min) * 0.02
    pad_y = (y_max - y_min) * 0.025
    quadrants = [
        # (x, y, text, ha, va)
        (x_max - pad_x, y_max - pad_y, "▲ 右上：涨且重仓（理想）", "right", "top"),
        (x_min + pad_x, y_max - pad_y, "× 左上：跌却重仓（踩雷）", "left", "top"),
        (x_min + pad_x, y_min + pad_y, "★ 左下：跌且轻仓（最有价值）", "left", "bottom"),
        (x_max - pad_x, y_min + pad_y, "△ 右下：涨却轻仓（少赚）", "right", "bottom"),
    ]
    for x_val, y_val, text, ha, va in quadrants:
        ax.text(x_val, y_val, text, fontsize=10, color=QUADRANT_LABEL_COLOR,
                ha=ha, va=va, alpha=0.9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35",
                          facecolor="white", edgecolor=GRID, alpha=0.9))

    # ── 轴/标题/图例 ─────────────────────────────
    ax.set_xlabel("沪深 300 当年涨跌幅 (%)", fontsize=11, color="#1f2937")
    ax.set_ylabel("评分卡年初目标仓位 (%)", fontsize=11, color="#1f2937")
    ax.set_title(
        f"V5.0 评分卡：年初目标仓位 vs 沪深 300 当年涨幅（{years[0]}–{years[-1]}, "
        f"{len(records)} 年）\n"
        "点大小 = |超额收益 vs 满仓 CS300|   绿=评分卡赚到/救到   红=拖后腿   灰=接近持平",
        fontsize=13, fontweight="bold", color="#1a1a2e", pad=14,
    )

    # 图例：手工画三色 dot
    handles = [
        plt.scatter([], [], s=140, c=COL_WIN, alpha=0.78, edgecolor="white",
                    linewidth=1.2, label=f"决策正向（超额 ≥ +{COL_NEUTRAL_THRESHOLD:.0f}pp）"),
        plt.scatter([], [], s=140, c=COL_LOSS, alpha=0.78, edgecolor="white",
                    linewidth=1.2, label=f"决策负向（超额 ≤ -{COL_NEUTRAL_THRESHOLD:.0f}pp）"),
        plt.scatter([], [], s=140, c=COL_NEUTRAL, alpha=0.78, edgecolor="white",
                    linewidth=1.2, label=f"决策中性（|超额| < {COL_NEUTRAL_THRESHOLD:.0f}pp）"),
    ]
    ax.legend(
        handles=handles + [neutral_line],
        loc="lower right", fontsize=9, framealpha=0.95,
        edgecolor=GRID,
    )

    # ── 底部数据来源 ──────────────────────────────
    fig.text(0.5, 0.015,
             f"数据源：{DATA_PATH.relative_to(ROOT)}（由 scripts/simulate_scorecard_20y.py 生成）  ·  "
             "P&L = 仓位% × CS300年涨幅 + (1-仓位%) × 2% 现金年化",
             ha="center", fontsize=8, color="#94a3b8")

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout(rect=(0, 0.03, 1, 1))
    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"已保存：{OUT_PNG}")


def print_quadrant_summary(records: list[dict]) -> None:
    """文字版四象限统计，方便看图前先抓主结论。"""
    quadrants = {"右上": [], "左上": [], "左下": [], "右下": []}
    for r in records:
        ret_pct = r["cs300_return_pct"]
        pos_pct = r["target_equity_pct"]
        excess = r["annual_pnl_pct"] - ret_pct
        if ret_pct >= ZERO_RETURN_PCT and pos_pct >= NEUTRAL_POSITION_PCT:
            quadrants["右上"].append((r["year"], pos_pct, ret_pct, excess))
        elif ret_pct < ZERO_RETURN_PCT and pos_pct >= NEUTRAL_POSITION_PCT:
            quadrants["左上"].append((r["year"], pos_pct, ret_pct, excess))
        elif ret_pct < ZERO_RETURN_PCT and pos_pct < NEUTRAL_POSITION_PCT:
            quadrants["左下"].append((r["year"], pos_pct, ret_pct, excess))
        else:
            quadrants["右下"].append((r["year"], pos_pct, ret_pct, excess))

    names = {
        "右上": "涨且重仓（理想）",
        "左上": "跌却重仓（踩雷）",
        "左下": "跌且轻仓（最有价值）",
        "右下": "涨却轻仓（少赚）",
    }
    print("\n" + "═" * 78)
    print("仓位 × 当年涨幅 — 四象限分布（中性档 75% / CS300 0% 为分界）")
    print("═" * 78)
    for quadrant_key, label in names.items():
        rows = quadrants[quadrant_key]
        total_excess = sum(e for _, _, _, e in rows)
        print(f"\n  【{quadrant_key} · {label}】 共 {len(rows)} 年  "
              f"累计超额 {total_excess:+.1f}pp")
        for year, pos_pct, ret_pct, excess in sorted(rows, key=lambda x: x[0]):
            print(f"    {year}  仓位 {pos_pct:>5.1f}%   CS300 {ret_pct:>+7.1f}%   "
                  f"超额 {excess:>+6.1f}pp")


def main() -> int:
    records = load_records()
    render(records)
    print_quadrant_summary(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

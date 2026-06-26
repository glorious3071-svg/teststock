#!/usr/bin/env python3.11
"""可视化 v3.4.11 评分卡映射对比 — baseline / P1a 阶梯 / P1b sigmoid

读取 data/backtests/scorecard_mapping_comparison.json，绘三面板对比：
  - 顶：21 年净值曲线（同初值 100 万复利）— 直观看终值差距
  - 中：每年目标仓位时序 — 直观看档位丰富度（baseline 卡 75%）
  - 底：评分→仓位映射函数曲线 — 直观看 3 条 mapping 的形状差异

输出：docs/assets/scorecard_mapping_comparison.png

设计意图：所有信息浓缩到一张图，spec §四 引用即可，无需翻多个产物。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "backtests" / "scorecard_mapping_comparison.json"
OUT_PNG = ROOT / "docs" / "assets" / "scorecard_mapping_comparison.png"

# ─── P1b sigmoid 参数（与回测脚本保持一致；改之需双侧同步）─
P1B_BASE_POSITION_PCT = 75.0
P1B_AMPLITUDE_PP = 40.0
P1B_SCALE = 4.0
P1B_CEILING_PCT = 95.0
P1B_FLOOR_PCT = 20.0

# ─── 评分扫描范围（覆盖 21 年实测 -9 ~ +10 并留余量）─
SCORE_PLOT_MIN = -12
SCORE_PLOT_MAX = 14
NEUTRAL_SCORE = 0
NEUTRAL_POSITION_PCT = 75.0
INITIAL_CAPITAL_WAN = 100.0     # 万元

# ─── 配色（与既有 visualize 脚本一致）─────────────────
COL_BASELINE = "#9ca3af"
COL_P1A = "#0891b2"
COL_P1B = "#dc2626"
GRID = "#dee2e6"
BG = "#f8f9fa"
COL_ANNOTATE = "#1a1a2e"

STRATEGY_DISPLAY = {
    "baseline": ("baseline (旧 8 档)", COL_BASELINE, "o"),
    "p1a_ladder": ("P1a 12 档阶梯 ★ 已采纳", COL_P1A, "s"),
    "p1b_sigmoid": ("P1b sigmoid 平滑", COL_P1B, "D"),
}

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"


def _baseline_mapping(score: int) -> float:
    """复刻 backtest.scorecard.score_to_target_equity v3.4.10 之前的旧 8 档。

    入库时已被覆盖，此处保留以画对比曲线（不 import 防止误用）。
    """
    if score <= -10: return 90.0
    if score <= -5:  return 80.0
    if score < 0:    return 75.0
    if score <= 3:   return 75.0
    if score <= 6:   return 60.0
    if score <= 9:   return 50.0
    if score <= 12:  return 30.0
    return 20.0


def _p1a_mapping(score: int) -> float:
    if score <= -10: return 95.0
    if score <= -7:  return 90.0
    if score <= -4:  return 85.0
    if score <= -1:  return 80.0
    if score == 0:   return 75.0
    if score <= 3:   return 70.0
    if score <= 6:   return 60.0
    if score <= 9:   return 50.0
    if score <= 12:  return 30.0
    return 20.0


def _p1b_mapping(score: int) -> float:
    raw = P1B_BASE_POSITION_PCT - P1B_AMPLITUDE_PP * math.tanh(score / P1B_SCALE)
    return max(P1B_FLOOR_PCT, min(P1B_CEILING_PCT, raw))


MAPPING_FUNCS = {
    "baseline": _baseline_mapping,
    "p1a_ladder": _p1a_mapping,
    "p1b_sigmoid": _p1b_mapping,
}


def load_payload() -> dict:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"找不到回测结果 {DATA_PATH}；请先运行 scripts/backtest_scorecard_mapping.py"
        )
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def compute_equity_curve(annual_pnl_pcts: list[float],
                          initial_wan: float = INITIAL_CAPITAL_WAN) -> np.ndarray:
    """复利串行得到净值曲线（含初始点）。"""
    equity = [initial_wan]
    for pnl in annual_pnl_pcts:
        equity.append(equity[-1] * (1.0 + pnl / 100.0))
    return np.array(equity)


def render(payload: dict) -> None:
    yearly = payload["yearly"]
    metrics = payload["metrics"]
    criteria = payload["criteria"]
    winner = payload["winner"]

    years = np.array([row["year"] for row in yearly])
    equity_x = np.array([years[0] - 1] + list(years))   # 含初始点的横轴

    fig, (ax_equity, ax_position, ax_mapping) = plt.subplots(
        3, 1, figsize=(15, 12),
        gridspec_kw={"height_ratios": [2.4, 1.6, 1.6], "hspace": 0.35},
    )
    fig.patch.set_facecolor("white")

    # ── 面板 1：净值曲线 ─────────────────────────
    ax_equity.set_facecolor(BG)
    ax_equity.grid(color=GRID, linewidth=0.5, alpha=0.7)
    for spine in ax_equity.spines.values():
        spine.set_color(GRID)

    for name, (label, color, marker) in STRATEGY_DISPLAY.items():
        annual_pnls = [row["strategies"][name]["annual_pnl_pct"] for row in yearly]
        curve = compute_equity_curve(annual_pnls)
        linewidth = 2.6 if name == winner else 1.8
        alpha = 1.0 if name == winner else 0.85
        ax_equity.plot(equity_x, curve, color=color, linewidth=linewidth,
                       marker=marker, markersize=5, markerfacecolor="white",
                       markeredgewidth=1.4, markeredgecolor=color,
                       label=f"{label}  终值 {curve[-1]:.1f} 万",
                       alpha=alpha, zorder=4 if name == winner else 3)

    ax_equity.axhline(INITIAL_CAPITAL_WAN, color="#475569",
                      linewidth=0.6, linestyle=":", alpha=0.6)
    ax_equity.set_ylabel("组合净值（万元，初始 100）", fontsize=11, color="#1f2937")
    ax_equity.set_title(
        f"V3.4.11 评分卡映射函数三方对比  ·  {years[0]}–{years[-1]}  ·  "
        f"赢家：{STRATEGY_DISPLAY[winner][0]}",
        fontsize=14, fontweight="bold", color=COL_ANNOTATE, pad=12,
    )
    ax_equity.legend(loc="upper left", fontsize=10, framealpha=0.95, edgecolor=GRID)
    ax_equity.tick_params(colors="#495057", labelsize=9)

    # 顶部右侧：4 项指标速览方块
    summary_text = _build_metrics_box(metrics, criteria, winner)
    ax_equity.text(
        0.985, 0.04, summary_text,
        transform=ax_equity.transAxes,
        fontsize=9, color=COL_ANNOTATE,
        ha="right", va="bottom",
        bbox=dict(boxstyle="round,pad=0.55", facecolor="white",
                  edgecolor=GRID, alpha=0.95),
    )

    # ── 面板 2：每年仓位时序 ───────────────────────
    ax_position.set_facecolor(BG)
    ax_position.grid(color=GRID, linewidth=0.5, alpha=0.7)
    for spine in ax_position.spines.values():
        spine.set_color(GRID)

    for name, (label, color, marker) in STRATEGY_DISPLAY.items():
        positions = [row["strategies"][name]["target_equity_pct"] for row in yearly]
        linewidth = 2.0 if name == winner else 1.4
        alpha = 1.0 if name == winner else 0.7
        ax_position.plot(years, positions, color=color, linewidth=linewidth,
                         marker=marker, markersize=5, markerfacecolor="white",
                         markeredgewidth=1.2, markeredgecolor=color,
                         label=label, alpha=alpha,
                         zorder=4 if name == winner else 3)

    ax_position.axhline(NEUTRAL_POSITION_PCT, color="#475569",
                        linewidth=0.6, linestyle="--", alpha=0.5)
    ax_position.set_ylabel("每年目标仓位 (%)", fontsize=11, color="#1f2937")
    ax_position.set_ylim(15, 100)
    ax_position.set_xticks(years)
    ax_position.set_xticklabels([str(y) for y in years], rotation=0, fontsize=8)
    ax_position.tick_params(colors="#495057", labelsize=9)

    # 仓位档位丰富度速览
    rich_text = _build_position_richness(yearly)
    ax_position.text(
        0.985, 0.04, rich_text,
        transform=ax_position.transAxes,
        fontsize=9, color=COL_ANNOTATE,
        ha="right", va="bottom",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white",
                  edgecolor=GRID, alpha=0.95),
    )

    # ── 面板 3：映射函数曲线（评分 → 仓位）─────────
    ax_mapping.set_facecolor(BG)
    ax_mapping.grid(color=GRID, linewidth=0.5, alpha=0.7)
    for spine in ax_mapping.spines.values():
        spine.set_color(GRID)

    scores = np.arange(SCORE_PLOT_MIN, SCORE_PLOT_MAX + 1)
    for name, (label, color, marker) in STRATEGY_DISPLAY.items():
        mapping_fn = MAPPING_FUNCS[name]
        positions = [mapping_fn(int(s)) for s in scores]
        linewidth = 2.2 if name == winner else 1.6
        alpha = 1.0 if name == winner else 0.75
        ax_mapping.plot(scores, positions, color=color, linewidth=linewidth,
                        marker=marker, markersize=4.5, markerfacecolor=color,
                        markeredgecolor="white", markeredgewidth=0.8,
                        label=label, alpha=alpha,
                        zorder=4 if name == winner else 3)

    # 评分实测分布范围标注
    actual_scores = [row["score"] for row in yearly]
    actual_min, actual_max = min(actual_scores), max(actual_scores)
    ax_mapping.axvspan(actual_min, actual_max,
                       color="#fde68a", alpha=0.25,
                       label=f"21 年实测评分区间 [{actual_min}, {actual_max}]")
    ax_mapping.axvline(NEUTRAL_SCORE, color="#475569",
                       linewidth=0.6, linestyle=":", alpha=0.6)
    ax_mapping.axhline(NEUTRAL_POSITION_PCT, color="#475569",
                       linewidth=0.6, linestyle=":", alpha=0.6)

    ax_mapping.set_xlabel("评分（risk − opportunity）", fontsize=11, color="#1f2937")
    ax_mapping.set_ylabel("目标仓位 (%)", fontsize=11, color="#1f2937")
    ax_mapping.set_xlim(SCORE_PLOT_MIN - 0.5, SCORE_PLOT_MAX + 0.5)
    ax_mapping.set_ylim(15, 100)
    ax_mapping.set_xticks(scores[::2])
    ax_mapping.legend(loc="upper right", fontsize=9, framealpha=0.95, edgecolor=GRID)
    ax_mapping.tick_params(colors="#495057", labelsize=9)
    ax_mapping.set_title("映射函数对比：评分 → 目标仓位",
                         fontsize=11, color=COL_ANNOTATE, pad=8)

    # ── 底部数据来源 ─────────────────────────────
    fig.text(
        0.5, 0.005,
        f"数据源：{DATA_PATH.relative_to(ROOT)}  ·  生成脚本：{Path(__file__).relative_to(ROOT)}  ·  "
        "回测：初始 100 万 / 现金年化 2% / CS300 全年持仓",
        ha="center", fontsize=8, color="#94a3b8",
    )

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"已保存：{OUT_PNG}")


def _build_metrics_box(metrics: dict, criteria: dict, winner: str) -> str:
    """4 维指标 + 采纳判定的速览方块（PingFang HK 渲染，避开缺失符号）。

    省略 check/cross 符号（在 PingFang HK 中缺失），ADOPT/PARTIAL/REJECT 标签
    已传达整体判定，单项 PASS/FAIL 由 Δ 数字方向反映。
    """
    base = metrics["baseline"]
    p1a = metrics["p1a_ladder"]
    p1b = metrics["p1b_sigmoid"]
    crit_a = criteria["p1a_ladder"]
    crit_b = criteria["p1b_sigmoid"]

    def _block(label: str, m: dict, crit: dict) -> list[str]:
        return [
            f"{label}  →  {crit['pass_count']}/4 {crit['decision']}",
            f"  累计回报 Δ {m['cumulative_return_pct'] - base['cumulative_return_pct']:+.1f}pp  "
            f"(门槛 +5pp)",
            f"  最大回撤 Δ {abs(m['max_drawdown_pct']) - abs(base['max_drawdown_pct']):+.1f}pp  "
            f"(门槛 ≤+5pp)",
            f"  年化波动 Δ {m['annualized_vol_pct'] - base['annualized_vol_pct']:+.2f}pp  "
            f"(门槛 ≤+3pp)",
            f"  Calmar   Δ {m['calmar'] - base['calmar']:+.3f}  "
            f"(门槛 ≥0)",
        ]

    lines = ["采纳判定 (vs baseline)", ""]
    lines += _block("P1a 阶梯 ★", p1a, crit_a)
    lines += [""]
    lines += _block("P1b sigmoid", p1b, crit_b)
    return "\n".join(lines)


def _build_position_richness(yearly: list[dict]) -> str:
    """3 策略仓位档位分布速览（短行紧凑）。"""
    lines = ["仓位档位分布 (21 年)"]
    for name, (label, _, _) in STRATEGY_DISPLAY.items():
        positions = [row["strategies"][name]["target_equity_pct"] for row in yearly]
        unique_count = len(set(round(p, 1) for p in positions))
        lines.append(
            f"  {name}: {unique_count} 档  "
            f"[{min(positions):.0f}%, {max(positions):.0f}%]"
        )
    return "\n".join(lines)


def print_year_diff_table(payload: dict) -> None:
    """终端打印 21 年逐行仓位/P&L 差异（与 spec §十一 v3.4.11 关键年份表对齐）。"""
    yearly = payload["yearly"]
    print("\n" + "═" * 90)
    print("21 年逐行：P1a 阶梯 vs baseline 的仓位与 P&L 差异")
    print("═" * 90)
    header = (f"{'年份':<6}{'CS300%':>9}{'评分':>6}  "
              f"{'base仓':>7}{'P1a仓':>7}  "
              f"{'baseP&L':>9}{'P1aP&L':>9}  {'ΔP&L':>8}")
    print(header)
    print("-" * 90)
    total_delta = 0.0
    for row in yearly:
        b = row["strategies"]["baseline"]
        a = row["strategies"]["p1a_ladder"]
        delta = a["annual_pnl_pct"] - b["annual_pnl_pct"]
        total_delta += delta
        flag = " ★" if abs(delta) >= 5.0 else ""
        print(f"{row['year']:<6}{row['cs300_return_pct']:>+8.1f}%"
              f"{row['score']:>+6}  "
              f"{b['target_equity_pct']:>6.0f}%{a['target_equity_pct']:>6.0f}%  "
              f"{b['annual_pnl_pct']:>+8.1f}%{a['annual_pnl_pct']:>+8.1f}%  "
              f"{delta:>+7.1f}pp{flag}")
    print("-" * 90)
    print(f"年度 ΔP&L 累加：{total_delta:+.1f}pp  "
          f"（复利后净值差异见图表面板 1）")


def main() -> int:
    payload = load_payload()
    render(payload)
    print_year_diff_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

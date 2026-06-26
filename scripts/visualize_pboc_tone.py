#!/usr/bin/env python3.11
"""央行口径（pboc_tone）真实性验证与可视化（2006-2026）

四张子图：
  ① 21 年央行口径时序色块（双轨对比 cewc_annual vs LLM 抽取）
  ② SHIBOR_3M 连续曲线 + 按口径上色背景（验证"宽松年利率是否真低"）
  ③ RRR 累计变动 + 1Y 定存基准利率 双轴（按口径上色背景）
  ④ 沪深300 年回报 vs 央行口径 散点（验证宽松年股市是否更优）
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "pboc_tone_validation.png"

# ── 颜色 ─────────────────────────────────────────────────────
COLOR_TIGHT   = "#dc2626"   # 从紧 红
COLOR_NEUTRAL = "#94a3b8"   # 稳健 灰
COLOR_LOOSE   = "#16a34a"   # 适度宽松 绿
COLOR_UNKNOWN = "#e5e7eb"   # 缺失/不一致 浅灰

DARK_BG  = "#f8f9fa"
GRID_COL = "#dee2e6"

TONE_COLOR = {
    "tight":   COLOR_TIGHT,
    "neutral": COLOR_NEUTRAL,
    "loose":   COLOR_LOOSE,
    None:      COLOR_UNKNOWN,
}

CS300_CODE = "000300.SH"


# ── 三态归一化 ───────────────────────────────────────────────
def normalize_tone(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if "紧" in s:
        return "tight"
    if "适度宽松" in s or "宽松" in s:
        return "loose"
    if "稳健" in s or "中性" in s:
        return "neutral"
    return None


# ── 数据加载 ──────────────────────────────────────────────────
def mysql_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"), charset="utf8mb4",
    )


def load_data() -> dict:
    conn = mysql_conn()
    cur = conn.cursor()

    # 1) cewc_annual 口径 + LLM 抽取的口径
    cur.execute("""
        SELECT a.apply_year, a.monetary_policy,
               (SELECT tag_value FROM cewc_tags
                WHERE apply_year = a.apply_year
                  AND tag_category='policy_stance' AND tag_name='monetary'
                  AND prompt_version='v2'
                ORDER BY confidence DESC LIMIT 1)
        FROM cewc_annual a ORDER BY a.apply_year
    """)
    tone_rows = [(int(y), normalize_tone(a), normalize_tone(l), a, l)
                 for y, a, l in cur.fetchall()]

    # 2) shibor_3m 月度均值
    shibor = pd.read_sql(
        "SELECT trade_date, rate_3m FROM shibor_daily ORDER BY trade_date",
        conn,
    )
    shibor["trade_date"] = pd.to_datetime(shibor["trade_date"])
    shibor["rate_3m"] = shibor["rate_3m"].astype(float)
    shibor_m = (shibor.set_index("trade_date")
                .resample("ME")["rate_3m"].mean().to_frame())

    # 3) cn_deposit_rate 月度阶梯
    cur.execute("SELECT effective_date, rate_after_pct FROM cn_deposit_rate ORDER BY effective_date")
    deposit_events = [(d, float(r)) for d, r in cur.fetchall()]

    # 4) cn_rrr_changes 12 月滚动累计
    rrr = pd.read_sql(
        """SELECT effective_date, rrr_change_pp FROM cn_rrr_changes
           WHERE inst_type IN ('large','all') ORDER BY effective_date""",
        conn,
    )
    rrr["effective_date"] = pd.to_datetime(rrr["effective_date"])
    rrr["rrr_change_pp"] = rrr["rrr_change_pp"].astype(float)

    # 5) 沪深300 年度回报
    cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM index_daily WHERE ts_code = %s",
                (CS300_CODE,))
    mn, mx = cur.fetchone()
    annual_ret = {}
    for y in range(2006, 2027):
        cur.execute("""SELECT close FROM index_daily WHERE ts_code=%s AND trade_date >= %s
                       ORDER BY trade_date ASC LIMIT 1""", (CS300_CODE, f"{y}-01-01"))
        o = cur.fetchone()
        cur.execute("""SELECT close FROM index_daily WHERE ts_code=%s AND trade_date <= %s
                       ORDER BY trade_date DESC LIMIT 1""", (CS300_CODE, f"{y}-12-31"))
        c = cur.fetchone()
        if o and c and o[0] and c[0]:
            annual_ret[y] = (float(c[0])/float(o[0]) - 1.0) * 100.0
    conn.close()

    return {
        "tones": tone_rows,
        "shibor_m": shibor_m,
        "deposit_events": deposit_events,
        "rrr": rrr,
        "annual_ret": annual_ret,
    }


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.6)


# ── 子图 ① 央行口径双轨色块 ──────────────────────────────────
def plot_tone_consistency(ax: plt.Axes, data: dict) -> tuple[int, int]:
    style_ax(ax)
    years = [r[0] for r in data["tones"]]
    annual_tones = [r[1] for r in data["tones"]]
    llm_tones    = [r[2] for r in data["tones"]]

    matches = 0
    for i, (y, a, l) in enumerate(zip(years, annual_tones, llm_tones)):
        ca = TONE_COLOR.get(a, COLOR_UNKNOWN)
        cl = TONE_COLOR.get(l, COLOR_UNKNOWN)
        # 上轨 cewc_annual
        ax.add_patch(mpatches.Rectangle((i - 0.4, 0.55), 0.8, 0.4,
                                          facecolor=ca, edgecolor="#1a1a2e", linewidth=0.5))
        # 下轨 LLM v2
        ax.add_patch(mpatches.Rectangle((i - 0.4, 0.05), 0.8, 0.4,
                                          facecolor=cl, edgecolor="#1a1a2e", linewidth=0.5))
        match = (a == l)
        matches += int(match)
        # 不一致打 ✗
        if not match:
            ax.text(i, 0.50, "✗", ha="center", va="center",
                    color="#dc2626", fontsize=10, fontweight="bold")

    ax.text(-1.3, 0.75, "cewc_annual", ha="right", va="center",
            fontsize=8, color="#1a1a2e")
    ax.text(-1.3, 0.25, "LLM v2",      ha="right", va="center",
            fontsize=8, color="#1a1a2e")

    ax.set_xticks(range(len(years)))
    ax.set_xticklabels([str(y) for y in years], rotation=45, fontsize=7)
    ax.set_xlim(-1.5, len(years) - 0.5)
    ax.set_ylim(0, 1)
    ax.set_yticks([])

    handles = [
        mpatches.Patch(color=COLOR_TIGHT,   label="从紧"),
        mpatches.Patch(color=COLOR_NEUTRAL, label="稳健"),
        mpatches.Patch(color=COLOR_LOOSE,   label="适度宽松"),
        mpatches.Patch(color=COLOR_UNKNOWN, label="未识别"),
    ]
    ax.legend(handles=handles, loc="upper right", fontsize=7, ncol=4,
              framealpha=0.9)
    rate = matches / len(years) * 100
    ax.set_title(f"① 央行口径双轨核验 — cewc_annual vs LLM v2（一致率 {matches}/{len(years)} = {rate:.0f}%）",
                 fontsize=10, color="#1a1a2e", pad=8, fontweight="bold")
    return matches, len(years)


# ── 子图 ② SHIBOR_3M + 口径色带 ──────────────────────────────
def plot_shibor_vs_tone(ax: plt.Axes, data: dict) -> None:
    style_ax(ax)
    sh = data["shibor_m"]
    ax.plot(sh.index, sh["rate_3m"], color="#1a1a2e", linewidth=1.0,
            label="SHIBOR 3M（月均）", zorder=10)

    # 按 apply_year 上色背景（apply_year=Y 对应日历年 Y）
    for y, a, _, _, _ in data["tones"]:
        color = TONE_COLOR.get(a, COLOR_UNKNOWN)
        ax.axvspan(pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31"),
                   alpha=0.20, color=color, zorder=0)

    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(pd.Timestamp("2006-10-01"), pd.Timestamp("2026-06-30"))
    ax.set_ylabel("SHIBOR 3M (%)", color="#1a1a2e", fontsize=9)
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    ax.set_title("② SHIBOR 3M 月均 vs cewc_annual 口径背景（验证「宽松年利率是否真低」）",
                 fontsize=10, color="#1a1a2e", pad=8, fontweight="bold")


# ── 子图 ③ RRR + 1Y 定存 + 口径色带 ───────────────────────────
def plot_rrr_deposit_vs_tone(ax: plt.Axes, data: dict) -> None:
    style_ax(ax)

    # RRR 累计：从 2006 开始的累计调整
    rrr = data["rrr"].copy()
    rrr = rrr[rrr.effective_date >= pd.Timestamp("2006-01-01")]
    rrr["cum"] = rrr["rrr_change_pp"].cumsum()
    ax.step(rrr.effective_date, rrr["cum"], color="#7c3aed",
            linewidth=1.2, where="post", label="RRR 累计变动（large）", zorder=10)
    ax.axhline(0, color="#1a1a2e", linewidth=0.6)
    ax.set_ylabel("RRR 累计变动 (pp)", color="#7c3aed", fontsize=9)
    ax.set_ylim(-10, 16)
    ax.tick_params(axis="y", labelcolor="#7c3aed")

    # 1Y 定存基准（阶梯）
    ax2 = ax.twinx()
    dep = data["deposit_events"]
    if dep:
        xs = [d for d, _ in dep] + [pd.Timestamp("2026-12-31").date()]
        ys = [r for _, r in dep] + [dep[-1][1]]
        ax2.step(xs, ys, color="#f59e0b", linewidth=1.2, where="post",
                 label="1Y 定存基准", zorder=10)
    ax2.set_ylabel("1Y 定存利率 (%)", color="#f59e0b", fontsize=9)
    ax2.tick_params(axis="y", labelcolor="#f59e0b")
    ax2.spines["right"].set_color(GRID_COL)

    # 背景按口径
    for y, a, _, _, _ in data["tones"]:
        color = TONE_COLOR.get(a, COLOR_UNKNOWN)
        ax.axvspan(pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31"),
                   alpha=0.20, color=color, zorder=0)

    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(pd.Timestamp("2006-01-01"), pd.Timestamp("2026-06-30"))

    l1, lab1 = ax.get_legend_handles_labels()
    l2, lab2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lab1 + lab2, loc="upper left", fontsize=7, framealpha=0.9)
    ax.set_title("③ RRR 累计变动 + 1Y 定存基准 vs 口径背景（验证「紧时是否真在加准/加息」）",
                 fontsize=10, color="#1a1a2e", pad=8, fontweight="bold")


# ── 子图 ④ 沪深300 年回报 vs 央行口径 ────────────────────────
def plot_returns_vs_tone(ax: plt.Axes, data: dict) -> dict:
    style_ax(ax)
    tone_to_x = {"tight": -1, "neutral": 0, "loose": 1, None: None}
    rets = data["annual_ret"]

    grouped: dict[str, list[float]] = {"tight": [], "neutral": [], "loose": []}
    for y, a, _, _, _ in data["tones"]:
        if a not in grouped: continue
        r = rets.get(y)
        if r is None: continue
        grouped[a].append(r)
        ax.scatter(tone_to_x[a] + (hash(y) % 30 - 15) / 60.0, r,
                   color=TONE_COLOR[a], s=70, alpha=0.7, edgecolor="#1a1a2e",
                   linewidth=0.6, zorder=10)
        ax.annotate(f"{y}", xy=(tone_to_x[a] + (hash(y) % 30 - 15) / 60.0, r),
                    xytext=(5, 3), textcoords="offset points",
                    fontsize=6.5, color="#495057")

    # 均值线 + 箱型
    for tone, xs in [("tight", -1), ("neutral", 0), ("loose", 1)]:
        vals = grouped[tone]
        if vals:
            avg = sum(vals) / len(vals)
            ax.scatter([xs], [avg], marker="*", s=300,
                       color=TONE_COLOR[tone], edgecolor="#1a1a2e", linewidth=1.0,
                       zorder=20)
            ax.text(xs, avg + 8, f"均值 {avg:+.1f}%\nn={len(vals)}",
                    ha="center", color=TONE_COLOR[tone], fontsize=8,
                    fontweight="bold")

    ax.axhline(0, color="#1a1a2e", linewidth=0.7, linestyle="--", alpha=0.5)
    ax.set_xticks([-1, 0, 1])
    ax.set_xticklabels(["从紧", "稳健", "适度宽松"], fontsize=10)
    ax.set_xlim(-1.6, 1.6)
    ax.set_ylim(-80, 130)
    ax.set_ylabel("沪深300 年回报 (%)", color="#1a1a2e", fontsize=9)
    ax.set_title("④ 沪深300 年回报 vs 央行口径（验证「宽松年股市是否更优」）",
                 fontsize=10, color="#1a1a2e", pad=8, fontweight="bold")
    return {t: (sum(v)/len(v) if v else None, len(v)) for t, v in grouped.items()}


# ── 主流程 ────────────────────────────────────────────────────
def main() -> None:
    data = load_data()

    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor("white")
    fig.suptitle("央行口径（pboc_tone）真实性验证（2006-2026）",
                 fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985)

    ax1 = fig.add_axes([0.07, 0.78, 0.88, 0.13])
    ax2 = fig.add_axes([0.07, 0.48, 0.88, 0.22])
    ax3 = fig.add_axes([0.07, 0.20, 0.88, 0.22])
    ax4 = fig.add_axes([0.07, 0.04, 0.42, 0.11])

    # 改成 2 行 1 列 + 底部 1 个，更合理布局
    fig.clf()
    fig.patch.set_facecolor("white")
    fig.suptitle("央行口径（pboc_tone）真实性验证（2006-2026）",
                 fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985)
    ax1 = fig.add_axes([0.06, 0.80, 0.90, 0.12])
    ax2 = fig.add_axes([0.06, 0.52, 0.90, 0.22])
    ax3 = fig.add_axes([0.06, 0.24, 0.90, 0.22])
    ax4 = fig.add_axes([0.06, 0.04, 0.40, 0.16])

    matches, total = plot_tone_consistency(ax1, data)
    plot_shibor_vs_tone(ax2, data)
    plot_rrr_deposit_vs_tone(ax3, data)
    stats = plot_returns_vs_tone(ax4, data)

    # 底部摘要文字
    summary_lines = [
        f"一致率（cewc_annual vs LLM v2）：{matches}/{total} = {matches/total*100:.1f}%",
        f"  不一致年份：2006/2007（半全文未明示口径，LLM 未抽到）、2025（cewc_annual 标'稳健'，LLM 标'适度宽松'； 实际 12 月会议定调'适度宽松'，cewc_annual 数据需校正）",
    ]
    s_avg = "  沪深300 年回报均值 — "
    for t, (avg, n) in stats.items():
        if avg is not None:
            s_avg += f"{t}（n={n}）={avg:+.1f}%   "
    summary_lines.append(s_avg)
    fig.text(0.50, 0.18, "\n".join(summary_lines), ha="left", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")


if __name__ == "__main__":
    main()

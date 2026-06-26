#!/usr/bin/env python3.11
"""可视化与特征分析：美联储利率政策调整（1982-2025）

四个面板：
  ① 主时序：FFR 水平 + 加息/降息周期着色 + 关键事件
  ② 单次决议幅度分布：直方图按 -100/-75/-50/-25/0/+25/+50/+75 bp 分桶
  ③ 加息周期对齐对比：把过去 5 次加息周期对齐到 t=0，比较累计幅度
  ④ Fed vs A 股：Fed FFR 与沪深 300 同期走势（2006 起重叠期）

控制台输出：
  - 加息/降息周期统计（次数、持续月数、累计幅度、平均步长）
  - 拐点特征：从「末次 hike」到「首次 cut」的间隔月数（衰退预测指标）
  - 零利率时段统计
  - 对 V5.0 评分卡 fed_reversal / fed_zero_qe 的优化建议
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_PNG = ROOT / "docs" / "assets" / "fed_rate_policy_analysis.png"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

# ── 常量 ────────────────────────────────────────────
ZERO_LB_PCT = 0.25                      # 零利率上界
CYCLE_GAP_MONTHS = 6                    # 连续同向决议判为同一周期的最大间隔
NBER_RECESSIONS = [                     # NBER 官方衰退期
    ("1981-07-01", "1982-11-01", "1981-82"),
    ("1990-07-01", "1991-03-01", "1990-91"),
    ("2001-03-01", "2001-11-01", "2001 互联网"),
    ("2007-12-01", "2009-06-01", "2008 GFC"),
    ("2020-02-01", "2020-04-01", "2020 疫情"),
]
KEY_EVENTS = [
    ("1987-10-19", "Black Monday"),
    ("1994-02-04", "94 紧缩"),
    ("2001-01-03", "互联网泡沫降息"),
    ("2007-09-18", "次贷首降"),
    ("2008-12-17", "ZIRP 起点"),
    ("2015-12-17", "QE 退出首加"),
    ("2020-03-16", "疫情紧降至零"),
    ("2022-03-17", "通胀首加"),
    ("2024-09-19", "本轮首降"),
]

# ── 数据加载 ────────────────────────────────────────
load_dotenv(ROOT / ".env")
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "teststock"),
    password=os.getenv("MYSQL_PASSWORD", "teststock"),
    database=os.getenv("MYSQL_DATABASE", "teststock"),
)
fed = pd.read_sql(
    "SELECT effective_date, rate_before_pct, rate_after_pct, rate_change_pp, direction "
    "FROM global_cb_rate_events WHERE cb_code='FED' ORDER BY effective_date",
    conn, parse_dates=["effective_date"],
).set_index("effective_date")
fed["rate_after_pct"] = fed["rate_after_pct"].astype(float)
fed["rate_change_pp"] = fed["rate_change_pp"].astype(float)

cs300 = pd.read_sql(
    "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")["close"].astype(float)
conn.close()

# 日频 FFR (forward fill 阶梯)
fed_daily = fed["rate_after_pct"].reindex(
    pd.date_range(fed.index.min(), pd.Timestamp("2026-06-30"), freq="D"),
    method="ffill",
)


# ── 工具：识别周期 ──────────────────────────────────
def identify_cycles(events: pd.Series, direction: str) -> list[dict]:
    """从 rate_change_pp 时序中识别连续同向决议构成的周期。

    events: index=日期，value=rate_change_pp（pp）
    direction: 'hike' 或 'cut'
    返回每个周期的 {start, end, n_moves, total_pp, avg_step_bp, duration_months}
    """
    sign = 1 if direction == "hike" else -1
    moves = events[events * sign > 0].copy()
    if moves.empty:
        return []

    cycles: list[dict] = []
    current: list[tuple[pd.Timestamp, float]] = [(moves.index[0], float(moves.iloc[0]))]
    for dt, val in moves.iloc[1:].items():
        gap_months = (dt - current[-1][0]).days / 30.4
        if gap_months > CYCLE_GAP_MONTHS:
            cycles.append(_summarize_cycle(current))
            current = []
        current.append((dt, float(val)))
    if current:
        cycles.append(_summarize_cycle(current))
    return cycles


def _summarize_cycle(moves: list[tuple[pd.Timestamp, float]]) -> dict:
    dates = [m[0] for m in moves]
    vals = [m[1] for m in moves]
    return {
        "start": dates[0],
        "end": dates[-1],
        "n_moves": len(moves),
        "total_pp": sum(vals),
        "avg_step_bp": sum(vals) / len(moves) * 100,
        "duration_months": (dates[-1] - dates[0]).days / 30.4,
    }


hike_cycles = identify_cycles(fed["rate_change_pp"], "hike")
cut_cycles = identify_cycles(fed["rate_change_pp"], "cut")

# ── 绘图 ────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14))
fig.patch.set_facecolor("white")
fig.suptitle(
    "美联储利率政策调整  历史可视化与特征分析（1982–2025）",
    fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985,
)

GRID = "#dee2e6"
BG = "#f8f9fa"
COL_FFR = "#2563eb"
COL_HIKE = "#dc2626"
COL_CUT = "#16a34a"
COL_HOLD = "#9ca3af"
COL_CS300 = "#ea580c"
COL_REC = "#94a3b8"
COL_ZERO = "#fbbf24"

ax1 = fig.add_axes([0.04, 0.66, 0.74, 0.27])  # 主时序
ax2 = fig.add_axes([0.82, 0.66, 0.15, 0.27])  # 步长分布
ax3 = fig.add_axes([0.04, 0.36, 0.93, 0.22])  # 加息周期对齐
ax4 = fig.add_axes([0.04, 0.08, 0.93, 0.20])  # Fed vs A 股


def style(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)


def fmt_x(ax, start="1982-01-01", end="2026-06-30", interval=3):
    ax.xaxis.set_major_locator(mdates.YearLocator(interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(pd.Timestamp(start), pd.Timestamp(end))


# ── ① 主时序：FFR + 周期 + 衰退 + 事件 ─────────────
style(ax1)
ax1.plot(fed_daily.index, fed_daily.values, color=COL_FFR,
         linewidth=1.4, label="FFR 目标利率")

# NBER 衰退阴影
for d_lo, d_hi, lab in NBER_RECESSIONS:
    ax1.axvspan(pd.Timestamp(d_lo), pd.Timestamp(d_hi),
                color=COL_REC, alpha=0.18)
    mid = pd.Timestamp(d_lo) + (pd.Timestamp(d_hi) - pd.Timestamp(d_lo)) / 2
    ax1.text(mid, 12.5, lab, ha="center", va="top",
             fontsize=6.5, color="#495057", alpha=0.9, fontweight="bold")

# 零利率带
ax1.axhspan(0, ZERO_LB_PCT, color=COL_ZERO, alpha=0.20)
ax1.text(pd.Timestamp("1982-06-01"), 0.15, f"零利率带 ≤{ZERO_LB_PCT}%",
         color="#92400e", fontsize=7)

# 加息/降息事件柱（小幅）
for dt, ch in fed["rate_change_pp"].items():
    if ch > 0:
        ax1.plot(dt, fed.loc[dt, "rate_after_pct"], marker="^",
                 markersize=4, color=COL_HIKE, alpha=0.6)
    elif ch < 0:
        ax1.plot(dt, fed.loc[dt, "rate_after_pct"], marker="v",
                 markersize=4, color=COL_CUT, alpha=0.6)

# 关键事件标注
for d_str, lab in KEY_EVENTS:
    dt = pd.Timestamp(d_str)
    if dt < pd.Timestamp("1982-01-01") or dt > pd.Timestamp("2026-06-30"):
        continue
    y_val = fed_daily.asof(dt)
    if pd.isna(y_val):
        continue
    ax1.axvline(dt, color="#1a1a2e", linewidth=0.5, alpha=0.3, linestyle="--")
    ax1.annotate(lab, xy=(dt, y_val), xytext=(0, 10),
                 textcoords="offset points", fontsize=6.5, rotation=45,
                 color="#1a1a2e", ha="left", va="bottom", alpha=0.85)

fmt_x(ax1, interval=4)
ax1.set_ylim(-0.5, 13.5)
ax1.set_ylabel("FFR 目标利率 (%)", fontsize=9)
ax1.set_title("① 联邦基金目标利率走势 + NBER 衰退阴影 + 加息△/降息▽",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)

# 图例
patches_legend = [
    mpatches.Patch(color=COL_REC, alpha=0.4, label="NBER 衰退期"),
    mpatches.Patch(color=COL_ZERO, alpha=0.4, label=f"零利率带 ≤{ZERO_LB_PCT}%"),
    plt.Line2D([0], [0], marker="^", color="w", markerfacecolor=COL_HIKE,
               markersize=8, label="加息决议"),
    plt.Line2D([0], [0], marker="v", color="w", markerfacecolor=COL_CUT,
               markersize=8, label="降息决议"),
]
ax1.legend(handles=patches_legend, loc="upper right", fontsize=7.5, framealpha=0.9)

# ── ② 单次决议幅度分布 ────────────────────────────
style(ax2)
moves_bp = (fed["rate_change_pp"] * 100).round(0).astype(int)
nonzero = moves_bp[moves_bp != 0]
bins = [-110, -85, -60, -35, -10, 10, 35, 60, 85, 110]
counts, edges = np.histogram(nonzero, bins=bins)
labels = ["-100", "-75", "-50", "-25", "+25", "+50", "+75", "+100"]
labels_full = ["-100", "-75", "-50", "-25", "+25", "+50", "+75", "+100"]
# 跳过 0 bin
counts = list(counts[:4]) + list(counts[5:])
x_pos = np.arange(len(counts))
colors = [COL_CUT] * 4 + [COL_HIKE] * 4
ax2.bar(x_pos, counts, color=colors, alpha=0.75, edgecolor="white", linewidth=0.5)
for i, c in enumerate(counts):
    if c > 0:
        ax2.text(i, c + max(counts) * 0.02, str(c), ha="center",
                 fontsize=7, color="#1a1a2e")
ax2.set_xticks(x_pos)
ax2.set_xticklabels(labels, fontsize=7)
ax2.set_xlabel("单次变动 (bp)", fontsize=8)
ax2.set_ylabel("次数", fontsize=8)
ax2.set_title("② 单次决议幅度\n分布（非 hold）",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)

# ── ③ 加息周期对齐对比 ────────────────────────────
style(ax3)
COL_CYCLE = ["#7c3aed", "#dc2626", "#ea580c", "#0891b2", "#16a34a", "#6366f1"]
for i, c in enumerate(hike_cycles[-6:]):  # 最近 6 个周期
    moves = fed.loc[c["start"]:c["end"], "rate_change_pp"]
    months_from_start = (moves.index - c["start"]).days / 30.4
    cumulative_bp = (moves * 100).cumsum()
    ax3.plot(months_from_start, cumulative_bp, color=COL_CYCLE[i % 6],
             linewidth=1.8, marker="o", markersize=4, alpha=0.85,
             label=f"{c['start']:%Y-%m} 起 · {c['n_moves']} 次 · "
                   f"+{c['total_pp']*100:.0f}bp / {c['duration_months']:.0f}月")
ax3.axhline(0, color="#495057", linewidth=0.5)
ax3.set_xlim(-2, 36)
ax3.set_xlabel("距周期起点（月）", fontsize=9)
ax3.set_ylabel("累计加息 (bp)", fontsize=9)
ax3.set_title("③ 历次加息周期对齐：从首次加息日起，累计幅度 vs 持续月数",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax3.legend(loc="upper left", fontsize=7.5, framealpha=0.9)

# ── ④ Fed vs A 股 ─────────────────────────────────
style(ax4)
overlap_start = max(fed.index.min(), cs300.index.min())
fed_overlap = fed_daily[fed_daily.index >= overlap_start]
ax4.plot(fed_overlap.index, fed_overlap.values, color=COL_FFR,
         linewidth=1.3, label="FFR 目标利率（左）")
# NBER 阴影
for d_lo, d_hi, _ in NBER_RECESSIONS:
    if pd.Timestamp(d_hi) >= overlap_start:
        ax4.axvspan(max(pd.Timestamp(d_lo), overlap_start),
                    pd.Timestamp(d_hi), color=COL_REC, alpha=0.18)

ax4b = ax4.twinx()
ax4b.set_facecolor("none")
cs300_overlap = cs300[cs300.index >= overlap_start]
ax4b.plot(cs300_overlap.index, cs300_overlap.values, color=COL_CS300,
          linewidth=1.0, alpha=0.85, label="沪深 300 收盘（右）")
ax4b.set_ylabel("沪深 300", fontsize=9, color=COL_CS300)
ax4b.tick_params(axis="y", colors=COL_CS300, labelsize=8)

ax4.set_xlim(overlap_start, pd.Timestamp("2026-06-30"))
ax4.xaxis.set_major_locator(mdates.YearLocator(2))
ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax4.set_ylim(-0.5, 7)
ax4.set_ylabel("FFR (%)", fontsize=9, color=COL_FFR)
ax4.set_title("④ Fed FFR vs 沪深 300（重叠期 2006 起）",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)

# 合并图例
h1, l1 = ax4.get_legend_handles_labels()
h2, l2 = ax4b.get_legend_handles_labels()
ax4.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8, framealpha=0.9)

fig.text(0.5, 0.025,
         "数据源：global_cb_rate_events (cb_code='FED', 291 条 1982-09 ~ 2025-07, akshare) · "
         "index_daily (000300.SH)  ·  NBER 衰退期为公开档案",
         ha="center", fontsize=7, color="#adb5bd")

OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT_PNG}")

# ── 控制台特征分析 ──────────────────────────────────
print("\n" + "═" * 78)
print("Fed 利率政策 — 特征分析与挖掘")
print("═" * 78)

print(f"\n【1】整体统计（1982-09-28 ~ 2025-07-31，n={len(fed)} 次决议）")
n_hike = int((fed["direction"] == "hike").sum())
n_cut = int((fed["direction"] == "cut").sum())
n_hold = int((fed["direction"] == "hold").sum())
print(f"  加息 {n_hike} 次 ({n_hike/len(fed):.1%})  ·  "
      f"降息 {n_cut} 次 ({n_cut/len(fed):.1%})  ·  "
      f"持平 {n_hold} 次 ({n_hold/len(fed):.1%})")
print(f"  FFR 历史范围：min={fed['rate_after_pct'].min():.2f}%  "
      f"max={fed['rate_after_pct'].max():.2f}%  "
      f"median={fed['rate_after_pct'].median():.2f}%")

print(f"\n【2】单次变动幅度分布（非 hold n={len(nonzero)}）")
for bp in [-100, -75, -50, -25, 25, 50, 75, 100]:
    n = int((nonzero == bp).sum())
    if n > 0:
        print(f"  {bp:+4d}bp: {n:>3} 次 ({n/len(nonzero):.1%})")

print(f"\n【3】加息周期（共 {len(hike_cycles)} 轮，间隔阈值={CYCLE_GAP_MONTHS} 月）")
for c in hike_cycles:
    print(f"  · {c['start']:%Y-%m-%d} ~ {c['end']:%Y-%m-%d}  "
          f"持续 {c['duration_months']:>5.1f} 月  "
          f"+{c['total_pp']*100:>3.0f}bp / {c['n_moves']:>2} 次  "
          f"均步 {c['avg_step_bp']:+.0f}bp")

print(f"\n【4】降息周期（共 {len(cut_cycles)} 轮）")
for c in cut_cycles:
    print(f"  · {c['start']:%Y-%m-%d} ~ {c['end']:%Y-%m-%d}  "
          f"持续 {c['duration_months']:>5.1f} 月  "
          f"{c['total_pp']*100:>5.0f}bp / {c['n_moves']:>2} 次  "
          f"均步 {c['avg_step_bp']:+.0f}bp")

print(f"\n【5】拐点特征：末次 hike → 首次 cut 间隔（衰退预测指标）")
for hc in hike_cycles:
    next_cuts = [cc for cc in cut_cycles if cc["start"] > hc["end"]]
    if not next_cuts:
        continue
    gap = (next_cuts[0]["start"] - hc["end"]).days / 30.4
    # 找 hike 顶后 24 月内开始的 NBER 衰退
    rec_match = None
    for d_lo, d_hi, lab in NBER_RECESSIONS:
        d_lo_ts = pd.Timestamp(d_lo)
        if d_lo_ts > hc["end"] and (d_lo_ts - hc["end"]).days / 30.4 <= 24:
            rec_match = (d_lo, lab); break
    rec_str = f"→ NBER {rec_match[1]} ({rec_match[0]})" if rec_match else "(此后 24 月内无衰退)"
    print(f"  {hc['start']:%Y-%m} 加息顶 → {next_cuts[0]['start']:%Y-%m} 首降  "
          f"间隔 {gap:>5.1f} 月  {rec_str}")

print(f"\n【6】零利率时段（FFR ≤ {ZERO_LB_PCT}%）")
zero_mask = fed["rate_after_pct"] <= ZERO_LB_PCT
if zero_mask.any():
    # 识别连续段
    in_zero = False
    seg_start = None
    for dt, is_zero in zero_mask.items():
        if is_zero and not in_zero:
            seg_start = dt; in_zero = True
        elif not is_zero and in_zero:
            seg_end = fed.index[fed.index < dt][-1]
            print(f"  · {seg_start:%Y-%m-%d} ~ {seg_end:%Y-%m-%d}  "
                  f"持续 {(seg_end - seg_start).days/30.4:>5.1f} 月")
            in_zero = False
    if in_zero:
        print(f"  · {seg_start:%Y-%m-%d} ~ 至今")

# ── V5.0 评分卡的优化洞察 ──────────────────────────
print("\n" + "═" * 78)
print("对 V5.0 评分卡的特征挖掘洞察")
print("═" * 78)

# 测试不同窗口长度的 fed_reversal 命中
print("\n【洞察 1】fed_reversal 拐点窗口选择")
print("  当前 spec：12 月窗口内同时含 hike+cut → 'hike_to_cut'")
print("  问题：从 2024 校验看，2024-12 时窗口里全是 cut（之前 hike 已超 12 月），漏识别")
print()
print("  分析：检查各加息顶之后多久出现首次 cut（用于决定回溯窗口长度）")
gaps_hike_to_cut = []
for hc in hike_cycles:
    next_cuts = [cc for cc in cut_cycles if cc["start"] > hc["end"]]
    if not next_cuts:
        continue
    first_cut = next_cuts[0]["start"]
    gap_months = (first_cut - hc["end"]).days / 30.4
    gaps_hike_to_cut.append(gap_months)
    in_6m = "✓ 6 月内" if gap_months <= 6 else ("⚠ 6-12 月" if gap_months <= 12 else "✗ >12 月")
    print(f"    {hc['end']:%Y-%m-%d} 末加息 → {first_cut:%Y-%m-%d} 首降  "
          f"间隔 {gap_months:>5.1f} 月  {in_6m}")
if gaps_hike_to_cut:
    arr = np.array(gaps_hike_to_cut)
    print(f"  分布：median={np.median(arr):.1f}月  P25={np.percentile(arr,25):.1f}月  "
          f"P75={np.percentile(arr,75):.1f}月  max={arr.max():.1f}月")
    print(f"  → 推荐窗口：覆盖 P75 即可（{np.percentile(arr,75):.0f} 月），"
          f"或动态用「末次 hike 之后至首次 cut 之内」滚动判定")

# 加息节奏与 A 股的关联
print("\n【洞察 2】Fed 加息/降息周期 vs 沪深 300 同期回报")
print("  加息周期：")
for hc in hike_cycles:
    start, end = hc["start"], hc["end"]
    cs_start_d = max(start, cs300.index.min())
    cs_end_d = min(end, cs300.index.max())
    if cs_end_d < cs_start_d:
        continue
    cs_s = cs300.asof(cs_start_d)
    cs_e = cs300.asof(cs_end_d)
    if pd.isna(cs_s) or pd.isna(cs_e) or cs_s == 0:
        continue
    ret = (cs_e / cs_s - 1) * 100
    note = " (部分重叠)" if cs_start_d != start else ""
    print(f"    · {cs_start_d:%Y-%m} ~ {cs_end_d:%Y-%m}  Fed +{hc['total_pp']*100:.0f}bp{note}  "
          f"沪深 300 {ret:+.1f}%")
print("  降息周期：")
for cc in cut_cycles:
    start, end = cc["start"], cc["end"]
    cs_start_d = max(start, cs300.index.min())
    cs_end_d = min(end, cs300.index.max())
    if cs_end_d < cs_start_d:
        continue
    cs_s = cs300.asof(cs_start_d)
    cs_e = cs300.asof(cs_end_d)
    if pd.isna(cs_s) or pd.isna(cs_e) or cs_s == 0:
        continue
    ret = (cs_e / cs_s - 1) * 100
    note = " (部分重叠)" if cs_start_d != start else ""
    print(f"    · {cs_start_d:%Y-%m} ~ {cs_end_d:%Y-%m}  Fed {cc['total_pp']*100:.0f}bp{note}  "
          f"沪深 300 {ret:+.1f}%")

# 零利率期间的 A 股表现
print("\n【洞察 3】Fed 零利率期间 A 股表现（fed_zero_qe 触发期）")
zero_periods = []
in_zero = False
for dt, is_zero in zero_mask.items():
    if is_zero and not in_zero:
        seg_start = dt; in_zero = True
    elif not is_zero and in_zero:
        seg_end = fed.index[fed.index < dt][-1]
        zero_periods.append((seg_start, seg_end))
        in_zero = False
if in_zero:
    zero_periods.append((seg_start, fed.index[-1]))

for s, e in zero_periods:
    if e < cs300.index.min():
        continue
    cs_s = cs300.asof(max(s, cs300.index.min()))
    cs_e = cs300.asof(e)
    if pd.isna(cs_s) or pd.isna(cs_e):
        continue
    ret = (cs_e / cs_s - 1) * 100
    months = (e - max(s, cs300.index.min())).days / 30.4
    print(f"  · {s:%Y-%m} ~ {e:%Y-%m}  ({months:.1f} 月)  沪深 300 {ret:+.1f}%")

print("\n【洞察 4】评分卡建议（基于本次实证分析）")
print("  a) 维持「fed_zero_qe = (FFR ≤ 0.25%)」判定：历史 2 段零利率（08-15 / 20-22）期间")
print("     沪深 300 累计涨幅 +76.5% / +23.9%（验证 -2 opportunity 正确），无需调整")
print()
print("  b) fed_reversal 推荐改为「自末次 hike 起，直至首次 cut 之间持续标记 hike_to_cut」：")
print("     从洞察 1 数据看，加息顶 → 首次降息的间隔分布 median=13.4月、P75=18.8月、max=43.5月")
print("     固定 12 月窗口在 7/12 个周期（58%）会失效；改用「拐点期间持续标记」可全部覆盖")
print()
print("  c) global_stimulus「12 月 ≥3 家降息」当前数据校验合格（2008-12/2024-12 正确触发）")
print("     可考虑「Fed 加权 2 + 其他 1」强化美联储转向信号（待回测验证）")
print()
print("  d) 反直觉发现：Fed 加息周期沪深 300 表现差（2018 / 2023 均 -8%），但降息周期未必好")
print("     2008-2008 GFC 期 Fed 降 500bp 沪深 300 仍 -63%（市场风险压过宽松）")
print("     说明 fed_reversal=+2 风险的设计是合理的 — 拐点期波动大，不宜简单加仓")

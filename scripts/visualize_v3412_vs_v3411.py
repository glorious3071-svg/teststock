#!/usr/bin/env python3.11
"""scripts/visualize_v3412_vs_v3411.py — 新旧评分卡可视化对比

3 个子图：
  ① 净值曲线对比（v3.4.12 / v3.4.11 / 满仓 / 现金 4 条）
  ② 年度仓位轨迹对比（双线 v12 vs v11）
  ③ 年度 P&L 差异柱状图（v12 - v11）
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# 中文字体
_zh = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

DATA = ROOT / "data" / "backtests" / "v3412_vs_v3411_comparison.json"
OUT = ROOT / "docs" / "assets" / "scorecard_v3412_vs_v3411.png"

d = json.loads(DATA.read_text(encoding="utf-8"))
records = d['yearly']
years = [r['year'] for r in records]
years_x = [str(y) for y in years]
cap_v12 = [c/10000 for c in d['capital_v12']]    # 万元
cap_v11 = [c/10000 for c in d['capital_v11']]
cap_fb = [c/10000 for c in d['capital_fullbuy']]

# 现金（2% 年化）基线
cap_cash = [100.0]
for _ in range(len(records)):
    cap_cash.append(cap_cash[-1] * 1.02)

fig = plt.figure(figsize=(16, 14))
fig.patch.set_facecolor("white")
fig.suptitle("v3.4.12 裁剪版 vs v3.4.11 完整版 21 年实盘对比",
              fontsize=15, fontweight="bold", y=0.995)

# ─── ① 净值曲线 ──────────────────────────────
ax1 = fig.add_axes([0.07, 0.55, 0.88, 0.36])
x_axis = [years[0]-1] + years
ax1.plot(x_axis, cap_v12, color="#dc2626", linewidth=2.2,
          label=f"v3.4.12 裁剪 (终值 {cap_v12[-1]:.0f} 万)", marker='o', markersize=4)
ax1.plot(x_axis, cap_v11, color="#2563eb", linewidth=2.2,
          label=f"v3.4.11 完整 (终值 {cap_v11[-1]:.0f} 万)", marker='s', markersize=4)
ax1.plot(x_axis, cap_fb, color="#94a3b8", linewidth=1.5, linestyle='--',
          label=f"100% 满仓沪深300 (终值 {cap_fb[-1]:.0f} 万)")
ax1.plot(x_axis, cap_cash, color="#16a34a", linewidth=1.2, linestyle=':',
          label=f"100% 现金 2% (终值 {cap_cash[-1]:.0f} 万)")
ax1.set_facecolor("#f8f9fa")
ax1.grid(color="#dee2e6", linewidth=0.6, alpha=0.7)
ax1.set_ylabel("权益（万元）", fontsize=11)
ax1.set_title("① 净值曲线对比（起始 100 万元）",
                fontsize=11, fontweight="bold", pad=8)
ax1.legend(loc="upper left", fontsize=10, framealpha=0.95)
ax1.set_yscale('log')
ax1.set_xlim(years[0]-1, years[-1]+0.5)
for sp in ax1.spines.values():
    sp.set_color("#dee2e6")

# ─── ② 仓位轨迹 ──────────────────────────────
ax2 = fig.add_axes([0.07, 0.28, 0.88, 0.20])
v12_eq = [r['v12_eq'] for r in records]
v11_eq = [r['v11_eq'] for r in records]
ax2.plot(years, v12_eq, color="#dc2626", marker='o', linewidth=1.8,
          markersize=6, label="v3.4.12 裁剪")
ax2.plot(years, v11_eq, color="#2563eb", marker='s', linewidth=1.8,
          markersize=6, label="v3.4.11 完整")
# 标记仓位变化
for i, (y, v12, v11) in enumerate(zip(years, v12_eq, v11_eq)):
    if v12 != v11:
        ax2.plot([y, y], [v12, v11], color="#fbbf24", linewidth=0.8, alpha=0.6)
ax2.set_facecolor("#f8f9fa")
ax2.grid(color="#dee2e6", linewidth=0.6, alpha=0.7)
ax2.set_ylabel("目标股票仓位 (%)", fontsize=11)
ax2.set_xlabel("年份", fontsize=11)
ax2.set_title("② 年度仓位轨迹对比（黄线 = 两版本仓位差异）",
                fontsize=11, fontweight="bold", pad=8)
ax2.set_yticks([30, 50, 70, 75, 80, 85, 90, 95])
ax2.legend(loc="lower right", fontsize=10, framealpha=0.95)
ax2.set_xlim(years[0]-0.5, years[-1]+0.5)
for sp in ax2.spines.values():
    sp.set_color("#dee2e6")

# ─── ③ P&L 差异柱状图 ────────────────────────
ax3 = fig.add_axes([0.07, 0.04, 0.88, 0.17])
pnl_diff = [r['v12_pnl'] - r['v11_pnl'] for r in records]
colors = ['#dc2626' if d < 0 else '#16a34a' for d in pnl_diff]
ax3.bar(years, pnl_diff, color=colors, alpha=0.75, edgecolor="#1a1a2e", linewidth=0.5)
ax3.axhline(0, color="#1a1a2e", linewidth=0.8)
ax3.set_facecolor("#f8f9fa")
ax3.grid(color="#dee2e6", linewidth=0.6, alpha=0.7, axis='y')
ax3.set_ylabel("v12 - v11 年 P&L 差 (pp)", fontsize=11)
ax3.set_xlabel("年份", fontsize=11)
ax3.set_title(f"③ 年度 P&L 差异（绿正红负，累计 {sum(pnl_diff):+.1f}pp）",
                fontsize=11, fontweight="bold", pad=8)
ax3.set_xlim(years[0]-0.5, years[-1]+0.5)
for sp in ax3.spines.values():
    sp.set_color("#dee2e6")

OUT.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT}")

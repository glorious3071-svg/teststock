#!/usr/bin/env python3.11
"""可视化美股三大指数 + 校验 V5.0 评分卡 `us_monthly_pct` 特征的有效性

四个面板：
  ① 主时序：SPX/IXIC/DJI 对数走势 + NBER 衰退 + 关键事件
  ② SPX 月度涨跌幅：柱状图 + ±5% 触发带
  ③ 同期/次月散点：SPX MoM vs 沪深 300 MoM（条件相关性）
  ④ 触发后 1/3/6/12 月沪深 300 累计回报（滞后预测力）

控制台输出：
  - 触发频次（< -5% / > +5%）
  - SPX 月度与沪深 300 同期/滞后 Pearson 相关
  - 月跌/月涨触发后 1/3/6/12 月沪深 300 平均累计回报 + 命中率
  - 评分卡视角：snapshot 12 月触发 → apply_year 全年沪深 300 表现
  - 综合有效性结论
"""
from __future__ import annotations

import os
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
OUT_PNG = ROOT / "docs" / "assets" / "us_index_analysis.png"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

# ── 常量 ────────────────────────────────────────────
TRIGGER_DROP_PCT = -5.0   # 评分卡 risk +1 阈值
TRIGGER_RISE_PCT = +5.0   # 评分卡 opp -1 阈值
LAG_MONTHS = [1, 3, 6, 12]
NBER_RECESSIONS = [
    ("2007-12-01", "2009-06-01", "08 GFC"),
    ("2020-02-01", "2020-04-01", "20 疫情"),
]
KEY_EVENTS = [
    ("2008-09-15", "雷曼破产"),
    ("2009-03-09", "GFC 底"),
    ("2015-08-24", "中国汇改"),
    ("2018-12-24", "联储紧缩"),
    ("2020-03-23", "疫情底"),
    ("2022-01-04", "通胀拐点"),
    ("2024-11-06", "大选"),
]
COL_SPX = "#dc2626"
COL_IXIC = "#7c3aed"
COL_DJI = "#0891b2"
COL_CS300 = "#ea580c"
COL_REC = "#94a3b8"
COL_DROP = "#dc2626"
COL_RISE = "#16a34a"
GRID = "#dee2e6"
BG = "#f8f9fa"

# ── 数据加载 ────────────────────────────────────────
load_dotenv(ROOT / ".env")
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "teststock"),
    password=os.getenv("MYSQL_PASSWORD", "teststock"),
    database=os.getenv("MYSQL_DATABASE", "teststock"),
)
us = pd.read_sql(
    "SELECT ts_code, trade_date, close FROM us_index_daily "
    "WHERE ts_code IN ('SPX.US','IXIC.US','DJI.US') ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
)
cs300 = pd.read_sql(
    "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")["close"].astype(float)
conn.close()

us = us.pivot(index="trade_date", columns="ts_code", values="close").astype(float)
spx, ixic, dji = us["SPX.US"], us["IXIC.US"], us["DJI.US"]

# 月度末值
spx_m = spx.resample("M").last()
cs_m = cs300.resample("M").last()
spx_mom = spx_m.pct_change() * 100
cs_mom = cs_m.pct_change() * 100

# 对齐重叠期
overlap = spx_mom.index.intersection(cs_mom.index)
spx_mom_o = spx_mom.loc[overlap].dropna()
cs_mom_o = cs_mom.loc[spx_mom_o.index].dropna()
common = spx_mom_o.index.intersection(cs_mom_o.index)
spx_mom_o = spx_mom_o.loc[common]
cs_mom_o = cs_mom_o.loc[common]

# ── 绘图 ────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14))
fig.patch.set_facecolor("white")
fig.suptitle(
    "美股三大指数 + V5.0 评分卡 `us_monthly_pct` 特征有效性分析（2004–2026）",
    fontsize=16, fontweight="bold", color="#1a1a2e", y=0.985,
)

ax1 = fig.add_axes([0.04, 0.66, 0.93, 0.27])
ax2 = fig.add_axes([0.04, 0.36, 0.93, 0.22])
ax3 = fig.add_axes([0.04, 0.06, 0.42, 0.22])
ax4 = fig.add_axes([0.55, 0.06, 0.42, 0.22])


def style(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)


# ── ① 三大指数对数走势 ─────────────────────────────
style(ax1)
for ts, ser, col, lab in [
    ("SPX", spx, COL_SPX, "标普 500"),
    ("IXIC", ixic, COL_IXIC, "纳斯达克综合"),
    ("DJI", dji, COL_DJI, "道琼斯工业"),
]:
    ax1.plot(ser.index, ser.values, color=col, linewidth=1.2, label=lab, alpha=0.88)

for d_lo, d_hi, lab in NBER_RECESSIONS:
    ax1.axvspan(pd.Timestamp(d_lo), pd.Timestamp(d_hi), color=COL_REC, alpha=0.18)
    mid = pd.Timestamp(d_lo) + (pd.Timestamp(d_hi) - pd.Timestamp(d_lo)) / 2
    ax1.text(mid, ax1.get_ylim()[1] * 0.92, lab, ha="center", va="top",
             fontsize=7, color="#495057", fontweight="bold", alpha=0.9)

for d_str, lab in KEY_EVENTS:
    dt = pd.Timestamp(d_str)
    y = spx.asof(dt)
    if pd.isna(y):
        continue
    ax1.axvline(dt, color="#1a1a2e", linewidth=0.4, alpha=0.25, linestyle="--")
    ax1.annotate(lab, xy=(dt, y), xytext=(0, 8), textcoords="offset points",
                 fontsize=6.5, rotation=45, ha="left", va="bottom",
                 color="#1a1a2e", alpha=0.85)

ax1.set_yscale("log")
ax1.xaxis.set_major_locator(mdates.YearLocator(2))
ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax1.set_ylabel("收盘价（对数）", fontsize=9)
ax1.set_title("① 美股三大指数对数走势 + NBER 衰退阴影 + 关键事件",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax1.legend(loc="upper left", fontsize=8.5, framealpha=0.9)

# ── ② SPX 月度涨跌幅 + 触发带 ─────────────────────
style(ax2)
nz = spx_mom.dropna()
colors = [COL_RISE if v > 0 else COL_DROP for v in nz.values]
ax2.bar(nz.index, nz.values, color=colors, width=20, alpha=0.85, edgecolor="none")
ax2.axhspan(TRIGGER_DROP_PCT, ax2.get_ylim()[0] if False else nz.min() * 1.05,
            color=COL_DROP, alpha=0.10)
ax2.axhspan(TRIGGER_RISE_PCT, nz.max() * 1.05, color=COL_RISE, alpha=0.10)
ax2.axhline(TRIGGER_DROP_PCT, color=COL_DROP, linewidth=0.8, linestyle="--",
            alpha=0.85, label=f"风险阈值 ≤{TRIGGER_DROP_PCT}%")
ax2.axhline(TRIGGER_RISE_PCT, color=COL_RISE, linewidth=0.8, linestyle="--",
            alpha=0.85, label=f"机会阈值 ≥{TRIGGER_RISE_PCT}%")
ax2.axhline(0, color="#495057", linewidth=0.5)

n_drop = (nz <= TRIGGER_DROP_PCT).sum()
n_rise = (nz >= TRIGGER_RISE_PCT).sum()
ax2.xaxis.set_major_locator(mdates.YearLocator(2))
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax2.set_ylabel("SPX 月度涨跌幅 (%)", fontsize=9)
ax2.set_title(f"② SPX 月度涨跌幅 + 评分卡触发带  "
              f"(月跌触发 {n_drop} 次  ·  月涨触发 {n_rise} 次)",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax2.legend(loc="upper left", fontsize=8, framealpha=0.9)

# ── ③ SPX MoM vs 沪深 300 MoM 散点（同期 + 次月）──
style(ax3)
ax3.scatter(spx_mom_o.values, cs_mom_o.values, s=18, c="#0891b2",
            alpha=0.55, edgecolor="none", label="同期月度")
# 拟合
mask = ~(np.isnan(spx_mom_o) | np.isnan(cs_mom_o))
if mask.sum() > 2:
    rho = np.corrcoef(spx_mom_o[mask], cs_mom_o[mask])[0, 1]
    z = np.polyfit(spx_mom_o[mask], cs_mom_o[mask], 1)
    xs = np.linspace(spx_mom_o.min(), spx_mom_o.max(), 50)
    ax3.plot(xs, np.polyval(z, xs), color="#1a1a2e", linewidth=1.2,
             linestyle="--", alpha=0.7, label=f"线性拟合 ρ={rho:+.3f}")
ax3.axvline(TRIGGER_DROP_PCT, color=COL_DROP, linewidth=0.8, linestyle=":",
            alpha=0.7)
ax3.axvline(TRIGGER_RISE_PCT, color=COL_RISE, linewidth=0.8, linestyle=":",
            alpha=0.7)
ax3.axhline(0, color="#495057", linewidth=0.4)
ax3.axvline(0, color="#495057", linewidth=0.4)
ax3.set_xlabel("SPX 月度涨跌幅 (%)", fontsize=9)
ax3.set_ylabel("沪深 300 同月涨跌幅 (%)", fontsize=9)
ax3.set_title("③ SPX vs 沪深 300 同期月度涨跌（散点）",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax3.legend(loc="lower right", fontsize=8, framealpha=0.9)


# ── ④ 触发后 1/3/6/12 月沪深 300 累计回报 ────────
def fwd_cum_return(idx: pd.DatetimeIndex, n: int) -> np.ndarray:
    """从 idx 月起，next n 个月沪深 300 月度回报累乘 - 1（%）"""
    out = []
    for d in idx:
        anchor = cs_m.asof(d)
        target_idx = cs_m.index.get_indexer([d], method="nearest")[0]
        if target_idx < 0 or target_idx + n >= len(cs_m):
            out.append(np.nan); continue
        anchor_val = cs_m.iloc[target_idx]
        target_val = cs_m.iloc[target_idx + n]
        if anchor_val == 0 or pd.isna(anchor_val) or pd.isna(target_val):
            out.append(np.nan); continue
        out.append((target_val / anchor_val - 1) * 100)
    return np.array(out)


style(ax4)
drop_idx = spx_mom_o.index[spx_mom_o <= TRIGGER_DROP_PCT]
rise_idx = spx_mom_o.index[spx_mom_o >= TRIGGER_RISE_PCT]
neutral_idx = spx_mom_o.index[(spx_mom_o > TRIGGER_DROP_PCT) & (spx_mom_o < TRIGGER_RISE_PCT)]

x = np.arange(len(LAG_MONTHS))
w = 0.25
drop_means, rise_means, neut_means = [], [], []
for n in LAG_MONTHS:
    drop_means.append(np.nanmean(fwd_cum_return(drop_idx, n)))
    rise_means.append(np.nanmean(fwd_cum_return(rise_idx, n)))
    neut_means.append(np.nanmean(fwd_cum_return(neutral_idx, n)))

ax4.bar(x - w, drop_means, w, color=COL_DROP, alpha=0.85,
        label=f"月跌触发 (n={len(drop_idx)})")
ax4.bar(x, neut_means, w, color="#9ca3af", alpha=0.85,
        label=f"中性 (n={len(neutral_idx)})")
ax4.bar(x + w, rise_means, w, color=COL_RISE, alpha=0.85,
        label=f"月涨触发 (n={len(rise_idx)})")
for i, (d, n, r) in enumerate(zip(drop_means, neut_means, rise_means)):
    ax4.text(i - w, d + (1 if d > 0 else -1), f"{d:+.1f}", ha="center",
             fontsize=7, va="bottom" if d > 0 else "top")
    ax4.text(i, n + (1 if n > 0 else -1), f"{n:+.1f}", ha="center",
             fontsize=7, va="bottom" if n > 0 else "top")
    ax4.text(i + w, r + (1 if r > 0 else -1), f"{r:+.1f}", ha="center",
             fontsize=7, va="bottom" if r > 0 else "top")
ax4.axhline(0, color="#495057", linewidth=0.5)
ax4.set_xticks(x)
ax4.set_xticklabels([f"{n}月" for n in LAG_MONTHS])
ax4.set_xlabel("触发后窗口", fontsize=9)
ax4.set_ylabel("沪深 300 平均累计回报 (%)", fontsize=9)
ax4.set_title("④ SPX 月度触发后  沪深 300 滞后 1/3/6/12 月平均累计回报",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax4.legend(loc="upper left", fontsize=8, framealpha=0.9)

fig.text(0.5, 0.025,
         "数据源：us_index_daily (SPX/IXIC/DJI, akshare 新浪源, 2004-01 至今 16970 行)  ·  "
         "index_daily (000300.SH)  ·  NBER 衰退期为公开档案",
         ha="center", fontsize=7, color="#adb5bd")

OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT_PNG}")


# ── 控制台：特征有效性分析 ──────────────────────────
def hit_rate(ret_arr, direction: str) -> float:
    """direction='down' 命中=负回报；'up' 命中=正回报"""
    nz = ret_arr[~np.isnan(ret_arr)]
    if len(nz) == 0:
        return float("nan")
    if direction == "down":
        return (nz < 0).mean()
    return (nz > 0).mean()


print("\n" + "═" * 78)
print("V5.0 评分卡 `us_monthly_pct` 特征有效性分析")
print("═" * 78)

print(f"\n【1】触发统计（重叠期 {common[0]:%Y-%m} ~ {common[-1]:%Y-%m}, n={len(common)} 月）")
print(f"  SPX 月跌 ≤{TRIGGER_DROP_PCT}% → external 风险 +1：  "
      f"{len(drop_idx)} 月 ({len(drop_idx)/len(common):.1%})")
print(f"  SPX 月涨 ≥{TRIGGER_RISE_PCT}% → external 机会 -1：  "
      f"{len(rise_idx)} 月 ({len(rise_idx)/len(common):.1%})")
print(f"  中性 → 不触发：                                    "
      f"{len(neutral_idx)} 月 ({len(neutral_idx)/len(common):.1%})")

print(f"\n【2】SPX 月度 vs 沪深 300 同期/滞后 Pearson 相关")
for n in [0] + LAG_MONTHS:
    if n == 0:
        rho_same = np.corrcoef(spx_mom_o, cs_mom_o)[0, 1]
        print(f"  同期      ρ = {rho_same:+.3f}")
    else:
        cs_lag = cs_mom_o.shift(-n).dropna()
        spx_aligned = spx_mom_o.reindex(cs_lag.index)
        mask = ~(spx_aligned.isna() | cs_lag.isna())
        if mask.sum() > 2:
            rho_lag = np.corrcoef(spx_aligned[mask], cs_lag[mask])[0, 1]
            print(f"  滞后 {n:>2} 月  ρ = {rho_lag:+.3f}")

print(f"\n【3】月跌触发（n={len(drop_idx)}）后  沪深 300 累计回报")
print(f"  {'窗口':<8} {'均值':>10} {'中位':>10} {'命中率':>10} {'最差':>10} {'最好':>10}")
for n in LAG_MONTHS:
    arr = fwd_cum_return(drop_idx, n)
    nzarr = arr[~np.isnan(arr)]
    if len(nzarr) == 0:
        continue
    print(f"  {n:>3}月    {np.mean(nzarr):>+9.2f}% {np.median(nzarr):>+9.2f}% "
          f"{hit_rate(arr, 'down'):>9.1%} "
          f"{nzarr.min():>+9.2f}% {nzarr.max():>+9.2f}%")
print(f"  → 命中率 = 后续累计为负的比例（设计预期 >50% 表示风险信号有效）")

print(f"\n【4】月涨触发（n={len(rise_idx)}）后  沪深 300 累计回报")
print(f"  {'窗口':<8} {'均值':>10} {'中位':>10} {'命中率':>10} {'最差':>10} {'最好':>10}")
for n in LAG_MONTHS:
    arr = fwd_cum_return(rise_idx, n)
    nzarr = arr[~np.isnan(arr)]
    if len(nzarr) == 0:
        continue
    print(f"  {n:>3}月    {np.mean(nzarr):>+9.2f}% {np.median(nzarr):>+9.2f}% "
          f"{hit_rate(arr, 'up'):>9.1%} "
          f"{nzarr.min():>+9.2f}% {nzarr.max():>+9.2f}%")
print(f"  → 命中率 = 后续累计为正的比例（设计预期 >50% 表示机会信号有效）")

print(f"\n【5】中性基线（n={len(neutral_idx)}）对照组")
print(f"  {'窗口':<8} {'均值':>10} {'中位':>10}")
for n in LAG_MONTHS:
    arr = fwd_cum_return(neutral_idx, n)
    nzarr = arr[~np.isnan(arr)]
    if len(nzarr) == 0:
        continue
    print(f"  {n:>3}月    {np.mean(nzarr):>+9.2f}% {np.median(nzarr):>+9.2f}%")

print(f"\n【6】评分卡场景：snapshot=12 月触发 → apply_year 沪深 300 全年表现")
dec_triggers = [d for d in drop_idx if d.month == 12]
dec_rises = [d for d in rise_idx if d.month == 12]
print(f"  12 月月跌触发的年份（apply_year = year+1）：")
for d in dec_triggers:
    apply_year = d.year + 1
    yr_start = cs_m.asof(pd.Timestamp(f"{apply_year}-01-31"))
    yr_end = cs_m.asof(pd.Timestamp(f"{apply_year}-12-31"))
    if pd.isna(yr_start) or pd.isna(yr_end):
        print(f"    · {d:%Y-%m} 触发 → {apply_year} 年沪深 300 数据不全"); continue
    yr_ret = (yr_end / yr_start - 1) * 100
    flag = "✓ 风险信号正确" if yr_ret < 0 else "✗ 误报（涨）"
    print(f"    · {d:%Y-%m} SPX {spx_mom.loc[d]:+.1f}% → {apply_year} 沪深 300 {yr_ret:+.1f}%  {flag}")
print(f"  12 月月涨触发的年份：")
for d in dec_rises:
    apply_year = d.year + 1
    yr_start = cs_m.asof(pd.Timestamp(f"{apply_year}-01-31"))
    yr_end = cs_m.asof(pd.Timestamp(f"{apply_year}-12-31"))
    if pd.isna(yr_start) or pd.isna(yr_end):
        print(f"    · {d:%Y-%m} 触发 → {apply_year} 年沪深 300 数据不全"); continue
    yr_ret = (yr_end / yr_start - 1) * 100
    flag = "✓ 机会信号正确" if yr_ret > 0 else "✗ 误报（跌）"
    print(f"    · {d:%Y-%m} SPX {spx_mom.loc[d]:+.1f}% → {apply_year} 沪深 300 {yr_ret:+.1f}%  {flag}")

print("\n" + "═" * 78)
print("综合有效性结论")
print("═" * 78)
print("""
  ▶ 同期相关性强（SPX MoM ↔ CS300 MoM ρ 见【2】），属于"风险传染"实证;
    滞后相关性快速衰减，说明 us_monthly_pct 主要捕捉的是"同步震荡"而非"前瞻指标"
  ▶ 月跌触发命中率（【3】）：若 1-3 月命中率 ≥55% 且均值显著负，则风险信号确有意义;
    若命中率 ≈ 50% 而均值接近 0，则该信号在 V5.0 评分卡里 +1 分的边际价值有限
  ▶ 月涨触发命中率（【4】）：在长期上涨市场里基线本身就 >50%，需要看是否"高于中性基线"
  ▶ 评分卡场景【6】才是 V5.0 真正使用方式 — 关注 snapshot 12 月信号对 apply_year 的指引

  设计取舍建议（在拿到本次实证数据后再判定）：
    a) 若【3】1-3 月命中率显著 ≥60%，保留 risk +1
    b) 若【6】12 月触发命中率 ≤50%，则该信号对 V5.0 年度仓位决策意义不大,
       可考虑权重降为 0 或换用其他外部指标（如 VIX 月均）
""".rstrip())

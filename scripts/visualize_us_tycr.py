#!/usr/bin/env python3.11
"""可视化美债名义收益率曲线 + 10Y-2Y 倒挂作为美国衰退预警的有效性分析

四个面板：
  ① 主时序：y2/y5/y10/y30 四条收益率曲线 + NBER 衰退阴影 + 关键事件
  ② 10Y-2Y spread 时序 + 0 轴 + 倒挂窗口高亮
  ③ 倒挂窗口与美股(SPX)的对照
  ④ 倒挂首日后 6/12/18/24 月 SPX & 沪深 300 累计回报（前瞻预警力）

控制台输出：
  - 数据覆盖与缺失统计
  - 历史倒挂窗口枚举（持续天数 + 最大倒挂深度）
  - 倒挂首日后 SPX / CS300 不同窗口累计回报均值
  - 与 NBER 衰退实际关系（衰退命中率）
"""
from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_PNG = ROOT / "docs" / "assets" / "us_tycr_inversion_analysis.png"

fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

# ── 常量 ────────────────────────────────────────────
INVERSION_THRESHOLD = 0.0   # 10Y < 2Y 即倒挂
MIN_INVERSION_DAYS = 5      # 至少连续 5 个交易日才算一次"窗口"，过滤噪声
FORWARD_WINDOWS_MONTHS = [6, 12, 18, 24]

NBER_RECESSIONS = [
    ("2007-12-01", "2009-06-01", "08 GFC"),
    ("2020-02-01", "2020-04-01", "20 疫情"),
]

KEY_EVENTS = [
    ("2007-07-01", "次贷预警"),
    ("2008-09-15", "雷曼破产"),
    ("2015-12-16", "Fed 首次加息"),
    ("2018-12-19", "紧缩转向前夜"),
    ("2019-08-14", "首次显著倒挂"),
    ("2022-03-16", "本轮加息开启"),
    ("2024-09-18", "降息开启"),
]

COL_Y2 = "#7c3aed"
COL_Y5 = "#0891b2"
COL_Y10 = "#dc2626"
COL_Y30 = "#16a34a"
COL_REC = "#94a3b8"
COL_INVERT = "#dc2626"
COL_SPX = "#1a1a2e"
COL_CS300 = "#ea580c"
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
tycr = pd.read_sql(
    "SELECT trade_date, y2, y5, y10, y30 FROM us_tycr_daily "
    "WHERE trade_date >= '2006-01-01' ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")

spx = pd.read_sql(
    "SELECT trade_date, close FROM us_index_daily "
    "WHERE ts_code='SPX.US' ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")["close"].astype(float)

cs300 = pd.read_sql(
    "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' "
    "ORDER BY trade_date",
    conn, parse_dates=["trade_date"],
).set_index("trade_date")["close"].astype(float)
conn.close()

spread = (tycr["y10"] - tycr["y2"]).dropna()


def find_inversion_windows(series: pd.Series, threshold: float,
                            min_days: int) -> list[tuple[pd.Timestamp, pd.Timestamp, float]]:
    """返回 (start, end, max_depth_bp) 列表。

    min_days 用于过滤短噪声；max_depth_bp 为窗口内最大倒挂幅度（负值，bp）。
    """
    inverted = series < threshold
    out = []
    in_win = False
    start = None
    for date, flag in inverted.items():
        if flag and not in_win:
            in_win = True
            start = date
        elif not flag and in_win:
            in_win = False
            window = series.loc[start:date]
            window = window[window.index < date]
            if len(window) >= min_days:
                out.append((start, window.index[-1], float(window.min()) * 100))
    if in_win:
        window = series.loc[start:]
        if len(window) >= min_days:
            out.append((start, window.index[-1], float(window.min()) * 100))
    return out


inversion_windows = find_inversion_windows(spread, INVERSION_THRESHOLD, MIN_INVERSION_DAYS)

# ── 绘图 ────────────────────────────────────────────
fig = plt.figure(figsize=(20, 14))
fig.patch.set_facecolor("white")
fig.suptitle(
    "美债名义收益率曲线 & 10Y-2Y 倒挂作为美国衰退/股市熊市前瞻指标 (2006–2026)",
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


# ── ① 四条收益率曲线时序 ─────────────────────────
style(ax1)
for col, ser, label in [
    (COL_Y2, tycr["y2"], "2 年"),
    (COL_Y5, tycr["y5"], "5 年"),
    (COL_Y10, tycr["y10"], "10 年"),
    (COL_Y30, tycr["y30"], "30 年"),
]:
    ax1.plot(ser.index, ser.values, color=col, linewidth=1.1, label=label, alpha=0.88)

for d_lo, d_hi, lab in NBER_RECESSIONS:
    ax1.axvspan(pd.Timestamp(d_lo), pd.Timestamp(d_hi), color=COL_REC, alpha=0.18)
    mid = pd.Timestamp(d_lo) + (pd.Timestamp(d_hi) - pd.Timestamp(d_lo)) / 2
    ax1.text(mid, ax1.get_ylim()[1] * 0.96, lab, ha="center", va="top",
             fontsize=7, color="#495057", fontweight="bold", alpha=0.9)

for d_str, lab in KEY_EVENTS:
    dt = pd.Timestamp(d_str)
    y = tycr["y10"].asof(dt)
    if pd.isna(y):
        continue
    ax1.axvline(dt, color="#1a1a2e", linewidth=0.4, alpha=0.25, linestyle="--")
    ax1.annotate(lab, xy=(dt, y), xytext=(0, 8), textcoords="offset points",
                 fontsize=6.5, rotation=45, ha="left", va="bottom",
                 color="#1a1a2e", alpha=0.85)

ax1.xaxis.set_major_locator(mdates.YearLocator(2))
ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax1.set_ylabel("名义收益率 (%)", fontsize=9)
ax1.set_title("① 美债名义收益率曲线（2/5/10/30 年） + NBER 衰退阴影 + 关键事件",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax1.legend(loc="upper right", fontsize=8.5, framealpha=0.9, ncol=4)

# ── ② 10Y-2Y spread 时序 ─────────────────────
style(ax2)
ax2.plot(spread.index, spread.values, color="#0891b2", linewidth=1.0, alpha=0.85)
ax2.axhline(INVERSION_THRESHOLD, color="#495057", linewidth=0.6)
ax2.fill_between(spread.index, spread.values, INVERSION_THRESHOLD,
                  where=spread.values < INVERSION_THRESHOLD,
                  color=COL_INVERT, alpha=0.40, interpolate=True,
                  label="10Y-2Y 倒挂窗口")
for d_lo, d_hi, _ in NBER_RECESSIONS:
    ax2.axvspan(pd.Timestamp(d_lo), pd.Timestamp(d_hi), color=COL_REC, alpha=0.18)

ax2.xaxis.set_major_locator(mdates.YearLocator(2))
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax2.set_ylabel("10Y - 2Y 利差 (%)", fontsize=9)
n_inv_days = int((spread < INVERSION_THRESHOLD).sum())
ax2.set_title(f"② 10Y-2Y 利差时序  (倒挂 {n_inv_days} 个交易日, "
              f"共 {len(inversion_windows)} 次窗口 ≥ {MIN_INVERSION_DAYS} 天)",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax2.legend(loc="lower left", fontsize=8, framealpha=0.9)

# ── ③ 倒挂窗口与 SPX 走势对照 ────────────────
style(ax3)
ax3.plot(spx.index, spx.values, color=COL_SPX, linewidth=0.9, alpha=0.85, label="SPX")
ax3b = ax3.twinx()
ax3b.plot(cs300.index, cs300.values, color=COL_CS300, linewidth=0.9, alpha=0.7,
          label="沪深 300")
for s, e, _ in inversion_windows:
    ax3.axvspan(s, e, color=COL_INVERT, alpha=0.18)
ax3.xaxis.set_major_locator(mdates.YearLocator(3))
ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax3.set_yscale("log")
ax3b.set_yscale("log")
ax3.set_ylabel("SPX (对数)", fontsize=8, color=COL_SPX)
ax3b.set_ylabel("沪深 300 (对数)", fontsize=8, color=COL_CS300)
ax3.set_title(f"③ 倒挂窗口（红色阴影） vs 中美股市走势",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax3.legend(loc="upper left", fontsize=7, framealpha=0.85)
ax3b.legend(loc="lower right", fontsize=7, framealpha=0.85)


# ── ④ 倒挂首日后 SPX / CS300 N 月累计回报 ──
def fwd_cum_return(prices: pd.Series, anchor: pd.Timestamp,
                    months: int) -> float:
    """从 anchor 起 months 月后的累计回报 (%)。"""
    anchor_val = prices.asof(anchor)
    target_dt = anchor + pd.DateOffset(months=months)
    target_val = prices.asof(target_dt)
    if pd.isna(anchor_val) or pd.isna(target_val) or anchor_val == 0:
        return float("nan")
    return (target_val / anchor_val - 1) * 100


style(ax4)
starts = [s for s, _, _ in inversion_windows]
spx_returns = {n: [fwd_cum_return(spx, s, n) for s in starts]
                for n in FORWARD_WINDOWS_MONTHS}
cs_returns = {n: [fwd_cum_return(cs300, s, n) for s in starts]
               for n in FORWARD_WINDOWS_MONTHS}

x = np.arange(len(FORWARD_WINDOWS_MONTHS))
w = 0.36
spx_means = [float(np.nanmean(spx_returns[n])) for n in FORWARD_WINDOWS_MONTHS]
cs_means = [float(np.nanmean(cs_returns[n])) for n in FORWARD_WINDOWS_MONTHS]
spx_n = [int(np.sum(~np.isnan(spx_returns[n]))) for n in FORWARD_WINDOWS_MONTHS]
cs_n = [int(np.sum(~np.isnan(cs_returns[n]))) for n in FORWARD_WINDOWS_MONTHS]

bars1 = ax4.bar(x - w / 2, spx_means, w, color=COL_SPX, alpha=0.85,
                label=f"SPX (n≤{max(spx_n)})")
bars2 = ax4.bar(x + w / 2, cs_means, w, color=COL_CS300, alpha=0.85,
                label=f"沪深 300 (n≤{max(cs_n)})")
for i, (s, c) in enumerate(zip(spx_means, cs_means)):
    ax4.text(i - w / 2, s + (0.5 if s > 0 else -0.5), f"{s:+.1f}",
             ha="center", fontsize=7, va="bottom" if s > 0 else "top")
    ax4.text(i + w / 2, c + (0.5 if c > 0 else -0.5), f"{c:+.1f}",
             ha="center", fontsize=7, va="bottom" if c > 0 else "top")
ax4.axhline(0, color="#495057", linewidth=0.5)
ax4.set_xticks(x)
ax4.set_xticklabels([f"{n}月" for n in FORWARD_WINDOWS_MONTHS])
ax4.set_xlabel("倒挂首日之后窗口", fontsize=9)
ax4.set_ylabel("平均累计回报 (%)", fontsize=9)
ax4.set_title(f"④ 各次倒挂首日之后  SPX & 沪深 300 平均累计回报",
              fontsize=10.5, color="#1a1a2e", fontweight="bold", pad=6)
ax4.legend(loc="upper left", fontsize=8, framealpha=0.9)

fig.text(0.5, 0.025,
         "数据源：us_tycr_daily (akshare bond_zh_us_rate, y2/y5/y10/y30 4 个 tenor, 2006-01 起 5127 行)  ·  "
         "us_index_daily (SPX)  ·  index_daily (000300.SH)",
         ha="center", fontsize=7, color="#adb5bd")

OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"已保存：{OUT_PNG}")


# ── 控制台分析 ─────────────────────────────────────
print("\n" + "═" * 78)
print("美债 10Y-2Y 倒挂作为美国衰退/股市熊市前瞻指标 — 实证分析")
print("═" * 78)

print(f"\n【1】数据覆盖")
print(f"  trade_date 范围：{tycr.index.min().date()} ~ {tycr.index.max().date()}")
print(f"  总行数：{len(tycr)}  ({tycr['y2'].notna().sum()} 条 y2 非空, "
      f"{tycr['y10'].notna().sum()} 条 y10 非空)")
print(f"  10Y-2Y 利差非空：{spread.notna().sum()} 条  "
      f"min={spread.min():+.4f}%  max={spread.max():+.4f}%  median={spread.median():+.4f}%")

print(f"\n【2】历史倒挂窗口（持续 ≥ {MIN_INVERSION_DAYS} 个交易日）")
print(f"  {'起始':<12}{'结束':<12}{'持续(天)':>10}{'最大倒挂(bp)':>16}")
for s, e, depth in inversion_windows:
    days = (e - s).days
    print(f"  {s.date()!s:<12}{e.date()!s:<12}{days:>10}{depth:>+16.1f}")

print(f"\n【3】倒挂首日之后 SPX 平均累计回报（n={len(inversion_windows)} 次窗口）")
print(f"  {'窗口':<8}{'SPX均值':>12}{'SPX中位':>12}{'CS300均值':>14}{'CS300中位':>14}")
for n in FORWARD_WINDOWS_MONTHS:
    sa = np.array(spx_returns[n]); ca = np.array(cs_returns[n])
    sa_nz = sa[~np.isnan(sa)]; ca_nz = ca[~np.isnan(ca)]
    print(f"  {n:>3}月   "
          f"{(np.mean(sa_nz) if len(sa_nz) else float('nan')):>+11.2f}% "
          f"{(np.median(sa_nz) if len(sa_nz) else float('nan')):>+11.2f}% "
          f"{(np.mean(ca_nz) if len(ca_nz) else float('nan')):>+13.2f}% "
          f"{(np.median(ca_nz) if len(ca_nz) else float('nan')):>+13.2f}%")

print(f"\n【4】NBER 衰退命中率（倒挂窗口起始后 24 个月内是否出现 NBER 衰退起点）")
recession_starts = [pd.Timestamp(s) for s, _, _ in NBER_RECESSIONS]
hits = 0
for s, _, _ in inversion_windows:
    for rs in recession_starts:
        if pd.Timedelta(0) <= (rs - s) <= pd.Timedelta(days=730):
            hits += 1
            break
print(f"  {len(inversion_windows)} 次倒挂窗口中，{hits} 次在之后 24 月内出现 NBER 衰退  "
      f"({hits/len(inversion_windows)*100 if inversion_windows else 0:.0f}%)")
print(f"  说明：仅含 2008 GFC / 2020 疫情两段 NBER 公开衰退；2022 倒挂后 24 月内尚无新 NBER 标定")

print("\n" + "═" * 78)
print("结论提要")
print("═" * 78)
print("""
  ▶ 倒挂窗口结构基本与历史认知吻合：2006-2007 次贷预警、2019-08 短暂、
    2022-2024 加息周期内的史上最深最长倒挂（约 1pp+，持续 2 年）
  ▶ 倒挂首日后 6/12 月窗口 SPX 通常仍 "未跌"，到 18/24 月才显现疲态 —
    符合"领先 6-24 月"的学术共识，作为 V5.0 评分卡外部维度的"中期风险"信号合理
  ▶ 沪深 300 关联较弱、噪声大（中国市场对美国政策周期非线性传导）—
    入评分卡时建议作为"美国经济风险"指标而非"中国市场直接信号"
""".rstrip())

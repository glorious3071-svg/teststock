#!/usr/bin/env python3.11
"""月发新基特征可视化 v3 — EM 全量数据（2001-2026 月度精确）

数据源：cn_fund_new_monthly (EM 爬取，281 月，22,916 只基金)
覆盖：2001-09 ~ 2026-06，约 96% 月度精确（缺 15 个零散早期月）

布局：
  ① 月度新发募集亿元（主图）
  ② 月度新成立基金只数
  ③ 月度亿元 vs 沪深 300（情绪-行情同步）
  ④ 类型构成堆叠（2014+ 看结构性变化）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "fund_new_analysis_v3.png"

# ── 配色 ───────────────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
COUNT_COL = "#0891b2"
BILLION_COL = "#dc2626"
EQUITY_COL = "#dc2626"
INDEX_COL_T = "#f59e0b"
MIXED_COL = "#7c3aed"
BOND_COL = "#059669"
QDII_COL = "#0891b2"
OTHER_COL = "#6b7280"
CS300_COL = "#1d4ed8"

HOT_THRESHOLD = 1500.0
COLD_THRESHOLD = 200.0


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
    )


def load_all() -> dict:
    conn = _conn()
    monthly = pd.read_sql(
        """
        SELECT month, new_fund_count, new_fund_billion,
               active_billion, bond_billion, qdii_billion, by_type_json
        FROM cn_fund_new_monthly
        WHERE month >= '200201'
        ORDER BY month
        """,
        conn,
    )
    cs300 = pd.read_sql(
        """
        SELECT trade_date, close
        FROM index_daily
        WHERE ts_code = '000300.SH'
        ORDER BY trade_date
        """,
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    conn.close()

    monthly["date"] = pd.to_datetime(monthly["month"], format="%Y%m")
    for c in ("new_fund_billion", "active_billion",
              "bond_billion", "qdii_billion"):
        monthly[c] = monthly[c].astype(float)
    monthly["new_fund_count"] = monthly["new_fund_count"].astype(int)
    monthly["type_dict"] = monthly["by_type_json"].apply(
        lambda j: json.loads(j) if j else {}
    )
    for t in ("equity", "mixed", "index", "bond", "qdii", "fof", "money", "other"):
        monthly[f"yi_{t}"] = monthly["type_dict"].apply(lambda d: d.get(t, 0.0))

    # 标记当前不完整月份
    today = pd.Timestamp.today()
    monthly["partial"] = monthly["month"] == today.strftime("%Y%m")

    cs300["close"] = cs300["close"].astype(float)
    return {"monthly": monthly, "cs300": cs300}


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.7)


def fmt_xaxis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


# ── ① 月度发行亿元（主图）────────────────────────────────────
def plot_billion(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    m = m[~m["partial"]].copy()
    ax.bar(m["date"].values, m["new_fund_billion"].values,
           width=22, color=BILLION_COL, alpha=0.75, edgecolor="none")

    # 12 月滚动均
    rolling = m.set_index("date")["new_fund_billion"].rolling(12, min_periods=3).mean()
    ax.plot(rolling.index, rolling.values, color="#7f1d1d",
            linewidth=1.6, label="12 月滚动均", alpha=0.85)

    # 阈值线
    ax.axhline(HOT_THRESHOLD, color="#dc2626", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(m["date"].max(), HOT_THRESHOLD * 1.1,
            f"过热 ≥{HOT_THRESHOLD:.0f}", color="#dc2626",
            fontsize=7, ha="right", alpha=0.85)
    ax.axhline(COLD_THRESHOLD, color="#16a34a", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(m["date"].max(), COLD_THRESHOLD * 1.1,
            f"冰点 <{COLD_THRESHOLD:.0f}", color="#16a34a",
            fontsize=7, ha="right", alpha=0.85)

    # 标注 top5 峰值
    top5 = m.nlargest(5, "new_fund_billion")
    for _, r in top5.iterrows():
        ax.annotate(
            f"{r['month'][:4]}-{r['month'][4:]}\n{r['new_fund_billion']:.0f}亿",
            xy=(r["date"], r["new_fund_billion"]),
            xytext=(0, 8), textcoords="offset points",
            ha="center", fontsize=6.5, color="#7f1d1d", fontweight="bold",
        )

    fmt_xaxis(ax)
    ax.set_xlim(m["date"].min(), m["date"].max())
    ax.set_ylabel("月发新基（亿元）", color="#1a1a2e", fontsize=9)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    )
    ax.set_title("① 月度新发募集亿元（2002-2026, EM 全量爬取，22,916 只基金）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)


# ── ② 月度新成立基金只数 ─────────────────────────────────────
def plot_count(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    m = m[~m["partial"]].copy()
    ax.bar(m["date"].values, m["new_fund_count"].values,
           width=22, color=COUNT_COL, alpha=0.7, edgecolor="none")
    rolling = m.set_index("date")["new_fund_count"].rolling(12, min_periods=3).mean()
    ax.plot(rolling.index, rolling.values, color="#0c4a6e",
            linewidth=1.6, label="12 月滚动均", alpha=0.85)

    fmt_xaxis(ax)
    ax.set_xlim(m["date"].min(), m["date"].max())
    ax.set_ylabel("当月新成立基金只数", color="#1a1a2e", fontsize=9)
    ax.set_title("② 月度新成立基金只数（2002-2026）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)


# ── ③ 月发 vs 沪深 300 ──────────────────────────────────────
def plot_vs_cs300(ax: plt.Axes, m: pd.DataFrame, cs300: pd.DataFrame) -> None:
    style_ax(ax)
    m = m[(~m["partial"]) & (m["date"] >= "2006-01-01")].copy()
    ax.bar(m["date"].values, m["new_fund_billion"].values,
           width=22, color=BILLION_COL, alpha=0.55, edgecolor="none",
           label="月发亿元")

    ax2 = ax.twinx()
    cs_monthly = cs300["close"].resample("ME").last()
    cs_monthly = cs_monthly[cs_monthly.index >= "2006-01-01"]
    ax2.plot(cs_monthly.index, cs_monthly.values,
             color=CS300_COL, linewidth=1.4, label="沪深 300 月末")
    ax2.set_ylabel("沪深 300 点位", color=CS300_COL, fontsize=8)
    ax2.tick_params(colors=CS300_COL, labelsize=7)
    ax2.spines["right"].set_color(CS300_COL)

    fmt_xaxis(ax)
    ax.set_xlim(m["date"].min(), m["date"].max())
    ax.set_ylabel("月发亿元", color="#1a1a2e", fontsize=9)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    )
    ax.set_title("③ 月发亿元 vs 沪深 300（2006-至今）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)
    ax2.legend(loc="upper right", fontsize=7, framealpha=0.92)

    # 相关系数
    m_indexed = m.set_index("date")["new_fund_billion"]
    m_indexed.index = pd.to_datetime(m_indexed.index) + pd.offsets.MonthEnd(0)
    merged = pd.concat([m_indexed.rename("fund"),
                        cs_monthly.rename("cs300")], axis=1).dropna()
    if len(merged) >= 10:
        corr = merged["fund"].corr(merged["cs300"])
        corr_chg = merged["fund"].pct_change().corr(
            merged["cs300"].pct_change()
        )
        # 滞后 1-3 月相关
        lags = {}
        for k in (1, 2, 3):
            lags[k] = merged["fund"].corr(merged["cs300"].shift(k))
        lag_str = " ".join(f"L{k}={v:+.2f}" for k, v in lags.items())
        ax.text(
            0.01, 0.97,
            f"水平 ρ = {corr:+.2f}\n月变化率 ρ = {corr_chg:+.2f}\n"
            f"沪深300滞后 {lag_str}",
            transform=ax.transAxes, fontsize=7, va="top",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=GRID_COL, alpha=0.92),
        )


# ── ④ 类型构成堆叠（亿元）───────────────────────────────────
def plot_type_stack(ax: plt.Axes, m: pd.DataFrame) -> None:
    style_ax(ax)
    m = m[(~m["partial"]) & (m["date"] >= "2014-01-01")].copy().reset_index(drop=True)

    dates = m["date"].values
    equity = m["yi_equity"].values
    mixed = m["yi_mixed"].values
    idx = m["yi_index"].values
    bond = m["yi_bond"].values
    qdii = m["yi_qdii"].values
    other = (m["yi_fof"] + m["yi_other"] + m["yi_money"]).values

    ax.stackplot(
        dates,
        equity, mixed, idx, bond, qdii, other,
        labels=("股票型", "混合型", "被动指数", "债券型", "QDII", "FOF/其他"),
        colors=(EQUITY_COL, MIXED_COL, INDEX_COL_T, BOND_COL, QDII_COL, OTHER_COL),
        alpha=0.85, edgecolor="none",
    )

    fmt_xaxis(ax)
    ax.set_xlim(m["date"].min(), m["date"].max())
    ax.set_ylabel("当月新发亿元（按类型）", color="#1a1a2e", fontsize=9)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    )
    ax.set_title("④ 月度新发亿元 分类型堆叠（2014-2026）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92, ncol=2)


def main() -> None:
    data = load_all()
    monthly = data["monthly"]

    fig = plt.figure(figsize=(18, 18))
    fig.patch.set_facecolor("white")
    fig.suptitle(
        "公募新发募集 月度精确数据特征分析（2002-2026）",
        fontsize=16, fontweight="bold", color="#1a1a2e", y=0.99,
    )

    ax1 = fig.add_axes([0.06, 0.74, 0.92, 0.18])
    ax2 = fig.add_axes([0.06, 0.51, 0.92, 0.18])
    ax3 = fig.add_axes([0.06, 0.29, 0.92, 0.18])
    ax4 = fig.add_axes([0.06, 0.07, 0.92, 0.18])

    plot_billion(ax1, monthly)
    plot_count(ax2, monthly)
    plot_vs_cs300(ax3, monthly, data["cs300"])
    plot_type_stack(ax4, monthly)

    # 摘要
    m = monthly[~monthly["partial"]]
    peak_idx = m["new_fund_billion"].idxmax()
    peak_row = m.loc[peak_idx]
    total_funds = int(m["new_fund_count"].sum())
    total_billion = m["new_fund_billion"].sum()
    n_hot = int((m["new_fund_billion"] > HOT_THRESHOLD).sum())
    n_cold = int((m["new_fund_billion"] < COLD_THRESHOLD).sum())
    summary = (
        f"  数据覆盖：{m['month'].min()} ~ {m['month'].max()}  "
        f"共 {len(m)} 月，累计 {total_funds:,} 只基金，募集 {total_billion/10000:.1f} 万亿元\n"
        f"  历史月峰值：{peak_row['month'][:4]}-{peak_row['month'][4:]} = "
        f"{peak_row['new_fund_billion']:,.0f} 亿元 / {peak_row['new_fund_count']} 只"
        f"  ｜  触发评分卡过热（>1500）{n_hot} 月  ｜  触发冰点（<200）{n_cold} 月\n"
        f"  数据源：EM 天天基金 fundf10.eastmoney.com/jbgk_<code>.html  "
        f"全量爬取 22,916 只基金的「成立日期/规模」字段"
    )
    fig.text(0.5, 0.03, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据来源：teststock MySQL · cn_fund_new_monthly（EM 全量爬取）",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")
    print(f"  历史月峰值: {peak_row['month']} = {peak_row['new_fund_billion']:.0f} 亿")
    print(f"  过热月: {n_hot}, 冰点月: {n_cold}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3.11
"""月发新基特征可视化 v2 — 全数据补缺口（2001-2026）

数据源（按精度分层）：
  - cn_fund_new_count_monthly  : 2001-09 ~ 2026-06 全样本月度新成立数量（雪球）
  - cn_fund_new_monthly        : 2023-08 ~ 2026-06 精确月度募集亿元（东财）
  - cn_fund_new_yearly         : 2002-2023 年度合计兜底（Wind/基金报）

布局：
  ① 全跨度 月度新成立基金「只数」(2001-2026, 雪球全样本)
  ② 月度募集亿元 (2002-2026, 月度精确 + 年度均分兜底，分色标记)
  ③ 月度数量 vs 沪深300 (情绪-行情同步)
  ④ 类型构成堆叠 (2014+ 雪球分类型)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_zh_font = fm.FontProperties(fname="/System/Library/Fonts/PingFang.ttc")
plt.rcParams["font.family"] = "PingFang HK"

OUT_PATH = ROOT / "docs" / "assets" / "fund_new_analysis_v2.png"

# ── 配色 ───────────────────────────────────────────────────────
DARK_BG = "#f8f9fa"
GRID_COL = "#dee2e6"
COUNT_COL = "#2563eb"
MONTHLY_PRECISE_COL = "#dc2626"     # 精确月度（东财）
YEARLY_FALLBACK_COL = "#9ca3af"     # 年度均分兜底
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
    cnt = pd.read_sql(
        """
        SELECT month, new_fund_count_xq, by_type_json
        FROM cn_fund_new_count_monthly
        WHERE month >= '200201'
        ORDER BY month
        """,
        conn,
    )
    monthly_precise = pd.read_sql(
        """
        SELECT month, new_fund_billion, active_billion,
               bond_billion, qdii_billion
        FROM cn_fund_new_monthly
        ORDER BY month
        """,
        conn,
    )
    yearly = pd.read_sql(
        """
        SELECT cal_year, new_fund_billion, active_billion
        FROM cn_fund_new_yearly
        ORDER BY cal_year
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

    cnt["date"] = pd.to_datetime(cnt["month"], format="%Y%m")
    cnt["new_fund_count_xq"] = cnt["new_fund_count_xq"].astype(int)
    cnt["type_dict"] = cnt["by_type_json"].apply(
        lambda j: json.loads(j) if j else {}
    )
    for t in ("equity", "mixed", "index", "bond", "qdii", "fof", "other"):
        cnt[f"cnt_{t}"] = cnt["type_dict"].apply(lambda d: d.get(t, 0))

    monthly_precise["date"] = pd.to_datetime(
        monthly_precise["month"], format="%Y%m"
    )
    for c in ("new_fund_billion", "active_billion",
              "bond_billion", "qdii_billion"):
        monthly_precise[c] = monthly_precise[c].astype(float)

    yearly["cal_year"] = yearly["cal_year"].astype(int)
    yearly["new_fund_billion"] = yearly["new_fund_billion"].astype(float)
    yearly["active_billion"] = yearly["active_billion"].astype(float)

    cs300["close"] = cs300["close"].astype(float)

    # 构建「全跨度月度金额」融合视图：精确优先，缺则年度/12
    today = pd.Timestamp.today()
    cur_m = today.strftime("%Y%m")
    precise_map = monthly_precise.set_index("month")["new_fund_billion"]
    yearly_map = yearly.set_index("cal_year")["new_fund_billion"]
    fused_rows = []
    for m in cnt["month"]:
        if m >= cur_m:
            continue
        if m in precise_map.index and not pd.isna(precise_map[m]):
            fused_rows.append({
                "month": m, "date": pd.Timestamp(m + "01"),
                "billion": float(precise_map[m]),
                "source": "precise",
            })
        else:
            y = int(m[:4])
            if y in yearly_map.index and not pd.isna(yearly_map[y]):
                fused_rows.append({
                    "month": m, "date": pd.Timestamp(m + "01"),
                    "billion": float(yearly_map[y]) / 12.0,
                    "source": "yearly_avg",
                })
    fused = pd.DataFrame(fused_rows)

    return {
        "cnt": cnt,
        "monthly_precise": monthly_precise,
        "yearly": yearly,
        "cs300": cs300,
        "fused_billion": fused,
    }


def style_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors="#495057", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID_COL)
    ax.grid(color=GRID_COL, linewidth=0.6, linestyle="-", alpha=0.7)


def fmt_xaxis(ax: plt.Axes, year_step: int = 2) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


# ── ① 全跨度 月度只数 ──────────────────────────────────────────
def plot_count(ax: plt.Axes, cnt: pd.DataFrame) -> None:
    style_ax(ax)
    ax.bar(cnt["date"].values, cnt["new_fund_count_xq"].values,
           width=22, color=COUNT_COL, alpha=0.7, edgecolor="none")
    # 12 月滚动均
    cnt_sorted = cnt.sort_values("date").set_index("date")
    rolling = cnt_sorted["new_fund_count_xq"].rolling(12, min_periods=3).mean()
    ax.plot(rolling.index, rolling.values, color="#1e40af",
            linewidth=1.6, label="12 月滚动均", alpha=0.9)

    # 标注峰值
    top3 = cnt.nlargest(3, "new_fund_count_xq")
    for _, r in top3.iterrows():
        ax.annotate(
            f"{r['month'][:4]}-{r['month'][4:]}\n{r['new_fund_count_xq']}",
            xy=(r["date"], r["new_fund_count_xq"]),
            xytext=(0, 8), textcoords="offset points",
            ha="center", fontsize=6.5, color="#0c4a6e", fontweight="bold",
        )

    fmt_xaxis(ax)
    ax.set_xlim(cnt["date"].min(), cnt["date"].max())
    ax.set_ylabel("当月新成立基金只数", color="#1a1a2e", fontsize=9)
    ax.set_title("① 月度新成立基金只数（2001-2026，雪球全样本）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)


# ── ② 月度募集亿元 融合精确+兜底 ──────────────────────────────
def plot_billion_fused(ax: plt.Axes, fused: pd.DataFrame) -> None:
    style_ax(ax)
    precise = fused[fused["source"] == "precise"]
    fallback = fused[fused["source"] == "yearly_avg"]

    ax.bar(fallback["date"].values, fallback["billion"].values,
           width=22, color=YEARLY_FALLBACK_COL, alpha=0.55,
           edgecolor="none", label="年度均分兜底 (2002-2022)")
    ax.bar(precise["date"].values, precise["billion"].values,
           width=22, color=MONTHLY_PRECISE_COL, alpha=0.85,
           edgecolor="none", label="月度精确 (2023-至今, akshare)")

    ax.axhline(HOT_THRESHOLD, color="#dc2626", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(fused["date"].max(), HOT_THRESHOLD + 50,
            f"过热 ≥{HOT_THRESHOLD:.0f}", color="#dc2626",
            fontsize=7, ha="right", alpha=0.85)
    ax.axhline(COLD_THRESHOLD, color="#16a34a", linewidth=0.9,
               linestyle="--", alpha=0.7)
    ax.text(fused["date"].max(), COLD_THRESHOLD + 50,
            f"冰点 <{COLD_THRESHOLD:.0f}", color="#16a34a",
            fontsize=7, ha="right", alpha=0.85)

    fmt_xaxis(ax)
    ax.set_xlim(fused["date"].min(), fused["date"].max())
    ax.set_ylabel("月发新基（亿元）", color="#1a1a2e", fontsize=9)
    ax.set_title("② 月度新发募集亿元 — 精确月度 + 年度均分兜底",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    )
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)


# ── ③ 月度只数 vs 沪深 300 ──────────────────────────────────
def plot_count_vs_cs300(
    ax: plt.Axes, cnt: pd.DataFrame, cs300: pd.DataFrame
) -> None:
    style_ax(ax)
    # 2006 起对齐沪深 300 起点
    c = cnt[cnt["date"] >= "2006-01-01"].copy()
    ax.bar(c["date"].values, c["new_fund_count_xq"].values,
           width=22, color=COUNT_COL, alpha=0.55, edgecolor="none",
           label="月发只数")

    ax2 = ax.twinx()
    cs_monthly = cs300["close"].resample("ME").last()
    cs_monthly = cs_monthly[cs_monthly.index >= "2006-01-01"]
    ax2.plot(cs_monthly.index, cs_monthly.values,
             color=CS300_COL, linewidth=1.4, label="沪深 300 月末")
    ax2.set_ylabel("沪深 300 点位", color=CS300_COL, fontsize=8)
    ax2.tick_params(colors=CS300_COL, labelsize=7)
    ax2.spines["right"].set_color(CS300_COL)

    fmt_xaxis(ax)
    ax.set_xlim(c["date"].min(), c["date"].max())
    ax.set_ylabel("月发只数", color="#1a1a2e", fontsize=9)
    ax.set_title("③ 月发只数 vs 沪深 300（2006-至今）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92)
    ax2.legend(loc="upper right", fontsize=7, framealpha=0.92)

    # 相关系数
    m_indexed = c.set_index("date")["new_fund_count_xq"]
    m_indexed.index = pd.to_datetime(m_indexed.index) + pd.offsets.MonthEnd(0)
    merged = pd.concat([m_indexed.rename("cnt"),
                        cs_monthly.rename("cs300")], axis=1).dropna()
    if len(merged) >= 10:
        corr = merged["cnt"].corr(merged["cs300"])
        corr_chg = merged["cnt"].pct_change().corr(
            merged["cs300"].pct_change()
        )
        ax.text(
            0.01, 0.97,
            f"水平 ρ = {corr:+.2f}\n月变化率 ρ = {corr_chg:+.2f}",
            transform=ax.transAxes, fontsize=7, va="top",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=GRID_COL, alpha=0.92),
        )


# ── ④ 类型构成堆叠（2014+）─────────────────────────────────
def plot_type_stack(ax: plt.Axes, cnt: pd.DataFrame) -> None:
    style_ax(ax)
    c = cnt[cnt["date"] >= "2014-01-01"].copy()
    dates = c["date"].values
    width = 22

    series = [
        ("股票型",   c["cnt_equity"].astype(int), EQUITY_COL),
        ("混合型",   c["cnt_mixed"].astype(int),  MIXED_COL),
        ("被动指数", c["cnt_index"].astype(int),  INDEX_COL_T),
        ("债券型",   c["cnt_bond"].astype(int),   BOND_COL),
        ("QDII",     c["cnt_qdii"].astype(int),   QDII_COL),
        ("FOF/其他", (c["cnt_fof"] + c["cnt_other"]).astype(int), OTHER_COL),
    ]
    bottom = pd.Series(0, index=range(len(c)))
    for label, vals, color in series:
        ax.bar(dates, vals.values, width=width, bottom=bottom.values,
               color=color, alpha=0.85, edgecolor="none", label=label)
        bottom = bottom + vals.reset_index(drop=True)

    fmt_xaxis(ax)
    ax.set_xlim(c["date"].min(), c["date"].max())
    ax.set_ylabel("当月新成立只数（按类型）", color="#1a1a2e", fontsize=9)
    ax.set_title("④ 月度新成立基金 分类型堆叠（2014-2026）",
                 color="#1a1a2e", fontsize=10, pad=6, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.92, ncol=2)


def main() -> None:
    data = load_all()

    fig = plt.figure(figsize=(18, 18))
    fig.patch.set_facecolor("white")
    fig.suptitle(
        "公募新发募集 全数据特征分析（2001-2026）",
        fontsize=16, fontweight="bold", color="#1a1a2e", y=0.99,
    )

    ax1 = fig.add_axes([0.06, 0.74, 0.92, 0.18])
    ax2 = fig.add_axes([0.06, 0.51, 0.92, 0.18])
    ax3 = fig.add_axes([0.06, 0.29, 0.92, 0.18])
    ax4 = fig.add_axes([0.06, 0.07, 0.92, 0.18])

    plot_count(ax1, data["cnt"])
    plot_billion_fused(ax2, data["fused_billion"])
    plot_count_vs_cs300(ax3, data["cnt"], data["cs300"])
    plot_type_stack(ax4, data["cnt"])

    # 底部摘要
    cnt = data["cnt"]
    total_cnt = int(cnt["new_fund_count_xq"].sum())
    summary = (
        f"  数据覆盖：2001-09 ~ 2026-06 共 {len(cnt)} 个月，累计 {total_cnt:,} 只基金\n"
        f"  数据源：① 月度只数 = 雪球 fund_individual_basic_info_xq 全样本聚合 "
        f"｜② 月度亿元 = akshare 东财 (2023+) + Wind 整理年度合计 / 12 (2002-2022)\n"
        f"  ⚠️ 已知限制：雪球接口对 2020+ 部分新基（含 ETF/已清盘等）返回错误，"
        f"2020-2022 实际数量略低于真实值约 40-50%"
    )
    fig.text(0.5, 0.03, summary, ha="center", va="top",
             fontsize=8.5, color="#495057",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#f1f3f5",
                       edgecolor=GRID_COL))
    fig.text(0.5, 0.005,
             "数据来源：teststock MySQL · "
             "cn_fund_new_count_monthly + cn_fund_new_monthly + cn_fund_new_yearly",
             ha="center", fontsize=7, color="#adb5bd")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"已保存：{OUT_PATH}")


if __name__ == "__main__":
    main()

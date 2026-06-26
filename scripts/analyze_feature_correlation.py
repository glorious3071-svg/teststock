#!/usr/bin/env python3.11
"""评分卡特征 vs 沪深 300 关联性分析

对 macro_annual_snapshot 表中的所有数值特征，逐个计算与「次年沪深 300 涨跌幅」
的关联强度（Spearman ρ）。

红线：
  - snapshot_date = (apply_year - 1, 12-31)
  - 次年沪深 300 收益 = year 全年 (year-01-01 ~ year-12-31)
  - 所有数据严防上帝视角

评估指标：
  - n: 样本数（特征有效的年份数）
  - ρ: Spearman 等级相关
  - |ρ|: 信号强度
  - 方向: ρ<0 → 评分卡反向指标（数值越大越要减仓）；ρ>0 → 正向指标
  - 显著性: |ρ|>0.5 强；0.3<|ρ|≤0.5 中；≤0.3 弱
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

OUT_CSV = ROOT / "data" / "scorecard_feature_correlation.csv"


# ── 特征字典: 评分卡用到的字段 + 元数据 ────────────────────────
# (字段名, 维度, 评分卡用途, 期望方向)
# 期望方向: -1 表示"高值=减仓信号"(ρ应负)，+1 表示"高值=加仓信号"(ρ应正)
FEATURES = [
    # 估值
    ("hs300_pe_ttm",       "valuation",   "PE >50/40/30 风险、<20/15 机会", -1),
    ("hs300_pb",           "valuation",   "PB >3 风险、<2 机会",          -1),
    # 流动性
    ("shibor_3m",          "liquidity",   "rate_cum_bp_12m 派生源",       -1),
    ("shibor_3m_yoy_bp",   "liquidity",   "加息累计基点 → 减仓信号",       -1),
    ("lpr_1y_yoy_bp",      "liquidity",   "LPR 变动 → 减仓信号",          -1),
    ("shibor_1y",          "liquidity",   "deposit_1y_rate 代理",         -1),
    # 基本面
    ("pmi_mfg",            "fundamental", "PMI 制造业, >53 过热",         -1),
    ("pmi_non_mfg",        "fundamental", "PMI 非制造业",                 +1),
    ("pmi_composite",      "fundamental", "PMI 综合",                     +1),
    ("ppi_yoy",            "fundamental", "PPI 同比",                     +1),
    ("ppi_accu",           "fundamental", "PPI 累计",                     +1),
    ("cpi_yoy",            "fundamental", "CPI 同比",                     -1),
    ("gdp_yoy",            "fundamental", "GDP 同比",                     +1),
    ("si_yoy",             "fundamental", "第二产业同比（IVA 代理）",      +1),
    ("ti_yoy",             "fundamental", "第三产业同比",                  +1),
    ("m1_yoy",             "fundamental", "M1 同比",                      +1),
    ("m2_yoy",             "fundamental", "M2 同比",                      +1),
    ("m1_m2_scissors",     "fundamental", "M1-M2 剪刀差 → 流动性活化",     +1),
    ("sf_stk_yoy",         "fundamental", "社融存量同比",                  +1),
    # 情绪 (margin)
    ("margin_rzrqye_yoy_pct", "sentiment", "两融 YoY <-20% 机会信号",     -1),
    ("margin_rzrqye",      "sentiment",   "两融余额绝对值",                -1),
    # 外部
    ("us_10y_nominal",     "external",    "美 10Y 名义利率",              -1),
    ("us_10y_real",        "external",    "美 10Y 实际利率",              -1),
    ("us_10y_real_yoy_bp", "external",    "美 10Y 实际利率 YoY",          -1),
    ("us_tbill_13w",       "external",    "美 3M 国库券",                 -1),
    ("libor_3m_usd",       "external",    "USD LIBOR 3M",                -1),
]


def main():
    conn = pymysql.connect(
        host="127.0.0.1", user="teststock", password="teststock", database="teststock",
    )
    snap = pd.read_sql(
        f"SELECT apply_year, {','.join(f[0] for f in FEATURES)} FROM macro_annual_snapshot ORDER BY apply_year",
        conn,
    )
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=["trade_date"], index_col="trade_date",
    )
    conn.close()

    # 计算次年涨跌
    yearly_ret = {}
    for year in range(int(snap["apply_year"].min()), int(snap["apply_year"].max()) + 1):
        try:
            o = cs.loc[cs.index >= f"{year}-01-01"].iloc[0]["close"]
            c = cs.loc[cs.index <= f"{year}-12-31"].iloc[-1]["close"]
            if (cs.index.max() - pd.Timestamp(f"{year}-12-31")).days < 0:
                continue  # 未完整
            yearly_ret[year] = (float(c) / float(o) - 1) * 100
        except Exception:
            pass
    rets = pd.Series(yearly_ret).sort_index()

    snap = snap.set_index("apply_year")
    df = snap.join(rets.rename("cs300_next_year_ret"), how="inner")

    # 算每个特征的 Spearman / Pearson
    results = []
    for col, dim, desc, expected_dir in FEATURES:
        sub = df[[col, "cs300_next_year_ret"]].dropna()
        n = len(sub)
        if n < 5:
            results.append({
                "维度": dim, "字段": col, "样本数 n": n,
                "ρ_Spearman": None, "ρ_Pearson": None,
                "期望方向": "+" if expected_dir > 0 else "-",
                "强度": "数据不足", "说明": desc,
            })
            continue
        rho_s = sub[col].rank().corr(sub["cs300_next_year_ret"].rank())
        rho_p = sub[col].corr(sub["cs300_next_year_ret"])
        # 强度
        a = abs(rho_s)
        strength = "强" if a > 0.5 else "中" if a > 0.3 else "弱" if a > 0.15 else "无"
        # 方向一致性
        if expected_dir > 0:
            dir_match = "✓" if rho_s > 0 else "✗"
        else:
            dir_match = "✓" if rho_s < 0 else "✗"
        results.append({
            "维度": dim, "字段": col, "样本数 n": n,
            "ρ_Spearman": round(rho_s, 3),
            "ρ_Pearson": round(rho_p, 3),
            "期望方向": "+" if expected_dir > 0 else "-",
            "实际方向": dir_match,
            "强度": strength,
            "说明": desc,
        })

    out = pd.DataFrame(results)
    out = out.sort_values(["维度", "ρ_Spearman"], ascending=[True, True], key=lambda x: x.fillna(99) if x.name == "ρ_Spearman" else x)

    print("=" * 110)
    print("评分卡特征 vs 次年沪深 300 涨跌  关联性分析")
    print("=" * 110)
    print(out.to_string(index=False))

    # 按 |ρ| 排序 top 强相关
    valid = out[out["ρ_Spearman"].notna()].copy()
    valid["|ρ|"] = valid["ρ_Spearman"].abs()
    print("\n" + "=" * 110)
    print("按 |ρ| 排序 — 信号强度榜单")
    print("=" * 110)
    print(valid.sort_values("|ρ|", ascending=False)[
        ["维度", "字段", "样本数 n", "ρ_Spearman", "期望方向", "实际方向", "强度"]
    ].to_string(index=False))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n结果已保存：{OUT_CSV}")


if __name__ == "__main__":
    main()

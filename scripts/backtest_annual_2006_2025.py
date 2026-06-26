#!/usr/bin/env python3.11
"""
2006-2025 完整年度回测

每年初用上年 12-31 的 snapshot 计算评分和仓位，全年持有。
输出逐年评分、仓位、CS300 涨跌、方向命中。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs,
    evaluate_scorecard,
    policy_triple_gate,
    score_to_target_equity,
)

load_dotenv(ROOT / ".env")


def db():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
        charset="utf8mb4",
    )


def load_inputs(cur, snap: date) -> ScorecardInputs:
    """加载 snapshot 时点的所有评分卡输入"""
    snap_str = snap.strftime("%Y-%m-%d")
    snap_m = snap.strftime("%Y%m")
    one_y_ago = (snap - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    # 估值
    cur.execute(
        "SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    pe, pb = (float(r[0]), float(r[1])) if r else (None, None)

    # 流动性
    cur.execute(
        "SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (snap_str,),
    )
    cur_r = cur.fetchone()
    cur.execute(
        "SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (one_y_ago,),
    )
    prior_r = cur.fetchone()
    rate_bp = (
        (float(cur_r[0]) - float(prior_r[0])) * 100
        if cur_r and prior_r and cur_r[0] is not None and prior_r[0] is not None
        else None
    )

    cur.execute(
        """SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
           WHERE effective_date > %s AND effective_date <= %s AND inst_type IN ('large','all')""",
        (one_y_ago, snap_str),
    )
    rrr_pp = float(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT AVG(rate_1y) FROM shibor_daily WHERE trade_date BETWEEN %s AND %s",
        ((snap - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), snap_str),
    )
    r = cur.fetchone()
    deposit = float(r[0]) if r and r[0] is not None else None

    # 基本面
    cur.execute(
        "SELECT month, pmi_mfg, pmi_production, pmi_new_order, pmi_non_mfg FROM cn_pmi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 12",
        (snap_m,),
    )
    pmis = cur.fetchall()
    consec = 0
    for _, v, _, _, _ in pmis:
        if v is not None and float(v) < 52:
            consec += 1
        else:
            break

    resume = (
        len(pmis) >= 2
        and pmis[1][1] is not None
        and pmis[0][1] is not None
        and float(pmis[1][1]) < 50 <= float(pmis[0][1])
    )

    last3 = [float(p[1]) for p in pmis[:3] if p[1] is not None]
    pmi_3m = sum(last3) / 3 if len(last3) == 3 else None

    pmi_po = (
        float(pmis[0][2]) - float(pmis[0][3])
        if pmis and pmis[0][2] is not None and pmis[0][3] is not None
        else None
    )

    pmi_non_mfg = float(pmis[0][4]) if pmis and pmis[0][4] is not None else None

    # PPI
    cur.execute(
        "SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (snap_m,),
    )
    r = cur.fetchone()
    ppi_now = float(r[0]) if r and r[0] is not None else None

    cur.execute(
        "SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (f"{snap.year - 1}{snap_m[4:]}",),
    )
    r = cur.fetchone()
    ppi_prior = float(r[0]) if r and r[0] is not None else None

    if ppi_now is None or ppi_prior is None:
        ppi_change = None
    elif ppi_prior >= 0 and ppi_now < 0:
        ppi_change = "turn_negative"
    elif ppi_prior < 0 and ppi_now >= 0:
        ppi_change = "turn_positive"
    else:
        ppi_change = "flat"

    # 情绪
    cur.execute(
        "SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (snap_m,),
    )
    r = cur.fetchone()
    nb, nc = (float(r[0]), int(r[1])) if r and r[0] is not None else (None, None)

    cur.execute(
        "SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    mc = float(r[0]) if r and r[0] is not None else None

    cur.execute(
        "SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1",
        (one_y_ago,),
    )
    r = cur.fetchone()
    mp = float(r[0]) if r and r[0] is not None else None

    margin_yoy = (mc / mp - 1) * 100 if mc and mp else None

    # 外部
    cur.execute(
        "SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    spx_cur = float(r[0]) if r and r[0] is not None else None

    last_me = (snap - pd.offsets.MonthBegin(1) - pd.Timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    cur.execute(
        "SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (last_me,),
    )
    r = cur.fetchone()
    spx_prior = float(r[0]) if r and r[0] is not None else None

    us_m = (spx_cur / spx_prior - 1) * 100 if spx_cur and spx_prior else None

    cur.execute(
        "SELECT effective_date, direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 8",
        (snap_str,),
    )
    fed_recent = cur.fetchall()
    fed_reversal = None
    last_cut = next((r for r in fed_recent if r[1] == "cut"), None)
    if last_cut:
        cur.execute(
            "SELECT direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date < %s ORDER BY effective_date DESC LIMIT 1",
            (last_cut[0],),
        )
        prev = cur.fetchone()
        if prev and prev[0] == "hike":
            fed_reversal = "hike_to_cut"

    cur.execute(
        "SELECT rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    fed_zero = (float(r[0]) <= 0.25) if r and r[0] is not None else False
    fed_rate_level = float(r[0]) if r and r[0] is not None else None

    snap_m_start = snap.replace(day=1).strftime("%Y-%m-%d")
    votes = 0
    for c in ("USA", "G4E", "CHN", "JPN", "G7"):
        cur.execute(
            "SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= %s ORDER BY period DESC LIMIT 3",
            (c, snap_m_start),
        )
        vals = [float(v[0]) for v in cur.fetchall() if v[0] is not None]
        if len(vals) >= 3 and vals[0] < 100 and vals[0] < vals[1] < vals[2]:
            votes += 1
    global_rec = votes >= 2

    six_mo_ago = (snap - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    cur.execute(
        """SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
           WHERE direction='cut' AND effective_date BETWEEN %s AND %s
             AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""",
        (six_mo_ago, snap_str),
    )
    cb_cut_n = int(cur.fetchone()[0])

    cur.execute(
        "SELECT gold_yoy_pct, vix_30d_avg FROM external_macro_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (snap_m,),
    )
    r = cur.fetchone()
    gold_yoy_pct = float(r[0]) if r and r[0] is not None else None
    vix_30d_avg = float(r[1]) if r and r[1] is not None else None

    # 企业景气指数（季度，前向填充）
    cur.execute(
        "SELECT quarter_date, boom_index FROM cn_enterprise_boom_quarterly ORDER BY quarter_date"
    )
    boom_rows = cur.fetchall()
    boom_series = pd.DataFrame(boom_rows, columns=["date", "boom_index"]).set_index(
        "date"
    )["boom_index"]
    boom_series.index = pd.to_datetime(boom_series.index)
    boom_monthly = boom_series.resample("ME").last().ffill()
    boom_monthly.index = boom_monthly.index.to_period("M").to_timestamp("M")
    snap_ts = pd.Timestamp(snap).to_period("M").to_timestamp("M")
    enterprise_boom = (
        float(boom_monthly.get(snap_ts, np.nan))
        if not pd.isna(boom_monthly.get(snap_ts, np.nan))
        else None
    )

    # 政策
    snap_year = snap.year
    cewc_apply = snap_year + 1 if snap.month == 12 else snap_year
    cur.execute(
        "SELECT tone, fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=%s",
        (cewc_apply,),
    )
    r = cur.fetchone()
    pboc_tone = cmt = None
    if r:
        tone, fiscal, monetary = r
        pboc_tone = (
            "loose"
            if monetary and "宽松" in monetary
            else ("tight" if monetary and "紧" in monetary else "neutral")
        )
        cmt = (
            "expansionary"
            if fiscal and "积极" in fiscal
            else ("dual_prevent" if tone and "双防" in tone else "neutral")
        )

    cur.execute(
        "SELECT direction FROM stamp_duty_events WHERE event_type='stamp_duty' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    stamp_duty = r[0] if r else None

    cur.execute(
        "SELECT direction FROM national_team_actions WHERE effective_date <= %s ORDER BY effective_date DESC LIMIT 1",
        (snap_str,),
    )
    r = cur.fetchone()
    national = r[0] if r else None

    return ScorecardInputs(
        cs300_pe_ttm=pe,
        cs300_pb=pb,
        rate_cum_bp_12m=rate_bp,
        rrr_cum_pp_12m=rrr_pp,
        deposit_1y_rate=deposit,
        pmi_below_52_months=consec,
        iva_yoy_trend=None,
        ppi_yoy=ppi_now,
        ppi_yoy_change=ppi_change,
        pmi_resume_expansion=resume,
        pmi_mfg_3m_avg=pmi_3m,
        pmi_prod_minus_order=pmi_po,
        pmi_non_mfg=pmi_non_mfg,
        new_fund_billion=nb,
        new_fund_count=nc,
        margin_growth_pct=margin_yoy,
        fed_reversal=fed_reversal,
        us_monthly_pct=us_m,
        global_recession=global_rec,
        fed_zero_qe=fed_zero,
        global_stimulus=cb_cut_n >= 3,
        cb_cuts_6m=cb_cut_n,
        gold_yoy_pct=gold_yoy_pct,
        vix_30d_avg=vix_30d_avg,
        fed_rate_level=fed_rate_level,
        enterprise_boom_index=enterprise_boom,
        pboc_tone=pboc_tone,
        stamp_duty=stamp_duty,
        central_meeting_tone=cmt,
        national_team_action=national,
    )


def main():
    conn = db()
    cur = conn.cursor()

    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn,
        parse_dates=["trade_date"],
        index_col="trade_date",
    )
    cs["close"] = cs["close"].astype(float)
    conn.close()

    print("=" * 100)
    print("2006-2025 完整年度回测（每年初用上年 12-31 snapshot 定仓，全年持有）")
    print("=" * 100)

    rows = []
    for year in range(2006, 2026):
        snap = date(year - 1, 12, 31)
        try:
            inp = load_inputs(cur, snap)
            r = evaluate_scorecard(year, inp)

            # 三重门校验
            eq = r.target_equity_pct
            if eq >= 80:
                passed, _ = policy_triple_gate(inp)
                if not passed:
                    eq = 80.0

            # CS300 全年涨跌
            try:
                cs_o = cs.loc[cs.index >= f"{year}-01-01"].iloc[0]["close"]
                cs_c = cs.loc[cs.index <= f"{year}-12-31"].iloc[-1]["close"]
                cs_ret = (cs_c / cs_o - 1) * 100
            except:
                cs_ret = None

            # 方向判定
            direction = ""
            if cs_ret is not None:
                if r.total_score < 0 and cs_ret > 0:
                    direction = "✓ 对"
                elif r.total_score > 0 and cs_ret < 0:
                    direction = "✓ 对"
                elif r.total_score < 0 and cs_ret < 0:
                    direction = "❌ 错（假加仓）"
                elif r.total_score > 0 and cs_ret > 0:
                    direction = "❌ 错（假减仓）"
                else:
                    direction = "- 中性"

            rows.append(
                {
                    "year": year,
                    "score": r.total_score,
                    "eq": eq,
                    "cs_ret": cs_ret,
                    "direction": direction,
                    "band": r.band,
                }
            )
        except Exception as e:
            print(f"  {year}: ERR {e}")

    df = pd.DataFrame(rows)

    print(
        f'\n{"年":>5}{"评分":>6}{"仓位":>6}{"CS300%":>9}{"方向":>20}{"档位":>15}'
    )
    print("-" * 75)
    for _, row in df.iterrows():
        cs_str = f'{row["cs_ret"]:+.1f}%' if row["cs_ret"] is not None else "N/A"
        print(
            f'{row["year"]:>5}{row["score"]:>+6d}{row["eq"]:>6.0f}%{cs_str:>9}{row["direction"]:>20}{row["band"]:>15}'
        )

    # 统计
    n_correct = (df["direction"].str.contains("✓")).sum()
    n_wrong = (df["direction"].str.contains("❌")).sum()
    n_total = len(df[df["direction"] != "- 中性"])

    print(f"\n{'=' * 100}")
    print("回测统计")
    print("=" * 100)
    print(f"  总年数：{len(df)}")
    print(f"  方向命中：{n_correct}/{n_total} = {n_correct / n_total * 100:.0f}%")
    print(f"  方向错误：{n_wrong}/{n_total} = {n_wrong / n_total * 100:.0f}%")

    # 分档统计
    print(f"\n分档表现：")
    for band in df["band"].unique():
        sub = df[df["band"] == band]
        avg_ret = sub["cs_ret"].mean()
        hit_rate = (sub["direction"].str.contains("✓")).sum() / len(sub) * 100 if len(sub) > 0 else 0
        print(f"  {band:15s}: {len(sub):3d} 年, 均收益 {avg_ret:+.1f}%, 命中率 {hit_rate:.0f}%")

    # 关键年份
    print(f"\n关键年份详情：")
    key_years = [2007, 2008, 2014, 2015, 2018, 2022, 2024, 2025]
    for y in key_years:
        sub = df[df["year"] == y]
        if not sub.empty:
            row = sub.iloc[0]
            print(f"  {y}: 评分 {row['score']:+d}, 仓位 {row['eq']:.0f}%, CS300 {row['cs_ret']:+.1f}%, {row['direction']}")


if __name__ == "__main__":
    main()

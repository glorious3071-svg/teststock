#!/usr/bin/env python3.11
"""scripts/backtest_v3412_vs_v3411.py — 新旧评分卡 21 年综合对比

通过临时"回填"10 条已注释规则模拟 v3.4.11 完整版，
与当前 v3.4.12 裁剪版做并排对比。

输出：
  1. 逐年评分/仓位/P&L 对比表
  2. 关键指标对比（累计回报/年化/波动/MDD/Calmar/Sharpe）
  3. 前后两段（前/后 10 年）超额收益分解
  4. 净值曲线 + 仓位时序 双面板可视化
  5. 落盘 data/backtests/v3412_vs_v3411_comparison.json
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    evaluate_scorecard, score_to_target_equity, ScoreItem,
)
from backtest.scorecard_adapter import load_scorecard_inputs, mysql_config

CASH_RATE = 0.02
CS300 = "000300.SH"
START_YEAR = 2006
END_YEAR = 2026


# ─── v3.4.11 完整版：回填 10 条已注释规则 ────────
def v3411_extra_items(inp) -> list[ScoreItem]:
    """模拟 v3.4.11 完整版中、v3.4.12 已注释的 10 条规则"""
    items = []
    # 估值：PE>30 / PE<20 / PB>3 / PB<2
    pe = inp.cs300_pe_ttm
    pb = inp.cs300_pb
    if pe is not None:
        if 30 < pe <= 40:
            items.append(ScoreItem("valuation", "PE>30", "risk", +1))
        if 15 <= pe < 20:
            items.append(ScoreItem("valuation", "PE<20", "opportunity", -1))
    if pb is not None:
        if pb > 3:
            items.append(ScoreItem("valuation", "PB>3", "risk", +1))
        if pb < 2:
            items.append(ScoreItem("valuation", "PB<2", "opportunity", -1))
    # 流动性：降息降准共振
    if (inp.rate_cum_bp_12m is not None and inp.rrr_cum_pp_12m is not None
            and inp.rate_cum_bp_12m < -100 and inp.rrr_cum_pp_12m < -1):
        items.append(ScoreItem("liquidity", "降息降准共振(双松)", "opportunity", -1))
    # 基本面：PMI<52连续≥6月 / PPI转负 / PPI触底反弹 / PMI3M均≥53
    if inp.pmi_below_52_months and inp.pmi_below_52_months >= 6:
        items.append(ScoreItem("fundamental", "PMI<52连续≥6月(深度收缩)",
                                 "opportunity", -1))
    if inp.ppi_yoy_change == "turn_negative":
        items.append(ScoreItem("fundamental", "PPI转负", "risk", +2))
    if inp.ppi_yoy_change == "turn_positive":
        items.append(ScoreItem("fundamental", "PPI触底反弹", "opportunity", -1))
    if inp.pmi_mfg_3m_avg is not None and inp.pmi_mfg_3m_avg >= 53.0:
        items.append(ScoreItem("fundamental", "PMI3M均≥53(景气过热)", "risk", +1))
    # 外部：美股月涨>5%
    if inp.us_monthly_pct is not None and inp.us_monthly_pct > 5:
        items.append(ScoreItem("external", "美股月涨>5%", "opportunity", -1))
    return items


def evaluate_both_versions(year, inp):
    """同时计算 v3.4.12 和 v3.4.11 的评分"""
    r12 = evaluate_scorecard(year, inp)
    extra = v3411_extra_items(inp)
    score_11 = r12.total_score + sum(it.score for it in extra)
    eq11, band11 = score_to_target_equity(score_11)
    return {
        'v12_score': r12.total_score, 'v12_eq': r12.target_equity_pct,
        'v12_items': len(r12.items),
        'v11_score': score_11, 'v11_eq': eq11,
        'v11_extra_items': [(it.name, it.score) for it in extra],
    }


def cs300_year(cur, year):
    cur.execute("SELECT trade_date, close FROM index_daily WHERE ts_code=%s AND trade_date >= %s ORDER BY trade_date ASC LIMIT 1",
                (CS300, f"{year}-01-01"))
    f = cur.fetchone()
    cur.execute("SELECT trade_date, close FROM index_daily WHERE ts_code=%s AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
                (CS300, f"{year}-12-31"))
    l = cur.fetchone()
    if not f or not l: return None
    return f[0], float(f[1]), l[0], float(l[1])


def annual_pnl(eq_pct, ret_pct, days=365):
    eq = eq_pct / 100
    return eq * ret_pct + (1 - eq) * CASH_RATE * 100 * days/365


def stats(curve, pnls, rets):
    """统计：总回报、年化、MDD、波动、Sharpe、Calmar"""
    n = len(pnls)
    total = (curve[-1]/curve[0] - 1) * 100
    ann = ((curve[-1]/curve[0])**(1/n) - 1) * 100
    peak, dd = curve[0], 0
    for v in curve:
        peak = max(peak, v); dd = min(dd, v/peak-1)
    mdd = dd * 100
    mean = sum(pnls)/n
    var = sum((p-mean)**2 for p in pnls)/n
    vol = var**0.5
    sharpe = (ann - 2.0) / vol if vol > 0 else 0
    calmar = ann / abs(mdd) if mdd < 0 else 0
    return {'total':total, 'ann':ann, 'mdd':mdd, 'vol':vol,
            'sharpe':sharpe, 'calmar':calmar}


def main():
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    records = []
    with conn.cursor() as cur:
        for year in range(START_YEAR, END_YEAR + 1):
            snap = date(year - 1, 12, 31)
            inp = load_scorecard_inputs(snap, conn=conn)
            both = evaluate_both_versions(year, inp)
            rng = cs300_year(cur, year)
            if not rng: continue
            f_d, op, l_d, cp = rng
            cs_ret = (cp/op - 1) * 100
            days = (l_d - f_d).days
            v12_pnl = annual_pnl(both['v12_eq'], cs_ret, days)
            v11_pnl = annual_pnl(both['v11_eq'], cs_ret, days)
            is_partial = (l_d.year == year and l_d.month < 12)
            records.append({
                'year':year, 'cs_ret':cs_ret, 'partial':is_partial,
                **both,
                'v12_pnl':v12_pnl, 'v11_pnl':v11_pnl,
                'fullbuy_pnl':cs_ret,
            })
    conn.close()
    n = len(records)

    # 累计权益
    cap_v12 = [1_000_000]
    cap_v11 = [1_000_000]
    cap_fb = [1_000_000]
    for r in records:
        cap_v12.append(cap_v12[-1] * (1 + r['v12_pnl']/100))
        cap_v11.append(cap_v11[-1] * (1 + r['v11_pnl']/100))
        cap_fb.append(cap_fb[-1] * (1 + r['fullbuy_pnl']/100))

    # ─── 输出 1：逐年对比表 ──────────────────────
    print("="*125)
    print(f"【v3.4.12 裁剪 vs v3.4.11 完整】 21 年实盘对比（起始 100 万元）")
    print("="*125)
    print(f"\n{'年':<5}{'CS300%':>9}|{'v12分':>6}{'v12仓':>6}{'v12年%':>8}{'v12万':>9}|"
          f"{'v11分':>6}{'v11仓':>6}{'v11年%':>8}{'v11万':>9}|"
          f"{'差额万':>9}{'额外触发':>20}")
    print("-"*125)
    for i, r in enumerate(records, 1):
        extras = [it for it in r['v11_extra_items']]
        extras_str = ",".join(f"{n[:6]}{s:+d}" for n, s in extras)[:18]
        partial = '*' if r['partial'] else ''
        print(f"{r['year']:<5}{r['cs_ret']:>+8.1f}%|"
              f"{r['v12_score']:>+6d}{r['v12_eq']:>5.0f}%"
              f"{r['v12_pnl']:>+7.1f}%{cap_v12[i]/10000:>9,.1f}|"
              f"{r['v11_score']:>+6d}{r['v11_eq']:>5.0f}%"
              f"{r['v11_pnl']:>+7.1f}%{cap_v11[i]/10000:>9,.1f}|"
              f"{(cap_v12[i]-cap_v11[i])/10000:>+9,.1f}{extras_str:>20} {partial}")

    # ─── 输出 2：关键指标对比 ─────────────────────
    v12_pnls = [r['v12_pnl'] for r in records]
    v11_pnls = [r['v11_pnl'] for r in records]
    fb_pnls = [r['fullbuy_pnl'] for r in records]
    s12 = stats(cap_v12, v12_pnls, [r['cs_ret'] for r in records])
    s11 = stats(cap_v11, v11_pnls, [r['cs_ret'] for r in records])
    sfb = stats(cap_fb, fb_pnls, [r['cs_ret'] for r in records])

    print('-'*125)
    print(f"\n=== 关键指标 ({n} 年) ===")
    print(f"\n   {'指标':<22}{'v3.4.12 裁剪':>14}{'v3.4.11 完整':>14}{'差距':>10}{'100% 满仓':>14}")
    print('-'*78)
    for name, k, fmt in [
        ('终值（万元）', None, None),
        ('累计回报 (%)', 'total', '+9.1f'),
        ('年化收益 (%)', 'ann', '+9.2f'),
        ('年化波动 (%)', 'vol', '9.2f'),
        ('最大回撤 (%)', 'mdd', '+9.2f'),
        ('Sharpe (-2%)', 'sharpe', '+9.3f'),
        ('Calmar', 'calmar', '+9.3f'),
    ]:
        if k is None:
            print(f"   {name:<22}{cap_v12[-1]/10000:>14,.1f}{cap_v11[-1]/10000:>14,.1f}"
                  f"{(cap_v12[-1]-cap_v11[-1])/10000:>+10,.1f}{cap_fb[-1]/10000:>14,.1f}")
        else:
            print(f"   {name:<22}{s12[k]:>14.3f}{s11[k]:>14.3f}"
                  f"{s12[k]-s11[k]:>+10.3f}{sfb[k]:>14.3f}")

    # ─── 输出 3：前后两段细分 ─────────────────────
    mid = n // 2
    def seg_stats(rs, pnls_key):
        cap = [1_000_000]
        for r in rs: cap.append(cap[-1] * (1 + r[pnls_key]/100))
        return stats(cap, [r[pnls_key] for r in rs], [r['cs_ret'] for r in rs])

    print(f"\n=== 前后两段对比（{START_YEAR}-{START_YEAR+mid-1} 前 / {START_YEAR+mid}-{END_YEAR} 后） ===")
    print(f"\n   {'区间':<22}{'v12 年化':>12}{'v11 年化':>12}{'满仓 年化':>13}"
          f"{'v12 超额':>11}{'v11 超额':>11}")
    print('-'*82)
    first_half = records[:mid]
    second_half = records[mid:]
    for label, sub in [(f"{START_YEAR}-{START_YEAR+mid-1} 前半段", first_half),
                        (f"{START_YEAR+mid}-{END_YEAR} 后半段", second_half)]:
        v12s = seg_stats(sub, 'v12_pnl')
        v11s = seg_stats(sub, 'v11_pnl')
        fbs = seg_stats(sub, 'fullbuy_pnl')
        print(f"   {label:<22}{v12s['ann']:>+11.2f}%{v11s['ann']:>+11.2f}%"
              f"{fbs['ann']:>+12.2f}%{v12s['ann']-fbs['ann']:>+10.2f}{v11s['ann']-fbs['ann']:>+10.2f}")

    # ─── 仓位分布 ────────────────────────────────
    from collections import Counter
    v12_dist = Counter(r['v12_eq'] for r in records)
    v11_dist = Counter(r['v11_eq'] for r in records)
    print(f"\n=== 仓位分布对比 ===")
    print(f"   {'仓位':<8}{'v12':>8}{'v11':>8}{'Δ':>6}")
    print('-'*32)
    all_pos = sorted(set(v12_dist.keys()) | set(v11_dist.keys()), reverse=True)
    for p in all_pos:
        a = v12_dist.get(p, 0); b = v11_dist.get(p, 0)
        print(f"   {p:>5.0f}%   {a:>5}年   {b:>5}年{a-b:>+5}年")

    # ─── 落盘 ────────────────────────────────────
    out = {
        'config': {'start_year':START_YEAR, 'end_year':END_YEAR,
                   'cash_rate':CASH_RATE},
        'yearly': records, 'capital_v12':cap_v12, 'capital_v11':cap_v11,
        'capital_fullbuy':cap_fb,
        'stats': {'v12':s12, 'v11':s11, 'fullbuy':sfb},
    }
    out_path = ROOT / "data" / "backtests" / "v3412_vs_v3411_comparison.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    print(f"\n结果已保存：{out_path}")

    return records, cap_v12, cap_v11, cap_fb


if __name__ == "__main__":
    main()

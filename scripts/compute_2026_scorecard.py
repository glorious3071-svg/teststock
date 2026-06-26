#!/usr/bin/env python3.11
"""compute_2026_scorecard.py — 计算 2026 年初评分卡（snapshot=2025-12-31）

严格调用 backtest/scorecard.py 的当前规则集（含 v5 月发新基 + v6 两融），
不引入任何规则修改，只填数据。

数据就绪清单见上方 probe；本脚本逐字段加载并打印每项触发明细。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs,
    evaluate_scorecard,
    policy_triple_gate,
)

load_dotenv(ROOT / ".env")

SNAP = date(2025, 12, 31)
APPLY_YEAR = 2026


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_inputs() -> tuple[ScorecardInputs, dict]:
    """加载所有评分卡字段，返回 (inputs, evidence_dict)"""
    ev = {}  # 数据来源/原始值
    conn = db()
    cur = conn.cursor()

    # === A. 估值 ===
    cur.execute("SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (SNAP,))
    pe, pb = cur.fetchone()
    pe, pb = float(pe), float(pb)
    ev['cs300_pe_ttm'] = f'{pe}（index_dailybasic @ 2025-12-31）'
    ev['cs300_pb'] = f'{pb}'

    # === B. 流动性 ===
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (SNAP,))
    cur_r = float(cur.fetchone()[0])
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= '2024-12-31' ORDER BY trade_date DESC LIMIT 1")
    prior_r = float(cur.fetchone()[0])
    rate_bp = (cur_r - prior_r) * 100
    ev['rate_cum_bp_12m'] = f'{rate_bp:+.1f} bp (SHIBOR_3M {prior_r}→{cur_r}%)'

    cur.execute("""SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
                   WHERE effective_date > '2024-12-31' AND effective_date <= %s AND inst_type IN ('large','all')""", (SNAP,))
    rrr_pp = float(cur.fetchone()[0])
    ev['rrr_cum_pp_12m'] = f'{rrr_pp:+.2f} pp（2025 全年无 RRR 调整）'

    # deposit: 2015-10-24 后用 SHIBOR_1Y 30 日均
    cur.execute("SELECT AVG(rate_1y) FROM shibor_daily WHERE trade_date BETWEEN '2025-12-01' AND '2025-12-31'")
    deposit = float(cur.fetchone()[0])
    ev['deposit_1y_rate'] = f'{deposit:.4f}%（SHIBOR_1Y 12 月均代理，2015-10 后基准冻结）'

    # === C. 基本面 ===
    # pmi_below_52_months: 倒数连续 <52 的月数
    cur.execute("SELECT month, pmi_mfg FROM cn_pmi_monthly WHERE month <= '202512' ORDER BY month DESC LIMIT 36")
    pmis = [(m, float(v)) for m, v in cur.fetchall()]
    consec = 0
    for _, v in pmis:
        if v < 52:
            consec += 1
        else:
            break
    ev['pmi_below_52_months'] = f'{consec} 月（2025 全年 pmi_mfg <52，倒推到上一次 ≥52 是 ?）'

    # pmi_resume_expansion: 前月 <50 且当月 >=50
    last = pmis[0][1]  # 202512
    prev = pmis[1][1]  # 202511
    resume = prev < 50 <= last
    ev['pmi_resume_expansion'] = f'{resume} (前月 {prev} {"≥" if prev>=50 else "<"} 50, 当月 {last} {"≥" if last>=50 else "<"} 50)'

    # pmi_mfg_3m_avg: 当月及前 2 月均值
    mfg_3 = [v for _, v in pmis[:3]]
    pmi_3m = sum(mfg_3) / 3
    ev['pmi_mfg_3m_avg'] = f'{pmi_3m:.2f}（{mfg_3}）'

    # pmi_prod_minus_order
    cur.execute("SELECT pmi_production, pmi_new_order FROM cn_pmi_monthly WHERE month='202512'")
    prod, ord_ = cur.fetchone()
    pmi_po = float(prod) - float(ord_)
    ev['pmi_prod_minus_order'] = f'{pmi_po:+.2f}（生产 {prod} − 订单 {ord_}）'

    # v10-R1: pmi_non_mfg
    cur.execute("SELECT pmi_non_mfg FROM cn_pmi_monthly WHERE month='202512'")
    r = cur.fetchone()
    pmi_non_mfg = float(r[0]) if r and r[0] is not None else None
    ev['pmi_non_mfg'] = f'{pmi_non_mfg}（v10-R1, 50-55 中性区不触发）'

    # ppi_yoy + ppi_yoy_change
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month='202512'")
    ppi_now = float(cur.fetchone()[0])
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month='202412'")
    ppi_prior = float(cur.fetchone()[0])
    if ppi_prior >= 0 and ppi_now < 0:
        ppi_change = 'turn_negative'
    elif ppi_prior < 0 and ppi_now >= 0:
        ppi_change = 'turn_positive'
    else:
        ppi_change = 'flat'
    ev['ppi_yoy'] = f'{ppi_now}%（当月）'
    ev['ppi_yoy_change'] = f'{ppi_change}（去年 {ppi_prior}% → 当月 {ppi_now}%）'

    # iva_yoy_trend: 表不存在
    ev['iva_yoy_trend'] = '⚠️ cn_iva_monthly 表不存在 → None（跳过该规则）'

    # === D. 情绪 v5 + v6 ===
    cur.execute("SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month='202512'")
    nb, nc = cur.fetchone()
    nb, nc = float(nb), int(nc)
    ev['new_fund_billion'] = f'{nb} 亿（cn_fund_new_monthly 202512）'
    ev['new_fund_count'] = f'{nc} 只'

    cur.execute("SELECT margin_rzrqye_yoy_pct FROM macro_annual_snapshot WHERE apply_year=2026")
    margin_yoy = float(cur.fetchone()[0])
    ev['margin_growth_pct'] = f'{margin_yoy:+.2f}%（margin_rzrqye_yoy_pct, snap 2025-12-31）'

    # === E. 外部 ===
    # us_monthly_pct: 12 月 close / 11 月末 close - 1
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= '2025-11-30' ORDER BY trade_date DESC LIMIT 1")
    spx_nov = float(cur.fetchone()[0])
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= '2025-12-31' ORDER BY trade_date DESC LIMIT 1")
    spx_dec = float(cur.fetchone()[0])
    us_m = (spx_dec / spx_nov - 1) * 100
    ev['us_monthly_pct'] = f'{us_m:+.2f}%（SPX 12 月环比，{spx_nov}→{spx_dec}）'

    # fed_reversal: 看 FED 最近是否 hike→cut 反转
    cur.execute("""SELECT effective_date, direction, rate_after_pct FROM global_cb_rate_events
                   WHERE cb_code='FED' AND effective_date <= %s
                   ORDER BY effective_date DESC LIMIT 8""", (SNAP,))
    fed_recent = cur.fetchall()
    # 简化：看最近一次 cut 之前是不是 hike
    last_cut = next((r for r in fed_recent if r[1] == 'cut'), None)
    fed_reversal = None
    if last_cut:
        cur.execute("""SELECT direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date < %s
                       ORDER BY effective_date DESC LIMIT 1""", (last_cut[0],))
        prev = cur.fetchone()
        if prev and prev[0] == 'hike':
            fed_reversal = 'hike_to_cut'
    ev['fed_reversal'] = f'{fed_reversal}（最近 FED 决议: {fed_recent[0][0]} {fed_recent[0][1]} {fed_recent[0][2]}%）'

    # fed_zero_qe: FED ≤ 0.25%
    fed_zero = float(fed_recent[0][2]) <= 0.25
    ev['fed_zero_qe'] = f'{fed_zero}（FED rate {fed_recent[0][2]}%）'

    # global_recession (OECD CLI 5 经济体投票, 当月 < 100 且连续 3 月下降)
    # 2025-12 数据 (注意：OECD 数据有滞后，最新 2026-05)
    countries = ['USA', 'G4E', 'CHN', 'JPN', 'G7']
    recession_votes = 0
    detail = []
    for c in countries:
        cur.execute("""SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= '2025-12-01'
                       ORDER BY period DESC LIMIT 3""", (c,))
        vals = [float(v[0]) for v in cur.fetchall()]
        if len(vals) >= 3:
            in_rec = vals[0] < 100 and vals[0] < vals[1] < vals[2]
            recession_votes += 1 if in_rec else 0
            detail.append(f'{c}={vals[0]:.1f}(↓{vals[1]:.1f}↓{vals[2]:.1f}) {"⚠️" if in_rec else ""}')
        else:
            detail.append(f'{c}=数据不足')
    global_recession = recession_votes >= 2
    ev['global_recession'] = f'{global_recession} (recession 投票 {recession_votes}/5: {", ".join(detail)})'

    # global_stimulus: 全球 ≥3 央行近 6 月降息 (简化判定)
    cur.execute("""SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
                   WHERE direction='cut' AND effective_date BETWEEN '2025-07-01' AND %s
                     AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""", (SNAP,))
    cb_cut_n = cur.fetchone()[0]
    global_stim = cb_cut_n >= 3
    ev['global_stimulus'] = f'{global_stim}（近 6 月 {cb_cut_n} 家主要央行降息）'

    # === F. 政策 ===
    cur.execute("SELECT theme, tone, fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=2026")
    cewc = cur.fetchone()
    theme, tone, fiscal, monetary = cewc
    # 映射: 货币"适度宽松" → loose
    pboc_tone = 'loose' if '宽松' in monetary else ('tight' if '紧' in monetary else 'neutral')
    ev['pboc_tone'] = f'{pboc_tone}（cewc 货币口径："{monetary}"）'

    # central_meeting_tone
    if '积极' in fiscal or 'expansionary' in tone.lower() or '宽松' in tone:
        cmt = 'expansionary'
    elif '双防' in tone or 'dual' in tone.lower():
        cmt = 'dual_prevent'
    else:
        cmt = 'neutral'
    ev['central_meeting_tone'] = f'{cmt}（cewc tone="{tone}", fiscal="{fiscal}"）'

    # stamp_duty: 最近事件方向（snapshot 之前最近的 stamp_duty 类）
    cur.execute("""SELECT direction, title, effective_date FROM stamp_duty_events
                   WHERE event_type='stamp_duty' AND effective_date <= %s
                   ORDER BY effective_date DESC LIMIT 1""", (SNAP,))
    sd = cur.fetchone()
    stamp_duty = sd[0] if sd else None
    ev['stamp_duty'] = f'{stamp_duty}（{sd[1]} @ {sd[2]}）' if sd else 'None'

    # national_team_action: 最近 entry/exit
    cur.execute("""SELECT direction, title, effective_date FROM national_team_actions
                   WHERE effective_date <= %s ORDER BY effective_date DESC LIMIT 1""", (SNAP,))
    nta = cur.fetchone()
    national = nta[0] if nta else None
    ev['national_team_action'] = f'{national}（{nta[1]} @ {nta[2]}）' if nta else 'None'

    conn.close()

    inp = ScorecardInputs(
        cs300_pe_ttm=pe, cs300_pb=pb,
        rate_cum_bp_12m=rate_bp, rrr_cum_pp_12m=rrr_pp, deposit_1y_rate=deposit,
        pmi_below_52_months=consec,
        iva_yoy_trend=None,
        ppi_yoy=ppi_now, ppi_yoy_change=ppi_change,
        pmi_resume_expansion=resume,
        pmi_mfg_3m_avg=pmi_3m,
        pmi_prod_minus_order=pmi_po,
        pmi_non_mfg=pmi_non_mfg,
        new_fund_billion=nb, new_fund_count=nc,
        margin_growth_pct=margin_yoy,
        fed_reversal=fed_reversal,
        us_monthly_pct=us_m,
        global_recession=global_recession,
        fed_zero_qe=fed_zero,
        global_stimulus=global_stim,
        pboc_tone=pboc_tone,
        stamp_duty=stamp_duty,
        central_meeting_tone=cmt,
        national_team_action=national,
    )
    return inp, ev


def main():
    inp, ev = load_inputs()
    r = evaluate_scorecard(APPLY_YEAR, inp)

    print('=' * 78)
    print(f'2026 年初评分卡  snapshot = {SNAP}, apply_year = {APPLY_YEAR}')
    print('=' * 78)

    print('\n【输入字段证据链】')
    for k, v in ev.items():
        print(f'  {k:30s} : {v}')

    # 按维度展示触发明细
    print('\n【各维度触发明细】')
    by_dim = r.items_by_dimension()
    dim_score = {}
    for dim in ('valuation', 'liquidity', 'fundamental', 'sentiment', 'external', 'policy'):
        items = by_dim.get(dim, [])
        sub = sum(it.score for it in items)
        dim_score[dim] = sub
        print(f'\n  ▌ {dim:13s} 小计 {sub:+d}')
        if not items:
            print(f'    (无触发)')
        for it in items:
            arrow = '↑' if it.direction == 'risk' else '↓'
            print(f'    {arrow} {it.name:30s} {it.score:+d}  [{it.direction}]')

    print(f'\n【总分汇总】')
    print(f'  估值 {dim_score["valuation"]:+d} | 流动性 {dim_score["liquidity"]:+d} | '
          f'基本面 {dim_score["fundamental"]:+d} | 情绪 {dim_score["sentiment"]:+d} | '
          f'外部 {dim_score["external"]:+d} | 政策 {dim_score["policy"]:+d}')
    print(f'  ═══════════════════════════════════════════════════════════════')
    print(f'  总分 = {r.total_score:+d}   →   档位 = {r.band}   →   目标股票仓位 = {r.target_equity_pct}%')

    # 加仓三重门
    if r.target_equity_pct >= 80.0 and r.total_score <= -5:
        print(f'\n【加仓档 — 检查政策实弹三重门】')
        passed, desc = policy_triple_gate(inp)
        print(f'  {desc}')
        if passed:
            print(f'  ✓ 通过 → 放行加仓至 {r.target_equity_pct}%')
        else:
            print(f'  ✗ 未通过 → 维持 75% 平衡档位（避免过早抄底）')

    print('\n' + '=' * 78)


if __name__ == '__main__':
    main()

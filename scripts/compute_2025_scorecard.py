#!/usr/bin/env python3.11
"""compute_2025_scorecard.py — 2025 年初评分卡（snapshot=2024-12-31）

详细打印每条命中规则、各维度小计、最终评分。
结合 2025 全年 CS300 +21.2% 这个事后事实，标记每条规则的方向命中情况。
"""

from __future__ import annotations

import os, sys
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

load_dotenv(ROOT / '.env')

SNAP = date(2024, 12, 31)
APPLY_YEAR = 2025
CS300_YEAR_RET = 21.2  # 事后已知：2025 全年 CS300 +21.2%


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_inputs():
    ev = {}
    conn = db()
    cur = conn.cursor()
    snap_str = SNAP.strftime('%Y-%m-%d')
    snap_m = SNAP.strftime('%Y%m')

    # 估值
    cur.execute("SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    pe, pb = cur.fetchone()
    pe, pb = float(pe), float(pb)
    ev['cs300_pe_ttm'] = f'{pe}'
    ev['cs300_pb'] = f'{pb}'

    # 流动性
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    cur_r = float(cur.fetchone()[0])
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= '2023-12-31' ORDER BY trade_date DESC LIMIT 1")
    prior_r = float(cur.fetchone()[0])
    rate_bp = (cur_r - prior_r) * 100
    ev['rate_cum_bp_12m'] = f'{rate_bp:+.1f} bp (SHIBOR_3M {prior_r}→{cur_r}%)'

    cur.execute("""SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
                   WHERE effective_date > '2023-12-31' AND effective_date <= %s AND inst_type IN ('large','all')""", (snap_str,))
    rrr_pp = float(cur.fetchone()[0])
    ev['rrr_cum_pp_12m'] = f'{rrr_pp:+.2f} pp'

    cur.execute("SELECT AVG(rate_1y) FROM shibor_daily WHERE trade_date BETWEEN '2024-12-01' AND '2024-12-31'")
    deposit = float(cur.fetchone()[0])
    ev['deposit_1y_rate'] = f'{deposit:.4f}%'

    # 基本面
    cur.execute("SELECT month, pmi_mfg, pmi_production, pmi_new_order, pmi_non_mfg FROM cn_pmi_monthly WHERE month <= '202412' ORDER BY month DESC LIMIT 12")
    pmis = cur.fetchall()
    consec = 0
    for _, v, _, _, _ in pmis:
        if v is not None and float(v) < 52:
            consec += 1
        else:
            break
    last = float(pmis[0][1]); prev = float(pmis[1][1])
    resume = prev < 50 <= last
    mfg_3 = [float(p[1]) for p in pmis[:3]]
    pmi_3m = sum(mfg_3) / 3
    prod = float(pmis[0][2]); ord_ = float(pmis[0][3])
    pmi_po = prod - ord_
    pmi_non_mfg = float(pmis[0][4]) if pmis[0][4] is not None else None
    ev['pmi_mfg'] = f'{last}'
    ev['pmi_below_52_months'] = f'{consec} 月'
    ev['pmi_resume_expansion'] = f'{resume} (前月 {prev} → 当月 {last})'
    ev['pmi_mfg_3m_avg'] = f'{pmi_3m:.2f}'
    ev['pmi_prod_minus_order'] = f'{pmi_po:+.2f}'
    ev['pmi_non_mfg'] = f'{pmi_non_mfg}'

    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month='202412'")
    ppi_now = float(cur.fetchone()[0])
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month='202312'")
    ppi_prior = float(cur.fetchone()[0])
    if ppi_prior >= 0 and ppi_now < 0: ppi_change = 'turn_negative'
    elif ppi_prior < 0 and ppi_now >= 0: ppi_change = 'turn_positive'
    else: ppi_change = 'flat'
    ev['ppi_yoy'] = f'{ppi_now}%'
    ev['ppi_yoy_change'] = f'{ppi_change} (去年 {ppi_prior}% → 当月 {ppi_now}%)'

    # 情绪
    cur.execute("SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month='202412'")
    nb, nc = cur.fetchone()
    nb, nc = float(nb), int(nc)
    ev['new_fund_billion'] = f'{nb}'
    ev['new_fund_count'] = f'{nc}'

    cur.execute("SELECT margin_rzrqye_yoy_pct FROM macro_annual_snapshot WHERE apply_year=2025")
    margin_yoy = float(cur.fetchone()[0])
    ev['margin_growth_pct'] = f'{margin_yoy:+.2f}%'

    # 外部
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= '2024-11-30' ORDER BY trade_date DESC LIMIT 1")
    spx_nov = float(cur.fetchone()[0])
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= '2024-12-31' ORDER BY trade_date DESC LIMIT 1")
    spx_dec = float(cur.fetchone()[0])
    us_m = (spx_dec / spx_nov - 1) * 100
    ev['us_monthly_pct'] = f'{us_m:+.2f}%'

    cur.execute("""SELECT effective_date, direction, rate_after_pct FROM global_cb_rate_events
                   WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 8""", (snap_str,))
    fed_recent = cur.fetchall()
    fed_reversal = None
    last_cut = next((r for r in fed_recent if r[1] == 'cut'), None)
    if last_cut:
        cur.execute("SELECT direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date < %s ORDER BY effective_date DESC LIMIT 1", (last_cut[0],))
        prev = cur.fetchone()
        if prev and prev[0] == 'hike': fed_reversal = 'hike_to_cut'
    ev['fed_reversal'] = f'{fed_reversal} (最近 FED 决议: {fed_recent[0][0]} {fed_recent[0][1]} {fed_recent[0][2]}%)'
    fed_zero = float(fed_recent[0][2]) <= 0.25
    ev['fed_zero_qe'] = f'{fed_zero}'

    votes = 0; detail = []
    for c in ('USA', 'G4E', 'CHN', 'JPN', 'G7'):
        cur.execute("SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= '2024-12-01' ORDER BY period DESC LIMIT 3", (c,))
        vals = [float(v[0]) for v in cur.fetchall()]
        if len(vals) >= 3:
            in_rec = vals[0] < 100 and vals[0] < vals[1] < vals[2]
            votes += 1 if in_rec else 0
            detail.append(f'{c}={vals[0]:.1f}{"⚠️" if in_rec else ""}')
    global_rec = votes >= 2
    ev['global_recession'] = f'{global_rec} ({votes}/5: {", ".join(detail)})'

    cur.execute("""SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
                   WHERE direction='cut' AND effective_date BETWEEN '2024-07-01' AND %s
                     AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""", (snap_str,))
    cb_cut_n = int(cur.fetchone()[0])
    global_stim = cb_cut_n >= 3
    ev['global_stimulus'] = f'{global_stim} (近6月 {cb_cut_n} 家)'
    ev['cb_cuts_6m'] = f'{cb_cut_n}'

    # 政策
    cur.execute("SELECT theme, tone, fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=2025")
    cewc = cur.fetchone()
    if cewc:
        _, tone, fiscal, monetary = cewc
        pboc_tone = 'loose' if monetary and '宽松' in monetary else 'neutral'
        cmt = 'expansionary' if fiscal and '积极' in fiscal else 'neutral'
    else:
        pboc_tone = cmt = None
    ev['pboc_tone'] = f'{pboc_tone} ({monetary})'
    ev['central_meeting_tone'] = f'{cmt} (tone={tone}, fiscal={fiscal})'

    cur.execute("""SELECT direction, title, effective_date FROM stamp_duty_events
                   WHERE event_type='stamp_duty' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    sd = cur.fetchone()
    stamp_duty = sd[0] if sd else None
    ev['stamp_duty'] = f'{stamp_duty} ({sd[1]} @ {sd[2]})' if sd else 'None'

    cur.execute("""SELECT direction, title, effective_date FROM national_team_actions
                   WHERE effective_date <= %s ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    nta = cur.fetchone()
    national = nta[0] if nta else None
    ev['national_team_action'] = f'{national} ({nta[1]} @ {nta[2]})' if nta else 'None'

    conn.close()

    inp = ScorecardInputs(
        cs300_pe_ttm=pe, cs300_pb=pb,
        rate_cum_bp_12m=rate_bp, rrr_cum_pp_12m=rrr_pp, deposit_1y_rate=deposit,
        pmi_below_52_months=consec, iva_yoy_trend=None,
        ppi_yoy=ppi_now, ppi_yoy_change=ppi_change,
        pmi_resume_expansion=resume, pmi_mfg_3m_avg=pmi_3m,
        pmi_prod_minus_order=pmi_po, pmi_non_mfg=pmi_non_mfg,
        new_fund_billion=nb, new_fund_count=nc,
        margin_growth_pct=margin_yoy,
        fed_reversal=fed_reversal, us_monthly_pct=us_m,
        global_recession=global_rec, fed_zero_qe=fed_zero,
        global_stimulus=global_stim, cb_cuts_6m=cb_cut_n,
        pboc_tone=pboc_tone, stamp_duty=stamp_duty,
        central_meeting_tone=cmt, national_team_action=national,
    )
    return inp, ev


def main():
    inp, ev = load_inputs()
    r = evaluate_scorecard(APPLY_YEAR, inp)

    print('=' * 80)
    print(f'2025 年初评分卡  snapshot = {SNAP}  apply_year = {APPLY_YEAR}')
    print(f'事后已知：2025 全年 CS300 +{CS300_YEAR_RET}%（用于方向命中校验）')
    print('=' * 80)

    print('\n【输入字段证据链】')
    for k, v in ev.items():
        print(f'  {k:30s} : {v}')

    by_dim = r.items_by_dimension()
    dim_score = {}
    print('\n【各维度触发明细 + 方向命中（事后看）】')
    for dim in ('valuation', 'liquidity', 'fundamental', 'sentiment', 'external', 'policy'):
        items = by_dim.get(dim, [])
        sub = sum(it.score for it in items)
        dim_score[dim] = sub
        print(f'\n  ▌ {dim:13s} 小计 {sub:+d}')
        if not items:
            print(f'    (无触发)')
        for it in items:
            arrow = '↑' if it.direction == 'risk' else '↓'
            # 事后方向命中（2025 涨 21.2%，所以「机会信号 -1」是对的，「风险信号 +1」是错的）
            correct = '✓' if it.direction == 'opportunity' else '❌ 事后错向'
            print(f'    {arrow} {it.name:30s} {it.score:+d}  [{it.direction}]  {correct}')

    print(f'\n【总分汇总】')
    print(f'  估值 {dim_score["valuation"]:+d} | 流动性 {dim_score["liquidity"]:+d} | '
          f'基本面 {dim_score["fundamental"]:+d} | 情绪 {dim_score["sentiment"]:+d} | '
          f'外部 {dim_score["external"]:+d} | 政策 {dim_score["policy"]:+d}')
    print(f'  ═══════════════════════════════════════════════════════════════')
    print(f'  总分 = {r.total_score:+d}   →   档位 = {r.band}   →   目标股票仓位 = {r.target_equity_pct}%')

    if r.target_equity_pct >= 80.0 and r.total_score <= -1:
        print(f'\n【加仓档 — 检查政策实弹三重门】')
        passed, desc = policy_triple_gate(inp)
        print(f'  {desc}')
        print(f'  {"✓ 通过" if passed else "✗ 未通过"} → 仓位 {r.target_equity_pct}%')

    # 事后整体评估
    print(f'\n【事后方向校验】')
    n_risk = sum(1 for it in r.items if it.direction == 'risk')
    n_opp = sum(1 for it in r.items if it.direction == 'opportunity')
    print(f'  风险信号 {n_risk} 条（事后看：错向，因 2025 涨）')
    print(f'  机会信号 {n_opp} 条（事后看：对向）')
    print(f'  净评分 {r.total_score:+d} → 目标仓位 {r.target_equity_pct}% → 事后看{"正确加仓 ✓" if r.target_equity_pct >= 80 else "应该加仓但仓位不足 ❌"}')
    print('\n' + '=' * 80)


if __name__ == '__main__':
    main()

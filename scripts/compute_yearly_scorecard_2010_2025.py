#!/usr/bin/env python3.11
"""compute_yearly_scorecard_2010_2025.py — 批量算每年初评分卡，聚焦极端年份

输出：
  - 每年初评分总分、各维度分、目标仓位、CS300 实际涨跌
  - 极端年份（|ret| ≥ 15%）的详细对比表
  - 显著有效特征 ranking（哪些规则在极端年触发最多）

从近到远展示。
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


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_inputs(cur, snap_year: int, snap_month: int = 12, snap_day: int = 31) -> ScorecardInputs:
    """通用月末 snapshot 加载所有字段"""
    snap = date(snap_year, snap_month, snap_day)
    snap_str = snap.strftime('%Y-%m-%d')
    snap_m = snap.strftime('%Y%m')
    one_y_ago = date(snap_year - 1, snap_month, snap_day).strftime('%Y-%m-%d')

    # 估值
    cur.execute("SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    pe, pb = (float(r[0]), float(r[1])) if r else (None, None)

    # 流动性
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    cur_r = cur.fetchone()
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (one_y_ago,))
    prior_r = cur.fetchone()
    rate_bp = None
    if cur_r and prior_r and cur_r[0] is not None and prior_r[0] is not None:
        rate_bp = (float(cur_r[0]) - float(prior_r[0])) * 100

    cur.execute("""SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
                   WHERE effective_date > %s AND effective_date <= %s AND inst_type IN ('large','all')""", (one_y_ago, snap_str))
    rrr_pp = float(cur.fetchone()[0] or 0)

    cur.execute("SELECT AVG(rate_1y) FROM shibor_daily WHERE trade_date BETWEEN %s AND %s",
                ((snap - __import__('pandas').Timedelta(days=30)).strftime('%Y-%m-%d'), snap_str))
    r = cur.fetchone()
    deposit = float(r[0]) if r and r[0] is not None else None

    # 基本面
    cur.execute("SELECT month, pmi_mfg, pmi_production, pmi_new_order, pmi_non_mfg FROM cn_pmi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 12", (snap_m,))
    pmis = cur.fetchall()
    consec = 0
    for _, v, _, _, _ in pmis:
        if v is not None and float(v) < 52:
            consec += 1
        else: break
    resume = (len(pmis) >= 2 and pmis[1][1] is not None and pmis[0][1] is not None
              and float(pmis[1][1]) < 50 <= float(pmis[0][1]))
    last3 = [float(p[1]) for p in pmis[:3] if p[1] is not None]
    pmi_3m = sum(last3) / 3 if len(last3) == 3 else None
    pmi_po = None
    if pmis and pmis[0][2] is not None and pmis[0][3] is not None:
        pmi_po = float(pmis[0][2]) - float(pmis[0][3])
    pmi_non_mfg = float(pmis[0][4]) if pmis and pmis[0][4] is not None else None

    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    ppi_now = float(r[0]) if r and r[0] is not None else None
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (f'{snap_year - 1}{snap_m[4:]}',))
    r = cur.fetchone()
    ppi_prior = float(r[0]) if r and r[0] is not None else None
    if ppi_now is None or ppi_prior is None: ppi_change = None
    elif ppi_prior >= 0 and ppi_now < 0: ppi_change = 'turn_negative'
    elif ppi_prior < 0 and ppi_now >= 0: ppi_change = 'turn_positive'
    else: ppi_change = 'flat'

    # 情绪
    cur.execute("SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    nb, nc = (float(r[0]), int(r[1])) if r and r[0] is not None else (None, None)

    cur.execute("SELECT margin_rzrqye_yoy_pct FROM macro_annual_snapshot WHERE apply_year=%s", (snap_year + 1,))
    r = cur.fetchone()
    margin_yoy = float(r[0]) if r and r[0] is not None else None

    # 外部
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    spx_cur = cur.fetchone()
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
                (date(snap_year, snap_month - 1 or 12, 28).strftime('%Y-%m-%d') if snap_month > 1 else f'{snap_year - 1}-11-30',))
    spx_prior = cur.fetchone()
    us_m = None
    if spx_cur and spx_prior and spx_cur[0] and spx_prior[0]:
        us_m = (float(spx_cur[0]) / float(spx_prior[0]) - 1) * 100

    cur.execute("SELECT effective_date, direction, rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 8", (snap_str,))
    fed_recent = cur.fetchall()
    fed_reversal = None
    last_cut = next((r for r in fed_recent if r[1] == 'cut'), None)
    if last_cut:
        cur.execute("SELECT direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date < %s ORDER BY effective_date DESC LIMIT 1", (last_cut[0],))
        prev = cur.fetchone()
        if prev and prev[0] == 'hike': fed_reversal = 'hike_to_cut'
    fed_zero = bool(fed_recent and float(fed_recent[0][2]) <= 0.25)

    snap_m_start = snap.replace(day=1).strftime('%Y-%m-%d')
    votes = 0
    for c in ('USA', 'G4E', 'CHN', 'JPN', 'G7'):
        cur.execute("SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= %s ORDER BY period DESC LIMIT 3", (c, snap_m_start))
        vals = [float(v[0]) for v in cur.fetchall() if v[0] is not None]
        if len(vals) >= 3 and vals[0] < 100 and vals[0] < vals[1] < vals[2]:
            votes += 1
    global_rec = votes >= 2

    six_mo_ago = date(snap_year, snap_month, snap_day) - __import__('pandas').Timedelta(days=180)
    cur.execute("""SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
                   WHERE direction='cut' AND effective_date BETWEEN %s AND %s
                     AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""",
                (six_mo_ago.strftime('%Y-%m-%d'), snap_str))
    cb_cut_n = int(cur.fetchone()[0])
    global_stim = cb_cut_n >= 3

    # 政策
    cur.execute("SELECT tone, fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=%s", (snap_year + 1,))
    r = cur.fetchone()
    pboc_tone = cmt = None
    if r:
        tone, fiscal, monetary = r
        pboc_tone = 'loose' if monetary and '宽松' in monetary else ('tight' if monetary and '紧' in monetary else 'neutral')
        cmt = 'expansionary' if fiscal and '积极' in fiscal else ('dual_prevent' if tone and '双防' in tone else 'neutral')

    cur.execute("""SELECT direction FROM stamp_duty_events WHERE event_type='stamp_duty' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    r = cur.fetchone()
    stamp_duty = r[0] if r else None

    cur.execute("""SELECT direction FROM national_team_actions WHERE effective_date <= %s ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    r = cur.fetchone()
    national = r[0] if r else None

    return ScorecardInputs(
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


def cs300_year_ret(cur, year: int):
    cur.execute("SELECT close FROM index_daily WHERE ts_code='000300.SH' AND trade_date >= %s ORDER BY trade_date ASC LIMIT 1", (f'{year}-01-01',))
    o = cur.fetchone()
    cur.execute("SELECT close FROM index_daily WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (f'{year}-12-31',))
    c = cur.fetchone()
    if not o or not c: return None
    return (float(c[0]) / float(o[0]) - 1) * 100


def main():
    conn = db()
    cur = conn.cursor()

    rows = []
    for apply_y in range(2011, 2026):
        snap_y = apply_y - 1
        try:
            inp = load_inputs(cur, snap_y)
            r = evaluate_scorecard(apply_y, inp)
            cs_ret = cs300_year_ret(cur, apply_y)
            by_dim = r.items_by_dimension()
            risk_items = [it.name for it in r.items if it.direction == 'risk']
            opp_items = [it.name for it in r.items if it.direction == 'opportunity']
            rows.append({
                'apply_year': apply_y,
                'cs_ret': cs_ret,
                'total': r.total_score,
                'target_eq': r.target_equity_pct,
                'band': r.band,
                'val': sum(it.score for it in by_dim.get('valuation', [])),
                'liq': sum(it.score for it in by_dim.get('liquidity', [])),
                'fun': sum(it.score for it in by_dim.get('fundamental', [])),
                'sen': sum(it.score for it in by_dim.get('sentiment', [])),
                'ext': sum(it.score for it in by_dim.get('external', [])),
                'pol': sum(it.score for it in by_dim.get('policy', [])),
                'risk_items': risk_items,
                'opp_items': opp_items,
            })
        except Exception as e:
            print(f'{apply_y}: ERR {e}')

    conn.close()

    # ── 全部年份总览 ─────────────────────────────────────
    print('='*108)
    print('2011-2025 每年初评分总览（按时间倒序）')
    print('='*108)
    print(f'{"年":>5}{"CS300%":>9}{"总分":>6}{"档位":<14}{"目标仓":>8}'
          f'{"估":>4}{"流":>4}{"基":>4}{"情":>4}{"外":>4}{"政":>4}{"方向":>8}')
    print('-'*108)
    for r in sorted(rows, key=lambda x: -x['apply_year']):
        # 方向命中：score < 0 ret > 0 或 score > 0 ret < 0 = 对
        if r['cs_ret'] is None:
            mark = '?'
        elif (r['total'] < 0 and r['cs_ret'] > 0) or (r['total'] > 0 and r['cs_ret'] < 0):
            mark = '✓ 对'
        elif (r['total'] < 0 and r['cs_ret'] < 0) or (r['total'] > 0 and r['cs_ret'] > 0):
            mark = '❌ 反'
        else:
            mark = '— 中'
        ret_str = f'{r["cs_ret"]:+.1f}%' if r['cs_ret'] is not None else '?'
        print(f'{r["apply_year"]:>5}{ret_str:>9}{r["total"]:>+6d}  {r["band"]:<14}{r["target_eq"]:>7.0f}%'
              f'{r["val"]:>+4d}{r["liq"]:>+4d}{r["fun"]:>+4d}{r["sen"]:>+4d}{r["ext"]:>+4d}{r["pol"]:>+4d}  {mark}')

    # ── 极端年份聚焦 ─────────────────────────────────────
    print(f'\n{"="*108}')
    print('极端年份（|CS300| ≥ 15%）触发规则明细（近→远）')
    print('='*108)
    extreme = [r for r in rows if r['cs_ret'] is not None and abs(r['cs_ret']) >= 15]
    extreme.sort(key=lambda x: -x['apply_year'])
    for r in extreme:
        label = '🟢 大涨' if r['cs_ret'] > 0 else '🔴 大跌'
        ret_str = f'{r["cs_ret"]:+.1f}%'
        print(f'\n  ─── {r["apply_year"]} 年  {label}  CS300={ret_str}  总分={r["total"]:+d}  仓位={r["target_eq"]:.0f}%  档位={r["band"]} ───')
        if r['opp_items']:
            print(f'    机会信号 ({len(r["opp_items"])}): {" | ".join(r["opp_items"])}')
        if r['risk_items']:
            print(f'    风险信号 ({len(r["risk_items"])}): {" | ".join(r["risk_items"])}')
        # 事后命中
        if (r['total'] < 0 and r['cs_ret'] > 0) or (r['total'] > 0 and r['cs_ret'] < 0):
            print(f'    → ✓ 方向命中（评分卡预测正确）')
        elif (r['total'] < 0 and r['cs_ret'] < 0) or (r['total'] > 0 and r['cs_ret'] > 0):
            print(f'    → ❌ 方向反向（评分卡预测错误）')

    # ── 显著有效特征 ranking ────────────────────────────
    print(f'\n{"="*108}')
    print('显著有效特征 ranking — 极端年份中规则触发次数 & 方向命中率')
    print('='*108)
    rule_stats = {}
    for r in extreme:
        is_up = r['cs_ret'] > 0
        for name in r['opp_items']:
            d = rule_stats.setdefault(name, {'opp': 0, 'risk': 0, 'opp_correct': 0, 'risk_correct': 0})
            d['opp'] += 1
            if is_up: d['opp_correct'] += 1
        for name in r['risk_items']:
            d = rule_stats.setdefault(name, {'opp': 0, 'risk': 0, 'opp_correct': 0, 'risk_correct': 0})
            d['risk'] += 1
            if not is_up: d['risk_correct'] += 1

    print(f'{"规则":<32}{"机会触发":>10}{"机会对":>9}{"风险触发":>10}{"风险对":>9}{"命中率":>10}')
    print('-'*108)
    summary = []
    for name, d in rule_stats.items():
        total = d['opp'] + d['risk']
        correct = d['opp_correct'] + d['risk_correct']
        hr = correct / total * 100 if total else 0
        summary.append((name, d, total, hr))
    for name, d, total, hr in sorted(summary, key=lambda x: -x[2]):  # 按触发次数排
        print(f'{name:<32}{d["opp"]:>10}{d["opp_correct"]:>9}{d["risk"]:>10}{d["risk_correct"]:>9}{hr:>9.0f}%')


if __name__ == '__main__':
    main()

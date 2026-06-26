#!/usr/bin/env python3.11
"""月度评分卡 — 2008-01 ~ 2025-12 共 216 月评分序列

红线：
  - snapshot_date = month_end (该月最后一个交易日)
  - 所有取数 ≤ snapshot_date，严防上帝视角
  - 事件类字段（pboc_tone / stamp_duty / national_team / fed_reversal）取 snap 前最近一次
  - 月度类字段（pmi / ppi / new_fund）取 snap 当月或更早（如 PMI 12 月数据 1 月初发布，snap 12-31 可用）

评估：
  - 月度评分 → 目标仓位
  - 与次 1/3/6/12 月 CS300 收益的 Spearman ρ
  - 月度仓位变化频率（看实际可执行性）
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs,
    evaluate_scorecard,
)

load_dotenv(ROOT / ".env")

OUT_CSV = ROOT / "data" / "monthly_scorecard_series.csv"


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_monthly_inputs(cur, snap: date) -> ScorecardInputs:
    """对 snapshot 月末计算所有评分字段"""
    snap_str = snap.strftime('%Y-%m-%d')
    snap_m = snap.strftime('%Y%m')

    # 估值
    cur.execute("SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    pe, pb = (float(r[0]), float(r[1])) if r else (None, None)

    # 流动性 — rate_cum_bp_12m
    one_year_ago = (snap - pd.Timedelta(days=365)).strftime('%Y-%m-%d')
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    cur_r = cur.fetchone()
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (one_year_ago,))
    prior_r = cur.fetchone()
    rate_bp = None
    if cur_r and prior_r and cur_r[0] is not None and prior_r[0] is not None:
        rate_bp = (float(cur_r[0]) - float(prior_r[0])) * 100

    # rrr_cum_pp_12m
    cur.execute("""SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
                   WHERE effective_date > %s AND effective_date <= %s
                     AND inst_type IN ('large','all')""", (one_year_ago, snap_str))
    rrr_pp = float(cur.fetchone()[0] or 0)

    # deposit_1y: 用 SHIBOR_1Y 当月 30 日均（≥ 2015-10-24 起一律用代理）
    cur.execute("""SELECT AVG(rate_1y) FROM shibor_daily
                   WHERE trade_date BETWEEN %s AND %s""",
                ((snap - pd.Timedelta(days=30)).strftime('%Y-%m-%d'), snap_str))
    r = cur.fetchone()
    deposit = float(r[0]) if r and r[0] is not None else None

    # 基本面 — pmi
    cur.execute("SELECT month, pmi_mfg, pmi_production, pmi_new_order FROM cn_pmi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 12",
                (snap_m,))
    pmis = [(m, float(v) if v is not None else None,
             float(p) if p is not None else None,
             float(o) if o is not None else None) for m, v, p, o in cur.fetchall()]
    pmi_mfg = pmis[0][1] if pmis else None
    # pmi_below_52_months
    consec = 0
    for _, v, _, _ in pmis:
        if v is not None and v < 52:
            consec += 1
        else:
            break
    # pmi_resume_expansion
    resume = (len(pmis) >= 2 and pmis[1][1] is not None and pmis[0][1] is not None
              and pmis[1][1] < 50 <= pmis[0][1])
    # pmi_mfg_3m_avg
    last3 = [v for _, v, _, _ in pmis[:3] if v is not None]
    pmi_3m = sum(last3) / len(last3) if len(last3) == 3 else None
    # pmi_prod_minus_order
    pmi_po = None
    if pmis and pmis[0][2] is not None and pmis[0][3] is not None:
        pmi_po = pmis[0][2] - pmis[0][3]

    # ppi_yoy + change
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    ppi_now = float(r[0]) if r and r[0] is not None else None
    # 12 月前
    sm_year = int(snap_m[:4]) - 1
    prior_m = f'{sm_year}{snap_m[4:]}'
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (prior_m,))
    r = cur.fetchone()
    ppi_prior = float(r[0]) if r and r[0] is not None else None
    if ppi_now is None or ppi_prior is None:
        ppi_change = None
    elif ppi_prior >= 0 and ppi_now < 0:
        ppi_change = 'turn_negative'
    elif ppi_prior < 0 and ppi_now >= 0:
        ppi_change = 'turn_positive'
    else:
        ppi_change = 'flat'

    # 情绪 — new_fund + margin
    cur.execute("SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    nb, nc = (float(r[0]), int(r[1])) if r and r[0] is not None else (None, None)

    # margin_growth_pct: 当月 vs 12 月前 同 trade_date 末日
    cur.execute("SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    margin_cur = float(r[0]) if r and r[0] is not None else None
    cur.execute("SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1", (one_year_ago,))
    r = cur.fetchone()
    margin_prior = float(r[0]) if r and r[0] is not None else None
    margin_yoy = None
    if margin_cur and margin_prior and margin_prior > 0:
        margin_yoy = (margin_cur / margin_prior - 1) * 100

    # 外部 — us_monthly_pct
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    spx_cur = float(r[0]) if r and r[0] is not None else None
    last_month_end = (snap - pd.offsets.MonthBegin(1) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (last_month_end,))
    r = cur.fetchone()
    spx_prior = float(r[0]) if r and r[0] is not None else None
    us_m = None
    if spx_cur and spx_prior:
        us_m = (spx_cur / spx_prior - 1) * 100

    # fed_reversal: 简化 — 最近一次 cut 前是不是 hike
    cur.execute("""SELECT effective_date, direction FROM global_cb_rate_events
                   WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 8""",
                (snap_str,))
    fed_recent = cur.fetchall()
    fed_reversal = None
    last_cut = next((r for r in fed_recent if r[1] == 'cut'), None)
    if last_cut:
        cur.execute("""SELECT direction FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date < %s
                       ORDER BY effective_date DESC LIMIT 1""", (last_cut[0],))
        prev = cur.fetchone()
        if prev and prev[0] == 'hike':
            fed_reversal = 'hike_to_cut'

    # fed_zero_qe
    cur.execute("SELECT rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    fed_zero = (float(r[0]) <= 0.25) if r and r[0] is not None else False

    # global_recession: 5 国 OECD CLI 投票（< 100 且连续 3 月下降）
    snap_month_start = snap.replace(day=1).strftime('%Y-%m-%d')
    votes = 0
    for c in ('USA', 'G4E', 'CHN', 'JPN', 'G7'):
        cur.execute("SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= %s ORDER BY period DESC LIMIT 3",
                    (c, snap_month_start))
        vals = [float(v[0]) for v in cur.fetchall() if v[0] is not None]
        if len(vals) >= 3 and vals[0] < 100 and vals[0] < vals[1] < vals[2]:
            votes += 1
    global_rec = votes >= 2

    # global_stimulus: 近 6 月 ≥3 家主要央行 cut
    six_mo_ago = (snap - pd.Timedelta(days=180)).strftime('%Y-%m-%d')
    cur.execute("""SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
                   WHERE direction='cut' AND effective_date BETWEEN %s AND %s
                     AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""",
                (six_mo_ago, snap_str))
    n = cur.fetchone()[0]
    global_stim = n >= 3

    # 政策 — cewc: 取 snap 之前最近一次 cewc 会议（每年 12 月）
    snap_year = snap.year
    # 12 月 snap 用本年 cewc, 1-11 月用上年
    cewc_apply = snap_year + 1 if snap.month == 12 else snap_year
    cur.execute("SELECT tone, fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=%s",
                (cewc_apply,))
    r = cur.fetchone()
    pboc_tone, cmt = None, None
    if r:
        _, fiscal, monetary = r[0], r[1], r[2]
        pboc_tone = 'loose' if monetary and '宽松' in monetary else ('tight' if monetary and '紧' in monetary else 'neutral')
        cmt = ('expansionary' if fiscal and '积极' in fiscal else
               'dual_prevent' if r[0] and '双防' in r[0] else 'neutral')

    # stamp_duty
    cur.execute("""SELECT direction FROM stamp_duty_events WHERE event_type='stamp_duty' AND effective_date <= %s
                   ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    r = cur.fetchone()
    stamp_duty = r[0] if r else None

    # national_team
    cur.execute("""SELECT direction FROM national_team_actions WHERE effective_date <= %s
                   ORDER BY effective_date DESC LIMIT 1""", (snap_str,))
    r = cur.fetchone()
    national = r[0] if r else None

    return ScorecardInputs(
        cs300_pe_ttm=pe, cs300_pb=pb,
        rate_cum_bp_12m=rate_bp, rrr_cum_pp_12m=rrr_pp, deposit_1y_rate=deposit,
        pmi_below_52_months=consec,
        iva_yoy_trend=None,  # 表缺失
        ppi_yoy=ppi_now, ppi_yoy_change=ppi_change,
        pmi_resume_expansion=resume,
        pmi_mfg_3m_avg=pmi_3m,
        pmi_prod_minus_order=pmi_po,
        new_fund_billion=nb, new_fund_count=nc,
        margin_growth_pct=margin_yoy,
        fed_reversal=fed_reversal,
        us_monthly_pct=us_m,
        global_recession=global_rec,
        fed_zero_qe=fed_zero,
        global_stimulus=global_stim,
        pboc_tone=pboc_tone,
        stamp_duty=stamp_duty,
        central_meeting_tone=cmt,
        national_team_action=national,
    )


def get_month_ends(start_year=2008, end_year=2025):
    """生成每月最后一天（用作 snapshot）"""
    months = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if m == 12:
                next_m = date(y + 1, 1, 1)
            else:
                next_m = date(y, m + 1, 1)
            month_end = next_m - pd.Timedelta(days=1)
            # 把 pandas.Timestamp 转回 date
            if hasattr(month_end, 'date'):
                months.append(month_end.date())
            else:
                months.append(month_end)
    return months


def main():
    conn = db()
    cur = conn.cursor()

    month_ends = get_month_ends(2008, 2025)
    print(f'共 {len(month_ends)} 个月待评分（2008-01 ~ 2025-12）')

    rows = []
    for i, me in enumerate(month_ends, 1):
        try:
            inp = load_monthly_inputs(cur, me)
            r = evaluate_scorecard(me.year, inp)
            by_dim = r.items_by_dimension()
            rows.append({
                'snapshot': me.strftime('%Y-%m-%d'),
                'year': me.year,
                'month': me.month,
                'total_score': r.total_score,
                'target_equity_pct': r.target_equity_pct,
                'band': r.band,
                'val_score': sum(it.score for it in by_dim.get('valuation', [])),
                'liq_score': sum(it.score for it in by_dim.get('liquidity', [])),
                'fun_score': sum(it.score for it in by_dim.get('fundamental', [])),
                'sen_score': sum(it.score for it in by_dim.get('sentiment', [])),
                'ext_score': sum(it.score for it in by_dim.get('external', [])),
                'pol_score': sum(it.score for it in by_dim.get('policy', [])),
                'pe': inp.cs300_pe_ttm,
                'pb': inp.cs300_pb,
                'rate_bp': inp.rate_cum_bp_12m,
                'pmi_mfg_3m': inp.pmi_mfg_3m_avg,
                'margin_yoy': inp.margin_growth_pct,
                'new_fund_b': inp.new_fund_billion,
                'pboc': inp.pboc_tone,
                'cmt': inp.central_meeting_tone,
                'national': inp.national_team_action,
            })
        except Exception as e:
            print(f'  {me}: ERR {e}')
        if i % 36 == 0:
            print(f'  已跑 {i}/{len(month_ends)}')

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False, encoding='utf-8-sig')
    print(f'\n保存 {len(df)} 行 → {OUT_CSV}')

    # 算次 N 月 CS300 收益
    cs = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
                     conn, parse_dates=['trade_date'], index_col='trade_date')
    cs['close'] = cs['close'].astype(float)
    cs_monthly = cs['close'].resample('ME').last()

    rets = pd.DataFrame()
    for n in (1, 3, 6, 12):
        rets[f'ret_{n}m'] = cs_monthly.pct_change(n).shift(-n) * 100
    rets.index = rets.index.strftime('%Y-%m')

    df['ym'] = pd.to_datetime(df['snapshot']).dt.strftime('%Y-%m')
    df_join = df.set_index('ym').join(rets, how='left')

    print('\n=== 月度评分卡 vs 次 N 月 CS300 收益 Spearman ρ ===')
    for n in (1, 3, 6, 12):
        sub = df_join[['total_score', f'ret_{n}m']].dropna()
        if len(sub) < 12:
            print(f'  次 {n} 月: 数据不足'); continue
        rho = sub['total_score'].rank().corr(sub[f'ret_{n}m'].rank())
        print(f'  次 {n} 月 (n={len(sub)}): ρ = {rho:+.3f}  '
              f'{"✓ 反向预测有效" if rho < -0.15 else "弱信号"}')

    # 按维度看 Spearman
    print('\n=== 各维度评分 vs 次 12 月 CS300 收益 Spearman ρ ===')
    for col in ('val_score', 'liq_score', 'fun_score', 'sen_score', 'ext_score', 'pol_score'):
        sub = df_join[[col, 'ret_12m']].dropna()
        rho = sub[col].rank().corr(sub['ret_12m'].rank())
        n = len(sub)
        var = sub[col].var()
        print(f'  {col:12s}  n={n:3d}  σ={var**0.5:.2f}  ρ(vs ret_12m) = {rho:+.3f}')

    # 月度仓位变化频率
    df['equity_change'] = df['target_equity_pct'].diff()
    n_change = (df['equity_change'] != 0).sum()
    print(f'\n=== 月度仓位变化频率 ===')
    print(f'  216 月中调仓 {n_change} 次（{n_change/len(df)*100:.0f}%）')
    print(f'  档位分布:')
    print(df['target_equity_pct'].value_counts().sort_index().to_string())

    conn.close()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3.11
"""ABCDE 组合验证 — 测试 9 个关键组合"""

from __future__ import annotations

import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv
from backtest.scorecard import ScorecardInputs, policy_triple_gate, score_to_target_equity

load_dotenv(ROOT / ".env")

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_full_monthly():
    conn = db()
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['v8_total'] = score['total_score'] + score['v8_extra']

    for c in ('pmi_below_52_months', 'pmi_resume_expansion',
              'ppi_yoy', 'rate_bp_12m', 'rrr_pp_12m', 'pe'):
        if c in ml.columns:
            score[c] = ml[c]

    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    ret = cs['close'].astype(float).resample('ME').last().pct_change() * 100
    ret.index = ret.index.to_period('M').to_timestamp('M')
    return score, ret


def apply_gate_eq(row, col):
    eq, _ = score_to_target_equity(int(row[col]))
    if eq < 80:
        return float(eq)
    inp = ScorecardInputs(
        pboc_tone=row['pboc'] if pd.notna(row['pboc']) else None,
        central_meeting_tone=row['cmt'] if pd.notna(row['cmt']) else None,
        ppi_yoy_change='turn_positive' if row['fun_score'] < 0 else 'flat',
    )
    passed, _ = policy_triple_gate(inp)
    return float(eq) if passed else 75.0


# 单规则 extra
def f_a(r):
    return -1 if pd.notna(r['pmi_below_52_months']) and r['pmi_below_52_months'] >= 6 else 0

def f_c(r):
    return -1 if (pd.notna(r['rate_bp_12m']) and pd.notna(r['rrr_pp_12m'])
                  and r['rate_bp_12m'] < -100 and r['rrr_pp_12m'] < -1) else 0

def f_d(r):
    return 3 if pd.notna(r['pmi_resume_expansion']) and r['pmi_resume_expansion'] else 0

def f_e(r):
    return -1 if (pd.notna(r['pe']) and pd.notna(r['rate_bp_12m'])
                  and r['pe'] < 15 and r['rate_bp_12m'] < -100) else 0


def spearman(xs, ys):
    if len(xs) < 2: return 0.0
    def avg_rank(a):
        n = len(a); idx = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n; i = 0
        while i < n:
            j = i
            while j + 1 < n and a[idx[j + 1]] == a[idx[i]]: j += 1
            avg = (i + j + 2) / 2.0
            for k in range(i, j + 1): r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = avg_rank(xs), avg_rank(ys)
    mx = sum(rx) / len(rx); my = sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def backtest(eq, ret):
    df = pd.DataFrame({'eq': eq, 'ret': ret}).dropna()
    df['eq_prev'] = df['eq'].shift(1).fillna(75.0)
    df['turn'] = (df['eq'] - df['eq_prev']).abs()
    cash_m = CASH_ANNUAL_RATE / 12 * 100
    df['net'] = df['eq']/100*df['ret'] + (1-df['eq']/100)*cash_m - df['turn']/100*COST_PCT
    df['nav'] = (1 + df['net']/100).cumprod() * INITIAL_CAPITAL
    n = len(df)
    cum = (df['nav'].iloc[-1]/INITIAL_CAPITAL - 1)*100
    peak = df['nav'].cummax()
    dd = (df['nav']/peak - 1).min() * 100
    return cum, dd


def main():
    score, ret = load_full_monthly()
    score['v8_eq'] = score.apply(lambda r: apply_gate_eq(r, 'v8_total'), axis=1)
    v8_eq = score['v8_eq'].shift(1).fillna(75.0)
    cum_b, dd_b = backtest(v8_eq, ret)
    next_ret = ret.shift(-1).reindex(score.index)
    rho_b = spearman(score['v8_total'].fillna(0).tolist(), next_ret.fillna(0).tolist())

    print(f"{'组合':<14}{'触发月':>8}{'P&L':>10}{'Δ vs v8':>10}{'DD':>10}{'Δ vs v8':>10}{'ρ':>9}  红线")
    print(f"{'v8 baseline':<14}{'-':>8}{cum_b:>+9.2f}%{'':>10}{dd_b:>+9.2f}%{'':>10}{rho_b:>+9.3f}")
    print('-' * 100)

    combos = [
        ('D 单独',      [f_d]),
        ('D+A',         [f_d, f_a]),
        ('D+C',         [f_d, f_c]),
        ('D+E',         [f_d, f_e]),
        ('D+A+C',       [f_d, f_a, f_c]),
        ('D+A+E',       [f_d, f_a, f_e]),
        ('D+C+E',       [f_d, f_c, f_e]),
        ('D+A+C+E 全',  [f_d, f_a, f_c, f_e]),
        ('A+C+E 无D',   [f_a, f_c, f_e]),
    ]

    for name, fns in combos:
        score['_extra'] = score.apply(lambda r: sum(f(r) for f in fns), axis=1)
        score['cand_total'] = score['v8_total'] + score['_extra']
        score['cand_eq'] = score.apply(lambda r: apply_gate_eq(r, 'cand_total'), axis=1)
        cand_eq = score['cand_eq'].shift(1).fillna(75.0)
        cum_c, dd_c = backtest(cand_eq, ret)
        rho_c = spearman(score['cand_total'].fillna(0).tolist(), next_ret.fillna(0).tolist())
        n_trig = (score['_extra'] != 0).sum()

        c1 = cum_c >= cum_b
        c2 = dd_c >= dd_b - 3.0
        c3 = rho_c <= rho_b
        passed = sum([c1, c2, c3])
        flag = ''.join(['✓' if c else '✗' for c in (c1, c2, c3)])
        verdict = 'ADOPT' if passed == 3 else f'REJECT'
        print(f'{name:<14}{n_trig:>8}{cum_c:>+9.2f}%{cum_c-cum_b:>+10.2f}'
              f'{dd_c:>+9.2f}%{dd_c-dd_b:>+10.2f}{rho_c:>+9.3f}  {flag} {verdict}')


if __name__ == '__main__':
    main()

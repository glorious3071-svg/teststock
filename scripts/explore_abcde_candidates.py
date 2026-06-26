#!/usr/bin/env python3.11
"""探索 ABCDE 五个候选优化方向 — 同样基于现有月度评分序列 simulate

baseline v8: 现有总分 + cb_cuts_6m
五个候选 (每个独立测试):
  A. PMI 多月连续过滤   (pmi_below_52_months>=3 +1, pmi_resume 要求连续 2 月 ≥50)
  B. PPI 绝对水平规则   (ppi_yoy < -3 → +1, ppi_yoy > 5 → -1)
  C. 流动性共振规则     (累计降息>100bp AND 累计降准>1pp → 额外 -1)
  D. 基本面方向反转     (iva_yoy_up → +1 而不是 -1, pmi_resume → +1 而不是 -2)
  E. 跨维度共振规则     (PE<15 AND 累计降息>100bp → 额外 -1)

每个方向跑 simulate，给出 P&L、ρ、红线判定。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import (
    ScorecardInputs,
    policy_triple_gate,
    score_to_target_equity,
)

load_dotenv(ROOT / ".env")

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
COST_PCT = 0.10


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_full_monthly():
    """加载月度评分 + 关键原始字段 + cs300 月收益"""
    conn = db()
    score = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
    score['snapshot'] = pd.to_datetime(score['snapshot'])
    score = score.set_index('snapshot').sort_index()
    score.index = score.index.to_period('M').to_timestamp('M')

    ml = pd.read_csv(ROOT / 'data' / 'ml_feature_dataset.csv')
    ml['snapshot'] = pd.to_datetime(ml['snapshot'])
    ml = ml.set_index('snapshot').sort_index()
    ml.index = ml.index.to_period('M').to_timestamp('M')

    # 合并 cb_cuts_6m + 其他关键字段
    score['cb_cuts_6m'] = ml['cb_cuts_6m']
    score['v8_extra'] = score['cb_cuts_6m'].apply(lambda x: -1 if pd.notna(x) and x >= 3 else 0)
    score['v8_total'] = score['total_score'] + score['v8_extra']

    # 拉 ABCDE 需要的关键字段
    for c in ('pmi_below_52_months', 'pmi_resume_expansion',
              'ppi_yoy', 'ppi_turn_negative', 'ppi_turn_positive',
              'rate_bp_12m', 'rrr_pp_12m', 'pe', 'pb'):
        if c in ml.columns:
            score[c] = ml[c]

    # cs300 月收益
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    conn.close()
    monthly = cs['close'].astype(float).resample('ME').last()
    ret = monthly.pct_change() * 100
    ret.index = ret.index.to_period('M').to_timestamp('M')
    return score, ret


def apply_gate_eq(row, score_col='v8_total') -> float:
    eq, _ = score_to_target_equity(int(row[score_col]))
    if eq < 80:
        return float(eq)
    inp = ScorecardInputs(
        pboc_tone=row['pboc'] if pd.notna(row['pboc']) else None,
        central_meeting_tone=row['cmt'] if pd.notna(row['cmt']) else None,
        ppi_yoy_change='turn_positive' if row['fun_score'] < 0 else 'flat',
    )
    passed, _ = policy_triple_gate(inp)
    return float(eq) if passed else 75.0


# ── 五个候选规则 ──────────────────────────────────────────────
def candidate_a(row):
    """PMI 连续过滤
      - pmi_below_52_months >= 3 月才 +1（原 >=2）
      - pmi_resume_expansion 要求前 2 月 < 50 + 当月 ≥50（更严格）
      实际上 dataset 里只有 pmi_resume_expansion 单月触发 bool，无法增强
      简化: 把 PMI<52 持续 ≥6 月触发 -1 (深度收缩，反向机会)
    """
    extra = 0
    if pd.notna(row['pmi_below_52_months']) and row['pmi_below_52_months'] >= 6:
        extra -= 1  # 深度收缩 → 机会反弹
    return extra


def candidate_b(row):
    """PPI 绝对水平
      - ppi_yoy < -3 → +1 (深度通缩，警示信号)
      - ppi_yoy > 5 → -1 (回升明显，机会)
    """
    extra = 0
    if pd.notna(row['ppi_yoy']):
        if row['ppi_yoy'] < -3: extra += 1
        if row['ppi_yoy'] > 5: extra -= 1
    return extra


def candidate_c(row):
    """流动性共振
      - rate_bp_12m < -100 AND rrr_pp_12m < -1 → 额外 -1
    """
    extra = 0
    if (pd.notna(row['rate_bp_12m']) and pd.notna(row['rrr_pp_12m'])
            and row['rate_bp_12m'] < -100 and row['rrr_pp_12m'] < -1):
        extra -= 1
    return extra


def candidate_d(row):
    """基本面方向反转
      - iva 暂无数据；这里测 pmi_resume_expansion 方向反转：原 -2，新 +1
      - 旧总分含 pmi_resume -2 → 新总分 = 旧 - (-2) + (+1) = 旧 + 3
    """
    extra = 0
    if pd.notna(row['pmi_resume_expansion']) and row['pmi_resume_expansion']:
        extra += 3  # 反转: -2 改 +1
    return extra


def candidate_e(row):
    """跨维度共振: PE<15 AND 累计降息>100bp → 额外 -1"""
    extra = 0
    if (pd.notna(row['pe']) and pd.notna(row['rate_bp_12m'])
            and row['pe'] < 15 and row['rate_bp_12m'] < -100):
        extra -= 1
    return extra


CANDIDATES = [
    ('A PMI 连续过滤', candidate_a),
    ('B PPI 绝对水平', candidate_b),
    ('C 流动性共振', candidate_c),
    ('D 基本面反转', candidate_d),
    ('E 跨维度共振', candidate_e),
]


# ── 回测工具 ──────────────────────────────────────────────────
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
    df['net'] = df['eq'] / 100 * df['ret'] + (1 - df['eq'] / 100) * cash_m - df['turn'] / 100 * COST_PCT
    df['nav'] = (1 + df['net'] / 100).cumprod() * INITIAL_CAPITAL
    n = len(df)
    cum = (df['nav'].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    ann = ((df['nav'].iloc[-1] / INITIAL_CAPITAL) ** (12 / n) - 1) * 100
    peak = df['nav'].cummax()
    dd = (df['nav'] / peak - 1).min() * 100
    n_trade = (df['turn'] > 0).sum()
    return {'cum': cum, 'ann': ann, 'dd': dd, 'n_trade': n_trade}


def main():
    score, ret = load_full_monthly()
    # baseline v8 仓位
    score['v8_eq'] = score.apply(lambda r: apply_gate_eq(r, 'v8_total'), axis=1)
    v8_eq = score['v8_eq'].shift(1).fillna(75.0)
    r_v8 = backtest(v8_eq, ret)

    # baseline ρ
    next_ret = ret.shift(-1).reindex(score.index)
    rho_v8 = spearman(score['v8_total'].fillna(0).tolist(), next_ret.fillna(0).tolist())

    print(f'{"":20s}{"触发月":>8}{"P&L":>9}{"vs v8":>10}{"DD":>9}{"vs v8":>9}{"ρ":>9}{"红线":>10}')
    print(f'{"v8 baseline":<20}{"":>8}{r_v8["cum"]:>+8.2f}%{"":>10}{r_v8["dd"]:>+8.2f}%{"":>9}{rho_v8:>+9.3f}')
    print('-' * 100)

    for name, fn in CANDIDATES:
        score['extra'] = score.apply(fn, axis=1)
        score['cand_total'] = score['v8_total'] + score['extra']
        score['cand_eq'] = score.apply(lambda r: apply_gate_eq(r, 'cand_total'), axis=1)
        cand_eq = score['cand_eq'].shift(1).fillna(75.0)
        r_c = backtest(cand_eq, ret)
        rho_c = spearman(score['cand_total'].fillna(0).tolist(), next_ret.fillna(0).tolist())
        n_trig = (score['extra'] != 0).sum()
        # 红线 3/3
        cond1 = r_c['cum'] >= r_v8['cum']
        cond2 = r_c['dd'] >= r_v8['dd'] - 3.0
        cond3 = rho_c <= rho_v8
        passed = sum([cond1, cond2, cond3])
        verdict = 'ADOPT ✓' if passed == 3 else f'REJECT ({passed}/3)'
        flag = ['✓' if cond1 else '✗', '✓' if cond2 else '✗', '✓' if cond3 else '✗']
        flag_str = ''.join(flag)
        print(f'{name:<20}{n_trig:>8}{r_c["cum"]:>+8.2f}%{r_c["cum"]-r_v8["cum"]:>+9.2f}'
              f'{r_c["dd"]:>+8.2f}%{r_c["dd"]-r_v8["dd"]:>+8.2f}'
              f'{rho_c:>+9.3f}  {flag_str}  {verdict}')


if __name__ == '__main__':
    main()

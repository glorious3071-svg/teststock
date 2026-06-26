#!/usr/bin/env python3.11
"""scripts/backtest_aggressive_mapping.py — 激进映射候选回测对比

测试拉大极值/减少现金缓冲后的收益变化。
在当前 v3.4.12 评分逻辑基础上（10 条规则已裁剪），仅替换 score→equity_pct 映射。

候选：
  - baseline   : v3.4.11 当前 12 档（95-20%）
  - A_edge     : 极值放大（100/20/10，其他不变）
  - B_full     : 中度激进（加仓侧 +5pp 全档提升）
  - C_extreme  : 全光谱拉伸（加 100/拉 95/降 5）
  - D_no_cash  : 0% 缓冲（极端利好 100%，极端风险 0% 全现金）
"""
from __future__ import annotations
import json, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import load_scorecard_inputs, mysql_config

CASH_RATE = 0.02
CS300 = "000300.SH"
START_YEAR = 2006
END_YEAR = 2026


def baseline_mapping(s):
    """v3.4.11 当前 12 档"""
    if s <= -10: return 95.0
    if s <= -7:  return 90.0
    if s <= -4:  return 85.0
    if s <= -1:  return 80.0
    if s == 0:   return 75.0
    if s <= 3:   return 70.0
    if s <= 6:   return 60.0
    if s <= 9:   return 50.0
    if s <= 12:  return 30.0
    return 20.0


def A_edge(s):
    """边缘加大：≤-10 → 100%, >12 → 10%, ≤12 → 20%"""
    if s <= -10: return 100.0
    if s <= -7:  return 90.0
    if s <= -4:  return 85.0
    if s <= -1:  return 80.0
    if s == 0:   return 75.0
    if s <= 3:   return 70.0
    if s <= 6:   return 60.0
    if s <= 9:   return 50.0
    if s <= 12:  return 20.0
    return 10.0


def B_full(s):
    """加仓侧 +5pp 全档提升，风险侧 -5pp"""
    if s <= -10: return 100.0  # 95→100
    if s <= -7:  return 95.0   # 90→95
    if s <= -4:  return 90.0   # 85→90
    if s <= -1:  return 85.0   # 80→85
    if s == 0:   return 75.0
    if s <= 3:   return 65.0   # 70→65
    if s <= 6:   return 55.0   # 60→55
    if s <= 9:   return 40.0   # 50→40
    if s <= 12:  return 20.0   # 30→20
    return 10.0                 # 20→10


def C_extreme(s):
    """全光谱拉伸：100/95/90/85/80 加仓 / 75 / 65/50/35/15/0 减仓"""
    if s <= -10: return 100.0
    if s <= -7:  return 95.0
    if s <= -4:  return 90.0
    if s <= -1:  return 85.0
    if s == 0:   return 80.0
    if s <= 3:   return 65.0
    if s <= 6:   return 50.0
    if s <= 9:   return 35.0
    if s <= 12:  return 15.0
    return 0.0


def D_no_cash(s):
    """极端无缓冲：利好 100% / 风险 0%"""
    if s <= -7:  return 100.0    # 比 baseline 更早进入满仓
    if s <= -4:  return 95.0
    if s <= -1:  return 85.0
    if s == 0:   return 75.0
    if s <= 3:   return 65.0
    if s <= 6:   return 45.0
    if s <= 9:   return 25.0
    if s <= 12:  return 10.0
    return 0.0


CANDIDATES = {
    "baseline (95-20%)":    baseline_mapping,
    "A 边缘放大 (100-10%)":  A_edge,
    "B 全档+5pp (100-10%)":  B_full,
    "C 全光谱 (100-0%)":     C_extreme,
    "D 无缓冲 (100-0%)":     D_no_cash,
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


def annual_pnl(eq_pct, ret_pct, days):
    eq = eq_pct / 100
    return eq * ret_pct + (1 - eq) * CASH_RATE * 100 * days/365


def stats(curve, pnls):
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
    sharpe = (ann - 2) / vol if vol > 0 else 0
    calmar = ann / abs(mdd) if mdd < 0 else 0
    return total, ann, mdd, vol, sharpe, calmar


def main():
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    # 跑评分 + 各 mapping 的年度仓位/PnL
    records = []
    with conn.cursor() as cur:
        for year in range(START_YEAR, END_YEAR + 1):
            snap = date(year - 1, 12, 31)
            inp = load_scorecard_inputs(snap, conn=conn)
            r = evaluate_scorecard(year, inp)
            rng = cs300_year(cur, year)
            if not rng: continue
            f_d, op, l_d, cp = rng
            cs_ret = (cp/op - 1) * 100
            days = (l_d - f_d).days
            mp_eq = {name: fn(r.total_score) for name, fn in CANDIDATES.items()}
            mp_pnl = {name: annual_pnl(eq, cs_ret, days) for name, eq in mp_eq.items()}
            records.append({
                'year': year, 'cs': cs_ret, 'score': r.total_score,
                'eq': mp_eq, 'pnl': mp_pnl,
                'partial': (l_d.year == year and l_d.month < 12),
            })
    conn.close()
    n = len(records)

    # 累计权益
    caps = {name: [1_000_000] for name in CANDIDATES}
    for r in records:
        for name in CANDIDATES:
            caps[name].append(caps[name][-1] * (1 + r['pnl'][name]/100))

    # ─── 逐年表 ────────────────────────────────
    print("="*150)
    print(f"【激进映射候选回测】 {START_YEAR}-{END_YEAR}  起始 100 万元（基于 v3.4.12 评分逻辑）")
    print("="*150)
    print(f"\n{'年':<5}{'CS300%':>8}{'分':>5}", end="")
    for name in CANDIDATES:
        short = name[:7]
        print(f"{short+'仓':>9}{short+'P&L':>9}", end="")
    print()
    print("-"*150)
    for r in records:
        partial = '*' if r['partial'] else ' '
        print(f"{r['year']:<5}{r['cs']:>+7.1f}%{r['score']:>+5d}", end="")
        for name in CANDIDATES:
            print(f"{r['eq'][name]:>8.0f}%{r['pnl'][name]:>+8.1f}%", end="")
        print(f" {partial}")

    print("-"*150)

    # ─── 终值/指标对比 ─────────────────────────
    print(f"\n=== 关键指标对比 ===\n")
    print(f"{'指标':<22}", end="")
    for name in CANDIDATES:
        print(f"{name:>22}", end="")
    print()
    print('-' * (22 + 22 * len(CANDIDATES)))

    stat_rows = {}
    for name in CANDIDATES:
        pnls = [r['pnl'][name] for r in records]
        stat_rows[name] = stats(caps[name], pnls)

    print(f"{'终值（万元）':<22}", end="")
    for name in CANDIDATES:
        print(f"{caps[name][-1]/10000:>22,.1f}", end="")
    print()

    for i, label in enumerate(['累计回报 (%)', '年化收益 (%)', '最大回撤 (%)',
                                  '年化波动 (%)', 'Sharpe (-2%)', 'Calmar']):
        print(f"{label:<22}", end="")
        for name in CANDIDATES:
            v = stat_rows[name][i]
            print(f"{v:>+22.3f}", end="")
        print()

    # ─── vs baseline 改善 ─────────────────────
    print(f"\n=== vs baseline 改善 ===\n")
    base_final = caps['baseline (95-20%)'][-1]
    base_ann = stat_rows['baseline (95-20%)'][1]
    base_mdd = stat_rows['baseline (95-20%)'][2]
    base_sharpe = stat_rows['baseline (95-20%)'][4]
    for name in CANDIDATES:
        if 'baseline' in name: continue
        d_final = caps[name][-1] - base_final
        d_ann = stat_rows[name][1] - base_ann
        d_mdd = stat_rows[name][2] - base_mdd
        d_sharpe = stat_rows[name][4] - base_sharpe
        print(f"  {name:<22}: 终值 {d_final/10000:+,.1f} 万  /  年化 {d_ann:+.2f}pp"
              f"  /  MDD {d_mdd:+.2f}pp  /  Sharpe {d_sharpe:+.3f}")

    # ─── 落盘 ────────────────────────────────────
    out = {
        'candidates': list(CANDIDATES.keys()),
        'yearly': records,
        'final_capital': {name: caps[name][-1] for name in CANDIDATES},
        'stats': {name: dict(zip(['total','ann','mdd','vol','sharpe','calmar'],
                                  stat_rows[name])) for name in CANDIDATES},
    }
    out_path = ROOT / "data" / "backtests" / "aggressive_mapping_comparison.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    print(f"\n结果已保存：{out_path}")


if __name__ == "__main__":
    main()

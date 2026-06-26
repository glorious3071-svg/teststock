#!/usr/bin/env python3.11
"""scripts/backtest_pruned_scorecard.py — 裁剪规则版 vs 完整版回测对比

3 个 candidate：
  - 完整版（baseline）：当前 v3.4.11 全部规则
  - 裁剪版 A（核心）：只保留 4 条 |t|>1.0 的核心规则
  - 裁剪版 B（宽松）：保留所有方向正确的规则（剔除 10 条方向错的）

不动 scorecard.py，通过 score 后处理重算：对每年的命中项过滤后重算总分。
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
    evaluate_scorecard, score_to_target_equity, ScorecardResult,
)
from backtest.scorecard_adapter import load_scorecard_inputs, mysql_config

CASH_RATE = 0.02
CS300 = "000300.SH"
START_YEAR = 2006
END_YEAR = 2025

# 从 rule_importance.json 加载的裁剪清单
# 4 条核心规则（|t|>1.0，p<0.20）
CORE_RULES = {
    "印花税/IPO放松",
    "累计加息>150bp",
    "累计加息>100bp",
    "印花税/IPO收紧",
}

# 10 条方向错的规则（应剔除）
WRONG_DIRECTION = {
    "PE<20",
    "PPI触底反弹",
    "PMI3M均≥53(景气过热)",
    "PB<2",
    "PPI转负",
    "PB>3",
    "降息降准共振(双松)",
    "PMI<52连续≥6月(深度收缩)",
    "PE>30",
    "美股月涨>5%",
}

CANDIDATES = {
    "完整版 (39 规则)": None,         # 不过滤
    "裁剪版 B (剔方向错)": WRONG_DIRECTION,  # 剔除 10 条方向错
    "裁剪版 A (仅 4 核心)": "core",     # 只保留 4 条核心
}


def cs300_year_return(cur, year):
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code=%s AND trade_date >= %s ORDER BY trade_date ASC LIMIT 1",
        (CS300, f"{year}-01-01"))
    o = cur.fetchone()
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code=%s AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (CS300, f"{year}-12-31"))
    c = cur.fetchone()
    if not o or not c: return None
    return (float(c[0]) / float(o[0]) - 1) * 100


def annual_pnl(equity_pct, cs_ret):
    eq = equity_pct / 100
    return eq * cs_ret + (1 - eq) * CASH_RATE * 100


def spearman(xs, ys):
    if len(xs) < 2: return 0.0
    def _rank(arr):
        pairs = sorted(enumerate(arr), key=lambda p: p[1])
        ranks = [0.0] * len(arr)
        for rk, (i, _) in enumerate(pairs):
            ranks[i] = float(rk + 1)
        return ranks
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx)/len(rx), sum(ry)/len(ry)
    num = sum((a-mx)*(b-my) for a,b in zip(rx,ry))
    dx = sum((a-mx)**2 for a in rx) ** 0.5
    dy = sum((b-my)**2 for b in ry) ** 0.5
    return num/(dx*dy) if dx > 0 and dy > 0 else 0.0


def max_drawdown(curve):
    peak = curve[0]; dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v/peak - 1)
    return dd * 100


def filter_items(items, mode):
    """根据 mode 过滤命中项"""
    if mode is None:
        return items
    if mode == "core":
        return [it for it in items if it.name in CORE_RULES]
    # 否则 mode 是 set 表示要剔除的规则名
    return [it for it in items if it.name not in mode]


def main():
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    # 每年评分一次
    raw_yearly = []
    with conn.cursor() as cur:
        for year in range(START_YEAR, END_YEAR + 1):
            snap = date(year - 1, 12, 31)
            inp = load_scorecard_inputs(snap, conn=conn)
            r = evaluate_scorecard(year, inp)
            cs_ret = cs300_year_return(cur, year)
            if cs_ret is None: continue
            raw_yearly.append({
                'year': year, 'cs_ret': cs_ret, 'items': list(r.items),
            })
    conn.close()
    n = len(raw_yearly)

    # 3 个 candidate 各跑一遍
    results = {}
    for label, mode in CANDIDATES.items():
        capital = 1_000_000
        scores, rets, pnls, positions = [], [], [], []
        curve = [capital]
        for r in raw_yearly:
            kept = filter_items(r['items'], mode)
            score = sum(it.score for it in kept)
            eq_pct, _ = score_to_target_equity(score)
            pnl = annual_pnl(eq_pct, r['cs_ret'])
            capital *= (1 + pnl/100)
            curve.append(capital)
            scores.append(score); rets.append(r['cs_ret']); pnls.append(pnl)
            positions.append(eq_pct)
        total = (capital/1_000_000 - 1) * 100
        ann = ((capital/1_000_000) ** (1/n) - 1) * 100
        mdd = max_drawdown(curve)
        rho = spearman([float(s) for s in scores], rets)
        results[label] = {
            'final': capital, 'total': total, 'ann': ann, 'mdd': mdd, 'rho': rho,
            'scores': scores, 'positions': positions, 'pnls': pnls,
        }

    # ─── 输出对比表 ──────────────────────────────
    print("=" * 110)
    print("【裁剪规则 vs 完整规则 回测对比】(2006-2025, 起始 100 万)")
    print("=" * 110)
    print(f"\n{'年':<5}{'CS300%':>9}", end="")
    for label in CANDIDATES:
        print(f"{label[:8]+'分':>10}{label[:8]+'仓':>8}", end="")
    print()
    print("-" * (5 + 9 + 18 * len(CANDIDATES)))
    for i, r in enumerate(raw_yearly):
        print(f"{r['year']:<5}{r['cs_ret']:>+8.1f}%", end="")
        for label in CANDIDATES:
            res = results[label]
            print(f"{res['scores'][i]:>+10d}{res['positions'][i]:>7.0f}%", end="")
        print()

    # ─── 汇总 ────────────────────────────────────
    print('-' * 110)
    print(f"\n=== 关键指标对比 ===\n")
    print(f"   {'指标':<22}", end="")
    for label in CANDIDATES:
        print(f"{label:>22}", end="")
    print()
    print('-' * (22 + 22 * len(CANDIDATES)))
    for metric_name, key in [('终值（万元）', 'final'), ('累计回报 (%)', 'total'),
                              ('年化收益 (%)', 'ann'), ('最大回撤 (%)', 'mdd'),
                              ('Spearman ρ', 'rho')]:
        print(f"   {metric_name:<22}", end="")
        for label in CANDIDATES:
            v = results[label][key]
            if metric_name == '终值（万元）':
                print(f"{v/10000:>22,.1f}", end="")
            elif metric_name == 'Spearman ρ':
                print(f"{v:>+22.3f}", end="")
            else:
                print(f"{v:>+22.2f}", end="")
        print()

    # 完整 vs 裁剪 对比
    baseline_total = results['完整版 (39 规则)']['total']
    baseline_ann = results['完整版 (39 规则)']['ann']
    print(f"\n=== vs 完整版差距 ===")
    for label in CANDIDATES:
        if '完整版' in label: continue
        v = results[label]
        delta_total = v['total'] - baseline_total
        delta_ann = v['ann'] - baseline_ann
        print(f"   {label:<22}: 累计差 {delta_total:+.1f}pp / 年化差 {delta_ann:+.2f}pp")

    # ─── 落盘 ────────────────────────────────────
    out = {
        'candidates': {label: {k: v for k, v in res.items() if k not in ('scores','positions','pnls')}
                       for label, res in results.items()},
        'core_rules': sorted(CORE_RULES),
        'wrong_direction_rules': sorted(WRONG_DIRECTION),
    }
    out_path = ROOT / "data" / "backtests" / "pruned_scorecard_comparison.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n结果已保存：{out_path}")


if __name__ == "__main__":
    main()

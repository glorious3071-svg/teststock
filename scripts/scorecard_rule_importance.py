#!/usr/bin/env python3.11
"""scripts/scorecard_rule_importance.py — 39 条规则的特征重要性 / t 检验

对每条规则：
  - 触发年份列表
  - 触发后当年沪深300回报均值
  - vs 未触发年份的回报均值
  - t 统计量 = (μ_hit - μ_miss) / pooled_se
  - 方向一致性：opportunity 规则触发后回报应 > 均值，risk 规则触发后应 < 均值

输出：data/backtests/rule_importance.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import load_scorecard_inputs, mysql_config

CS300 = "000300.SH"
START_YEAR = 2006
END_YEAR = 2025


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


def welch_t(xs, ys):
    """Welch's t-test (不假设方差齐性)"""
    nx, ny = len(xs), len(ys)
    if nx < 2 or ny < 2: return 0.0, 1.0
    mx, my = sum(xs)/nx, sum(ys)/ny
    vx = sum((a-mx)**2 for a in xs)/(nx-1)
    vy = sum((a-my)**2 for a in ys)/(ny-1)
    se = sqrt(vx/nx + vy/ny)
    if se == 0: return 0.0, 1.0
    t = (mx - my) / se
    # 简化 p 值估算（双侧，自由度近似）
    df = ((vx/nx + vy/ny)**2) / ((vx/nx)**2/(nx-1) + (vy/ny)**2/(ny-1))
    # 极简 p 值估算（df > 10 时 t 近似正态）
    import math
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / sqrt(2))))
    return t, p


def main():
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    # 跑 20 年所有评分
    yearly = []
    with conn.cursor() as cur:
        for year in range(START_YEAR, END_YEAR + 1):
            snap = date(year - 1, 12, 31)
            inp = load_scorecard_inputs(snap, conn=conn)
            r = evaluate_scorecard(year, inp)
            cs_ret = cs300_year_return(cur, year)
            if cs_ret is None: continue
            yearly.append({
                'year': year, 'cs_ret': cs_ret,
                'items': [(it.name, it.direction, it.score, it.dimension)
                          for it in r.items],
            })
    conn.close()

    n = len(yearly)
    all_rets = [r['cs_ret'] for r in yearly]
    overall_mean = sum(all_rets) / n
    print(f"\n已加载 {n} 年数据，沪深300 均年化回报 {overall_mean:+.2f}%\n")

    # 收集每条规则的触发年份
    rule_data = defaultdict(lambda: {'years': [], 'direction': None,
                                       'score_pts': None, 'dimension': None})
    for r in yearly:
        for name, direction, pts, dim in r['items']:
            rule_data[name]['years'].append(r['year'])
            rule_data[name]['direction'] = direction
            rule_data[name]['score_pts'] = pts
            rule_data[name]['dimension'] = dim

    # 对每条规则做 t 检验
    results = []
    for name, info in rule_data.items():
        hit_years = set(info['years'])
        hit_rets = [r['cs_ret'] for r in yearly if r['year'] in hit_years]
        miss_rets = [r['cs_ret'] for r in yearly if r['year'] not in hit_years]
        if not hit_rets: continue

        mean_hit = sum(hit_rets) / len(hit_rets)
        mean_miss = sum(miss_rets) / len(miss_rets) if miss_rets else 0
        diff = mean_hit - mean_miss
        t, p = welch_t(hit_rets, miss_rets)

        # 方向一致性：
        # opportunity 规则 → 触发后回报应 > miss（diff > 0）
        # risk 规则 → 触发后回报应 < miss（diff < 0）
        expected_sign = +1 if info['direction'] == 'opportunity' else -1
        actual_sign = +1 if diff > 0 else -1
        direction_correct = (expected_sign == actual_sign)

        results.append({
            'name': name,
            'dimension': info['dimension'],
            'direction': info['direction'],
            'score_pts': info['score_pts'],
            'n_hits': len(hit_rets),
            'mean_hit': mean_hit,
            'mean_miss': mean_miss,
            'diff': diff,
            't_stat': t,
            'p_value': p,
            'direction_correct': direction_correct,
            'hit_years': sorted(hit_years),
        })

    # 排序：方向错误优先，方向对的按 |t| 降序
    def sort_key(r):
        return (r['direction_correct'], -abs(r['t_stat']))
    results.sort(key=sort_key)

    # ─── 输出表格 ────────────────────────────────
    print("=" * 130)
    print("【规则重要性 t 检验】")
    print("=" * 130)
    print(f"\n{'#':<3}{'规则名':<28}{'维度':<14}{'方向':<14}{'分':>4}{'触发':>5}"
          f"{'触发均%':>10}{'未触发均%':>11}{'差pp':>8}{'t':>7}{'p':>7}{'对':>4}")
    print('-' * 130)
    for i, r in enumerate(results, 1):
        mark_ok = '✓' if r['direction_correct'] else '✗'
        dir_zh = '机会' if r['direction'] == 'opportunity' else '风险'
        print(f"{i:<3}{r['name']:<28}{r['dimension']:<14}{dir_zh:<14}{r['score_pts']:>+4d}"
              f"{r['n_hits']:>5}{r['mean_hit']:>+9.1f}%{r['mean_miss']:>+10.1f}%"
              f"{r['diff']:>+7.1f}{r['t_stat']:>+7.2f}{r['p_value']:>7.3f}{mark_ok:>4}")

    # ─── 统计汇总 ────────────────────────────────
    print('-' * 130)
    n_total = len(results)
    n_wrong = sum(1 for r in results if not r['direction_correct'])
    n_lowsig = sum(1 for r in results if abs(r['t_stat']) < 1.0)
    n_lowhit = sum(1 for r in results if r['n_hits'] < 3)
    n_prune = sum(1 for r in results if not r['direction_correct'] or abs(r['t_stat']) < 1.0 or r['n_hits'] < 3)

    print(f"\n=== 总览 ===")
    print(f"   总规则数：{n_total}")
    print(f"   方向错误（risk 触发但市场涨，或 opp 触发但市场跌）：{n_wrong} ({n_wrong/n_total*100:.0f}%)")
    print(f"   低显著性（|t| < 1.0）：{n_lowsig} ({n_lowsig/n_total*100:.0f}%)")
    print(f"   低触发数（n < 3）：{n_lowhit} ({n_lowhit/n_total*100:.0f}%)")
    print(f"   建议裁剪候选（满足任一）：{n_prune} ({n_prune/n_total*100:.0f}%)")

    # 裁剪候选清单
    prune_list = [r for r in results
                  if not r['direction_correct'] or abs(r['t_stat']) < 1.0 or r['n_hits'] < 3]
    keep_list = [r for r in results if r not in prune_list]

    print(f"\n=== 裁剪候选清单（{len(prune_list)} 条） ===")
    for r in prune_list:
        reasons = []
        if not r['direction_correct']: reasons.append("方向错")
        if abs(r['t_stat']) < 1.0: reasons.append(f"|t|={abs(r['t_stat']):.2f}")
        if r['n_hits'] < 3: reasons.append(f"n={r['n_hits']}")
        print(f"   {r['name']:<28} [{','.join(reasons)}]  触发 {r['n_hits']} 次  均回报 {r['mean_hit']:+.1f}%")

    print(f"\n=== 保留清单（{len(keep_list)} 条核心规则） ===")
    for r in keep_list:
        dir_zh = '机会' if r['direction'] == 'opportunity' else '风险'
        print(f"   {r['name']:<28} [{dir_zh} {r['score_pts']:+d}]  t={r['t_stat']:+.2f}  n={r['n_hits']}  均回报 {r['mean_hit']:+.1f}%")

    # ─── 落盘 ────────────────────────────────────
    out = {
        'overall_mean_return_pct': overall_mean,
        'n_years': n,
        'total_rules_observed': n_total,
        'summary': {
            'n_wrong_direction': n_wrong,
            'n_low_significance': n_lowsig,
            'n_low_hits': n_lowhit,
            'n_prune_candidates': n_prune,
        },
        'rules': results,
        'prune_candidates': [r['name'] for r in prune_list],
        'keep_list': [r['name'] for r in keep_list],
    }
    out_path = ROOT / "data" / "backtests" / "rule_importance.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n结果已保存：{out_path}")


if __name__ == "__main__":
    main()

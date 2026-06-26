#!/usr/bin/env python3.11
"""scripts/backtest_walk_forward.py — 量化评分卡过拟合幅度

三个角度的样本外验证：
  1. 前后两段对比：2006-2015（in-sample 已知）vs 2016-2025（OOS 验证）
  2. 滚动 Spearman ρ：从 5 年窗口滚动到 21 年，看 ρ 稳定性
  3. 留一年法（LOO）：把每一年单独剔除，看缺该年时累计表现的变化

输出：data/backtests/walk_forward_diagnostics.json
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

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import (
    AdapterOptions, load_scorecard_inputs, mysql_config,
)

CASH_RATE = 0.02
CS300 = "000300.SH"
START_YEAR = 2006
END_YEAR = 2025  # 排除 2026（部分年）


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


def main():
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    # ─── 一次跑 21 年评分 + P&L ────────────
    yearly = []
    with conn.cursor() as cur:
        for year in range(START_YEAR, END_YEAR + 1):
            snap = date(year - 1, 12, 31)
            inp = load_scorecard_inputs(snap, conn=conn)
            r = evaluate_scorecard(year, inp)
            cs_ret = cs300_year_return(cur, year)
            if cs_ret is None: continue
            pnl = annual_pnl(r.target_equity_pct, cs_ret)
            yearly.append({
                'year': year, 'score': r.total_score,
                'equity_pct': r.target_equity_pct,
                'cs_ret': cs_ret, 'pnl': pnl,
            })
    conn.close()
    n = len(yearly)
    print(f"已加载 {n} 年评分 + P&L 数据 ({yearly[0]['year']}-{yearly[-1]['year']})\n")

    # ─── 分析 1：前后两段对比 ────────────────────
    print("=" * 80)
    print("【分析 1】前后两段对比")
    print("=" * 80)
    mid = n // 2
    first_half = yearly[:mid]   # 2006-2015
    second_half = yearly[mid:]  # 2016-2025

    def stats(rows, label):
        scores = [r['score'] for r in rows]
        crets = [r['cs_ret'] for r in rows]
        pnls = [r['pnl'] for r in rows]
        capital = 1_000_000
        peak = capital; mdd = 0
        for p in pnls:
            capital *= (1 + p/100)
            peak = max(peak, capital)
            mdd = min(mdd, capital/peak - 1)
        years = len(rows)
        total = (capital/1_000_000 - 1) * 100
        ann = ((capital/1_000_000) ** (1/years) - 1) * 100
        rho = spearman([float(s) for s in scores], crets)
        # 满仓对照
        fb = 1_000_000
        for cr in crets:
            fb *= (1 + cr/100)
        fb_total = (fb/1_000_000 - 1) * 100
        fb_ann = ((fb/1_000_000) ** (1/years) - 1) * 100
        return {
            'label': label, 'years': years,
            'total_ret': total, 'ann_ret': ann,
            'mdd': mdd * 100, 'rho': rho,
            'fb_total': fb_total, 'fb_ann': fb_ann,
            'excess_ann': ann - fb_ann,
        }

    s1 = stats(first_half, "2006-2015 (前半段)")
    s2 = stats(second_half, "2016-2025 (后半段)")
    s_all = stats(yearly, "2006-2025 (全样本)")

    print(f"\n{'区间':<22}{'年数':>5}{'累计回报%':>10}{'年化%':>8}{'MDD%':>8}"
          f"{'ρ(score,ret)':>14}{'满仓年化%':>11}{'超额年化pp':>12}")
    print("-" * 90)
    for s in (s1, s2, s_all):
        print(f"{s['label']:<22}{s['years']:>5}{s['total_ret']:>+9.1f}%{s['ann_ret']:>+7.2f}%"
              f"{s['mdd']:>+7.2f}%{s['rho']:>+14.3f}{s['fb_ann']:>+10.2f}%{s['excess_ann']:>+12.2f}")

    overfit_gap = s1['ann_ret'] - s2['ann_ret']
    print(f"\n>>> 前后两段年化差距：{overfit_gap:+.2f}pp（>3pp 视为强过拟合迹象）")
    print(f">>> 后半段 ρ vs 前半段 ρ：{s2['rho']:+.3f} vs {s1['rho']:+.3f}（差距 {s2['rho']-s1['rho']:+.3f}）")

    # ─── 分析 2：滚动 Spearman ρ ─────────────────
    print("\n" + "=" * 80)
    print("【分析 2】滚动 Spearman ρ（窗口大小递增）")
    print("=" * 80)
    print(f"\n{'窗口':<22}{'年数':>5}{'ρ':>8}{'超额年化pp':>14}")
    print("-" * 50)
    rolling = []
    for win in [5, 7, 10, 13, 15, 18, 21]:
        if win > n: break
        # 取最新 win 年
        sub = yearly[-win:]
        s = stats(sub, "")
        rolling.append({'win': win, 'rho': s['rho'], 'excess': s['excess_ann']})
        print(f"{'最近 ' + str(win) + ' 年':<22}{win:>5}{s['rho']:>+7.3f}{s['excess_ann']:>+13.2f}")
    print(f"\n>>> 如 ρ 不稳定（如忽正忽负）→ 评分卡信号方向不可靠")
    print(f">>> 如 ρ 越远越接近 0 → 早年规则在新数据上失效")

    # ─── 分析 3：留一年法（LOO） ─────────────────
    print("\n" + "=" * 80)
    print("【分析 3】留一年法（Leave-One-Out）— 看每年对累计回报的贡献")
    print("=" * 80)
    print(f"\n{'剔除年份':<10}{'当年沪深300%':>13}{'当年仓位':>9}{'当年 P&L%':>10}"
          f"{'剔除后累计%':>14}{'对累计贡献pp':>14}")
    print("-" * 75)
    full_capital = 1_000_000
    for r in yearly:
        full_capital *= (1 + r['pnl']/100)
    full_total = (full_capital/1_000_000 - 1) * 100

    loo_records = []
    for skip in yearly:
        cap = 1_000_000
        for r in yearly:
            if r['year'] == skip['year']: continue
            cap *= (1 + r['pnl']/100)
        loo_total = (cap/1_000_000 - 1) * 100
        contribution = full_total - loo_total  # 该年对全样本贡献
        loo_records.append({
            'year': skip['year'], 'cs_ret': skip['cs_ret'],
            'equity_pct': skip['equity_pct'], 'pnl': skip['pnl'],
            'loo_total': loo_total, 'contribution': contribution,
        })

    # 按贡献绝对值排序，展示 top 5 正/负贡献
    sorted_loo = sorted(loo_records, key=lambda x: x['contribution'], reverse=True)
    print(f"\n  正贡献 Top 5（最依赖的年份）：")
    for r in sorted_loo[:5]:
        print(f"  {r['year']:>4}      {r['cs_ret']:>+12.1f}%{r['equity_pct']:>8.0f}%{r['pnl']:>+9.1f}%"
              f"{r['loo_total']:>+13.1f}%{r['contribution']:>+13.1f}")
    print(f"\n  负贡献 Top 5（拖后腿的年份）：")
    for r in sorted_loo[-5:]:
        print(f"  {r['year']:>4}      {r['cs_ret']:>+12.1f}%{r['equity_pct']:>8.0f}%{r['pnl']:>+9.1f}%"
              f"{r['loo_total']:>+13.1f}%{r['contribution']:>+13.1f}")

    top_contrib = sum(r['contribution'] for r in sorted_loo[:3])
    print(f"\n>>> Top 3 正贡献年份累计：{top_contrib:+.1f}pp（占全样本 {full_total:.1f}% 的 {top_contrib/full_total*100:.1f}%）")
    print(f">>> 如 Top 3 占比 > 60% → 表现高度依赖少数年份 → 过拟合特征")

    # ─── 落盘 ────────────────────────────────────
    out = {
        'sample_split': {
            'first_half': {'label': s1['label'], 'years': s1['years'],
                          'total_ret': s1['total_ret'], 'ann_ret': s1['ann_ret'],
                          'mdd': s1['mdd'], 'rho': s1['rho'], 'fb_ann': s1['fb_ann']},
            'second_half': {'label': s2['label'], 'years': s2['years'],
                           'total_ret': s2['total_ret'], 'ann_ret': s2['ann_ret'],
                           'mdd': s2['mdd'], 'rho': s2['rho'], 'fb_ann': s2['fb_ann']},
            'overfit_gap_pp': overfit_gap,
        },
        'rolling_rho': rolling,
        'leave_one_out': loo_records,
        'full_sample_total_ret': full_total,
        'top3_contribution_share': top_contrib / full_total,
    }
    out_path = ROOT / "data" / "backtests" / "walk_forward_diagnostics.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n结果已保存：{out_path}")


if __name__ == "__main__":
    main()

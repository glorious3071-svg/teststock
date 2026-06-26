#!/usr/bin/env python3.11
"""backtest_scorecard_pmi.py — PMI 评分卡改进的回测验证

对比 baseline（pmi_mfg_3m_avg / pmi_prod_minus_order 关闭）vs candidate（开启），
看 PMI 新规则对沪深300 战略仓位决策的边际效果。

策略模型（简化）：
  - 每年 1-1 按 target_equity_pct 配置沪深300 ETF（用 index 收盘代理）
  - 余额按 CASH_ANNUAL_RATE 计利息
  - 年末结算，资金滚到下一年

评价指标：
  - 累计回报 / 年化收益 / 年化波动 / 最大回撤
  - 评分 ↔ 当年沪深300涨跌 Spearman 相关
  - 方向命中率：score > 0 时跌 / score < 0 时涨

输出：终端表格 + JSON 落盘到 data/backtests/scorecard_pmi_comparison.json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import ScorecardResult, evaluate_scorecard
from backtest.scorecard_adapter import (
    AdapterOptions,
    load_scorecard_inputs,
    mysql_config,
)

# ─── 回测参数 ─────────────────────────────────────────────────
START_YEAR = 2008                 # 2007-12 PMI 3M 才齐
END_YEAR = 2025                   # 26 年还没完整
INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02           # 现金年化 2%
CS300_CODE = "000300.SH"
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_pmi_comparison.json"


# ─── 取沪深300 年初/年末收盘 ──────────────────────────────────
def cs300_year_open_close(cur, year: int) -> tuple[float | None, float | None]:
    """返回 (年初首个交易日收盘, 年末最后交易日收盘)"""
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date >= %s
        ORDER BY trade_date ASC LIMIT 1
        """,
        (CS300_CODE, f"{year}-01-01"),
    )
    o = cur.fetchone()
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (CS300_CODE, f"{year}-12-31"),
    )
    c = cur.fetchone()
    if not o or not c or o[0] is None or c[0] is None:
        return None, None
    return float(o[0]), float(c[0])


# ─── 策略 P&L ────────────────────────────────────────────────
def annual_pnl(equity_pct: float, cs300_ret_pct: float, cash_rate: float) -> float:
    """单年简化收益（%）：股票仓 × 沪深300涨跌 + 现金仓 × 2%"""
    equity_w = equity_pct / 100.0
    cash_w = 1.0 - equity_w
    return (equity_w * cs300_ret_pct) + (cash_w * cash_rate * 100.0)


# ─── 指标汇总 ────────────────────────────────────────────────
@dataclass
class StrategyMetrics:
    cumulative_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    max_drawdown_pct: float
    spearman_score_vs_return: float
    direction_hit_rate_pct: float


def spearman(xs: list[float], ys: list[float]) -> float:
    """简化 Spearman：用 rank 后的 Pearson"""
    def _rank(arr: list[float]) -> list[float]:
        pairs = sorted(enumerate(arr), key=lambda p: p[1])
        ranks = [0.0] * len(arr)
        for rk, (orig_i, _) in enumerate(pairs):
            ranks[orig_i] = float(rk + 1)
        return ranks
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = min(dd, (v / peak - 1.0))
    return dd * 100.0


def compute_metrics(scores: list[int], rets: list[float],
                    annual_pnls: list[float]) -> StrategyMetrics:
    equity = [INITIAL_CAPITAL]
    for p in annual_pnls:
        equity.append(equity[-1] * (1.0 + p / 100.0))
    cum = (equity[-1] / equity[0] - 1.0) * 100.0
    years = len(annual_pnls)
    ann_ret = ((equity[-1] / equity[0]) ** (1.0 / years) - 1.0) * 100.0 if years else 0.0
    mean = sum(annual_pnls) / years if years else 0.0
    var = sum((p - mean) ** 2 for p in annual_pnls) / years if years else 0.0
    vol = var ** 0.5
    mdd = max_drawdown(equity)
    rho = spearman([float(s) for s in scores], rets)
    # 方向命中率：score >0 -> 期望 ret <0；score <0 -> 期望 ret >0
    hits = sum(
        1 for s, r in zip(scores, rets)
        if (s > 0 and r < 0) or (s < 0 and r > 0) or (s == 0)
    )
    hit_rate = hits / len(scores) * 100.0 if scores else 0.0
    return StrategyMetrics(
        cumulative_return_pct=cum,
        annualized_return_pct=ann_ret,
        annualized_vol_pct=vol,
        max_drawdown_pct=mdd,
        spearman_score_vs_return=rho,
        direction_hit_rate_pct=hit_rate,
    )


# ─── 主流程 ──────────────────────────────────────────────────
def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    rows = []
    base_scores, new_scores = [], []
    base_pnls, new_pnls = [], []
    rets = []

    print(f"{'年份':<6}{'沪深300%':>10}{'基线分':>8}{'基线仓位':>9}{'基线P&L':>10}"
          f"{'候选分':>8}{'候选仓位':>9}{'候选P&L':>10}{'Δ命中':>8}")
    print("-" * 86)

    try:
        with conn.cursor() as cur:
            for year in range(START_YEAR, END_YEAR + 1):
                snapshot = date(year - 1, 12, 31)
                inp_base = load_scorecard_inputs(
                    snapshot,
                    options=AdapterOptions(include_pmi_3m_avg=False,
                                           include_pmi_prod_order=False),
                    conn=conn,
                )
                inp_new = load_scorecard_inputs(
                    snapshot,
                    options=AdapterOptions(include_pmi_3m_avg=True,
                                           include_pmi_prod_order=True),
                    conn=conn,
                )
                r_base = evaluate_scorecard(year, inp_base)
                r_new = evaluate_scorecard(year, inp_new)

                o, c = cs300_year_open_close(cur, year)
                if o is None:
                    print(f"{year}: 行情缺，跳过")
                    continue
                cs300_ret = (c / o - 1.0) * 100.0

                base_p = annual_pnl(r_base.target_equity_pct, cs300_ret, CASH_ANNUAL_RATE)
                new_p = annual_pnl(r_new.target_equity_pct, cs300_ret, CASH_ANNUAL_RATE)

                # 新增规则命中差异
                delta_items = (
                    set(it.name for it in r_new.items)
                    - set(it.name for it in r_base.items)
                )
                delta_mark = "+".join(sorted(delta_items)) if delta_items else "—"

                print(f"{year:<6}{cs300_ret:>9.1f}%{r_base.total_score:>+8d}"
                      f"{r_base.target_equity_pct:>8.0f}%{base_p:>9.2f}%"
                      f"{r_new.total_score:>+8d}{r_new.target_equity_pct:>8.0f}%"
                      f"{new_p:>9.2f}% {delta_mark}")

                rows.append({
                    "year": year,
                    "cs300_return_pct": cs300_ret,
                    "baseline": {
                        "score": r_base.total_score,
                        "target_equity_pct": r_base.target_equity_pct,
                        "band": r_base.band,
                        "annual_pnl_pct": base_p,
                    },
                    "candidate": {
                        "score": r_new.total_score,
                        "target_equity_pct": r_new.target_equity_pct,
                        "band": r_new.band,
                        "annual_pnl_pct": new_p,
                        "added_items": sorted(delta_items),
                    },
                })
                base_scores.append(r_base.total_score)
                new_scores.append(r_new.total_score)
                base_pnls.append(base_p)
                new_pnls.append(new_p)
                rets.append(cs300_ret)
    finally:
        conn.close()

    m_base = compute_metrics(base_scores, rets, base_pnls)
    m_new = compute_metrics(new_scores, rets, new_pnls)

    print("\n=== 指标汇总 ===")
    print(f"{'指标':<28}{'baseline':>14}{'candidate':>14}{'Δ':>10}")
    print("-" * 66)
    for label, key in [
        ("累计回报 (%)", "cumulative_return_pct"),
        ("年化收益 (%)", "annualized_return_pct"),
        ("年化波动 (%)", "annualized_vol_pct"),
        ("最大回撤 (%)", "max_drawdown_pct"),
        ("Spearman ρ(score, ret)", "spearman_score_vs_return"),
        ("方向命中率 (%)", "direction_hit_rate_pct"),
    ]:
        b = getattr(m_base, key)
        n = getattr(m_new, key)
        print(f"{label:<28}{b:>14.2f}{n:>14.2f}{n - b:>+10.2f}")

    # 采纳判定（plan 文件采纳标准）
    criteria = {
        "累计回报增加": m_new.cumulative_return_pct > m_base.cumulative_return_pct,
        "回撤可控 (Δ ≤ +5pp)": m_new.max_drawdown_pct >= m_base.max_drawdown_pct - 5.0,
        "Spearman ρ 持平或上升": m_new.spearman_score_vs_return >= m_base.spearman_score_vs_return,
    }
    passed = sum(criteria.values())
    print(f"\n=== 采纳判定 ===")
    for c, v in criteria.items():
        print(f"  {'✓' if v else '✗'}  {c}")
    print(f"\n命中 {passed}/3 → {'采纳 ✅' if passed >= 2 else '不采纳 ❌'}")

    out = {
        "config": {
            "start_year": START_YEAR,
            "end_year": END_YEAR,
            "initial_capital": INITIAL_CAPITAL,
            "cash_annual_rate": CASH_ANNUAL_RATE,
            "cs300_code": CS300_CODE,
        },
        "rows": rows,
        "metrics": {
            "baseline": asdict(m_base),
            "candidate": asdict(m_new),
        },
        "criteria": criteria,
        "decision": "ADOPT" if passed >= 2 else "REJECT",
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存：{OUT_PATH}")
    return out


if __name__ == "__main__":
    run()

#!/usr/bin/env python3.11
"""backtest_scorecard_stamp_duty.py — v3.4.6 候选：印花税/IPO 接入评分卡 2 组合回测

对比：
  - baseline    : stamp_duty=None（当前状态，policy 维度的印花税规则从未触发）
  - +stamp_duty : 从 stamp_duty_events 取 snapshot 前 12 月最近事件

P&L 模型与 v3.4.5 一致：按 target_equity 配沪深300 + 现金（2% 年化）。
评价指标：累计回报 / 年化 / 波动 / 最大回撤 / Spearman ρ / 方向命中率。
采纳判据：累计回报 ↑ + 最大回撤 Δ≤+5pp + Spearman ρ ≤ baseline。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import (
    AdapterOptions,
    load_scorecard_inputs,
    mysql_config,
)

START_YEAR = 2008
END_YEAR = 2025
INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
CS300_CODE = "000300.SH"
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_stamp_duty_comparison.json"

CANDIDATES = {
    "baseline":    AdapterOptions(include_stamp_duty=False),
    "+stamp_duty": AdapterOptions(include_stamp_duty=True),
}


def cs300_year_open_close(cur, year: int) -> tuple[float | None, float | None]:
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code=%s AND trade_date >= %s "
        "ORDER BY trade_date ASC LIMIT 1",
        (CS300_CODE, f"{year}-01-01"),
    )
    o = cur.fetchone()
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code=%s AND trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT 1",
        (CS300_CODE, f"{year}-12-31"),
    )
    c = cur.fetchone()
    if not o or not c or o[0] is None or c[0] is None:
        return None, None
    return float(o[0]), float(c[0])


def annual_pnl(equity_pct: float, cs300_ret_pct: float, cash_rate: float) -> float:
    eq = equity_pct / 100.0
    return eq * cs300_ret_pct + (1 - eq) * cash_rate * 100.0


@dataclass
class StrategyMetrics:
    cumulative_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    max_drawdown_pct: float
    spearman_score_vs_return: float
    direction_hit_rate_pct: float


def spearman(xs: list[float], ys: list[float]) -> float:
    def _rank(arr: list[float]) -> list[float]:
        pairs = sorted(enumerate(arr), key=lambda p: p[1])
        ranks = [0.0] * len(arr)
        for rk, (orig_i, _) in enumerate(pairs):
            ranks[orig_i] = float(rk + 1)
        return ranks
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx)/len(rx), sum(ry)/len(ry)
    num = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    dx = sum((a-mx)**2 for a in rx) ** 0.5
    dy = sum((b-my)**2 for b in ry) ** 0.5
    return num/(dx*dy) if dx > 0 and dy > 0 else 0.0


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0]
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1.0)
    return dd * 100.0


def compute_metrics(scores: list[int], rets: list[float],
                    pnls: list[float]) -> StrategyMetrics:
    equity = [INITIAL_CAPITAL]
    for p in pnls:
        equity.append(equity[-1] * (1.0 + p / 100.0))
    cum = (equity[-1] / equity[0] - 1.0) * 100.0
    n = len(pnls)
    ann_ret = ((equity[-1] / equity[0]) ** (1.0 / n) - 1.0) * 100.0 if n else 0.0
    mean = sum(pnls)/n if n else 0.0
    var = sum((p-mean)**2 for p in pnls)/n if n else 0.0
    vol = var ** 0.5
    mdd = max_drawdown(equity)
    rho = spearman([float(s) for s in scores], rets)
    hits = sum(1 for s, r in zip(scores, rets)
               if (s > 0 and r < 0) or (s < 0 and r > 0) or s == 0)
    hit_rate = hits/len(scores)*100.0 if scores else 0.0
    return StrategyMetrics(
        cumulative_return_pct=cum,
        annualized_return_pct=ann_ret,
        annualized_vol_pct=vol,
        max_drawdown_pct=mdd,
        spearman_score_vs_return=rho,
        direction_hit_rate_pct=hit_rate,
    )


def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    yearly: dict[int, dict] = {}
    rets: list[float] = []
    years: list[int] = []

    try:
        with conn.cursor() as cur:
            for year in range(START_YEAR, END_YEAR + 1):
                snapshot = date(year - 1, 12, 31)
                o, c = cs300_year_open_close(cur, year)
                if o is None:
                    continue
                cs300_ret = (c / o - 1.0) * 100.0
                rets.append(cs300_ret)
                years.append(year)
                yearly[year] = {"cs300_ret": cs300_ret, "evals": {}}

                for name, opts in CANDIDATES.items():
                    inp = load_scorecard_inputs(snapshot, options=opts, conn=conn)
                    r = evaluate_scorecard(year, inp)
                    pnl = annual_pnl(r.target_equity_pct, cs300_ret, CASH_ANNUAL_RATE)
                    yearly[year]["evals"][name] = {
                        "score": r.total_score,
                        "target_equity_pct": r.target_equity_pct,
                        "pnl_pct": pnl,
                        "stamp_duty": inp.stamp_duty,
                    }
    finally:
        conn.close()

    print(f"\n=== 18 年评分对比 ({START_YEAR}-{END_YEAR}) ===\n")
    header = f"{'年':<5}{'CS300%':>9}"
    for n in CANDIDATES:
        header += f"{n+' duty':>15}{n+' 分':>9}{n+' 仓':>7}"
    print(header)
    print("-" * len(header))
    for y in years:
        row = f"{y:<5}{yearly[y]['cs300_ret']:>+8.1f}%"
        for n in CANDIDATES:
            ev = yearly[y]["evals"][n]
            duty = str(ev.get("stamp_duty") or "")[:10]
            row += f"{duty:>15}{ev['score']:>+9d}{ev['target_equity_pct']:>6.0f}%"
        print(row)

    metrics: dict[str, StrategyMetrics] = {}
    for n in CANDIDATES:
        scores = [yearly[y]["evals"][n]["score"] for y in years]
        pnls = [yearly[y]["evals"][n]["pnl_pct"] for y in years]
        metrics[n] = compute_metrics(scores, rets, pnls)

    base_m = metrics["baseline"]
    print(f"\n=== 指标汇总 ===")
    print(f"{'指标':<28}" + "".join(f"{n:>14}" for n in CANDIDATES))
    print("-" * (28 + 14 * len(CANDIDATES)))
    for label, key in [
        ("累计回报 (%)", "cumulative_return_pct"),
        ("年化收益 (%)", "annualized_return_pct"),
        ("年化波动 (%)", "annualized_vol_pct"),
        ("最大回撤 (%)", "max_drawdown_pct"),
        ("Spearman ρ(score,ret)", "spearman_score_vs_return"),
        ("方向命中率 (%)", "direction_hit_rate_pct"),
    ]:
        row = f"{label:<28}"
        for n in CANDIDATES:
            row += f"{getattr(metrics[n], key):>14.2f}"
        print(row)

    print(f"\n=== 采纳判定（+stamp_duty vs baseline）===")
    m = metrics["+stamp_duty"]
    criteria = {
        "累计回报 ↑": m.cumulative_return_pct > base_m.cumulative_return_pct,
        "回撤 Δ≤+5pp": m.max_drawdown_pct >= base_m.max_drawdown_pct - 5.0,
        "Spearman ρ ≤ baseline": m.spearman_score_vs_return <= base_m.spearman_score_vs_return,
    }
    passed = sum(criteria.values())
    decision = "ADOPT" if passed >= 2 else "REJECT"
    print(f"\n  +stamp_duty ({decision}, {passed}/3):")
    for c, v in criteria.items():
        print(f"    {'✓' if v else '✗'}  {c}")

    out = {
        "config": {
            "start_year": START_YEAR,
            "end_year": END_YEAR,
            "candidates": list(CANDIDATES.keys()),
        },
        "yearly": {str(y): yearly[y] for y in years},
        "metrics": {n: asdict(m) for n, m in metrics.items()},
        "decision": {"criteria": criteria, "passed": passed, "decision": decision},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存：{OUT_PATH}")
    return out


if __name__ == "__main__":
    run()

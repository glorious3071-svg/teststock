#!/usr/bin/env python3.11
"""backtest_scorecard_v343.py — 评分卡 v3.4.3 候选规则回测（已评估，未采纳）

**评估结论（2026-06，未采纳）**：
  - 删除 us_monthly_pct（B 组）：Spearman ρ 从 -0.41 → -0.38（变差），P&L 持平。
  - 加入 VIX 月均 > 30 → opp -2（C 组）：Spearman ρ 从 -0.41 → -0.50（显著改善），
    但 P&L 完全持平（仅 2009 触发一次，评分从 -2 → -4 仍在 75% 平衡档内）。
  - 删除 us + 加 VIX（D 组）：Spearman ρ -0.41 → -0.48，P&L 持平。

  结论与 v3.4.2 完全相同：候选信号方向有效，但被档位映射粒度（75%/80% 平衡档）吞没，
  无法改变 P&L。按"P&L 必须正向改善才采纳"严格标准，VIX 规则**不采纳**入评分卡。

  保留：
    - cboe_vix_daily 数据表（基础设施有用，未来可重启）
    - data/backtests/scorecard_v343_comparison.json（评估快照）
    - 本脚本作为评估方法论历史记录

  回退：
    - scorecard.py 的 vix_monthly_avg 字段 + score_external VIX 规则
    - scorecard_adapter.py 的 _vix_monthly_avg / include_vix_monthly_avg

  当前脚本剩余对比组合：
    - A_baseline      : us_monthly_pct ON  （生产配置）
    - B_no_us_monthly : us_monthly_pct OFF（消除噪声实验）

采纳判据：
  ✓ 累计回报增加
  ✓ 最大回撤 Δ ≤ +5pp
  ✓ Spearman ρ 更负或持平（评分高 → 跌、评分低 → 涨，业务上 ρ 越负越好）

输出：
  - 终端 2 组合对比表格 + 触发年份明细
  - JSON 落盘 data/backtests/scorecard_v343_comparison.json
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

# ─── 回测参数 ─────────────────────────────────────────────────
START_YEAR = 2008
END_YEAR = 2025
INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
CS300_CODE = "000300.SH"
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_v343_comparison.json"

# 评估后剩余对比组合（VIX 候选已回退，故移除 C/D；保留 A vs B 验证 us_monthly_pct 影响）
COMBOS = {
    "A_baseline":      AdapterOptions(include_us_monthly_pct=True),
    "B_no_us_monthly": AdapterOptions(include_us_monthly_pct=False),
}


# ─── 取沪深300 年初/年末收盘 ──────────────────────────────────
def cs300_year_open_close(cur, year: int) -> tuple[float | None, float | None]:
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
    equity_w = equity_pct / 100.0
    cash_w = 1.0 - equity_w
    return (equity_w * cs300_ret_pct) + (cash_w * cash_rate * 100.0)


# ─── 指标 ────────────────────────────────────────────────────
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
                    pnls: list[float]) -> StrategyMetrics:
    equity = [INITIAL_CAPITAL]
    for p in pnls:
        equity.append(equity[-1] * (1.0 + p / 100.0))
    cum = (equity[-1] / equity[0] - 1.0) * 100.0
    years = len(pnls)
    ann = ((equity[-1] / equity[0]) ** (1.0 / years) - 1.0) * 100.0 if years else 0.0
    mean = sum(pnls) / years if years else 0.0
    var = sum((p - mean) ** 2 for p in pnls) / years if years else 0.0
    vol = var ** 0.5
    mdd = max_drawdown(equity)
    rho = spearman([float(s) for s in scores], rets)
    hits = sum(
        1 for s, r in zip(scores, rets)
        if (s > 0 and r < 0) or (s < 0 and r > 0) or s == 0
    )
    hit = hits / len(scores) * 100.0 if scores else 0.0
    return StrategyMetrics(cum, ann, vol, mdd, rho, hit)


# ─── 主流程 ──────────────────────────────────────────────────
def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

    # 收集每组合每年数据
    series = {k: {"scores": [], "pnls": [], "items": []} for k in COMBOS}
    cs300_rets = []
    years_used = []
    yearly_rows = []

    print(f"\n{'年份':<6}{'CS300%':>8}  "
          + "  ".join(f"{k[:14]:>14}" for k in COMBOS.keys()))
    print("-" * (16 + len(COMBOS) * 16))

    try:
        with conn.cursor() as cur:
            for year in range(START_YEAR, END_YEAR + 1):
                snapshot = date(year - 1, 12, 31)

                year_results = {}
                for name, opts in COMBOS.items():
                    inp = load_scorecard_inputs(snapshot, options=opts, conn=conn)
                    res = evaluate_scorecard(year, inp)
                    year_results[name] = res

                o, c = cs300_year_open_close(cur, year)
                if o is None:
                    print(f"{year}: 行情缺，跳过")
                    continue
                ret = (c / o - 1.0) * 100.0

                cs300_rets.append(ret)
                years_used.append(year)

                summary_cells = []
                row_record = {"year": year, "cs300_return_pct": ret, "combos": {}}
                for name, res in year_results.items():
                    pnl = annual_pnl(res.target_equity_pct, ret, CASH_ANNUAL_RATE)
                    series[name]["scores"].append(res.total_score)
                    series[name]["pnls"].append(pnl)
                    series[name]["items"].append(
                        [(it.name, it.score) for it in res.items]
                    )
                    summary_cells.append(
                        f"s{res.total_score:+d}/{int(res.target_equity_pct):2d}%/p{pnl:+5.1f}"
                    )
                    row_record["combos"][name] = {
                        "score": res.total_score,
                        "target_equity_pct": res.target_equity_pct,
                        "band": res.band,
                        "annual_pnl_pct": pnl,
                        "items": [(it.name, it.score) for it in res.items],
                    }
                yearly_rows.append(row_record)

                print(f"{year:<6}{ret:>+7.1f}%  " + "  ".join(f"{s:>14}" for s in summary_cells))
    finally:
        conn.close()

    # 4 组合指标汇总
    metrics = {
        name: compute_metrics(series[name]["scores"], cs300_rets, series[name]["pnls"])
        for name in COMBOS
    }

    print("\n" + "=" * 88)
    print("=== 组合指标汇总 ===")
    print(f"{'指标':<28}" + "".join(f"{name[:14]:>15}" for name in COMBOS))
    print("-" * 88)
    for label, key in [
        ("累计回报 (%)", "cumulative_return_pct"),
        ("年化收益 (%)", "annualized_return_pct"),
        ("年化波动 (%)", "annualized_vol_pct"),
        ("最大回撤 (%)", "max_drawdown_pct"),
        ("Spearman ρ", "spearman_score_vs_return"),
        ("方向命中率 (%)", "direction_hit_rate_pct"),
    ]:
        cells = "".join(f"{getattr(metrics[name], key):>+15.2f}" for name in COMBOS)
        print(f"{label:<28}{cells}")

    # 采纳判定（每个候选组合 vs baseline A）
    # 业务正确性：评分高 = 减仓信号，期望次年跌；评分低 = 加仓信号，期望次年涨。
    # 所以 Spearman ρ 越负，"评分↔涨跌"反向关系越强，预测力越好。
    base = metrics["A_baseline"]
    print("\n" + "=" * 88)
    print("=== 采纳判定（vs A_baseline，3/3 标准）===")
    decisions = {}
    for name in [k for k in COMBOS if k != "A_baseline"]:
        m = metrics[name]
        crit = {
            "累计回报增加":           m.cumulative_return_pct > base.cumulative_return_pct,
            "回撤可控 (Δ ≤ +5pp)":    m.max_drawdown_pct >= base.max_drawdown_pct - 5.0,
            "Spearman ρ 更负或持平":  m.spearman_score_vs_return <= base.spearman_score_vs_return,
        }
        passed = sum(crit.values())
        verdict = "ADOPT ✅" if passed == 3 else ("PARTIAL ⚠️" if passed == 2 else "REJECT ❌")
        decisions[name] = {"criteria": crit, "passed": passed, "verdict": verdict}
        print(f"\n[{name}]  {verdict}  ({passed}/3)")
        for c, v in crit.items():
            mark = "✓" if v else "✗"
            print(f"    {mark}  {c}")

    # 触发年份明细
    print("\n=== us_monthly_pct 触发年份明细（A_baseline 命中但 B_no_us_monthly 没有）===")
    for i, year in enumerate(years_used):
        a_items = set(n for n, _ in series["A_baseline"]["items"][i])
        b_items = set(n for n, _ in series["B_no_us_monthly"]["items"][i])
        removed = a_items - b_items
        if any("美股" in n for n in removed):
            print(f"  {year} 应用年: 移除 {[n for n in removed if '美股' in n]}"
                  f"  CS300={cs300_rets[i]:+.1f}%")

    out = {
        "config": {
            "start_year": START_YEAR,
            "end_year": END_YEAR,
            "initial_capital": INITIAL_CAPITAL,
            "cash_annual_rate": CASH_ANNUAL_RATE,
            "cs300_code": CS300_CODE,
            "combos": {k: asdict(v) for k, v in COMBOS.items()},
        },
        "rows": yearly_rows,
        "metrics": {k: asdict(v) for k, v in metrics.items()},
        "decisions": decisions,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存：{OUT_PATH}")
    return out


if __name__ == "__main__":
    run()

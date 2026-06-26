#!/usr/bin/env python3.11
"""backtest_scorecard_mapping.py — 评分卡映射函数 baseline / P1a / P1b 三方对比

破除 75% 中性带钝化的实验：scripts/simulate_scorecard_20y.py 跑出的 21 年回测显示
评分卡产出仓位高度量化（21 年 11 年卡 75%），根因在 backtest/scorecard.py:260-276
score_to_target_equity 的 8 档表中性带跨 8pp。本脚本不改 39 条评分规则、不改
evaluate_scorecard，只用 3 套 mapping 函数对同一评分序列重算 P&L 看效果。

策略：
  - baseline     : 现有 score_to_target_equity（8 档，中性带 75% 吞 8 个评分值）
  - p1a_ladder   : 12 档加密阶梯（中性带细化为 80/75/70 三档）
  - p1b_sigmoid  : tanh 连续平滑（amp=40, scale=4, cap=[20, 95]）

P&L 模型与 scripts/simulate_scorecard_20y.py 一致：
  - 每年年初按 mapping 给出的 target_equity_pct 配 CS300，其余现金
  - 年末复利结算，下一年再调仓

采纳判据（4 维，映射实验专用——评分不变，Spearman 必同所以换成 Calmar）：
  1. 累计回报：candidate ≥ baseline + 5pp
  2. 最大回撤：|MDD_c| - |MDD_b| ≤ +5pp
  3. 年化波动：vol_c - vol_b ≤ +3pp
  4. Calmar  ：candidate ≥ baseline
  ≥3/4 → ADOPT     2/4 (必含 1+2) → PARTIAL     <2/4 → REJECT

输出：终端三方对比表 + JSON → data/backtests/scorecard_mapping_comparison.json
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import ScorecardResult, evaluate_scorecard, score_to_target_equity
from backtest.scorecard_adapter import (
    AdapterOptions,
    load_scorecard_inputs,
    mysql_config,
)

# ─── 回测参数（与 simulate_scorecard_20y.py 对齐）─────────────────
START_YEAR = 2006
END_YEAR = 2026
INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
CS300_CODE = "000300.SH"
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_mapping_comparison.json"

# ─── 采纳判据常量 ──────────────────────────────────────────────
CUMULATIVE_RETURN_MIN_DELTA_PP = 5.0
MAX_DRAWDOWN_TOLERANCE_PP = 5.0
ANNUALIZED_VOL_TOLERANCE_PP = 3.0
TIEBREAK_CUMRET_GAP_PP = 10.0     # 两候选累计回报差 < 该值则按可解释性选 P1a


# ─── 映射函数 ────────────────────────────────────────────────
MappingFn = Callable[[int], tuple[float, str]]


def mapping_baseline(score: int) -> tuple[float, str]:
    """当前生产映射，直接复用 backtest.scorecard.score_to_target_equity"""
    return score_to_target_equity(score)


def mapping_p1a_ladder(score: int) -> tuple[float, str]:
    """P1a — 12 档加密阶梯，攻击 75% 中性带本体

    设计要点：
      * 保留 score≥+10 → 30%/20% 的极端段（守 2008 救场决策）
      * 把原 [-4, +3] 共 8 评分塌成 75% 改为「-4~-1→80, 0→75, +1~+3→70」3 个细档
      * 每 3 个评分跳 5pp，与评分实际标差 4.30 大致匹配
    """
    if score <= -10: return 95.0, "极度便宜+刺激共振"
    if score <= -7:  return 90.0, "深度机会"
    if score <= -4:  return 85.0, "机会显著"
    if score <= -1:  return 80.0, "机会偏多"
    if score == 0:   return 75.0, "平衡"
    if score <= 3:   return 70.0, "中性偏防"
    if score <= 6:   return 60.0, "风险偏多"
    if score <= 9:   return 50.0, "风险显著"
    if score <= 12:  return 30.0, "高风险"
    return 20.0, "极端风险"


# ─── P1b sigmoid 参数 ───────────────────────────────────────
P1B_BASE_POSITION_PCT = 75.0       # 0 评分对应的中性仓位
P1B_AMPLITUDE_PP = 40.0            # 极端评分对中性的最大偏离
P1B_SCALE = 4.0                    # tanh 缩放系数 ≈ 评分实际 σ
P1B_CEILING_PCT = 95.0
P1B_FLOOR_PCT = 20.0

# 仓位区间 → band 标签
_P1B_BAND_LABELS = (
    (90.0, "极度便宜+刺激共振"),
    (82.5, "深度机会"),
    (77.5, "机会偏多"),
    (72.5, "平衡"),
    (62.5, "中性偏防"),
    (45.0, "风险偏多"),
    (35.0, "风险显著"),
    (25.0, "高风险"),
    (0.0,  "极端风险"),
)


def _p1b_band_label(position_pct: float) -> str:
    for threshold, label in _P1B_BAND_LABELS:
        if position_pct >= threshold:
            return label
    return "极端风险"


def mapping_p1b_sigmoid(score: int) -> tuple[float, str]:
    """P1b — tanh 连续平滑映射，完全去离散化

    pos = clip(75 - 40 · tanh(score / 4), 20, 95)

    关键校准：
      * score=0  → 75%
      * score=+10 → 75 - 40·tanh(2.5) ≈ 35.5%   逼近 baseline 30% 的 2008 救场
      * score=-5  → 75 + 40·0.848 = 108.9 → 截至 95%
    """
    raw = P1B_BASE_POSITION_PCT - P1B_AMPLITUDE_PP * math.tanh(score / P1B_SCALE)
    position_pct = max(P1B_FLOOR_PCT, min(P1B_CEILING_PCT, raw))
    return position_pct, _p1b_band_label(position_pct)


STRATEGIES: dict[str, MappingFn] = {
    "baseline":     mapping_baseline,
    "p1a_ladder":   mapping_p1a_ladder,
    "p1b_sigmoid":  mapping_p1b_sigmoid,
}


# ─── 取 CS300 年初/年末 ────────────────────────────────────
def cs300_year_open_close(cur, year: int) -> tuple[float | None, float | None]:
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date >= %s
        ORDER BY trade_date ASC LIMIT 1
        """,
        (CS300_CODE, f"{year}-01-01"),
    )
    open_row = cur.fetchone()
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (CS300_CODE, f"{year}-12-31"),
    )
    close_row = cur.fetchone()
    if not open_row or not close_row or open_row[0] is None or close_row[0] is None:
        return None, None
    return float(open_row[0]), float(close_row[0])


# ─── P&L / 指标 ─────────────────────────────────────────────
def annual_pnl(equity_pct: float, cs300_ret_pct: float, cash_rate: float) -> float:
    equity_w = equity_pct / 100.0
    cash_w = 1.0 - equity_w
    return equity_w * cs300_ret_pct + cash_w * cash_rate * 100.0


def max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd = min(dd, value / peak - 1.0)
    return dd * 100.0


@dataclass
class StrategyMetrics:
    cumulative_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    max_drawdown_pct: float
    calmar: float
    final_capital: float


def compute_metrics(annual_pnls: list[float]) -> StrategyMetrics:
    equity = [INITIAL_CAPITAL]
    for pnl in annual_pnls:
        equity.append(equity[-1] * (1.0 + pnl / 100.0))
    years = len(annual_pnls)
    cum_ret = (equity[-1] / equity[0] - 1.0) * 100.0
    ann_ret = ((equity[-1] / equity[0]) ** (1.0 / years) - 1.0) * 100.0 if years else 0.0
    mean = sum(annual_pnls) / years if years else 0.0
    var = sum((p - mean) ** 2 for p in annual_pnls) / years if years else 0.0
    vol = math.sqrt(var)
    mdd = max_drawdown(equity)
    calmar = ann_ret / abs(mdd) if mdd != 0 else float("inf")
    return StrategyMetrics(
        cumulative_return_pct=cum_ret,
        annualized_return_pct=ann_ret,
        annualized_vol_pct=vol,
        max_drawdown_pct=mdd,
        calmar=calmar,
        final_capital=equity[-1],
    )


# ─── 采纳判据 ───────────────────────────────────────────────
@dataclass
class CriteriaResult:
    cumulative_return_pass: bool
    max_drawdown_pass: bool
    annualized_vol_pass: bool
    calmar_pass: bool
    pass_count: int
    decision: str           # ADOPT / PARTIAL / REJECT


def evaluate_criteria(baseline: StrategyMetrics,
                      candidate: StrategyMetrics) -> CriteriaResult:
    cum_pass = candidate.cumulative_return_pct >= (
        baseline.cumulative_return_pct + CUMULATIVE_RETURN_MIN_DELTA_PP
    )
    mdd_pass = abs(candidate.max_drawdown_pct) - abs(baseline.max_drawdown_pct) \
        <= MAX_DRAWDOWN_TOLERANCE_PP
    vol_pass = candidate.annualized_vol_pct - baseline.annualized_vol_pct \
        <= ANNUALIZED_VOL_TOLERANCE_PP
    calmar_pass = candidate.calmar >= baseline.calmar
    pass_count = sum([cum_pass, mdd_pass, vol_pass, calmar_pass])

    if pass_count >= 3:
        decision = "ADOPT"
    elif pass_count >= 2 and cum_pass and mdd_pass:
        decision = "PARTIAL"
    else:
        decision = "REJECT"

    return CriteriaResult(
        cumulative_return_pass=cum_pass,
        max_drawdown_pass=mdd_pass,
        annualized_vol_pass=vol_pass,
        calmar_pass=calmar_pass,
        pass_count=pass_count,
        decision=decision,
    )


def pick_winner(results: dict[str, CriteriaResult],
                metrics: dict[str, StrategyMetrics]) -> str:
    """从 ADOPT/PARTIAL 候选中挑出最终推荐。

    规则：
      - 优先 ADOPT > PARTIAL > REJECT
      - 同级别按累计回报；累计回报差 < TIEBREAK_CUMRET_GAP_PP 时选 p1a_ladder
        （离散档位易解释、易人工干预、与 v3.4.x 历史延续）
    """
    candidates = [name for name in results if name != "baseline"]
    by_decision = {"ADOPT": [], "PARTIAL": [], "REJECT": []}
    for name in candidates:
        by_decision[results[name].decision].append(name)

    for tier in ("ADOPT", "PARTIAL"):
        pool = by_decision[tier]
        if not pool:
            continue
        if len(pool) == 1:
            return pool[0]
        # 多个：累计回报差 ≥ 阈值挑高者，否则选 p1a_ladder
        pool_sorted = sorted(pool, key=lambda n: -metrics[n].cumulative_return_pct)
        top, runner_up = pool_sorted[0], pool_sorted[1]
        if metrics[top].cumulative_return_pct \
                - metrics[runner_up].cumulative_return_pct >= TIEBREAK_CUMRET_GAP_PP:
            return top
        return "p1a_ladder" if "p1a_ladder" in pool else top
    return "none"


# ─── 主流程 ─────────────────────────────────────────────────
def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())
    adapter_options = AdapterOptions()    # 与 simulate_scorecard_20y 一致

    yearly_rows: list[dict] = []
    pnls_by_strategy: dict[str, list[float]] = {name: [] for name in STRATEGIES}
    cs300_rets: list[float] = []
    scores: list[int] = []

    header = (
        f"{'年份':<6}{'CS300%':>9}{'评分':>6}  "
        f"{'base仓':>7}{'baseP&L':>9}  "
        f"{'p1a仓':>7}{'p1aP&L':>9}  "
        f"{'p1b仓':>8}{'p1bP&L':>9}"
    )
    print(f"\n{'='*92}")
    print(f"评分卡映射函数三方对比 ({START_YEAR}–{END_YEAR})  "
          f"初始 {INITIAL_CAPITAL/1e4:.0f} 万  现金年化 {CASH_ANNUAL_RATE*100:.1f}%")
    print(f"{'='*92}")
    print(header)
    print(f"{'-'*92}")

    try:
        with conn.cursor() as cur:
            for year in range(START_YEAR, END_YEAR + 1):
                snapshot = date(year - 1, 12, 31)
                inp = load_scorecard_inputs(snapshot, options=adapter_options, conn=conn)
                result: ScorecardResult = evaluate_scorecard(year, inp)

                cs_open, cs_close = cs300_year_open_close(cur, year)
                if cs_open is None:
                    print(f"{year}: 行情缺，跳过")
                    continue
                cs_ret = (cs_close / cs_open - 1.0) * 100.0

                row: dict = {
                    "year": year,
                    "score": result.total_score,
                    "cs300_return_pct": cs_ret,
                    "strategies": {},
                }
                for name, mapping_fn in STRATEGIES.items():
                    position_pct, band = mapping_fn(result.total_score)
                    pnl = annual_pnl(position_pct, cs_ret, CASH_ANNUAL_RATE)
                    pnls_by_strategy[name].append(pnl)
                    row["strategies"][name] = {
                        "target_equity_pct": position_pct,
                        "band": band,
                        "annual_pnl_pct": pnl,
                    }
                yearly_rows.append(row)
                scores.append(result.total_score)
                cs300_rets.append(cs_ret)

                print(
                    f"{year:<6}{cs_ret:>+8.1f}%{result.total_score:>+6}  "
                    f"{row['strategies']['baseline']['target_equity_pct']:>6.0f}%"
                    f"{row['strategies']['baseline']['annual_pnl_pct']:>+8.1f}%  "
                    f"{row['strategies']['p1a_ladder']['target_equity_pct']:>6.0f}%"
                    f"{row['strategies']['p1a_ladder']['annual_pnl_pct']:>+8.1f}%  "
                    f"{row['strategies']['p1b_sigmoid']['target_equity_pct']:>7.1f}%"
                    f"{row['strategies']['p1b_sigmoid']['annual_pnl_pct']:>+8.1f}%"
                )
    finally:
        conn.close()

    # ── 指标汇总 ──────────────────────────────────
    metrics = {name: compute_metrics(pnls_by_strategy[name]) for name in STRATEGIES}

    print(f"\n{'='*92}")
    print(f"指标汇总（{len(scores)} 年）")
    print(f"{'='*92}")
    print(f"{'指标':<22}" + "".join(f"{name:>16}" for name in STRATEGIES))
    print(f"{'-'*92}")
    for label, key, fmt in [
        ("终值 (万)",       "final_capital",         lambda v: f"{v/1e4:>16.2f}"),
        ("累计回报 (%)",   "cumulative_return_pct", lambda v: f"{v:>+16.1f}"),
        ("年化收益 (%)",   "annualized_return_pct", lambda v: f"{v:>+16.2f}"),
        ("年化波动 (%)",   "annualized_vol_pct",    lambda v: f"{v:>16.2f}"),
        ("最大回撤 (%)",   "max_drawdown_pct",      lambda v: f"{v:>+16.1f}"),
        ("Calmar",         "calmar",                lambda v: f"{v:>16.3f}"),
    ]:
        line = f"{label:<22}"
        for name in STRATEGIES:
            line += fmt(getattr(metrics[name], key))
        print(line)

    # ── 采纳判据 ──────────────────────────────────
    print(f"\n{'='*92}")
    print(f"采纳判据 vs baseline（≥3/4 ADOPT  ·  2/4 含累计+回撤 PARTIAL  ·  否则 REJECT）")
    print(f"{'='*92}")
    criteria_results: dict[str, CriteriaResult] = {"baseline": CriteriaResult(
        True, True, True, True, 4, "BASELINE"
    )}
    for name in STRATEGIES:
        if name == "baseline":
            continue
        res = evaluate_criteria(metrics["baseline"], metrics[name])
        criteria_results[name] = res
        print(f"\n  【{name}】 {res.pass_count}/4  →  {res.decision}")
        print(f"    {'✓' if res.cumulative_return_pass else '✗'}  "
              f"累计回报 ≥ baseline + {CUMULATIVE_RETURN_MIN_DELTA_PP:.0f}pp  "
              f"(Δ={metrics[name].cumulative_return_pct - metrics['baseline'].cumulative_return_pct:+.1f}pp)")
        print(f"    {'✓' if res.max_drawdown_pass else '✗'}  "
              f"最大回撤恶化 ≤ +{MAX_DRAWDOWN_TOLERANCE_PP:.0f}pp  "
              f"(Δ={abs(metrics[name].max_drawdown_pct) - abs(metrics['baseline'].max_drawdown_pct):+.1f}pp)")
        print(f"    {'✓' if res.annualized_vol_pass else '✗'}  "
              f"年化波动恶化 ≤ +{ANNUALIZED_VOL_TOLERANCE_PP:.0f}pp  "
              f"(Δ={metrics[name].annualized_vol_pct - metrics['baseline'].annualized_vol_pct:+.2f}pp)")
        print(f"    {'✓' if res.calmar_pass else '✗'}  "
              f"Calmar ≥ baseline  "
              f"(Δ={metrics[name].calmar - metrics['baseline'].calmar:+.3f})")

    winner = pick_winner(criteria_results, metrics)
    print(f"\n{'='*92}")
    if winner == "none":
        print(f"  推荐：保持 baseline，两候选均未通过采纳判据 ❌")
    else:
        decision = criteria_results[winner].decision
        print(f"  推荐 candidate：{winner}  "
              f"（{decision}，累计回报 {metrics[winner].cumulative_return_pct:+.1f}% "
              f"vs baseline {metrics['baseline'].cumulative_return_pct:+.1f}%）")

    # ── 仓位分布对比 ─────────────────────────────
    print(f"\n{'='*92}")
    print(f"仓位分布（每策略 21 年内的 unique 档位数 & 中位/平均/极差）")
    print(f"{'='*92}")
    for name in STRATEGIES:
        positions = [row["strategies"][name]["target_equity_pct"] for row in yearly_rows]
        positions_sorted = sorted(positions)
        unique_count = len(set(round(p, 1) for p in positions))
        median = positions_sorted[len(positions_sorted) // 2]
        avg = sum(positions) / len(positions)
        spread = max(positions) - min(positions)
        print(f"  {name:<14} unique={unique_count:>3}  median={median:>5.1f}%  "
              f"avg={avg:>5.1f}%  range=[{min(positions):.1f}, {max(positions):.1f}]  "
              f"spread={spread:.1f}pp")

    # ── 落盘 ──────────────────────────────────────
    out = {
        "config": {
            "start_year": START_YEAR,
            "end_year": END_YEAR,
            "initial_capital": INITIAL_CAPITAL,
            "cash_annual_rate": CASH_ANNUAL_RATE,
            "cs300_code": CS300_CODE,
            "p1b_params": {
                "base_position_pct": P1B_BASE_POSITION_PCT,
                "amplitude_pp": P1B_AMPLITUDE_PP,
                "scale": P1B_SCALE,
                "ceiling_pct": P1B_CEILING_PCT,
                "floor_pct": P1B_FLOOR_PCT,
            },
            "criteria_thresholds": {
                "cumulative_return_min_delta_pp": CUMULATIVE_RETURN_MIN_DELTA_PP,
                "max_drawdown_tolerance_pp": MAX_DRAWDOWN_TOLERANCE_PP,
                "annualized_vol_tolerance_pp": ANNUALIZED_VOL_TOLERANCE_PP,
            },
        },
        "yearly": yearly_rows,
        "metrics": {name: asdict(metrics[name]) for name in STRATEGIES},
        "criteria": {name: asdict(criteria_results[name]) for name in STRATEGIES},
        "winner": winner,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已保存：{OUT_PATH}")
    return out


if __name__ == "__main__":
    run()

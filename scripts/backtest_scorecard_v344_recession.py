#!/usr/bin/env python3.11
"""backtest_scorecard_v344_recession.py — global_recession 候选回测（已评估，未采纳）

**评估结论（2026-06，未采纳）**：
  对比组合：
    - A_baseline      : include_global_recession=False（原 default）
    - B_+recession    : include_global_recession=True（评分卡 spec §六 行 178 承诺接入）

  规则触发（5 经济体 OECD CLI 投票 ≥2 票）：
    apply_year ∈ {2009, 2012, 2016, 2019, 2023} 共 5 次

  指标对比（2008-2025，18 年）：
    | 指标              | A_baseline | B_+recession | Δ |
    |---|---:|---:|---:|
    | 累计回报 (%)       |    +70.11  |    +68.34    | **-1.77** |
    | 年化收益 (%)       |     +3.00  |     +2.94    |   -0.06   |
    | 最大回撤 (%)       |    -32.12  |    -32.12    |    0.00   |
    | Spearman ρ        |    -0.41   |    -0.29     | **+0.12** (恶化) |

  关键单年（跨档位）：
    - 2016: score -6 → -4，target 80%→75%，CS300=-4.6% → P&L +0.3pp 略好
    - 2019: score -5 → -3，target 80%→75%，CS300=+38.0% → P&L **-1.8pp 恶化**
            （2018 制造业 PMI 全球同步下行触发 5/5 票，但 A 股 2019 是 V 型反转年）

  根本原因：与 v3.4.2 PPI/PMI 候选、v3.4.3 VIX 候选完全相同：
    1. 其他 3 次触发（2009/2012/2023）在 75% 平衡档内部，P&L 不变
    2. 2 次跨档（2016/2019），其中 2019 单年负贡献 -1.8pp 直接吞没 2016 的 +0.3pp
    3. OECD CLI 是制造业景气信号，对中国权益市场预测力不足（A 股 vs 全球制造业不同步）

  结论与启示：
    按"P&L 必须正向改善才采纳"严格标准 **不采纳** global_recession 入评分卡 default。
    保留：
      - oecd_cli_monthly 数据表与 scripts/import_oecd_cli.py（数据治理基础设施）
      - backtest/scorecard_adapter.py 的 _global_recession 函数 + include_global_recession 开关
        （默认 False，需要时可显式 opts.include_global_recession=True 启用）
      - 评估快照 data/backtests/scorecard_v344_recession_comparison.json
      - 本脚本作为评估方法论历史记录

采纳判据：
  ✓ 累计回报增加
  ✓ 最大回撤 Δ ≤ +5pp
  ✓ Spearman ρ 更负或持平（评分高 → 跌、评分低 → 涨，业务上 ρ 越负越好）

输出：
  - 终端 2 组合对比 + 触发年份明细
  - JSON 落盘 data/backtests/scorecard_v344_recession_comparison.json
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
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_v344_recession_comparison.json"

# 评估组合：A_baseline 不开 global_recession；B 开
COMBOS = {
    "A_baseline":    AdapterOptions(include_global_recession=False),
    "B_+recession":  AdapterOptions(include_global_recession=True),
}


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


def annual_pnl(equity_pct: float, cs300_ret_pct: float, cash_rate: float) -> float:
    equity_w = equity_pct / 100.0
    cash_w = 1.0 - equity_w
    return (equity_w * cs300_ret_pct) + (cash_w * cash_rate * 100.0)


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


def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())

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

                open_close = cs300_year_open_close(cur, year)
                if open_close[0] is None:
                    print(f"{year}: 行情缺，跳过")
                    continue
                ret = (open_close[1] / open_close[0] - 1.0) * 100.0

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

    metrics = {
        name: compute_metrics(series[name]["scores"], cs300_rets, series[name]["pnls"])
        for name in COMBOS
    }

    print("\n" + "=" * 72)
    print("=== 组合指标汇总 ===")
    print(f"{'指标':<28}" + "".join(f"{name[:14]:>15}" for name in COMBOS))
    print("-" * 72)
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

    base = metrics["A_baseline"]
    print("\n" + "=" * 72)
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

    print("\n=== global_recession 触发年份明细（B 命中但 A 没有）===")
    for i, year in enumerate(years_used):
        a_items = set(n for n, _ in series["A_baseline"]["items"][i])
        b_items = set(n for n, _ in series["B_+recession"]["items"][i])
        added = b_items - a_items
        if any("衰退" in n for n in added):
            a_eq = next(
                (it for it in COMBOS), None  # placeholder
            )
            print(f"  apply_year={year}  CS300={cs300_rets[i]:+.1f}%  新增 {[n for n in added if '衰退' in n]}")

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

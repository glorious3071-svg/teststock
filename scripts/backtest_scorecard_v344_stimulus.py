#!/usr/bin/env python3.11
"""backtest_scorecard_v344_stimulus.py — global_stimulus 候选回测（3/3 标准评估）

候选规则（评分卡 spec §六 行 180）：
  5 大央行 (Fed/ECB/BoE/BoJ/PBoC) 过去 12 个月 cut 投票 ≥3 家 → external -1 分
  PBoC 计票：cn_deposit_rate.direction='cut' ∪ cn_rrr_changes.rrr_change_pp<0（"large"/"all"）

对比组合：
  - A_baseline     : include_global_stimulus=False（当前 default）
  - B_+stimulus    : include_global_stimulus=True

采纳判据（与 v3.4.3 / v3.4.4 一致的 3/3 标准）：
  ✓ 累计回报增加
  ✓ 最大回撤 Δ ≤ +5pp
  ✓ Spearman ρ 更负或持平（评分高 → 跌、评分低 → 涨，业务上 ρ 越负越好）

输出：
  - 终端 2 组合对比 + 触发年份明细
  - JSON 落盘 data/backtests/scorecard_v344_stimulus_comparison.json
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
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_v344_stimulus_comparison.json"

# 评估组合：A_baseline 不开 global_stimulus；B 开
COMBOS = {
    "A_baseline":   AdapterOptions(include_global_stimulus=False),
    "B_+stimulus":  AdapterOptions(include_global_stimulus=True),
}

# 采纳判据阈值
DRAWDOWN_TOLERANCE_PP = 5.0  # 最大回撤可恶化上限


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
            f"回撤可控 (Δ ≤ +{DRAWDOWN_TOLERANCE_PP:.0f}pp)":
                m.max_drawdown_pct >= base.max_drawdown_pct - DRAWDOWN_TOLERANCE_PP,
            "Spearman ρ 更负或持平":  m.spearman_score_vs_return <= base.spearman_score_vs_return,
        }
        passed = sum(crit.values())
        verdict = "ADOPT ✅" if passed == 3 else ("PARTIAL ⚠️" if passed == 2 else "REJECT ❌")
        decisions[name] = {"criteria": crit, "passed": passed, "verdict": verdict}
        print(f"\n[{name}]  {verdict}  ({passed}/3)")
        for c, v in crit.items():
            mark = "✓" if v else "✗"
            print(f"    {mark}  {c}")

    print("\n=== global_stimulus 触发年份明细（B 命中但 A 没有）===")
    for i, year in enumerate(years_used):
        a_items = set(n for n, _ in series["A_baseline"]["items"][i])
        b_items = set(n for n, _ in series["B_+stimulus"]["items"][i])
        added = b_items - a_items
        if any("同步刺激" in n for n in added):
            print(
                f"  apply_year={year}  CS300={cs300_rets[i]:+.1f}%  "
                f"score Δ={series['B_+stimulus']['scores'][i] - series['A_baseline']['scores'][i]:+d}  "
                f"target Δ={series['B_+stimulus']['pnls'][i] - series['A_baseline']['pnls'][i]:+.2f}pp"
            )

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

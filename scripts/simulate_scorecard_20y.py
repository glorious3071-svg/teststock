#!/usr/bin/env python3.11
"""V5.0 评分卡 20 年完整投资模拟（2006 年初 ~ 2026 年底）

模型：
  - 启动资金 100 万
  - 每年年初依据 snapshot=(year-1)-12-31 的评分卡指示设定目标仓位
  - 当年沪深 300 仓位贡献 = equity% × CS300 年度回报
  - 现金仓位贡献 = (1 - equity%) × CASH_ANNUAL_RATE
  - 年末复利，下一年再调仓

输出：
  - 终端：年度逐行明细（评分/档位/目标仓位/CS300 回报/年度 P&L/年末权益/累计回报）
        + 命中分析（哪几年评分判断对/错）
        + 与"满仓 CS300"和"全现金"两条基准对比
        + 最终年化 / 最大回撤 / 命中率
  - JSON：data/backtests/scorecard_20y_simulation.json

用法：
  python3.11 scripts/simulate_scorecard_20y.py
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

from backtest.scorecard import ScorecardResult, evaluate_scorecard
from backtest.scorecard_adapter import (
    AdapterOptions,
    load_scorecard_inputs,
    mysql_config,
)

# ─── 参数 ─────────────────────────────────────────────
START_YEAR = 2006
END_YEAR = 2026
INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02       # 现金年化（货币基金/活期均值）
CS300_CODE = "000300.SH"
OUT_PATH = ROOT / "data" / "backtests" / "scorecard_20y_simulation.json"


@dataclass
class YearRecord:
    year: int
    snapshot_date: str
    score: int
    band: str
    target_equity_pct: float
    cs300_open: float
    cs300_close: float
    cs300_return_pct: float
    equity_pnl_pct: float       # 当年股票贡献
    cash_pnl_pct: float         # 当年现金贡献
    annual_pnl_pct: float       # 当年组合总收益
    year_end_capital: float
    cumulative_return_pct: float
    top_items: list[tuple[str, int]]


def cs300_year_bounds(cur, year: int) -> tuple[float | None, float | None]:
    """返回 (year-初首交易日 close, year-末末交易日 close)。"""
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


def annual_components(equity_pct: float, cs_return_pct: float,
                       cash_rate: float) -> tuple[float, float, float]:
    """返回 (equity_pnl_pct, cash_pnl_pct, total_pnl_pct)。"""
    equity_w = equity_pct / 100.0
    cash_w = 1.0 - equity_w
    eq_pnl = equity_w * cs_return_pct
    cash_pnl = cash_w * cash_rate * 100.0
    return eq_pnl, cash_pnl, eq_pnl + cash_pnl


def max_drawdown(equity_curve: list[float]) -> float:
    """最大回撤 (%)，负值。"""
    peak = equity_curve[0]
    dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = min(dd, (v / peak - 1.0))
    return dd * 100.0


def annualized(total_ret_pct: float, years: int) -> float:
    return ((1.0 + total_ret_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0


def run() -> dict:
    load_dotenv(ROOT / ".env")
    conn = pymysql.connect(**mysql_config())
    opts = AdapterOptions()  # 当前 default = 全部已采纳的评分卡规则

    capital = INITIAL_CAPITAL
    records: list[YearRecord] = []
    equity_curve: list[float] = [capital]

    fullstock_capital = INITIAL_CAPITAL    # 基准 A：满仓 CS300
    cashonly_capital = INITIAL_CAPITAL     # 基准 B：全现金

    header = (
        f"{'年':<4}{'评分':>5}  {'档位':<10}{'仓位':>6}  "
        f"{'CS300':>8}  {'股贡献':>8}{'现金贡献':>10}{'年P&L':>9}  "
        f"{'年末权益':>12}  {'累计回报':>10}"
    )
    print(f"\n{'='*108}")
    print(f"V5.0 评分卡 20 年完整投资模拟  ({START_YEAR}年初 ~ {END_YEAR}年底)  "
          f"初始 {INITIAL_CAPITAL/1e4:.0f} 万  现金年化 {CASH_ANNUAL_RATE*100:.1f}%")
    print(f"{'='*108}")
    print(header)
    print(f"{'-'*108}")

    try:
        with conn.cursor() as cur:
            for year in range(START_YEAR, END_YEAR + 1):
                snapshot = date(year - 1, 12, 31)
                inp = load_scorecard_inputs(snapshot, options=opts, conn=conn)
                res: ScorecardResult = evaluate_scorecard(year, inp)

                cs_open, cs_close = cs300_year_bounds(cur, year)
                if cs_open is None:
                    print(f"{year}: 行情缺，跳过")
                    continue
                cs_ret = (cs_close / cs_open - 1.0) * 100.0

                eq_pnl, cash_pnl, total_pnl = annual_components(
                    res.target_equity_pct, cs_ret, CASH_ANNUAL_RATE,
                )

                year_start_capital = capital
                capital = capital * (1.0 + total_pnl / 100.0)
                equity_curve.append(capital)

                fullstock_capital *= 1.0 + cs_ret / 100.0
                cashonly_capital *= 1.0 + CASH_ANNUAL_RATE

                cum_ret = (capital / INITIAL_CAPITAL - 1.0) * 100.0
                top_items = sorted(
                    [(it.name, it.score) for it in res.items],
                    key=lambda x: -abs(x[1]),
                )[:5]

                records.append(YearRecord(
                    year=year,
                    snapshot_date=snapshot.isoformat(),
                    score=res.total_score,
                    band=res.band,
                    target_equity_pct=res.target_equity_pct,
                    cs300_open=cs_open,
                    cs300_close=cs_close,
                    cs300_return_pct=cs_ret,
                    equity_pnl_pct=eq_pnl,
                    cash_pnl_pct=cash_pnl,
                    annual_pnl_pct=total_pnl,
                    year_end_capital=capital,
                    cumulative_return_pct=cum_ret,
                    top_items=top_items,
                ))

                print(f"{year:<4}{res.total_score:>+5}  {res.band:<10}"
                      f"{int(res.target_equity_pct):>5}%  "
                      f"{cs_ret:>+7.1f}%  "
                      f"{eq_pnl:>+7.1f}% {cash_pnl:>+9.1f}%{total_pnl:>+8.1f}%  "
                      f"{capital/1e4:>10.2f}万  {cum_ret:>+8.1f}%")

    finally:
        conn.close()

    # ── 汇总指标 ─────────────────────────
    years = len(records)
    final_ret = (capital / INITIAL_CAPITAL - 1.0) * 100.0
    annual_ret = annualized(final_ret, years)
    mdd = max_drawdown(equity_curve)

    fullstock_ret = (fullstock_capital / INITIAL_CAPITAL - 1.0) * 100.0
    fullstock_ann = annualized(fullstock_ret, years)
    cashonly_ret = (cashonly_capital / INITIAL_CAPITAL - 1.0) * 100.0
    cashonly_ann = annualized(cashonly_ret, years)

    # 命中率：评分负 vs CS300 涨；评分正 vs CS300 跌；评分 0 → 不计
    hits = 0
    valid = 0
    for r in records:
        if r.score == 0:
            continue
        valid += 1
        if (r.score < 0 and r.cs300_return_pct > 0) or (r.score > 0 and r.cs300_return_pct < 0):
            hits += 1
    hit_rate = hits / valid * 100 if valid else 0.0

    print(f"{'-'*108}")
    print(f"{'最终':<4}{'':>5}  {'':<10}{'':>6}  {'':>8}  "
          f"{'':>8}{'':>10}{'':>9}  {capital/1e4:>10.2f}万  {final_ret:>+8.1f}%")
    print()
    print(f"{'='*108}")
    print(f"汇总（{years} 年 / {records[0].year}-{records[-1].year}）")
    print(f"{'='*108}")
    print(f"  评分卡组合       初始 100 万 → 终值 {capital/1e4:>8.2f} 万  "
          f"累计 {final_ret:>+8.1f}%  年化 {annual_ret:>+6.2f}%  最大回撤 {mdd:>+6.1f}%")
    print(f"  满仓 CS300 基准  初始 100 万 → 终值 {fullstock_capital/1e4:>8.2f} 万  "
          f"累计 {fullstock_ret:>+8.1f}%  年化 {fullstock_ann:>+6.2f}%")
    print(f"  全现金 2% 基准   初始 100 万 → 终值 {cashonly_capital/1e4:>8.2f} 万  "
          f"累计 {cashonly_ret:>+8.1f}%  年化 {cashonly_ann:>+6.2f}%")
    print()
    print(f"  方向命中率（剔除 score=0）：{hits}/{valid} = {hit_rate:.1f}%")
    print(f"  超额收益 vs 满仓 CS300：{final_ret - fullstock_ret:+.1f}pp  "
          f"({annual_ret - fullstock_ann:+.2f}pp 年化)")
    print(f"  超额收益 vs 全现金：     {final_ret - cashonly_ret:+.1f}pp")

    print()
    print(f"{'='*108}")
    print("关键年份叙事（评分卡为啥这么判 + 实际结果）")
    print(f"{'='*108}")
    narrative_years = {
        2007: "牛市顶预警年",
        2008: "全球金融危机年",
        2009: "政策底反弹年",
        2014: "降息周期 + 杠杆牛启动",
        2015: "股灾年",
        2018: "贸易战 + 紧缩冲击",
        2020: "疫情冲击 + 政策对冲",
        2022: "稳增长承压年",
        2024: "924 反转年",
        2025: "适度宽松周期",
    }
    for r in records:
        if r.year not in narrative_years:
            continue
        items_str = ", ".join(f"{n}({s:+d})" for n, s in r.top_items[:4])
        print(f"\n  【{r.year}】{narrative_years[r.year]}")
        print(f"    评分卡: 评分 {r.score:+d} / 仓位 {int(r.target_equity_pct)}%  "
              f"({r.band})")
        print(f"    主信号: {items_str}")
        print(f"    实际:    CS300 {r.cs300_return_pct:+.1f}% → 评分卡组合年收 {r.annual_pnl_pct:+.1f}%  "
              f"(满仓口径会拿到 {r.cs300_return_pct:+.1f}%)")

    print()
    print(f"{'='*108}")
    print("回撤最深与表现最好年份")
    print(f"{'='*108}")
    sorted_years = sorted(records, key=lambda r: r.annual_pnl_pct)
    print(f"  最差 3 年:")
    for r in sorted_years[:3]:
        print(f"    {r.year}  组合 {r.annual_pnl_pct:+6.1f}%  (CS300 {r.cs300_return_pct:+6.1f}%, "
              f"仓位 {int(r.target_equity_pct)}%)")
    print(f"  最好 3 年:")
    for r in sorted_years[-3:][::-1]:
        print(f"    {r.year}  组合 {r.annual_pnl_pct:+6.1f}%  (CS300 {r.cs300_return_pct:+6.1f}%, "
              f"仓位 {int(r.target_equity_pct)}%)")

    out = {
        "config": {
            "start_year": START_YEAR,
            "end_year": END_YEAR,
            "initial_capital": INITIAL_CAPITAL,
            "cash_annual_rate": CASH_ANNUAL_RATE,
            "cs300_code": CS300_CODE,
        },
        "yearly": [asdict(r) for r in records],
        "summary": {
            "final_capital": capital,
            "final_return_pct": final_ret,
            "annualized_return_pct": annual_ret,
            "max_drawdown_pct": mdd,
            "hit_rate_pct": hit_rate,
            "fullstock_final": fullstock_capital,
            "fullstock_return_pct": fullstock_ret,
            "fullstock_annualized_pct": fullstock_ann,
            "cashonly_final": cashonly_capital,
            "cashonly_return_pct": cashonly_ret,
            "cashonly_annualized_pct": cashonly_ann,
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已保存：{OUT_PATH}")
    return out


if __name__ == "__main__":
    run()

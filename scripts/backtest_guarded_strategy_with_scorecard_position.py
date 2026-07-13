#!/usr/bin/env python3
"""Backtest guarded Top-5 CSI strategy with annual scorecard position sizing.

Selection return comes from scripts/backtest_news_vector_guarded_strategy.py.
Total equity exposure comes from the V5.0 annual scorecard:
snapshot=(year-1)-12-31 -> evaluate_scorecard(year) -> target_equity_pct.
Uninvested cash earns CASH_ANNUAL_RATE.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import AdapterOptions, load_scorecard_inputs, mysql_config
from scripts.validate_csi_rank import forward_return

OUT_DIR = ROOT / "data" / "ml"
GUARDED_RETURNS = OUT_DIR / "news_vector_guarded_strategy_yearly_returns.csv"
GUARDED_HOLDINGS = OUT_DIR / "news_vector_guarded_strategy_yearly_holdings.csv"
OUT_JSON = OUT_DIR / "guarded_strategy_scorecard_position_backtest.json"
OUT_CSV = OUT_DIR / "guarded_strategy_scorecard_position_yearly.csv"

CASH_ANNUAL_RATE = 0.02


@dataclass
class YearRecord:
    year: int
    snapshot_date: str
    score: int
    band: str
    target_equity_pct: float
    strategy_return: float
    benchmark_return: float
    cash_return: float
    portfolio_return: float
    year_start_capital: float
    year_end_capital: float
    cumulative_return: float
    top_codes: str
    top_names: str


def read_guarded_returns() -> dict[int, dict[str, Any]]:
    with GUARDED_RETURNS.open(encoding="utf-8") as f:
        return {int(row["year"]): row for row in csv.DictReader(f)}


def read_guarded_holdings() -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = {}
    with GUARDED_HOLDINGS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(int(row["year"]), []).append(row)
    return out


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0]
    dd = 0.0
    for value in curve:
        peak = max(peak, value)
        dd = min(dd, value / peak - 1.0)
    return dd


def annualized(total_return: float, years: int) -> float:
    if years <= 0:
        return 0.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def run(year_from: int, year_to: int, initial_capital: float) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    guarded = read_guarded_returns()
    holdings = read_guarded_holdings()
    conn = pymysql.connect(**mysql_config())
    opts = AdapterOptions()
    capital = initial_capital
    full_guarded_capital = initial_capital
    full_cs300_capital = initial_capital
    cash_capital = initial_capital
    curve = [capital]
    full_guarded_curve = [full_guarded_capital]
    full_cs300_curve = [full_cs300_capital]
    records: list[YearRecord] = []

    try:
        for year in range(year_from, year_to + 1):
            if year not in guarded:
                continue
            snapshot = date(year - 1, 12, 31)
            inputs = load_scorecard_inputs(snapshot, options=opts, conn=conn)
            scorecard = evaluate_scorecard(year, inputs)
            equity_w = float(scorecard.target_equity_pct) / 100.0
            strategy_return = float(guarded[year]["strategy_return"])
            benchmark_return = float(guarded[year]["benchmark_return"])
            cash_return = CASH_ANNUAL_RATE
            portfolio_return = equity_w * strategy_return + (1.0 - equity_w) * cash_return
            year_start = capital
            capital *= 1.0 + portfolio_return
            full_guarded_capital *= 1.0 + strategy_return
            full_cs300_capital *= 1.0 + benchmark_return
            cash_capital *= 1.0 + cash_return
            curve.append(capital)
            full_guarded_curve.append(full_guarded_capital)
            full_cs300_curve.append(full_cs300_capital)

            year_holdings = sorted(holdings.get(year, []), key=lambda r: int(r["rank"]))
            records.append(
                YearRecord(
                    year=year,
                    snapshot_date=snapshot.isoformat(),
                    score=int(scorecard.total_score),
                    band=scorecard.band,
                    target_equity_pct=float(scorecard.target_equity_pct),
                    strategy_return=strategy_return,
                    benchmark_return=benchmark_return,
                    cash_return=cash_return,
                    portfolio_return=portfolio_return,
                    year_start_capital=year_start,
                    year_end_capital=capital,
                    cumulative_return=capital / initial_capital - 1.0,
                    top_codes="|".join(r["ts_code"] for r in year_holdings),
                    top_names="|".join(r["index_name"] for r in year_holdings),
                )
            )
    finally:
        conn.close()

    years = len(records)
    final_return = capital / initial_capital - 1.0
    full_guarded_return = full_guarded_capital / initial_capital - 1.0
    full_cs300_return = full_cs300_capital / initial_capital - 1.0
    cash_return_total = cash_capital / initial_capital - 1.0
    summary = {
        "initial_capital": initial_capital,
        "final_capital": capital,
        "years": years,
        "final_return": final_return,
        "annualized_return": annualized(final_return, years),
        "max_drawdown": max_drawdown(curve),
        "full_guarded_final_capital": full_guarded_capital,
        "full_guarded_return": full_guarded_return,
        "full_guarded_annualized": annualized(full_guarded_return, years),
        "full_guarded_max_drawdown": max_drawdown(full_guarded_curve),
        "full_cs300_final_capital": full_cs300_capital,
        "full_cs300_return": full_cs300_return,
        "full_cs300_annualized": annualized(full_cs300_return, years),
        "full_cs300_max_drawdown": max_drawdown(full_cs300_curve),
        "cash_final_capital": cash_capital,
        "cash_return": cash_return_total,
        "cash_annual_rate": CASH_ANNUAL_RATE,
    }
    payload = {
        "strategy": "guarded_top5_with_scorecard_position",
        "year_from": year_from,
        "year_to": year_to,
        "records": [asdict(r) for r in records],
        "summary": summary,
        "source_returns": str(GUARDED_RETURNS),
        "source_holdings": str(GUARDED_HOLDINGS),
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="year_from", type=int, default=2021)
    parser.add_argument("--to", dest="year_to", type=int, default=2025)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    args = parser.parse_args()
    result = run(args.year_from, args.year_to, args.initial_capital)
    summary = result["summary"]

    def pct(v: float) -> str:
        return f"{v * 100:.2f}%"

    print("Guarded Top-5 + Annual Scorecard Position Backtest")
    print(f"  years: {summary['years']} initial={summary['initial_capital']:,.0f}")
    print(
        f"  final={summary['final_capital']:,.0f} "
        f"return={pct(summary['final_return'])} annualized={pct(summary['annualized_return'])} "
        f"max_dd={pct(summary['max_drawdown'])}"
    )
    print(
        f"  full_guarded={summary['full_guarded_final_capital']:,.0f} "
        f"return={pct(summary['full_guarded_return'])}"
    )
    print(
        f"  full_cs300={summary['full_cs300_final_capital']:,.0f} "
        f"return={pct(summary['full_cs300_return'])}"
    )
    print("\nYearly:")
    for row in result["records"]:
        print(
            f"  {row['year']}: score={row['score']:+d} eq={row['target_equity_pct']:.0f}% "
            f"strategy={pct(row['strategy_return'])} portfolio={pct(row['portfolio_return'])} "
            f"capital={row['year_end_capital']:,.0f}"
        )
    print(f"\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

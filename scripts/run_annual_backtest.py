#!/usr/bin/env python3
"""Run backtest from annual direction session allocation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import fetch_etf_daily
from backtest.engine import BacktestConfig, format_metrics, run_strategic_allocation

SESSION_DIR = ROOT / "data" / "annual_direction_sessions"


def load_session(year: int) -> dict:
    path = SESSION_DIR / f"backtest_{year}.json"
    if not path.exists():
        raise SystemExit(f"会话不存在: {path}\n请先运行: python scripts/run_annual_direction.py start {year}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="年初定方向战略配置回测")
    parser.add_argument("year", type=int, help="配置年份，如 2011")
    parser.add_argument("--cash", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--end", default=None, help="结束日期，默认 year-12-31")
    args = parser.parse_args()

    session = load_session(args.year)
    alloc = session.get("last_allocation")
    if not alloc or not alloc.get("etf_allocations"):
        raise SystemExit(f"{args.year} 尚无配置方案，请先运行定方向 Agent")

    start = f"{args.year}-01-01"
    end = args.end or f"{args.year}-12-31"
    etf_allocs = alloc["etf_allocations"]
    codes = [a["ts_code"] for a in etf_allocs]

    print(f"=== {args.year} 年初定方向回测 ===")
    print(f"权益仓位: {alloc.get('equity_weight_pct')}% | 现金仓位: {alloc.get('cash_weight_pct')}%")
    print("ETF 配置:")
    for a in etf_allocs:
        print(f"  {a['ts_code']} {a.get('name', '')} {a['weight_pct']}%")
    print(f"\n区间: {start} ~ {end}")
    print(f"初始资金: {args.cash:,.0f} 元")
    print("拉取行情...")

    prices = fetch_etf_daily(codes, start, end)
    if prices.empty:
        raise SystemExit("未获取到行情数据")

    config = BacktestConfig(initial_cash=args.cash, start_date=start, end_date=end)
    result = run_strategic_allocation(prices, etf_allocs, config)

    print(f"\n首个交易日建仓: {result.trades.iloc[0]['trade_date'] if not result.trades.empty else 'N/A'}")
    print("\n=== 回测结果 ===")
    print(format_metrics(result.metrics))

    if not result.trades.empty:
        print("=== 建仓记录 ===")
        cols = ["trade_date", "ts_code", "weight_pct", "shares", "price", "amount", "commission"]
        print(result.trades[cols].to_string(index=False))

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"annual_{args.year}"
    equity_path = out_dir / f"{tag}_equity.csv"
    trades_path = out_dir / f"{tag}_trades.csv"
    result.equity_curve.to_csv(equity_path)
    result.trades.to_csv(trades_path, index=False)

    summary = {
        "year": args.year,
        "allocation": alloc,
        "config": {"start": start, "end": end, "initial_cash": args.cash},
        "metrics": result.metrics,
        "first_trade_date": result.trades.iloc[0]["trade_date"] if not result.trades.empty else None,
    }
    summary_path = out_dir / f"{tag}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\n净值曲线: {equity_path}")
    print(f"交易记录: {trades_path}")
    print(f"摘要: {summary_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run a simple 2006 ETF backtest with 1M initial capital."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import fetch_etf_daily, load_listed_etfs
from backtest.engine import BacktestConfig, format_metrics, run_buy_and_hold, run_equal_weight

# 2006年实际有行情的宽基ETF（按上市先后）
DEFAULT_2006_UNIVERSE = [
    "510050.SH",  # 上证50，2005上市，2006全年可交易
    "159901.SZ",  # 深100，2006-04上市
    "510180.SH",  # 上证180，2006-05上市
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple ETF backtest")
    parser.add_argument("--start", default="2006-01-01")
    parser.add_argument("--end", default="2006-12-31")
    parser.add_argument("--cash", type=float, default=1_000_000)
    parser.add_argument(
        "--strategy",
        choices=["buy_hold", "equal_weight"],
        default="buy_hold",
        help="buy_hold: 单标的买入持有; equal_weight: 月度等权",
    )
    parser.add_argument("--code", default="510050.SH", help="buy_hold 策略使用的ETF")
    args = parser.parse_args()

    config = BacktestConfig(
        initial_cash=args.cash,
        start_date=args.start,
        end_date=args.end,
    )

    if args.strategy == "buy_hold":
        codes = [args.code]
        print(f"策略: 买入持有 {args.code}")
    else:
        etfs = load_listed_etfs(args.start)
        known = [c for c in DEFAULT_2006_UNIVERSE if c in set(etfs["ts_code"])]
        codes = known or DEFAULT_2006_UNIVERSE
        print(f"策略: 月度等权 {codes}")

    print(f"区间: {args.start} ~ {args.end}")
    print(f"初始资金: {args.cash:,.0f} 元")
    print("拉取行情...")

    prices = fetch_etf_daily(codes, args.start, args.end)
    if prices.empty:
        raise SystemExit("未获取到行情数据")

    print(f"行情矩阵: {prices.shape[0]} 个交易日 x {prices.shape[1]} 只ETF")

    if args.strategy == "buy_hold":
        result = run_buy_and_hold(prices, args.code, config)
    else:
        etfs = load_listed_etfs(args.end)
        list_dates = etfs.set_index("ts_code")["list_date"].to_dict()
        result = run_equal_weight(prices, codes, list_dates, config)

    print("\n=== 回测结果 ===")
    print(format_metrics(result.metrics))

    if not result.trades.empty:
        print("=== 交易记录 ===")
        print(result.trades.to_string(index=False))

    out = ROOT / "data" / f"backtest_{args.start[:4]}_{args.strategy}.csv"
    result.equity_curve.to_csv(out)
    print(f"\n净值曲线已保存: {out}")


if __name__ == "__main__":
    main()

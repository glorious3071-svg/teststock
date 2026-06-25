"""Simple ETF backtest engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd


@dataclass
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    start_date: str = "2006-01-01"
    end_date: str = "2006-12-31"
    commission_rate: float = 0.0003  # 万三单边
    slippage_rate: float = 0.0
    rebalance: Literal["none", "equal_weight"] = "none"


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, float]
    config: BacktestConfig


def _apply_cost(price: float, side: str, commission_rate: float, slippage_rate: float) -> float:
    slip = 1 + slippage_rate if side == "buy" else 1 - slippage_rate
    return price * slip


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min())


def _cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    days = (equity.index[-1] - equity.index[0]).days
    if days <= 0:
        return 0.0
    total = equity.iloc[-1] / equity.iloc[0]
    return float(total ** (365.25 / days) - 1)


def run_buy_and_hold(
    prices: pd.DataFrame,
    ts_code: str,
    config: BacktestConfig,
) -> BacktestResult:
    """All-in buy on first available bar, hold until end."""
    if ts_code not in prices.columns:
        raise ValueError(f"No price data for {ts_code}")

    px = prices[ts_code].dropna()
    px = px.loc[(px.index >= pd.Timestamp(config.start_date)) & (px.index <= pd.Timestamp(config.end_date))]
    if px.empty:
        raise ValueError(f"No prices in range for {ts_code}")

    trade_date = px.index[0]
    buy_price = _apply_cost(px.iloc[0], "buy", config.commission_rate, config.slippage_rate)
    shares = int(config.initial_cash / buy_price / 100) * 100  # A股ETF按100份整数
    if shares <= 0:
        raise ValueError("Initial cash too small to buy 100 shares")

    cost = shares * buy_price
    commission = cost * config.commission_rate
    cash = config.initial_cash - cost - commission

    holdings = pd.Series({ts_code: shares}, dtype=float)
    trades = [
        {
            "trade_date": trade_date.strftime("%Y-%m-%d"),
            "ts_code": ts_code,
            "side": "buy",
            "price": buy_price,
            "shares": shares,
            "amount": cost,
            "commission": commission,
        }
    ]

    equity_rows = []
    for dt, row in prices.loc[trade_date:].iterrows():
        if dt > pd.Timestamp(config.end_date):
            break
        price = row.get(ts_code)
        if pd.isna(price):
            continue
        market_value = holdings[ts_code] * price
        equity_rows.append(
            {
                "trade_date": dt,
                "cash": cash,
                "market_value": market_value,
                "equity": cash + market_value,
                "holdings": shares,
            }
        )

    equity_curve = pd.DataFrame(equity_rows).set_index("trade_date")
    metrics = _calc_metrics(equity_curve["equity"], config.initial_cash)

    return BacktestResult(
        equity_curve=equity_curve,
        trades=pd.DataFrame(trades),
        metrics=metrics,
        config=config,
    )


def run_equal_weight(
    prices: pd.DataFrame,
    universe: list[str],
    list_dates: dict[str, pd.Timestamp],
    config: BacktestConfig,
) -> BacktestResult:
    """Equal-weight rebalance on first trading day each month for listed ETFs."""
    start = pd.Timestamp(config.start_date)
    end = pd.Timestamp(config.end_date)
    px = prices.loc[(prices.index >= start) & (prices.index <= end), universe].copy()

    cash = config.initial_cash
    holdings = pd.Series(0.0, index=universe)
    trades: list[dict] = []
    equity_rows: list[dict] = []

    rebalance_dates = pd.date_range(start, end, freq="MS")
    rebalance_set = set(rebalance_dates)

    for dt, row in px.iterrows():
        if dt in rebalance_set or not equity_rows:
            active = [
                code
                for code in universe
                if list_dates.get(code, pd.Timestamp.max) <= dt and pd.notna(row.get(code))
            ]
            if active:
                # sell all
                for code in universe:
                    shares = holdings[code]
                    if shares <= 0:
                        continue
                    price = row.get(code)
                    if pd.isna(price):
                        continue
                    sell_price = _apply_cost(price, "sell", config.commission_rate, config.slippage_rate)
                    amount = shares * sell_price
                    commission = amount * config.commission_rate
                    cash += amount - commission
                    holdings[code] = 0
                    trades.append(
                        {
                            "trade_date": dt.strftime("%Y-%m-%d"),
                            "ts_code": code,
                            "side": "sell",
                            "price": sell_price,
                            "shares": shares,
                            "amount": amount,
                            "commission": commission,
                        }
                    )

                target = cash / len(active)
                for code in active:
                    price = row[code]
                    buy_price = _apply_cost(price, "buy", config.commission_rate, config.slippage_rate)
                    shares = int(target / buy_price / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * buy_price
                    commission = cost * config.commission_rate
                    total = cost + commission
                    if total > cash:
                        continue
                    cash -= total
                    holdings[code] = shares
                    trades.append(
                        {
                            "trade_date": dt.strftime("%Y-%m-%d"),
                            "ts_code": code,
                            "side": "buy",
                            "price": buy_price,
                            "shares": shares,
                            "amount": cost,
                            "commission": commission,
                        }
                    )

        market_value = sum(holdings[code] * row[code] for code in universe if pd.notna(row.get(code)))
        equity_rows.append(
            {
                "trade_date": dt,
                "cash": cash,
                "market_value": market_value,
                "equity": cash + market_value,
            }
        )

    equity_curve = pd.DataFrame(equity_rows).set_index("trade_date")
    metrics = _calc_metrics(equity_curve["equity"], config.initial_cash)

    return BacktestResult(
        equity_curve=equity_curve,
        trades=pd.DataFrame(trades),
        metrics=metrics,
        config=config,
    )


def run_strategic_allocation(
    prices: pd.DataFrame,
    etf_allocations: list[dict],
    config: BacktestConfig,
) -> BacktestResult:
    """Buy-and-hold portfolio with target weights on first trading day (年初定方向)."""
    start = pd.Timestamp(config.start_date)
    end = pd.Timestamp(config.end_date)
    codes = [a["ts_code"] for a in etf_allocations]
    px = prices.loc[(prices.index >= start) & (prices.index <= end), codes].copy()
    if px.empty:
        raise ValueError("No price data in backtest range")

    first_dt = px.dropna(how="all").index[0]
    row = px.loc[first_dt]

    cash = config.initial_cash
    holdings = pd.Series(0.0, index=codes)
    trades: list[dict] = []

    for alloc in etf_allocations:
        code = alloc["ts_code"]
        weight = float(alloc["weight_pct"]) / 100.0
        price = row.get(code)
        if pd.isna(price):
            raise ValueError(f"No price on {first_dt.date()} for {code}")

        target_amount = config.initial_cash * weight
        buy_price = _apply_cost(price, "buy", config.commission_rate, config.slippage_rate)
        shares = int(target_amount / buy_price / 100) * 100
        if shares <= 0:
            continue

        cost = shares * buy_price
        commission = cost * config.commission_rate
        total = cost + commission
        if total > cash:
            shares = int((cash / (1 + config.commission_rate)) / buy_price / 100) * 100
            if shares <= 0:
                continue
            cost = shares * buy_price
            commission = cost * config.commission_rate
            total = cost + commission

        cash -= total
        holdings[code] = shares
        trades.append(
            {
                "trade_date": first_dt.strftime("%Y-%m-%d"),
                "ts_code": code,
                "side": "buy",
                "price": buy_price,
                "shares": shares,
                "amount": cost,
                "commission": commission,
                "weight_pct": alloc["weight_pct"],
            }
        )

    equity_rows: list[dict] = []
    for dt, price_row in px.loc[first_dt:].iterrows():
        if dt > end:
            break
        market_value = sum(
            holdings[code] * price_row[code]
            for code in codes
            if holdings[code] > 0 and pd.notna(price_row.get(code))
        )
        equity_rows.append(
            {
                "trade_date": dt,
                "cash": cash,
                "market_value": market_value,
                "equity": cash + market_value,
            }
        )

    equity_curve = pd.DataFrame(equity_rows).set_index("trade_date")
    metrics = _calc_metrics(equity_curve["equity"], config.initial_cash)

    return BacktestResult(
        equity_curve=equity_curve,
        trades=pd.DataFrame(trades),
        metrics=metrics,
        config=config,
    )


def _calc_metrics(equity: pd.Series, initial_cash: float) -> dict[str, float]:
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_cash - 1
    return {
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_return": total_return,
        "total_return_pct": total_return * 100,
        "cagr": _cagr(equity),
        "max_drawdown": _max_drawdown(equity),
        "max_drawdown_pct": _max_drawdown(equity) * 100,
        "trading_days": float(len(equity)),
    }


def format_metrics(metrics: dict[str, float]) -> str:
    return (
        f"初始资金: {metrics['initial_cash']:,.0f}\n"
        f"期末净值: {metrics['final_equity']:,.2f}\n"
        f"总收益率: {metrics['total_return_pct']:.2f}%\n"
        f"年化收益: {metrics['cagr'] * 100:.2f}%\n"
        f"最大回撤: {metrics['max_drawdown_pct']:.2f}%\n"
        f"交易日数: {int(metrics['trading_days'])}\n"
    )

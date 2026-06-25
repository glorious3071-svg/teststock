"""Portfolio state for v5.0 trigger-engine backtest.

承载 v4.0 v3 反应式纪律所需的账户状态：
- 现金、持仓、目标权重
- 当前实际权重计算
- 偏离度检测（A 再平衡触发器用）
- 交易执行（买/卖/再平衡）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class Position:
    """单个 ETF 持仓"""
    ts_code: str
    shares: float = 0.0
    avg_cost: float = 0.0
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price


@dataclass
class TargetAllocation:
    """战略层下发的目标配置"""
    apply_year: int
    equity_weight_pct: float  # 权益总仓位 %
    cash_weight_pct: float    # 现金仓位 %
    etf_targets: dict[str, float] = field(default_factory=dict)
    # ts_code -> 占总资产 %（例：{"510300.SH": 40, "510660.SH": 20}）

    def total_check(self) -> float:
        """权重和（理论应 ≈ 100）"""
        return self.equity_weight_pct + self.cash_weight_pct

    def etf_total(self) -> float:
        return sum(self.etf_targets.values())


@dataclass
class Portfolio:
    """账户状态 — v5.0 战术层操作对象"""
    initial_cash: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    target: TargetAllocation | None = None
    commission_rate: float = 0.0003

    # 触发器锁定期: (rule_id, direction) -> locked_until_ym (YYYYMM)
    locks: dict[tuple[str, str], str] = field(default_factory=dict)

    def __post_init__(self):
        if self.cash == 0.0:
            self.cash = self.initial_cash

    # ===== 资产价值 =====
    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    def total_equity(self) -> float:
        return self.cash + self.total_market_value()

    def cash_pct(self) -> float:
        eq = self.total_equity()
        return self.cash / eq * 100 if eq > 0 else 0

    def equity_pct(self) -> float:
        return 100 - self.cash_pct()

    def position_pct(self, ts_code: str) -> float:
        """单个持仓占总资产 %"""
        eq = self.total_equity()
        if eq <= 0:
            return 0
        p = self.positions.get(ts_code)
        return (p.market_value / eq * 100) if p else 0

    # ===== 价格更新 =====
    def mark_to_market(self, prices: dict[str, float]) -> None:
        """更新所有持仓的最新价格"""
        for code, price in prices.items():
            if code in self.positions and price > 0:
                self.positions[code].last_price = price

    # ===== 交易 =====
    def buy(self, ts_code: str, amount: float, price: float) -> dict[str, Any]:
        """按金额买入（不超过现金）"""
        if amount <= 0 or price <= 0:
            return {"ok": False, "reason": "invalid amount/price"}
        amount = min(amount, self.cash / (1 + self.commission_rate))
        shares = int(amount / price / 100) * 100
        if shares <= 0:
            return {"ok": False, "reason": "shares < 100"}
        cost = shares * price
        commission = cost * self.commission_rate
        total = cost + commission
        if total > self.cash:
            return {"ok": False, "reason": "insufficient cash"}
        self.cash -= total
        if ts_code not in self.positions:
            self.positions[ts_code] = Position(ts_code=ts_code)
        p = self.positions[ts_code]
        old_value = p.shares * p.avg_cost
        new_value = old_value + cost
        p.shares += shares
        p.avg_cost = new_value / p.shares if p.shares > 0 else 0
        p.last_price = price
        return {"ok": True, "shares": shares, "cost": cost, "commission": commission}

    def sell(self, ts_code: str, shares: float, price: float) -> dict[str, Any]:
        """卖出指定份额"""
        if ts_code not in self.positions:
            return {"ok": False, "reason": "no position"}
        p = self.positions[ts_code]
        shares = min(shares, p.shares)
        shares = int(shares / 100) * 100
        if shares <= 0:
            return {"ok": False, "reason": "shares < 100"}
        amount = shares * price
        commission = amount * self.commission_rate
        self.cash += amount - commission
        p.shares -= shares
        p.last_price = price
        return {"ok": True, "shares": shares, "amount": amount, "commission": commission}

    def sell_pct_of_equity(self, ts_code: str, pct: float, price: float) -> dict[str, Any]:
        """卖出占总资产 pct% 的持仓"""
        eq = self.total_equity()
        amount = eq * pct / 100
        shares = int(amount / price / 100) * 100
        return self.sell(ts_code, shares, price)

    # ===== 偏离度检测 (A 触发器) =====
    def deviation(self, ts_code: str) -> float:
        """当前权重 - 目标权重（pp）"""
        if not self.target:
            return 0
        actual = self.position_pct(ts_code)
        target = self.target.etf_targets.get(ts_code, 0)
        return actual - target

    def max_abs_deviation(self) -> tuple[str | None, float]:
        """返回偏离最大的 ETF 及其偏离值"""
        if not self.target:
            return None, 0
        max_code, max_dev = None, 0
        all_codes = set(self.target.etf_targets.keys()) | set(self.positions.keys())
        for code in all_codes:
            dev = self.deviation(code)
            if abs(dev) > abs(max_dev):
                max_dev = dev
                max_code = code
        return max_code, max_dev

    # ===== 触发器锁定期 =====
    def is_locked(self, rule_id: str, direction: str, current_ym: str) -> bool:
        key = (rule_id, direction)
        locked_until = self.locks.get(key)
        if not locked_until:
            return False
        return current_ym <= locked_until

    def set_lock(self, rule_id: str, direction: str, lock_until_ym: str) -> None:
        self.locks[(rule_id, direction)] = lock_until_ym

    # ===== 快照 =====
    def snapshot(self) -> dict[str, Any]:
        return {
            "cash": round(self.cash, 2),
            "cash_pct": round(self.cash_pct(), 2),
            "equity_pct": round(self.equity_pct(), 2),
            "total_equity": round(self.total_equity(), 2),
            "positions": {
                code: {
                    "shares": p.shares,
                    "market_value": round(p.market_value, 2),
                    "pct": round(self.position_pct(code), 2),
                    "avg_cost": round(p.avg_cost, 4),
                    "last_price": round(p.last_price, 4),
                }
                for code, p in self.positions.items() if p.shares > 0
            },
            "locks": {f"{rid}_{d}": until for (rid, d), until in self.locks.items()},
        }

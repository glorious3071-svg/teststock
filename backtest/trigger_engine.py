"""v5.0 触发器引擎 — 实现 v4.0 v3 反应式纪律

设计原则：
1. 确定性规则 — 不调用 LLM，给定相同输入永远输出相同决策
2. 反应式 — 只看当月已发生的事实（PE/价格/已宣布的政策）
3. 锁定期对称化 — 同向触发后 3 月不再触发；反向可立即触发
4. 版本固化 — 规则版本号写入历史，回测可严格复现
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from backtest.portfolio import Portfolio


# =====================================================
# 信号容器
# =====================================================
@dataclass
class MonthlySignals:
    """单月信号快照 — 触发器消费的输入"""
    year_month: str            # YYYYMM
    trade_date: str            # YYYY-MM-DD 当月最后交易日

    # 估值（沪深300）
    cs300_pe_ttm: float | None = None
    cs300_pb: float | None = None

    # 月度动量
    cs300_pct: float | None = None        # 当月涨跌幅 %
    cs300_3m_pct: float | None = None     # 近 3 月累计 %

    # 政策事件
    rrr_cut_in_month: bool = False        # 当月降准
    rate_cut_in_month: bool = False       # 当月降息
    policy_tone_positive: bool = False    # 当月重大正面政策

    # 当月各 ETF 的收盘价（用于交易）
    etf_close: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "year_month": self.year_month,
            "trade_date": self.trade_date,
            "cs300_pe_ttm": self.cs300_pe_ttm,
            "cs300_pb": self.cs300_pb,
            "cs300_pct": self.cs300_pct,
            "cs300_3m_pct": self.cs300_3m_pct,
            "rrr_cut_in_month": self.rrr_cut_in_month,
            "rate_cut_in_month": self.rate_cut_in_month,
            "policy_tone_positive": self.policy_tone_positive,
            "etf_count": len(self.etf_close),
        }


# =====================================================
# 触发器结果
# =====================================================
@dataclass
class TriggerResult:
    rule_id: str
    rule_version: str = "v4.0_v3"
    condition_met: bool = False
    lock_blocked: bool = False
    triggered: bool = False
    action: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


# =====================================================
# 触发器基类
# =====================================================
class TriggerBase(ABC):
    rule_id: str = ""
    direction: str = ""          # 'reduce' / 'add' / 'rebalance'
    lock_months: int = 3
    version: str = "v4.0_v3"

    @abstractmethod
    def check_condition(self, signals: MonthlySignals,
                        portfolio: Portfolio) -> tuple[bool, str]:
        """评估条件是否满足。返回 (是否满足, 说明)"""
        ...

    @abstractmethod
    def execute(self, signals: MonthlySignals,
                portfolio: Portfolio) -> dict[str, Any]:
        """执行动作。返回 action 详情"""
        ...

    def is_locked(self, signals: MonthlySignals, portfolio: Portfolio) -> bool:
        if self.lock_months <= 0:
            return False
        return portfolio.is_locked(self.rule_id, self.direction, signals.year_month)

    def set_lock(self, signals: MonthlySignals, portfolio: Portfolio) -> None:
        if self.lock_months <= 0:
            return
        y = int(signals.year_month[:4])
        m = int(signals.year_month[4:])
        m += self.lock_months
        while m > 12:
            m -= 12
            y += 1
        portfolio.set_lock(self.rule_id, self.direction, f"{y:04d}{m:02d}")

    def evaluate(self, signals: MonthlySignals,
                 portfolio: Portfolio) -> TriggerResult:
        """完整流程：条件 → 锁定检查 → 执行 → 更新锁定"""
        result = TriggerResult(rule_id=self.rule_id, rule_version=self.version)
        met, note = self.check_condition(signals, portfolio)
        result.condition_met = met
        result.notes = note
        if not met:
            return result
        if self.is_locked(signals, portfolio):
            result.lock_blocked = True
            return result
        result.action = self.execute(signals, portfolio)
        result.triggered = True
        self.set_lock(signals, portfolio)
        return result


# =====================================================
# A: 再平衡触发器
# =====================================================
class RebalanceTrigger(TriggerBase):
    rule_id = "A"
    direction = "rebalance"
    lock_months = 0      # 再平衡无锁定期
    DEVIATION_THRESHOLD_PP = 5.0

    def check_condition(self, signals, portfolio):
        if not portfolio.target:
            return False, "无目标配置"
        code, dev = portfolio.max_abs_deviation()
        if abs(dev) >= self.DEVIATION_THRESHOLD_PP:
            return True, f"{code} 偏离 {dev:+.2f}pp ≥ ±{self.DEVIATION_THRESHOLD_PP}"
        return False, f"最大偏离 {dev:+.2f}pp < 阈值"

    def execute(self, signals, portfolio):
        """卖超买不足 → 恢复目标权重"""
        actions = []
        target = portfolio.target
        eq = portfolio.total_equity()

        # 先卖超持的
        for code, target_pct in target.etf_targets.items():
            actual_pct = portfolio.position_pct(code)
            if actual_pct > target_pct + 1.0:  # 留 1pp 余地避免来回交易
                price = signals.etf_close.get(code)
                if not price:
                    continue
                overweight_pp = actual_pct - target_pct
                excess_value = eq * overweight_pp / 100
                shares_to_sell = int(excess_value / price / 100) * 100
                if shares_to_sell > 0:
                    r = portfolio.sell(code, shares_to_sell, price)
                    if r.get("ok"):
                        actions.append({"side": "sell", "ts_code": code, **r})

        # 再买不足的
        for code, target_pct in target.etf_targets.items():
            actual_pct = portfolio.position_pct(code)
            if actual_pct < target_pct - 1.0:
                price = signals.etf_close.get(code)
                if not price:
                    continue
                shortfall_pp = target_pct - actual_pct
                need_value = eq * shortfall_pp / 100
                r = portfolio.buy(code, need_value, price)
                if r.get("ok"):
                    actions.append({"side": "buy", "ts_code": code, **r})

        return {"type": "rebalance", "trades": actions}


# =====================================================
# B: 政策触发器
# =====================================================
class PolicyTrigger(TriggerBase):
    rule_id = "B"
    direction = "add"
    lock_months = 3
    PE_THRESHOLD = 12.0
    CASH_USE_PCT = 30.0  # 动用现金的 30%

    def check_condition(self, signals, portfolio):
        if signals.cs300_pe_ttm is None:
            return False, "无PE数据"
        if signals.cs300_pe_ttm >= self.PE_THRESHOLD:
            return False, f"PE={signals.cs300_pe_ttm:.2f} ≥ {self.PE_THRESHOLD}"
        has_easing = signals.rrr_cut_in_month or signals.rate_cut_in_month
        if not has_easing:
            return False, "当月无降准降息"
        if portfolio.cash <= 0:
            return False, "无现金可加"
        return True, (f"PE={signals.cs300_pe_ttm:.2f}<{self.PE_THRESHOLD} 且当月"
                      f"{'降准' if signals.rrr_cut_in_month else ''}"
                      f"{'/降息' if signals.rate_cut_in_month else ''}")

    def execute(self, signals, portfolio):
        """动用现金 30% 加仓至各 ETF（按目标权重比例）"""
        cash_to_use = portfolio.cash * self.CASH_USE_PCT / 100
        target = portfolio.target
        actions = []
        if not target or not target.etf_targets:
            return {"type": "policy_add", "trades": [], "note": "no target"}
        total_target = sum(target.etf_targets.values())
        if total_target <= 0:
            return {"type": "policy_add", "trades": [], "note": "no etf target"}
        for code, target_pct in target.etf_targets.items():
            price = signals.etf_close.get(code)
            if not price:
                continue
            amount = cash_to_use * (target_pct / total_target)
            r = portfolio.buy(code, amount, price)
            if r.get("ok"):
                actions.append({"side": "buy", "ts_code": code, **r})
        return {"type": "policy_add", "cash_used_pct": self.CASH_USE_PCT, "trades": actions}


# =====================================================
# C: 减仓触发器
# =====================================================
class ReduceTrigger(TriggerBase):
    rule_id = "C"
    direction = "reduce"
    lock_months = 3
    PE_THRESHOLD = 16.0
    MONTHLY_SURGE = 15.0     # 单月涨 15%
    QUARTERLY_SURGE = 25.0   # 3月累计 25%
    REDUCE_PCT_OF_EQUITY = 10.0  # 减仓 10pp 至现金

    def check_condition(self, signals, portfolio):
        if signals.cs300_pe_ttm is None:
            return False, "无PE数据"
        if signals.cs300_pe_ttm <= self.PE_THRESHOLD:
            return False, f"PE={signals.cs300_pe_ttm:.2f} ≤ {self.PE_THRESHOLD}"
        m1 = signals.cs300_pct or 0
        m3 = signals.cs300_3m_pct or 0
        if m1 < self.MONTHLY_SURGE and m3 < self.QUARTERLY_SURGE:
            return False, f"涨幅未达阈值 (1M={m1:.1f}%, 3M={m3:.1f}%)"
        if portfolio.equity_pct() <= 0:
            return False, "已无权益仓位"
        return True, (f"PE={signals.cs300_pe_ttm:.2f}>{self.PE_THRESHOLD} "
                      f"+ 1M涨{m1:.1f}% / 3M涨{m3:.1f}%")

    def execute(self, signals, portfolio):
        """按持仓比例卖出，总额 = 总资产 × 10pp"""
        eq = portfolio.total_equity()
        reduce_value = eq * self.REDUCE_PCT_OF_EQUITY / 100
        total_mv = portfolio.total_market_value()
        if total_mv <= 0:
            return {"type": "reduce", "trades": [], "note": "no position"}
        actions = []
        for code, p in list(portfolio.positions.items()):
            if p.shares <= 0:
                continue
            price = signals.etf_close.get(code) or p.last_price
            if price <= 0:
                continue
            position_share = p.market_value / total_mv
            sell_value = reduce_value * position_share
            shares_to_sell = int(sell_value / price / 100) * 100
            if shares_to_sell > 0:
                r = portfolio.sell(code, shares_to_sell, price)
                if r.get("ok"):
                    actions.append({"side": "sell", "ts_code": code, **r})
        return {"type": "reduce", "reduce_pct": self.REDUCE_PCT_OF_EQUITY, "trades": actions}


# =====================================================
# D: 加仓触发器
# =====================================================
class AddTrigger(TriggerBase):
    rule_id = "D"
    direction = "add"
    lock_months = 3
    PE_THRESHOLD = 11.0
    MONTHLY_DROP = -10.0
    QUARTERLY_DROP = -20.0
    CASH_USE_PCT = 50.0  # 现金的 50%

    def check_condition(self, signals, portfolio):
        if signals.cs300_pe_ttm is None:
            return False, "无PE数据"
        if signals.cs300_pe_ttm >= self.PE_THRESHOLD:
            return False, f"PE={signals.cs300_pe_ttm:.2f} ≥ {self.PE_THRESHOLD}"
        m1 = signals.cs300_pct or 0
        m3 = signals.cs300_3m_pct or 0
        if m1 > self.MONTHLY_DROP and m3 > self.QUARTERLY_DROP:
            return False, f"跌幅未达阈值 (1M={m1:.1f}%, 3M={m3:.1f}%)"
        if portfolio.cash <= 0:
            return False, "无现金"
        return True, (f"PE={signals.cs300_pe_ttm:.2f}<{self.PE_THRESHOLD} "
                      f"+ 1M跌{m1:.1f}% / 3M跌{m3:.1f}%")

    def execute(self, signals, portfolio):
        """动用现金 50% 加仓"""
        cash_to_use = portfolio.cash * self.CASH_USE_PCT / 100
        target = portfolio.target
        if not target or not target.etf_targets:
            return {"type": "add", "trades": [], "note": "no target"}
        total_target = sum(target.etf_targets.values())
        if total_target <= 0:
            return {"type": "add", "trades": [], "note": "no etf target"}
        actions = []
        for code, target_pct in target.etf_targets.items():
            price = signals.etf_close.get(code)
            if not price:
                continue
            amount = cash_to_use * (target_pct / total_target)
            r = portfolio.buy(code, amount, price)
            if r.get("ok"):
                actions.append({"side": "buy", "ts_code": code, **r})
        return {"type": "add", "cash_used_pct": self.CASH_USE_PCT, "trades": actions}


# =====================================================
# 触发器引擎主类
# =====================================================
@dataclass
class TriggerEngine:
    """组合 v4.0 v3 四大触发器，按优先级顺序评估"""

    triggers: list[TriggerBase] = field(default_factory=list)

    def __post_init__(self):
        if not self.triggers:
            # 默认 v4.0 v3 四大触发器
            # 评估顺序：先 C/D 极端反应，再 B 政策，最后 A 再平衡
            self.triggers = [
                ReduceTrigger(),
                AddTrigger(),
                PolicyTrigger(),
                RebalanceTrigger(),
            ]

    def evaluate_month(self, signals: MonthlySignals,
                       portfolio: Portfolio) -> list[TriggerResult]:
        """对一个月份运行所有触发器，按顺序，触发后允许继续评估其他规则"""
        results = []
        for trig in self.triggers:
            r = trig.evaluate(signals, portfolio)
            results.append(r)
        return results

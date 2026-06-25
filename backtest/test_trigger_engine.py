"""触发器引擎单元测试

验证 v4.0 v3 四个触发器的核心逻辑：
- 条件判断正确
- 锁定期对称化
- 极端情景行为符合预期
"""

from __future__ import annotations

import unittest

from backtest.portfolio import Portfolio, TargetAllocation
from backtest.trigger_engine import (
    AddTrigger,
    MonthlySignals,
    PolicyTrigger,
    RebalanceTrigger,
    ReduceTrigger,
    TriggerEngine,
)


def make_portfolio(cash=1_000_000) -> Portfolio:
    pf = Portfolio(initial_cash=cash)
    pf.target = TargetAllocation(
        apply_year=2017,
        equity_weight_pct=70,
        cash_weight_pct=30,
        etf_targets={"510300.SH": 50.0, "510660.SH": 20.0},
    )
    return pf


def make_signals(**kwargs) -> MonthlySignals:
    defaults = dict(
        year_month="201706",
        trade_date="2017-06-30",
        cs300_pe_ttm=14.0,
        cs300_pb=1.5,
        cs300_pct=2.0,
        cs300_3m_pct=5.0,
        etf_close={"510300.SH": 3.50, "510660.SH": 2.80},
    )
    defaults.update(kwargs)
    return MonthlySignals(**defaults)


class TestReduceTrigger(unittest.TestCase):
    def test_pe_below_threshold_not_triggered(self):
        """PE=14 < 16 → 不触发"""
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.0)
        signals = make_signals(cs300_pe_ttm=14.0, cs300_pct=20.0)
        trig = ReduceTrigger()
        r = trig.evaluate(signals, pf)
        self.assertFalse(r.condition_met)

    def test_pe_above_and_surge_triggered(self):
        """PE=18 + 单月涨 20% → 触发减仓"""
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.0)
        signals = make_signals(cs300_pe_ttm=18.0, cs300_pct=20.0)
        trig = ReduceTrigger()
        r = trig.evaluate(signals, pf)
        self.assertTrue(r.condition_met)
        self.assertTrue(r.triggered)
        self.assertEqual(r.action["type"], "reduce")
        self.assertGreater(len(r.action["trades"]), 0)

    def test_lock_period_blocks_second_trigger(self):
        """触发后 3 个月内同向再次满足条件应被锁"""
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.0)
        s1 = make_signals(year_month="201706", cs300_pe_ttm=18.0, cs300_pct=20.0)
        trig = ReduceTrigger()
        r1 = trig.evaluate(s1, pf)
        self.assertTrue(r1.triggered)
        # 下个月
        s2 = make_signals(year_month="201707", cs300_pe_ttm=19.0, cs300_pct=25.0)
        r2 = trig.evaluate(s2, pf)
        self.assertTrue(r2.condition_met)
        self.assertTrue(r2.lock_blocked)
        self.assertFalse(r2.triggered)
        # 锁定期 3 个月后应解锁(201710 > lock_until 201709)
        s3 = make_signals(year_month="201710", cs300_pe_ttm=20.0, cs300_pct=25.0)
        r3 = trig.evaluate(s3, pf)
        self.assertFalse(r3.lock_blocked)


class TestAddTrigger(unittest.TestCase):
    def test_pe_low_and_drop_triggered(self):
        """PE=10 + 单月跌 15% → 触发加仓"""
        pf = make_portfolio()
        signals = make_signals(cs300_pe_ttm=10.0, cs300_pct=-15.0)
        trig = AddTrigger()
        r = trig.evaluate(signals, pf)
        self.assertTrue(r.condition_met)
        self.assertTrue(r.triggered)
        self.assertEqual(r.action["type"], "add")

    def test_no_cash_blocked(self):
        """无现金 → 不能触发加仓"""
        pf = Portfolio(initial_cash=0)
        pf.target = TargetAllocation(
            apply_year=2018, equity_weight_pct=100, cash_weight_pct=0,
            etf_targets={"510300.SH": 100},
        )
        signals = make_signals(cs300_pe_ttm=10.0, cs300_pct=-15.0)
        r = AddTrigger().evaluate(signals, pf)
        self.assertFalse(r.condition_met)


class TestPolicyTrigger(unittest.TestCase):
    def test_pe_low_and_easing_triggered(self):
        """PE=11.5 + 当月降准 → 触发"""
        pf = make_portfolio()
        signals = make_signals(cs300_pe_ttm=11.5, rrr_cut_in_month=True)
        r = PolicyTrigger().evaluate(signals, pf)
        self.assertTrue(r.condition_met)
        self.assertTrue(r.triggered)

    def test_pe_high_not_triggered_even_with_easing(self):
        """PE=13 ≥ 12 → 不触发，即使有降息"""
        pf = make_portfolio()
        signals = make_signals(cs300_pe_ttm=13.0, rate_cut_in_month=True)
        r = PolicyTrigger().evaluate(signals, pf)
        self.assertFalse(r.condition_met)


class TestRebalanceTrigger(unittest.TestCase):
    def test_no_deviation_not_triggered(self):
        """目标 50/20，实际持仓接近目标 → 不触发"""
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.50)  # 50% 仓位
        pf.buy("510660.SH", 200_000, 2.80)  # 20% 仓位
        signals = make_signals()
        r = RebalanceTrigger().evaluate(signals, pf)
        self.assertFalse(r.condition_met)

    def test_large_deviation_triggered(self):
        """510300 实际 70% > 目标 50% (偏离 +20pp) → 触发"""
        pf = make_portfolio()
        pf.buy("510300.SH", 700_000, 3.50)
        signals = make_signals()
        r = RebalanceTrigger().evaluate(signals, pf)
        self.assertTrue(r.condition_met)
        self.assertTrue(r.triggered)


class TestSymmetricLock(unittest.TestCase):
    """关键: C 锁住后 D 仍可立即触发"""
    def test_c_locked_does_not_block_d(self):
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.0)
        s_c = make_signals(year_month="201706", cs300_pe_ttm=18, cs300_pct=20)
        ReduceTrigger().evaluate(s_c, pf)  # C 触发, reduce 方向锁定
        # 同月 D 想触发(虽然现实中不可能同月既过热又暴跌)
        s_d = make_signals(year_month="201706", cs300_pe_ttm=10, cs300_pct=-15)
        r_d = AddTrigger().evaluate(s_d, pf)
        # 即使 C 锁了 reduce, D 是 add 方向, 应不受影响
        self.assertTrue(r_d.condition_met)
        self.assertTrue(r_d.triggered)


class TestEngine(unittest.TestCase):
    def test_engine_default_four_triggers(self):
        """默认引擎包含四大触发器"""
        eng = TriggerEngine()
        rules = {t.rule_id for t in eng.triggers}
        self.assertEqual(rules, {"A", "B", "C", "D"})

    def test_engine_evaluate_returns_all(self):
        """月度评估返回所有规则结果"""
        pf = make_portfolio()
        pf.buy("510300.SH", 500_000, 3.50)
        pf.buy("510660.SH", 200_000, 2.80)
        signals = make_signals()
        results = TriggerEngine().evaluate_month(signals, pf)
        self.assertEqual(len(results), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)

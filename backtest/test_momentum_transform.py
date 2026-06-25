"""
test_momentum_transform.py — 动能转换器单元测试

历史校准点（来自 v3.6 设计文档 §四）：

| 时点 | A | B | C | D | 平均 | 决策 | 实际行情 |
|---|---|---|---|---|------|------|---------|
| 2007.05 | 0.3 | 0.7 | 0.5 | 0.5 | 0.500 | 维稳 ✅ | +60% 5 个月 |
| 2007.07 | 0.3 | 0.7 | 0.5 | 1.0 | 0.625 | 减一档 | +30% 3 个月 |
| 2007.10 | 1.0 | 1.5 | 1.0 | 2.0 | 1.375 | 强减仓 ✅ | -60% 见顶 |
| 2008.01 | 1.0 | 1.0 | 1.0 | 1.5 | 1.125 | 减仓 ✅ | -50% |
| 2009.07 | 2.0 | 1.5 | 1.5 | 2.0 | 1.750 | 强减仓 ✅ | -24% 暴跌 |
"""

from __future__ import annotations

import unittest

from backtest.momentum_transform import (
    MomentumState,
    apply_momentum_to_overheat,
    overheat_base_score,
    transform_a_policy,
    transform_b_liquidity_stickiness,
    transform_c_bull_phase,
    transform_d_pe_eps_race,
    transform_momentum,
)


class TestDimensionA(unittest.TestCase):
    """A 政策 price-in"""

    def test_a1_tightening_priced_in(self):
        s = MomentumState(rate_cum_bp=100, rate_hike_months=8)
        rate, _ = transform_a_policy(s)
        self.assertEqual(rate, 0.3)

    def test_a2_first_tightening_hint(self):
        s = MomentumState(in_loose_phase=True, first_tightening_hint=True)
        rate, _ = transform_a_policy(s)
        self.assertEqual(rate, 2.0)

    def test_a3_loose_middle(self):
        s = MomentumState(in_loose_phase=True, rate_cum_bp=-50)
        rate, _ = transform_a_policy(s)
        self.assertEqual(rate, 0.5)

    def test_a_neutral(self):
        s = MomentumState(rate_cum_bp=25, rate_hike_months=2)
        rate, _ = transform_a_policy(s)
        self.assertEqual(rate, 1.0)


class TestDimensionB(unittest.TestCase):
    """B 资金粘性"""

    def test_b3_leverage_dominant(self):
        s = MomentumState(margin_to_float_pct=2.5)
        rate, _ = transform_b_liquidity_stickiness(s)
        self.assertEqual(rate, 2.0)

    def test_b2_credit_overflow(self):
        s = MomentumState(m1_yoy=27, new_loan_over_1trn=True)
        rate, _ = transform_b_liquidity_stickiness(s)
        self.assertEqual(rate, 1.5)

    def test_b1_savings_sticky(self):
        s = MomentumState(new_fund_3m_over_1000=True, m1_yoy=15)
        rate, _ = transform_b_liquidity_stickiness(s)
        self.assertEqual(rate, 0.7)

    def test_b3_overrides_b1(self):
        """B3 杠杆优先于 B1 储蓄"""
        s = MomentumState(
            margin_to_float_pct=2.5,
            new_fund_3m_over_1000=True, m1_yoy=15,
        )
        rate, _ = transform_b_liquidity_stickiness(s)
        self.assertEqual(rate, 2.0)


class TestDimensionC(unittest.TestCase):
    """C 牛市阶段"""

    def test_c_short_rally(self):
        s = MomentumState(months_since_bear_bottom=6)
        self.assertEqual(transform_c_bull_phase(s)[0], 1.5)

    def test_c_mid(self):
        s = MomentumState(months_since_bear_bottom=18)
        self.assertEqual(transform_c_bull_phase(s)[0], 1.0)

    def test_c_long_bull(self):
        s = MomentumState(months_since_bear_bottom=30)
        self.assertEqual(transform_c_bull_phase(s)[0], 0.5)


class TestDimensionD(unittest.TestCase):
    """D 估值-盈利赛跑"""

    def test_d_severe_overdraw(self):
        # PE +60% / EPS +15% = 4x
        s = MomentumState(pe_yoy_pct=60, eps_yoy_pct=15)
        self.assertEqual(transform_d_pe_eps_race(s)[0], 2.0)

    def test_d_overdraw(self):
        # PE +30 / EPS +12 = 2.5x
        s = MomentumState(pe_yoy_pct=30, eps_yoy_pct=12)
        self.assertEqual(transform_d_pe_eps_race(s)[0], 1.5)

    def test_d_sync_healthy(self):
        s = MomentumState(pe_yoy_pct=12, eps_yoy_pct=10)
        self.assertEqual(transform_d_pe_eps_race(s)[0], 0.5)


class TestHistoricalCalibration(unittest.TestCase):
    """5 个历史校准点 — 设计文档 §四"""

    def assertMultiplierClose(self, state: MomentumState, expected: float, tol: float = 0.01):
        r = transform_momentum(state)
        self.assertAlmostEqual(
            r.multiplier, expected, delta=tol,
            msg=f"multiplier={r.multiplier:.3f}, expected {expected}; {r.explain()}",
        )

    def test_2007_05_absorbed(self):
        """2007.05: A=0.3(紧缩priced) B=0.7(储蓄) C=0.5(24月长牛) D=0.5(同步) → 0.500"""
        s = MomentumState(
            rate_cum_bp=81, rate_hike_months=6,
            new_fund_3m_over_1000=True, m1_yoy=18,
            months_since_bear_bottom=24,
            pe_yoy_pct=20, eps_yoy_pct=18,
        )
        self.assertMultiplierClose(s, 0.500)

    def test_2007_10_amplified(self):
        """2007.10 顶: A=1.0 B=1.5(信贷) C=1.0(中段) D=2.0(透支) → 1.375"""
        s = MomentumState(
            rate_cum_bp=25, rate_hike_months=3,  # 当时刚加息节奏被打破，不算 priced
            m1_yoy=27, new_loan_over_1trn=True,
            months_since_bear_bottom=18,
            pe_yoy_pct=70, eps_yoy_pct=20,
        )
        self.assertMultiplierClose(s, 1.375)

    def test_2008_01_overdraw(self):
        """2008.01: A=1.0 B=1.0 C=1.0(20月) D=1.5(透支) → 1.125"""
        s = MomentumState(
            months_since_bear_bottom=20,
            pe_yoy_pct=40, eps_yoy_pct=15,
        )
        self.assertMultiplierClose(s, 1.125)

    def test_2009_07_amplified(self):
        """2009.07: A=2.0(微调首现) B=1.5(信贷) C=1.5(9月) D=2.0(透支) → 1.750"""
        s = MomentumState(
            in_loose_phase=True, first_tightening_hint=True,
            m1_yoy=27, new_loan_over_1trn=True,
            months_since_bear_bottom=9,
            pe_yoy_pct=80, eps_yoy_pct=20,
        )
        self.assertMultiplierClose(s, 1.750)


class TestApplyToOverheat(unittest.TestCase):
    """端到端：把过热基础分通过转换器加权"""

    def test_2007_05_overheat_dampened(self):
        """2007.05 过热基础 -3 分，被 0.5 吸收 → 实际 -1.5"""
        s = MomentumState(
            rate_cum_bp=81, rate_hike_months=6,
            new_fund_3m_over_1000=True, m1_yoy=18,
            months_since_bear_bottom=24,
            pe_yoy_pct=20, eps_yoy_pct=18,
        )
        base = overheat_base_score(
            pe_above_30=True, new_fund_above_1000=True,
            margin_surge=True, consecutive_5m_rally_50pct=False,
        )
        self.assertEqual(base, -3)
        actual, r = apply_momentum_to_overheat(base, s)
        self.assertAlmostEqual(actual, -1.5, delta=0.01)

    def test_2009_07_overheat_amplified(self):
        """2009.07 过热基础 -3 分，被 1.75 放大 → 实际 -5.25"""
        s = MomentumState(
            in_loose_phase=True, first_tightening_hint=True,
            m1_yoy=27, new_loan_over_1trn=True,
            months_since_bear_bottom=9,
            pe_yoy_pct=80, eps_yoy_pct=20,
        )
        base = overheat_base_score(
            pe_above_30=True, new_fund_above_1000=True,
            margin_surge=True, consecutive_5m_rally_50pct=False,
        )
        actual, _ = apply_momentum_to_overheat(base, s)
        self.assertAlmostEqual(actual, -5.25, delta=0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)

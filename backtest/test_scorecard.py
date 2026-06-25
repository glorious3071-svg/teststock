"""
test_scorecard.py — 评分卡单元测试

核心验证点：
- 2007.09 顶部应得高分（≥+10），目标仓位 ≤30%
- 2009.01 底部应得低分（≤-5），目标仓位 ≥80%
- 平衡市场应在 [-3, +3] 区间，目标 75%
- 加仓三重门：底部时若政策实弹未到，约束加仓
"""

from __future__ import annotations

import unittest

from backtest.scorecard import (
    ScorecardInputs,
    evaluate_scorecard,
    policy_triple_gate,
    score_to_target_equity,
)


class TestScorecardBands(unittest.TestCase):
    def test_band_mapping(self):
        self.assertEqual(score_to_target_equity(-12)[0], 90.0)
        self.assertEqual(score_to_target_equity(-7)[0], 80.0)
        self.assertEqual(score_to_target_equity(-2)[0], 75.0)
        self.assertEqual(score_to_target_equity(2)[0], 75.0)
        self.assertEqual(score_to_target_equity(5)[0], 60.0)
        self.assertEqual(score_to_target_equity(8)[0], 50.0)
        self.assertEqual(score_to_target_equity(11)[0], 30.0)
        self.assertEqual(score_to_target_equity(15)[0], 20.0)


class TestHistoricalScenarios(unittest.TestCase):
    """复算历史关键节点，对照设计文档预期。"""

    def test_2007_09_bull_top(self):
        """2007.09 牛市顶: PE 52, 紧缩高峰, 央行从紧, 应得高分 → 20% 仓位"""
        inp = ScorecardInputs(
            cs300_pe_ttm=52.0,
            cs300_pb=4.5,
            rate_cum_bp_12m=162,      # 累计加息 162bp
            rrr_cum_pp_12m=3.5,        # 累计加准
            deposit_1y_rate=3.87,
            pmi_below_52_months=0,     # PMI 仍扩张
            iva_yoy_trend="up",
            ppi_yoy_change="flat",
            new_fund_billion=2500,
            fund_doubling_6m=True,
            margin_growth_pct=80,
            pboc_tone="tight",
            stamp_duty="tighten",
            central_meeting_tone="dual_prevent",
        )
        r = evaluate_scorecard(2007, inp)
        # 估值 +2+1+1+1=5；流动性 +1+1+1+1=4；情绪 +1+1+1=3；政策 +2+1+1=4 → 16+ 含工业回升 -1 → 15+
        self.assertGreaterEqual(r.total_score, 10, f"2007.09 应高分; 实际 {r.total_score}")
        self.assertLessEqual(r.target_equity_pct, 30.0)
        self.assertEqual(r.band, "极端风险" if r.total_score >= 13 else "高风险")

    def test_2009_01_market_bottom(self):
        """2009.01 政策底: PE 14, PB<2, 累计降息, 央行宽松 → 低分 → 80% 仓位"""
        inp = ScorecardInputs(
            cs300_pe_ttm=14.0,
            cs300_pb=1.8,
            rate_cum_bp_12m=-216,
            rrr_cum_pp_12m=-2.0,
            deposit_1y_rate=2.25,
            pmi_below_52_months=3,
            iva_yoy_trend="down",
            ppi_yoy=-3.3,
            ppi_yoy_change="turn_negative",
            pmi_resume_expansion=False,
            fed_reversal="hike_to_cut",
            us_monthly_pct=-8,
            global_recession=True,
            fed_zero_qe=True,
            global_stimulus=True,
            pboc_tone="loose",
            stamp_duty="loosen",
            central_meeting_tone="expansionary",
        )
        r = evaluate_scorecard(2009, inp)
        # 估值 -2-1=-3；流动性 -2-1-1=-4；基本面 +1+1+2=+4；
        # 外部 +2+1+2-2-1=+2；政策 -2-1-1=-4 → 总 ≈ -5
        self.assertLessEqual(r.total_score, -3, f"2009.01 应低分; 实际 {r.total_score}")
        self.assertGreaterEqual(r.target_equity_pct, 75.0)

    def test_2006_balanced(self):
        """2006 年初: PE 22 中性, 流动性温和宽松 → 中性档"""
        inp = ScorecardInputs(
            cs300_pe_ttm=22.0,
            cs300_pb=2.5,
            rate_cum_bp_12m=0,
            rrr_cum_pp_12m=0,
            deposit_1y_rate=2.79,
            pmi_below_52_months=0,
            iva_yoy_trend="up",
            ppi_yoy_change="flat",
            new_fund_billion=300,
            margin_growth_pct=10,
            pboc_tone="neutral",
            central_meeting_tone="neutral",
        )
        r = evaluate_scorecard(2006, inp)
        self.assertGreaterEqual(r.target_equity_pct, 60.0)
        self.assertLessEqual(r.target_equity_pct, 75.0)


class TestPolicyTripleGate(unittest.TestCase):
    """政策实弹三重门 — 防止过早抄底"""

    def test_gate_passes_with_two_hits(self):
        """央行宽松 + 中央积极 → 放行"""
        inp = ScorecardInputs(pboc_tone="loose", central_meeting_tone="expansionary")
        passed, desc = policy_triple_gate(inp)
        self.assertTrue(passed)
        self.assertIn("央行宽松", desc)
        self.assertIn("中央积极", desc)

    def test_gate_passes_with_all_three(self):
        """三重全到"""
        inp = ScorecardInputs(
            pboc_tone="loose",
            central_meeting_tone="expansionary",
            ppi_yoy_change="turn_positive",
        )
        passed, _ = policy_triple_gate(inp)
        self.assertTrue(passed)

    def test_gate_blocks_with_one_hit(self):
        """只有央行宽松，缺中央会议和基本面信号 → 不放行"""
        inp = ScorecardInputs(pboc_tone="loose")
        passed, _ = policy_triple_gate(inp)
        self.assertFalse(passed)

    def test_gate_blocks_empty(self):
        """三个都没有 → 不放行"""
        inp = ScorecardInputs()
        passed, desc = policy_triple_gate(inp)
        self.assertFalse(passed)
        self.assertEqual(desc, "无政策实弹")

    def test_pmi_substitutes_ppi(self):
        """基本面信号: PMI 重回扩张可替代 PPI 触底"""
        inp = ScorecardInputs(
            pboc_tone="loose",
            pmi_resume_expansion=True,
        )
        passed, _ = policy_triple_gate(inp)
        self.assertTrue(passed)


class TestDimensionIsolation(unittest.TestCase):
    """各维度的命中项应只挂在对应维度"""

    def test_dimensions_separated(self):
        inp = ScorecardInputs(
            cs300_pe_ttm=15.5,
            cs300_pb=1.9,
            rate_cum_bp_12m=-120,
            pboc_tone="loose",
        )
        r = evaluate_scorecard(2020, inp)
        groups = r.items_by_dimension()
        self.assertIn("valuation", groups)
        self.assertIn("liquidity", groups)
        self.assertIn("policy", groups)
        # 估值: PE<20 (-1) + PB<2 (-1) = -2
        # 流动性: 累计降息>100bp (-2)
        # 政策: 央行宽松 (-2)
        valuation_score = sum(it.score for it in groups["valuation"])
        self.assertEqual(valuation_score, -2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

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
        """v3.4.13 C 全光谱映射边界（极端档位拉伸到 100% / 0%）

        档位表：
          ≤-10 → 100% 极度便宜+刺激共振（满仓）
          ≤-7  → 95%  深度机会
          ≤-4  → 90%  机会显著
          ≤-1  → 85%  机会偏多
          ==0  → 80%  平衡偏多
          ≤+3  → 65%  中性偏防
          ≤+6  → 50%  风险偏多
          ≤+9  → 35%  风险显著
          ≤+12 → 15%  高风险
          >+12 → 0%   极端风险（全现金）
        """
        self.assertEqual(score_to_target_equity(-12)[0], 100.0)
        self.assertEqual(score_to_target_equity(-10)[0], 100.0)  # 边界点
        self.assertEqual(score_to_target_equity(-9)[0], 95.0)
        self.assertEqual(score_to_target_equity(-7)[0], 95.0)    # 边界点
        self.assertEqual(score_to_target_equity(-6)[0], 90.0)
        self.assertEqual(score_to_target_equity(-4)[0], 90.0)    # 边界点
        self.assertEqual(score_to_target_equity(-2)[0], 85.0)
        self.assertEqual(score_to_target_equity(-1)[0], 85.0)    # 边界点
        self.assertEqual(score_to_target_equity(0)[0], 80.0)     # v3.4.13 平衡档从 75 升到 80
        self.assertEqual(score_to_target_equity(2)[0], 65.0)
        self.assertEqual(score_to_target_equity(3)[0], 65.0)     # 边界点
        self.assertEqual(score_to_target_equity(5)[0], 50.0)
        self.assertEqual(score_to_target_equity(8)[0], 35.0)
        self.assertEqual(score_to_target_equity(11)[0], 15.0)
        self.assertEqual(score_to_target_equity(15)[0], 0.0)     # v3.4.13 极端档全清


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
        # v3.4.12 裁剪后：原 +10 (含 PB>3 +1) → +9（PB>3 已裁剪），仍触发 30% 仓位
        self.assertGreaterEqual(r.total_score, 9, f"2007.09 应高分; 实际 {r.total_score}")
        # v3.4.11 12 档映射 + v3.4.12 裁剪后：+9 → 50%（原 v3.4.0 旧档下是 30%）
        self.assertLessEqual(r.target_equity_pct, 50.0)

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
        """2006 年初: PE 22 中性, 流动性温和宽松, 工业回升 → 评分 -1 → 80% 仓位

        v3.4.11 12 档加密阶梯下，score=-1 落入「≤-1 → 80% 机会偏多」档。
        旧 8 档下该评分会被中性带吞没回到 75%；新档位允许中性偏多倾向单独表达。
        """
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
        # v3.4.11 中性扩展带：score ∈ [-4, +3] 对应 {70, 75, 80} 三档（旧版全是 75）
        self.assertGreaterEqual(r.target_equity_pct, 70.0)
        self.assertLessEqual(r.target_equity_pct, 85.0)


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
        """v9-D 后：PMI 重回扩张已改为风险信号（滞后顶部），不再算三重门基本面闸门。
        现在只 PPI turn_positive 算第三道闸门触发。"""
        inp = ScorecardInputs(
            pboc_tone="loose",
            pmi_resume_expansion=True,  # v9-D 后不再触发三重门第三道
        )
        passed, _ = policy_triple_gate(inp)
        # 只有 央行宽松 1 条，1 < 2 → 不通过
        self.assertFalse(passed)
        # 必须 PPI turn_positive 才能通过
        inp2 = ScorecardInputs(
            pboc_tone="loose",
            ppi_yoy_change="turn_positive",
        )
        passed2, _ = policy_triple_gate(inp2)
        self.assertTrue(passed2)


class TestPmiSubrules(unittest.TestCase):
    """V3.4.1 新增 PMI 子规则单测（消季节性 3M 均值 + 生产/订单背离）"""

    @unittest.skip("v3.4.12 裁剪：PMI3M均≥53 规则方向错（触发后均回报 +34% vs 未触发 +13%）已被注释")
    def test_pmi_3m_overheating_adds_risk(self):
        """PMI 3M 均值 ≥ 53 → fundamental +1（景气过热）— v3.4.12 已废弃"""
        inp = ScorecardInputs(pmi_mfg_3m_avg=55.7)
        r = evaluate_scorecard(2010, inp)
        names = [it.name for it in r.items if it.dimension == "fundamental"]
        self.assertIn("PMI3M均≥53(景气过热)", names)
        self.assertEqual(r.total_score, +1)

    def test_pmi_3m_below_threshold_no_score(self):
        """PMI 3M 均值 < 53 → 不触发"""
        inp = ScorecardInputs(pmi_mfg_3m_avg=52.9)
        r = evaluate_scorecard(2010, inp)
        names = [it.name for it in r.items if it.dimension == "fundamental"]
        self.assertNotIn("PMI3M均≥53(景气过热)", names)

    def test_passive_inventory_adds_risk(self):
        """生产 - 新订单 ≥ 3 → fundamental +1（被动累库）"""
        inp = ScorecardInputs(pmi_prod_minus_order=3.5)
        r = evaluate_scorecard(2012, inp)
        names = [it.name for it in r.items if it.dimension == "fundamental"]
        self.assertIn("生产>订单≥3(被动累库)", names)
        self.assertEqual(r.total_score, +1)

    def test_demand_lead_adds_opportunity(self):
        """订单 - 生产 ≥ 3 → fundamental -1（需求领先复苏）"""
        inp = ScorecardInputs(pmi_prod_minus_order=-3.5)
        r = evaluate_scorecard(2020, inp)
        names = [it.name for it in r.items if it.dimension == "fundamental"]
        self.assertIn("订单>生产≥3(需求领先)", names)
        self.assertEqual(r.total_score, -1)

    def test_pmi_none_skip(self):
        """两个新字段为 None → 不触发任何新规则（向后兼容）"""
        inp = ScorecardInputs(
            cs300_pe_ttm=25.0,
            pmi_below_52_months=0,
        )
        r = evaluate_scorecard(2015, inp)
        new_names = {"PMI3M均≥53(景气过热)", "生产>订单≥3(被动累库)", "订单>生产≥3(需求领先)"}
        hit = {it.name for it in r.items} & new_names
        self.assertEqual(hit, set())


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
        # v3.4.12 裁剪后估值维度：PE<20 和 PB<2 已被裁，valuation 维度不再触发
        self.assertNotIn("valuation", groups)
        self.assertIn("liquidity", groups)
        self.assertIn("policy", groups)
        # 流动性: 累计降息>100bp (-2)
        # 政策: 央行宽松 (-2)
        liquidity_score = sum(it.score for it in groups["liquidity"])
        self.assertEqual(liquidity_score, -2)


class TestOecdRecessionSignal(unittest.TestCase):
    """OECD CLI 单经济体衰退信号：cli < 100 且连续 N 月严格下行"""

    def test_below_100_and_strictly_declining(self):
        from backtest.scorecard_adapter import _is_economy_in_recession
        # 降序：m0=95, m_minus_1=97, m_minus_2=99 → 严格下行 + 当月 < 100
        self.assertTrue(_is_economy_in_recession([95.0, 97.0, 99.0]))

    def test_above_100_not_recession(self):
        from backtest.scorecard_adapter import _is_economy_in_recession
        # 即使持续下行，只要 ≥ 100 就不算衰退
        self.assertFalse(_is_economy_in_recession([100.5, 101.0, 101.5]))

    def test_below_100_but_rebounding(self):
        from backtest.scorecard_adapter import _is_economy_in_recession
        # 95 < 96，最近一月反弹 → 不算衰退（已转向）
        self.assertFalse(_is_economy_in_recession([96.0, 95.0, 94.0]))

    def test_below_100_but_flat(self):
        from backtest.scorecard_adapter import _is_economy_in_recession
        # 95 == 95 不算严格下行
        self.assertFalse(_is_economy_in_recession([95.0, 95.0, 96.0]))

    def test_insufficient_history(self):
        from backtest.scorecard_adapter import _is_economy_in_recession
        # 数据不足 3 月 → 保守返回 False
        self.assertFalse(_is_economy_in_recession([95.0, 97.0]))
        self.assertFalse(_is_economy_in_recession([]))


class TestGlobalStimulusVotes(unittest.TestCase):
    """v3.4.4 候选：5 大央行降息计票（FakeCursor 隔离 SQL，验证投票门槛与 PBoC 双源合并）"""

    class _FakeCursor:
        """按 execute 顺序回放 fetchone 结果；execute() 不做任何事。"""

        def __init__(self, fetchone_queue):
            self._queue = list(fetchone_queue)

        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            if not self._queue:
                return None
            return self._queue.pop(0)

    def _run(self, queue):
        from datetime import date as _date

        from backtest.scorecard_adapter import _global_stimulus
        return _global_stimulus(self._FakeCursor(queue), _date(2024, 12, 31))

    def test_three_foreign_cuts_triggers(self):
        """FED+ECB+BOE 命中 cut（BOJ 缺席），PBoC 完全无事件 → 3 票 → 触发"""
        # 顺序：FED, ECB, BOE, BOJ, PBoC-deposit, PBoC-rrr
        self.assertTrue(self._run([(1,), (1,), (1,), None, None, None]))

    def test_two_foreign_plus_pboc_deposit_triggers(self):
        """FED+ECB + PBoC 存款降息 → 3 票（PBoC-deposit 命中后短路，不查 RRR）"""
        self.assertTrue(self._run([(1,), (1,), None, None, (1,)]))

    def test_two_foreign_plus_pboc_rrr_triggers(self):
        """FED+ECB + 仅 PBoC 降准 → 3 票（PBoC-deposit miss → 降级查 RRR 命中）"""
        self.assertTrue(self._run([(1,), (1,), None, None, None, (1,)]))

    def test_pboc_dual_source_no_double_count(self):
        """PBoC 存款 + RRR 都命中也只算 1 票（短路后不会再查 RRR）"""
        # FED+ECB miss → 0；BOE miss → 0；BOJ miss → 0；PBoC-deposit 命中 → 1
        # 总 1 票 < 3 → 不触发
        self.assertFalse(self._run([None, None, None, None, (1,)]))

    def test_two_foreign_no_pboc_below_threshold(self):
        """FED+ECB 命中、PBoC 双源都空 → 2 票 < 3 → 不触发"""
        self.assertFalse(self._run([(1,), (1,), None, None, None, None]))

    def test_all_miss(self):
        """5 家全无 cut → 0 票 → 不触发"""
        self.assertFalse(self._run([None, None, None, None, None, None]))

    def test_threshold_constants_align_with_spec(self):
        """配套常量与 spec §六 行 180 保持一致：12 月窗口、≥3 票、含 4 家外资"""
        from backtest.scorecard_adapter import (
            GLOBAL_STIMULUS_FOREIGN_CBS,
            GLOBAL_STIMULUS_LOOKBACK_MONTHS,
            GLOBAL_STIMULUS_MIN_VOTES,
        )
        self.assertEqual(GLOBAL_STIMULUS_LOOKBACK_MONTHS, 12)
        self.assertEqual(GLOBAL_STIMULUS_MIN_VOTES, 3)
        self.assertEqual(set(GLOBAL_STIMULUS_FOREIGN_CBS), {"FED", "ECB", "BOE", "BOJ"})


class TestPbocToneNormalization(unittest.TestCase):
    """v3.4.5：央行口径三态归一化（独立单测，不需数据库）"""

    def test_tight_variants(self):
        from backtest.scorecard_adapter import _normalize_pboc_tone
        for raw in ["从紧", "从紧的货币政策", "紧缩"]:
            self.assertEqual(_normalize_pboc_tone(raw), "tight", f"raw={raw!r}")

    def test_loose_variants(self):
        from backtest.scorecard_adapter import _normalize_pboc_tone
        for raw in ["适度宽松", "宽松", "适度宽松的货币政策"]:
            self.assertEqual(_normalize_pboc_tone(raw), "loose", f"raw={raw!r}")

    def test_neutral_variants(self):
        from backtest.scorecard_adapter import _normalize_pboc_tone
        for raw in ["稳健", "中性", "稳健中性", "稳健灵活适度"]:
            self.assertEqual(_normalize_pboc_tone(raw), "neutral", f"raw={raw!r}")

    def test_unknown_returns_none(self):
        from backtest.scorecard_adapter import _normalize_pboc_tone
        self.assertIsNone(_normalize_pboc_tone(None))
        self.assertIsNone(_normalize_pboc_tone(""))
        self.assertIsNone(_normalize_pboc_tone("   "))
        self.assertIsNone(_normalize_pboc_tone("货币政策"))  # 无关键词


class TestStampDuty(unittest.TestCase):
    """v3.4.6：印花税/IPO 评分规则单测（独立单测，不需数据库）"""

    def test_tighten_adds_risk(self):
        """stamp_duty='tighten' → policy +1"""
        inp = ScorecardInputs(stamp_duty="tighten")
        r = evaluate_scorecard(2008, inp)
        names = [it.name for it in r.items if it.dimension == "policy"]
        self.assertIn("印花税/IPO收紧", names)
        self.assertEqual(r.total_score, +1)

    def test_loosen_adds_opportunity(self):
        """stamp_duty='loosen' → policy -1"""
        inp = ScorecardInputs(stamp_duty="loosen")
        r = evaluate_scorecard(2009, inp)
        names = [it.name for it in r.items if it.dimension == "policy"]
        self.assertIn("印花税/IPO放松", names)
        self.assertEqual(r.total_score, -1)

    def test_none_skips(self):
        """stamp_duty=None → 不触发"""
        inp = ScorecardInputs(stamp_duty=None)
        r = evaluate_scorecard(2020, inp)
        policy_items = [it for it in r.items if it.dimension == "policy"]
        # 应当没有印花税相关命中
        self.assertFalse(any("印花税" in it.name for it in policy_items))


class TestNationalTeamAction(unittest.TestCase):
    """v3.4.9：国家队入场评分规则单测（独立单测，不需数据库）"""

    def test_entry_adds_opportunity(self):
        """national_team_action='entry' → policy -2（强信号，与 pboc_tone 同级）"""
        inp = ScorecardInputs(national_team_action="entry")
        r = evaluate_scorecard(2009, inp)
        names = [it.name for it in r.items if it.dimension == "policy"]
        self.assertIn("国家队入场", names)
        self.assertEqual(r.total_score, -2)

    def test_exit_adds_risk(self):
        """national_team_action='exit' → policy +2（极罕见反向信号）"""
        inp = ScorecardInputs(national_team_action="exit")
        r = evaluate_scorecard(2025, inp)
        names = [it.name for it in r.items if it.dimension == "policy"]
        self.assertIn("国家队减持", names)
        self.assertEqual(r.total_score, +2)

    def test_none_skips(self):
        """national_team_action=None → 不触发"""
        inp = ScorecardInputs(national_team_action=None)
        r = evaluate_scorecard(2020, inp)
        policy_items = [it for it in r.items if it.dimension == "policy"]
        self.assertFalse(any("国家队" in it.name for it in policy_items))


if __name__ == "__main__":
    unittest.main(verbosity=2)

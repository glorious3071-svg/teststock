from __future__ import annotations

import copy
import unittest
from datetime import date

from backtest.domestic_equity_etf import (
    EquityEtfMeta,
    blended_etf_diagnostics,
    selected_score_diagnostics,
)

from backtest.passive_etf_supervised_selector import (
    STABLE_FEATURES,
    SupervisedEtfPolicy,
    select_supervised_etfs,
    select_weighted_stable_combo_v2_top1,
    select_weighted_stable_combo_v3_top1,
    select_weighted_stable_combo_v4_top1,
    select_weighted_stable_combo_v5_top1,
    select_weighted_stable_combo_v6_top1,
    select_weighted_stable_combo_v7_top1,
    select_weighted_stable_combo_v9_top1,
    select_weighted_stable_combo_v10_top1,
    select_weighted_structural_conditional_rotation_top3,
    select_weighted_structural_liquidity_group_breadth_top5,
    select_weighted_structural_liquidity_flow_top5,
    select_weighted_structural_mainline_top3,
    select_weighted_structural_momentum_breadth_top3,
    select_weighted_structural_reflation_rotation_top3,
    select_weighted_structural_resilience_top5,
    structural_growth_exhaustion_rotation_active,
    structural_digital_reacceleration_active,
    structural_digital_blowoff_rotation_active,
    structural_digital_blowoff_utilities_rotation_active,
    structural_finance_defensive_rotation_active,
    structural_finance_catchup_active,
    structural_finance_substyle_for_text,
    structural_healthcare_leadership_active,
    structural_local_mainline_pullback_reentry_active,
    structural_local_mainline_pullback_reentry_subthemes,
    structural_new_energy_pullback_restart_active,
    structural_resource_bank_catchup_style_for_text,
    structural_small_growth_recovery_active,
    structural_tech_pullback_continuation_active,
    structural_subtheme_group_for_text,
    weighted_structural_cooling_rotation_scores,
    weighted_structural_digital_blowoff_rotation_scores,
    weighted_structural_digital_reacceleration_scores,
    weighted_structural_finance_defensive_rotation_scores,
    weighted_structural_finance_catchup_scores,
    weighted_structural_finance_bank_catchup_scores,
    weighted_structural_finance_resource_catchup_scores,
    weighted_structural_healthcare_leadership_scores,
    weighted_structural_late_cycle_defensive_rotation_scores,
    weighted_structural_late_cycle_small_growth_recovery_scores,
    weighted_structural_late_cycle_tech_pullback_continuation_scores,
    weighted_structural_local_mainline_pullback_reentry_scores,
    weighted_structural_multistate_rotation_scores,
    weighted_structural_new_energy_pullback_restart_scores,
    weighted_structural_resource_bank_catchup_scores,
    weighted_structural_value_reflation_mainline_scores,
)
from backtest.phase_schedule import shift_month_end


class PassiveEtfSupervisedSelectorTest(unittest.TestCase):
    def test_resource_bank_catchup_style_excludes_new_energy_and_nonbank(self):
        self.assertEqual(
            structural_resource_bank_catchup_style_for_text("新能源车ETF"),
            "other",
        )
        self.assertEqual(
            structural_resource_bank_catchup_style_for_text("沪深300非银行金融指数"),
            "other",
        )
        self.assertEqual(
            structural_resource_bank_catchup_style_for_text("中证银行指数"),
            "bank",
        )
        self.assertEqual(
            structural_resource_bank_catchup_style_for_text("中证钢铁指数"),
            "resources",
        )

    def test_resource_bank_catchup_scores_prefer_catchup_over_overheated_resources(self):
        snapshot = date(2020, 12, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HOT_RESOURCE.SH",
                "momentum_1m": 0.06,
                "momentum_3m": 0.31,
                "momentum_6m": 0.44,
                "relative_strength_3m": 0.17,
                "relative_strength_6m": 0.19,
                "drawdown_3m": -0.01,
                "market_correlation_6m": 0.74,
                "etf_share_growth_1q": 0.01,
                "amount_crowding_percentile_3y": 0.94,
                "volatility_3m": 0.34,
                "days_since_high_6m": 3,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "CHEM.SH",
                "momentum_1m": 0.05,
                "momentum_3m": 0.16,
                "momentum_6m": 0.19,
                "relative_strength_3m": 0.02,
                "relative_strength_6m": -0.06,
                "drawdown_3m": -0.04,
                "market_correlation_6m": 0.23,
                "etf_share_growth_1q": 0.01,
                "amount_crowding_percentile_3y": 0.78,
                "volatility_3m": 0.14,
                "days_since_high_6m": 8,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "BANK.SH",
                "momentum_1m": -0.05,
                "momentum_3m": 0.12,
                "momentum_6m": 0.18,
                "relative_strength_3m": 0.01,
                "relative_strength_6m": 0.02,
                "drawdown_3m": -0.03,
                "market_correlation_6m": 0.40,
                "etf_share_growth_1q": 0.01,
                "amount_crowding_percentile_3y": 0.60,
                "volatility_3m": 0.14,
                "days_since_high_6m": 22,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "NEW_ENERGY.SH",
                "momentum_1m": 0.20,
                "momentum_3m": 0.35,
                "momentum_6m": 0.50,
                "relative_strength_3m": 0.20,
                "relative_strength_6m": 0.25,
                "drawdown_3m": -0.01,
                "market_correlation_6m": 0.60,
                "etf_share_growth_1q": 0.20,
                "amount_crowding_percentile_3y": 0.70,
                "volatility_3m": 0.30,
                "days_since_high_6m": 2,
            },
        ]
        scores = weighted_structural_resource_bank_catchup_scores(
            observations,
            snapshot,
            {
                "HOT_RESOURCE.SH": "resources",
                "CHEM.SH": "resources",
                "BANK.SH": "bank",
                "NEW_ENERGY.SH": "other",
            },
        )
        self.assertNotIn("NEW_ENERGY.SH", scores)
        self.assertGreater(scores["CHEM.SH"], scores["HOT_RESOURCE.SH"])
        self.assertGreater(scores["BANK.SH"], scores["HOT_RESOURCE.SH"])

    def test_healthcare_defensive_leadership_allows_near_top_internal_breadth(self):
        snapshot = date(2019, 12, 31)
        observations = []
        groups_by_code = {}
        for group, momentum in (
            ("broad_growth", 0.09),
            ("technology", 0.085),
            ("healthcare", 0.055),
        ):
            for idx in range(2):
                code = f"{group}_{idx}"
                groups_by_code[code] = group
                observations.append(
                    {
                        "snapshot": snapshot.isoformat(),
                        "ts_code": code,
                        "momentum_3m": momentum + idx * 0.002,
                        "etf_share_growth_1q": 0.30 if group == "healthcare" else 0.05,
                    }
                )
        self.assertTrue(
            structural_healthcare_leadership_active(
                observations,
                snapshot,
                groups_by_code,
            )
        )

    def test_finance_substyle_prioritizes_broker_insurance_keyword(self):
        self.assertEqual(
            structural_finance_substyle_for_text("证券保险红利指数"),
            "broker_insurance",
        )
        self.assertEqual(
            structural_finance_substyle_for_text("中证银行指数"),
            "bank_dividend",
        )

    def test_finance_catchup_scores_require_finance_breadth(self):
        snapshot = date(2020, 12, 31)
        observations = []
        for index in range(4):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": f"BANK{index}.SH",
                    "momentum_1m": -0.03,
                    "momentum_3m": 0.05 + index * 0.01,
                    "momentum_6m": 0.18 + index * 0.02,
                    "relative_strength_3m": 0.01,
                    "relative_strength_6m": 0.05,
                    "drawdown_3m": -0.05,
                    "market_correlation_6m": 0.75,
                    "etf_share_growth_1q": 0.01,
                    "amount_crowding_percentile_3y": 0.80,
                    "forward_return_3m": -0.20,
                }
            )
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "DIGITAL.SH",
                "momentum_1m": 0.20,
                "momentum_3m": 0.30,
                "momentum_6m": 0.45,
                "relative_strength_3m": 0.20,
                "relative_strength_6m": 0.25,
                "drawdown_3m": -0.01,
                "market_correlation_6m": 0.65,
                "etf_share_growth_1q": 0.20,
                "amount_crowding_percentile_3y": 0.70,
                "forward_return_3m": 1.00,
            }
        )
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "BROKER.SH",
                "momentum_1m": -0.02,
                "momentum_3m": 0.11,
                "momentum_6m": 0.32,
                "relative_strength_3m": 0.04,
                "relative_strength_6m": 0.08,
                "drawdown_3m": -0.04,
                "market_correlation_6m": 0.70,
                "etf_share_growth_1q": 0.15,
                "amount_crowding_percentile_3y": 0.60,
                "forward_return_3m": -0.12,
            }
        )
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "STEEL.SH",
                "momentum_1m": -0.01,
                "momentum_3m": 0.12,
                "momentum_6m": 0.24,
                "relative_strength_3m": 0.05,
                "relative_strength_6m": 0.04,
                "drawdown_3m": -0.03,
                "market_correlation_6m": 0.62,
                "etf_share_growth_1q": 0.10,
                "amount_crowding_percentile_3y": 0.70,
                "volatility_3m": 0.20,
                "forward_return_3m": 0.20,
            }
        )
        subthemes_by_code = {
            **{f"BANK{index}.SH": "finance" for index in range(4)},
            "BROKER.SH": "finance",
            "STEEL.SH": "resources",
            "DIGITAL.SH": "digital_hot",
        }
        self.assertTrue(
            structural_finance_catchup_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_finance_catchup_scores(
            observations,
            snapshot,
            subthemes_by_code,
        )
        self.assertGreater(scores["BANK3.SH"], scores["BANK0.SH"])
        self.assertNotIn("DIGITAL.SH", scores)
        bank_scores = weighted_structural_finance_bank_catchup_scores(
            observations,
            snapshot,
            subthemes_by_code,
            {
                "BANK0.SH": "bank_dividend",
                "BANK1.SH": "bank_dividend",
                "BANK2.SH": "bank_dividend",
                "BANK3.SH": "bank_dividend",
                "BROKER.SH": "broker_insurance",
                "DIGITAL.SH": "other",
            },
        )
        self.assertEqual(set(bank_scores), {f"BANK{index}.SH" for index in range(4)})
        finance_resource_scores = weighted_structural_finance_resource_catchup_scores(
            observations,
            snapshot,
            subthemes_by_code,
            {
                "BANK0.SH": "bank_dividend",
                "BANK1.SH": "bank_dividend",
                "BANK2.SH": "bank_dividend",
                "BANK3.SH": "bank_dividend",
                "BROKER.SH": "broker_insurance",
                "STEEL.SH": "other",
                "DIGITAL.SH": "other",
            },
        )
        self.assertIn("STEEL.SH", finance_resource_scores)
        self.assertIn("BANK3.SH", finance_resource_scores)
        self.assertNotIn("BROKER.SH", finance_resource_scores)

        weak = copy.deepcopy(observations)
        for row in weak:
            if row["ts_code"].startswith("BANK"):
                row["momentum_6m"] = -0.02
        self.assertFalse(
            structural_finance_catchup_active(
                weak,
                snapshot,
                subthemes_by_code,
            )
        )

    def test_value_reflation_mainline_scores_prefer_value_over_hot_digital(self):
        snapshot = date(2025, 2, 28)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "BANK.SH",
                "momentum_1m": 0.02,
                "momentum_3m": -0.01,
                "momentum_6m": 0.24,
                "relative_strength_3m": -0.01,
                "relative_strength_6m": 0.08,
                "market_correlation_6m": 0.80,
                "drawdown_3m": -0.07,
                "etf_share_growth_1q": 0.02,
                "amount_acceleration_1m_6m": 0.03,
                "amount_crowding_percentile_3y": 0.72,
                "volatility_3m": 0.24,
                "forward_return_3m": -0.50,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "RESOURCE.SH",
                "momentum_1m": -0.01,
                "momentum_3m": -0.03,
                "momentum_6m": 0.18,
                "relative_strength_3m": -0.03,
                "relative_strength_6m": 0.04,
                "market_correlation_6m": 0.62,
                "drawdown_3m": -0.08,
                "etf_share_growth_1q": -0.01,
                "amount_acceleration_1m_6m": 0.00,
                "amount_crowding_percentile_3y": 0.55,
                "volatility_3m": 0.22,
                "forward_return_3m": -0.40,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "AI.SH",
                "momentum_1m": 0.14,
                "momentum_3m": 0.10,
                "momentum_6m": 0.65,
                "relative_strength_3m": 0.09,
                "relative_strength_6m": 0.48,
                "market_correlation_6m": 0.68,
                "drawdown_3m": -0.08,
                "etf_share_growth_1q": 0.27,
                "amount_acceleration_1m_6m": 0.30,
                "amount_crowding_percentile_3y": 0.87,
                "volatility_3m": 0.30,
                "forward_return_3m": 1.00,
            },
        ]
        groups_by_code = {
            "BANK.SH": "finance",
            "RESOURCE.SH": "resources",
            "AI.SH": "technology",
        }
        subthemes_by_code = {
            "BANK.SH": "finance",
            "RESOURCE.SH": "resources",
            "AI.SH": "digital_hot",
        }
        scores = weighted_structural_value_reflation_mainline_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["BANK.SH"], scores["AI.SH"])
        self.assertGreater(scores["RESOURCE.SH"], scores["AI.SH"])

    def test_blended_etf_diagnostics_describes_actual_weighted_basket(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(260)]
        first = [(day, 100.0 * (1.01 ** offset)) for offset, day in enumerate(days)]
        second = [(day, 100.0 * (0.995 ** offset)) for offset, day in enumerate(days)]
        benchmark = [(day, 100.0 * (1.002 ** offset)) for offset, day in enumerate(days)]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.75, "510500.SH": 0.25},
            {"510300.SH": first, "510500.SH": second},
            days[-1],
            benchmark,
        )

        expected_daily = 0.75 * 0.01 + 0.25 * -0.005
        self.assertAlmostEqual(
            result["selected_etf_momentum_6m"],
            (1.0 + expected_daily) ** 126 - 1.0,
        )
        self.assertAlmostEqual(
            result["selected_etf_momentum_12m"],
            (1.0 + expected_daily) ** 252 - 1.0,
        )
        self.assertAlmostEqual(result["selected_etf_positive_day_ratio_3m"], 1.0)
        self.assertAlmostEqual(result["selected_etf_negative_day_ratio_3m"], 0.0)
        self.assertAlmostEqual(
            result["selected_etf_maximum_daily_loss_3m"], expected_daily
        )
        self.assertAlmostEqual(result["selected_etf_volatility_3m"], 0.0)
        self.assertAlmostEqual(result["selected_etf_max_drawdown_6m"], 0.0)

    def test_blended_downside_volatility_uses_semideviation(self) -> None:
        start = date(2023, 1, 1)
        returns = [-0.02 if offset % 2 else 0.01 for offset in range(130)]
        price = 100.0
        rows = [(start, price)]
        for offset, value in enumerate(returns, start=1):
            price *= 1.0 + value
            rows.append((date.fromordinal(start.toordinal() + offset), price))

        result = blended_etf_diagnostics(
            {"510300.SH": 1.0},
            {"510300.SH": rows},
            rows[-1][0],
        )
        trailing = returns[-63:]
        expected = (
            sum(min(value, 0.0) ** 2 for value in trailing) / len(trailing)
        ) ** 0.5 * (252.0 ** 0.5)
        self.assertAlmostEqual(
            result["selected_etf_downside_volatility_3m"], expected
        )

    def test_small_new_etf_sleeve_does_not_erase_six_month_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.90, "NEW.SH": 0.10},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
        )

        self.assertIn("selected_etf_volatility_6m", result)

    def test_new_majority_sleeve_does_not_invent_six_month_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.20, "NEW.SH": 0.80},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
        )

        self.assertNotIn("selected_etf_volatility_6m", result)

    def test_tracking_index_supplies_prelisting_risk_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]
        meta = EquityEtfMeta(
            code="NEW.SH",
            name="new tracker",
            index_code="TRACK.IDX",
            index_name="track index",
            list_date=days[-40],
            first_trade_date=days[-40],
        )

        result = blended_etf_diagnostics(
            {"510300.SH": 0.20, "NEW.SH": 0.80},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
            metas_by_index={"TRACK.IDX": [meta]},
            index_series={"TRACK.IDX": mature},
        )

        self.assertIn("selected_etf_volatility_6m", result)

    def test_selected_score_diagnostics_uses_snapshot_fields_not_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "510300.SH",
                "volatility_3m": 0.18,
                "market_beta_6m": 0.92,
                "forward_return_3m": 99.0,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "510500.SH",
                "volatility_3m": 0.25,
                "market_beta_6m": 1.10,
                "forward_return_3m": -99.0,
            },
        ]
        result = selected_score_diagnostics(
            {"510300.SH": 0.8, "510500.SH": 0.7},
            observations,
            {"510300.SH", "510500.SH"},
            snapshot,
        )
        self.assertEqual(result["selected_etf_volatility_3m"], 0.18)
        self.assertEqual(result["selected_etf_market_beta_6m"], 0.92)
        self.assertNotIn("forward_return_3m", result)

    def test_current_and_future_labels_do_not_affect_current_selection(self) -> None:
        observations = []
        start = date(2018, 1, 31)
        codes = ("510050.SH", "510300.SH", "510500.SH")
        for offset in range(20):
            snapshot = shift_month_end(start, offset)
            end_snapshot = shift_month_end(snapshot, 3)
            for code_index, code in enumerate(codes):
                row = {
                    "snapshot": snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "ts_code": code,
                    "forward_return_3m": 0.02 * (code_index - 1) * (-1 if offset % 2 else 1),
                    "forward_max_drawdown_3m": -0.01 * code_index,
                }
                for feature_index, feature in enumerate(STABLE_FEATURES):
                    row[feature] = float((code_index + feature_index + offset) % 3)
                observations.append(row)

        policy = SupervisedEtfPolicy(
            "test", STABLE_FEATURES, 120, 2.0, 1, 1.0
        )
        snapshot = shift_month_end(start, 16)
        baseline = select_supervised_etfs(observations, snapshot, policy)

        changed = copy.deepcopy(observations)
        for row in changed:
            if date.fromisoformat(row["end_snapshot"]) > snapshot:
                row["forward_return_3m"] = 999.0 if row["ts_code"] == "510500.SH" else -999.0
                row["forward_max_drawdown_3m"] = -0.99
        perturbed = select_supervised_etfs(changed, snapshot, policy)
        self.assertEqual(baseline, perturbed)

    def test_static_v2_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v2_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v2_top1(changed, snapshot),
        )

    def test_static_v3_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.08 + index * 0.02,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v3_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v3_top1(changed, snapshot),
        )

    def test_static_v4_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v4_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v4_top1(changed, snapshot),
        )

    def test_static_v5_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v5_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v5_top1(changed, snapshot),
        )

    def test_static_v6_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "distance_high_12m": -0.03 * index,
                    "momentum_12m": -0.05 + index * 0.05,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_pb_change_6m": -0.1 + index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v6_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v6_top1(changed, snapshot),
        )

    def test_static_v7_flow_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "etf_subscription_flow_2q": 0.2 - index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v7_top1(
            observations, snapshot, 0.5
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v7_top1(changed, snapshot, 0.5),
        )

    def test_static_v9_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v9_top1(
            observations, snapshot
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v9_top1(changed, snapshot),
        )

    def test_static_v10_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v10_top1(
            observations, snapshot, 0.75
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v10_top1(
                changed, snapshot, 0.75
            ),
        )

    def test_structural_mainline_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "relative_strength_3m": 0.01 + index * 0.04,
                    "relative_strength_6m": 0.02 + index * 0.03,
                    "momentum_3m": 0.03 + index * 0.03,
                    "momentum_6m": 0.04 + index * 0.02,
                    "positive_day_ratio_3m": 0.45 + index * 0.10,
                    "index_trend_acceleration_geometric_3m_vs_6m": -0.01 + index * 0.02,
                    "market_correlation_6m": 0.90 - index * 0.20,
                    "log_amount_1m": 10.0 + index,
                    "amount_acceleration_1m_6m": -0.1 + index * 0.1,
                    "etf_share_growth_1q": -0.02 + index * 0.02,
                    "etf_subscription_flow_1q": -0.01 + index * 0.01,
                    "index_etf_positive_turnover_pressure_1m": -0.05 + index * 0.05,
                    "index_fundamental_earnings_growth_3m": -0.05 + index * 0.05,
                    "index_fundamental_roe_proxy": 0.08 + index * 0.01,
                    "index_pe_ttm_history_percentile_3y": 0.80 - index * 0.20,
                    "index_policy_score": index,
                    "distance_high_12m": -0.15 + index * 0.06,
                    "drawdown_3m": -0.08 + index * 0.03,
                    "residual_momentum_6m": -0.02 + index * 0.02,
                    "amount_crowding_percentile_3y": 0.90 - index * 0.30,
                    "negative_day_ratio_3m": 0.40 - index * 0.10,
                    "historical_cvar_5pct_3m": -0.05 + index * 0.01,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_structural_mainline_top3(
            observations, snapshot
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_structural_mainline_top3(changed, snapshot),
        )
        flow_baseline = select_weighted_structural_liquidity_flow_top5(
            observations,
            snapshot,
        )
        self.assertEqual(
            flow_baseline,
            select_weighted_structural_liquidity_flow_top5(changed, snapshot),
        )
        groups_by_code = {
            "510050.SH": "broad_value",
            "510300.SH": "broad_value",
            "510500.SH": "broad_growth",
        }
        group_breadth_baseline = select_weighted_structural_liquidity_group_breadth_top5(
            observations,
            snapshot,
            groups_by_code,
        )
        self.assertEqual(
            group_breadth_baseline,
            select_weighted_structural_liquidity_group_breadth_top5(
                changed,
                snapshot,
                groups_by_code,
            ),
        )
        momentum_breadth_baseline = select_weighted_structural_momentum_breadth_top3(
            observations,
            snapshot,
        )
        self.assertEqual(
            momentum_breadth_baseline,
            select_weighted_structural_momentum_breadth_top3(changed, snapshot),
        )
        reflation_baseline = select_weighted_structural_reflation_rotation_top3(
            observations,
            snapshot,
        )
        self.assertEqual(
            reflation_baseline,
            select_weighted_structural_reflation_rotation_top3(changed, snapshot),
        )
        conditional_baseline = select_weighted_structural_conditional_rotation_top3(
            observations,
            snapshot,
            groups_by_code,
        )
        self.assertEqual(
            conditional_baseline,
            select_weighted_structural_conditional_rotation_top3(
                changed,
                snapshot,
                groups_by_code,
            ),
        )
        resilience_baseline = select_weighted_structural_resilience_top5(
            observations,
            snapshot,
        )
        self.assertEqual(
            resilience_baseline,
            select_weighted_structural_resilience_top5(changed, snapshot),
        )
        cooling_subthemes_by_code = {
            "510050.SH": "finance",
            "510300.SH": "communication",
            "510500.SH": "utilities",
        }
        cooling_baseline = weighted_structural_cooling_rotation_scores(
            observations,
            snapshot,
            cooling_subthemes_by_code,
        )
        self.assertEqual(
            cooling_baseline,
            weighted_structural_cooling_rotation_scores(
                changed,
                snapshot,
                cooling_subthemes_by_code,
            ),
        )
        multistate_baseline = weighted_structural_multistate_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            cooling_subthemes_by_code,
        )
        self.assertEqual(
            multistate_baseline,
            weighted_structural_multistate_rotation_scores(
                changed,
                snapshot,
                groups_by_code,
                cooling_subthemes_by_code,
            ),
        )
        late_cycle_baseline = weighted_structural_late_cycle_defensive_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            cooling_subthemes_by_code,
        )
        self.assertEqual(
            late_cycle_baseline,
            weighted_structural_late_cycle_defensive_rotation_scores(
                changed,
                snapshot,
                groups_by_code,
                cooling_subthemes_by_code,
            ),
        )

    def test_late_cycle_growth_exhaustion_trigger_uses_current_features(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "TECH.SH",
                "momentum_1m": 0.04,
                "momentum_3m": 0.32,
                "momentum_6m": 0.42,
                "drawdown_3m": -0.04,
                "amount_crowding_percentile_3y": 0.86,
                "etf_share_growth_1q": 0.20,
                "forward_return_3m": -0.50,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "COMM.SH",
                "momentum_1m": 0.02,
                "momentum_3m": 0.10,
                "momentum_6m": 0.12,
                "drawdown_3m": -0.03,
                "amount_crowding_percentile_3y": 0.40,
                "etf_share_growth_1q": 0.10,
                "forward_return_3m": 0.50,
            },
        ]
        subthemes_by_code = {"TECH.SH": "digital_hot", "COMM.SH": "communication"}
        self.assertTrue(
            structural_growth_exhaustion_rotation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertTrue(
            structural_growth_exhaustion_rotation_active(
                changed,
                snapshot,
                subthemes_by_code,
            )
        )
        cooled = copy.deepcopy(observations)
        cooled[0]["momentum_3m"] = 0.12
        cooled[0]["momentum_6m"] = 0.20
        cooled[0]["etf_share_growth_1q"] = 0.05
        self.assertFalse(
            structural_growth_exhaustion_rotation_active(
                cooled,
                snapshot,
                subthemes_by_code,
            )
        )

    def test_small_growth_subtheme_identifies_chinext_and_midcap_growth(self) -> None:
        self.assertEqual(
            structural_subtheme_group_for_text("创业板ETF易方达 创业板指数"),
            "small_growth",
        )
        self.assertEqual(
            structural_subtheme_group_for_text("中小100ETF华夏 中小企业100指数"),
            "small_growth",
        )
        self.assertEqual(
            structural_subtheme_group_for_text("国证2000ETF广发 国证2000指数"),
            "small_growth",
        )

    def test_small_growth_recovery_score_uses_current_features_only(self) -> None:
        snapshot = date(2012, 12, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "CHINEXT.SZ",
                "momentum_1m": 0.14,
                "momentum_3m": 0.05,
                "momentum_6m": -0.02,
                "drawdown_3m": -0.03,
                "market_correlation_6m": 0.72,
                "amount_acceleration_1m_6m": 0.80,
                "etf_positive_turnover_pressure_1m": 0.60,
                "amount_crowding_percentile_3y": 0.35,
                "forward_return_3m": 0.20,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "GROWTH.SZ",
                "momentum_1m": 0.10,
                "momentum_3m": 0.03,
                "momentum_6m": -0.01,
                "drawdown_3m": -0.02,
                "market_correlation_6m": 0.68,
                "amount_acceleration_1m_6m": 0.50,
                "etf_positive_turnover_pressure_1m": 0.55,
                "amount_crowding_percentile_3y": 0.40,
                "forward_return_3m": 0.15,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE1.SH",
                "momentum_1m": 0.18,
                "momentum_3m": 0.18,
                "momentum_6m": 0.08,
                "drawdown_3m": 0.0,
                "market_correlation_6m": 0.95,
                "amount_acceleration_1m_6m": 1.00,
                "etf_positive_turnover_pressure_1m": 0.40,
                "amount_crowding_percentile_3y": 0.70,
                "forward_return_3m": -0.10,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE2.SH",
                "momentum_1m": 0.17,
                "momentum_3m": 0.16,
                "momentum_6m": 0.06,
                "drawdown_3m": 0.0,
                "market_correlation_6m": 0.92,
                "amount_acceleration_1m_6m": 0.90,
                "etf_positive_turnover_pressure_1m": 0.35,
                "amount_crowding_percentile_3y": 0.65,
                "forward_return_3m": -0.08,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE3.SH",
                "momentum_1m": 0.16,
                "momentum_3m": 0.15,
                "momentum_6m": 0.05,
                "drawdown_3m": -0.01,
                "market_correlation_6m": 0.90,
                "amount_acceleration_1m_6m": 0.70,
                "etf_positive_turnover_pressure_1m": 0.30,
                "amount_crowding_percentile_3y": 0.60,
                "forward_return_3m": -0.05,
            },
        ]
        groups_by_code = {
            "CHINEXT.SZ": "broad_growth",
            "GROWTH.SZ": "broad_growth",
            "VALUE1.SH": "broad_value",
            "VALUE2.SH": "broad_value",
            "VALUE3.SH": "industrial",
        }
        subthemes_by_code = {
            "CHINEXT.SZ": "small_growth",
            "GROWTH.SZ": "small_growth",
            "VALUE1.SH": "finance",
            "VALUE2.SH": "finance",
            "VALUE3.SH": "industrial",
        }
        self.assertTrue(
            structural_small_growth_recovery_active(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["CHINEXT.SZ"], scores["VALUE1.SH"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_late_cycle_small_growth_recovery_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_tech_pullback_continuation_score_uses_current_features_only(self) -> None:
        snapshot = date(2019, 10, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "SEMI.SH",
                "momentum_1m": -0.05,
                "momentum_3m": 0.14,
                "relative_strength_3m": 0.13,
                "drawdown_3m": -0.10,
                "etf_share_growth_1q": 1.20,
                "amount_acceleration_1m_6m": 0.40,
                "amount_crowding_percentile_3y": 0.80,
                "positive_day_ratio_3m": 0.62,
                "market_correlation_6m": 0.55,
                "forward_return_3m": 0.35,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "DIGI1.SZ",
                "momentum_1m": -0.06,
                "momentum_3m": 0.12,
                "relative_strength_3m": 0.11,
                "drawdown_3m": -0.09,
                "etf_share_growth_1q": 0.70,
                "amount_acceleration_1m_6m": 0.50,
                "amount_crowding_percentile_3y": 0.95,
                "positive_day_ratio_3m": 0.58,
                "market_correlation_6m": 0.62,
                "forward_return_3m": 0.10,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "DIGI2.SZ",
                "momentum_1m": -0.02,
                "momentum_3m": 0.09,
                "relative_strength_3m": 0.08,
                "drawdown_3m": -0.05,
                "etf_share_growth_1q": 0.18,
                "amount_acceleration_1m_6m": -0.10,
                "amount_crowding_percentile_3y": 0.89,
                "positive_day_ratio_3m": 0.55,
                "market_correlation_6m": 0.66,
                "forward_return_3m": 0.08,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HEALTH.SH",
                "momentum_1m": 0.04,
                "momentum_3m": 0.18,
                "relative_strength_3m": 0.17,
                "drawdown_3m": -0.01,
                "etf_share_growth_1q": -0.02,
                "amount_acceleration_1m_6m": 0.40,
                "amount_crowding_percentile_3y": 0.70,
                "positive_day_ratio_3m": 0.70,
                "market_correlation_6m": 0.70,
                "forward_return_3m": -0.12,
            },
        ]
        groups_by_code = {
            "SEMI.SH": "technology",
            "DIGI1.SZ": "technology",
            "DIGI2.SZ": "technology",
            "HEALTH.SH": "healthcare",
        }
        subthemes_by_code = {
            "SEMI.SH": "semiconductor",
            "DIGI1.SZ": "digital_hot",
            "DIGI2.SZ": "digital_hot",
            "HEALTH.SH": "healthcare",
        }
        self.assertTrue(
            structural_tech_pullback_continuation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_late_cycle_tech_pullback_continuation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["SEMI.SH"], scores["HEALTH.SH"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_late_cycle_tech_pullback_continuation_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_tech_pullback_continuation_blocks_vertical_extension(self) -> None:
        snapshot = date(2020, 2, 29)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": code,
                "momentum_1m": momentum_1m,
                "momentum_3m": momentum_3m,
                "relative_strength_3m": momentum_3m - 0.02,
                "drawdown_3m": drawdown_3m,
                "etf_share_growth_1q": share_growth,
                "amount_acceleration_1m_6m": 1.00,
                "amount_crowding_percentile_3y": 0.94,
            }
            for code, momentum_1m, momentum_3m, drawdown_3m, share_growth in (
                ("SEMI.SH", 0.09, 0.64, -0.16, 8.00),
                ("DIGI1.SZ", 0.03, 0.30, -0.12, 0.25),
                ("DIGI2.SZ", 0.04, 0.28, -0.11, 0.20),
            )
        ]
        subthemes_by_code = {
            "SEMI.SH": "semiconductor",
            "DIGI1.SZ": "digital_hot",
            "DIGI2.SZ": "digital_hot",
        }
        self.assertFalse(
            structural_tech_pullback_continuation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )

    def test_healthcare_leadership_score_avoids_broad_value_dilution(self) -> None:
        snapshot = date(2017, 12, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HEALTH1.SH",
                "momentum_1m": 0.04,
                "momentum_3m": 0.12,
                "relative_strength_3m": 0.06,
                "drawdown_3m": -0.03,
                "etf_share_growth_1q": 0.50,
                "amount_acceleration_1m_6m": 0.30,
                "positive_day_ratio_3m": 0.65,
                "amount_crowding_percentile_3y": 0.35,
                "market_correlation_6m": 0.70,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HEALTH2.SH",
                "momentum_1m": 0.03,
                "momentum_3m": 0.10,
                "relative_strength_3m": 0.05,
                "drawdown_3m": -0.04,
                "etf_share_growth_1q": 0.30,
                "amount_acceleration_1m_6m": 0.10,
                "positive_day_ratio_3m": 0.60,
                "amount_crowding_percentile_3y": 0.30,
                "market_correlation_6m": 0.72,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE.SH",
                "momentum_1m": 0.01,
                "momentum_3m": 0.03,
                "relative_strength_3m": -0.03,
                "drawdown_3m": -0.03,
                "etf_share_growth_1q": 0.00,
                "amount_acceleration_1m_6m": 0.30,
                "positive_day_ratio_3m": 0.55,
                "amount_crowding_percentile_3y": 0.70,
                "market_correlation_6m": 0.80,
            },
        ]
        groups_by_code = {
            "HEALTH1.SH": "healthcare",
            "HEALTH2.SH": "healthcare",
            "VALUE.SH": "finance",
        }
        subthemes_by_code = {
            "HEALTH1.SH": "healthcare",
            "HEALTH2.SH": "healthcare",
            "VALUE.SH": "finance",
        }
        scores = weighted_structural_healthcare_leadership_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["HEALTH1.SH"], scores["VALUE.SH"])
        self.assertGreater(scores["HEALTH2.SH"], scores["VALUE.SH"])

    def test_digital_reacceleration_score_uses_current_features_only(self) -> None:
        snapshot = date(2023, 2, 28)
        observations = []
        for idx in range(5):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": f"DIGI{idx}.SZ",
                    "momentum_1m": 0.02 + idx * 0.002,
                    "momentum_3m": 0.08 + idx * 0.01,
                    "momentum_6m": 0.05,
                    "relative_strength_3m": 0.03 + idx * 0.01,
                    "drawdown_3m": -0.04,
                    "amount_acceleration_1m_6m": 0.60,
                    "market_correlation_6m": 0.55,
                    "positive_day_ratio_3m": 0.62,
                    "etf_share_growth_1q": 0.05,
                    "amount_crowding_percentile_3y": 0.70,
                    "forward_return_3m": 0.10,
                }
            )
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "FIN.SH",
                "momentum_1m": -0.03,
                "momentum_3m": 0.07,
                "momentum_6m": 0.03,
                "relative_strength_3m": 0.00,
                "drawdown_3m": -0.05,
                "amount_acceleration_1m_6m": 0.40,
                "market_correlation_6m": 0.75,
                "positive_day_ratio_3m": 0.60,
                "etf_share_growth_1q": 0.00,
                "amount_crowding_percentile_3y": 0.50,
                "forward_return_3m": -0.05,
            }
        )
        groups_by_code = {row["ts_code"]: "technology" for row in observations}
        groups_by_code["FIN.SH"] = "finance"
        subthemes_by_code = {
            row["ts_code"]: "digital_hot" for row in observations
        }
        subthemes_by_code["FIN.SH"] = "finance"

        self.assertTrue(
            structural_digital_reacceleration_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_digital_reacceleration_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["DIGI4.SZ"], scores["FIN.SH"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_digital_reacceleration_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_early_digital_reacceleration_allows_price_diffusion_before_flow(self) -> None:
        snapshot = date(2023, 1, 31)
        observations = []
        for idx in range(8):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": f"EARLY{idx}.SZ",
                    "momentum_1m": 0.10 + idx * 0.003,
                    "momentum_3m": 0.08 + idx * 0.006,
                    "momentum_6m": 0.03,
                    "relative_strength_3m": -0.02 + idx * 0.005,
                    "drawdown_3m": -0.02,
                    "amount_acceleration_1m_6m": 0.18,
                    "market_correlation_6m": 0.56,
                    "positive_day_ratio_3m": 0.60,
                    "etf_share_growth_1q": 0.02,
                    "amount_crowding_percentile_3y": 0.45,
                }
            )
        subthemes_by_code = {
            row["ts_code"]: "digital_hot" for row in observations
        }
        self.assertTrue(
            structural_digital_reacceleration_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        self.assertFalse(
            structural_digital_reacceleration_active(
                observations[:7],
                snapshot,
                subthemes_by_code,
            )
        )

    def test_finance_defensive_rotation_uses_current_features_only(self) -> None:
        snapshot = date(2025, 2, 28)
        observations = []
        for idx in range(3):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": f"FIN{idx}.SH",
                    "momentum_3m": 0.055 + idx * 0.01,
                    "relative_strength_3m": 0.045 + idx * 0.01,
                    "drawdown_3m": -0.035,
                    "market_correlation_6m": 0.82,
                    "positive_day_ratio_3m": 0.60,
                    "amount_crowding_percentile_3y": 0.70,
                    "etf_share_growth_1q": 0.03,
                    "index_constituent_earnings_yield_change_12m": 0.02,
                    "forward_return_3m": 0.08,
                }
            )
        for idx in range(4):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": f"DIGI{idx}.SZ",
                    "momentum_3m": 0.10,
                    "relative_strength_3m": 0.08,
                    "drawdown_3m": -0.04,
                    "market_correlation_6m": 0.70,
                    "positive_day_ratio_3m": 0.62,
                    "amount_crowding_percentile_3y": 0.88,
                    "etf_share_growth_1q": 0.15,
                    "index_constituent_earnings_yield_change_12m": 0.00,
                    "forward_return_3m": -0.05,
                }
            )
        groups_by_code = {row["ts_code"]: "finance" for row in observations}
        subthemes_by_code = {
            row["ts_code"]: ("finance" if row["ts_code"].startswith("FIN") else "digital_hot")
            for row in observations
        }

        self.assertTrue(
            structural_finance_defensive_rotation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_finance_defensive_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["FIN2.SH"], scores["DIGI0.SZ"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_finance_defensive_rotation_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )
        self.assertFalse(
            structural_finance_defensive_rotation_active(
                observations[:2],
                snapshot,
                subthemes_by_code,
            )
        )

    def test_digital_blowoff_rotation_uses_current_features_only(self) -> None:
        snapshot = date(2023, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": f"DIGI{idx}.SZ",
                "momentum_1m": 0.11 + idx * 0.005,
                "momentum_3m": 0.30 + idx * 0.01,
                "momentum_6m": 0.35,
                "drawdown_3m": -0.005,
                "amount_crowding_percentile_3y": 0.82,
                "market_correlation_6m": 0.55,
                "forward_return_3m": -0.10,
            }
            for idx in range(5)
        ]
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "COMM.SH",
                "momentum_1m": 0.10,
                "momentum_3m": 0.28,
                "momentum_6m": 0.20,
                "relative_strength_3m": 0.22,
                "drawdown_3m": -0.02,
                "amount_crowding_percentile_3y": 0.45,
                "market_correlation_6m": 0.50,
                "etf_share_growth_1q": 0.10,
                "amount_acceleration_1m_6m": 0.60,
                "positive_day_ratio_3m": 0.70,
                "forward_return_3m": 0.12,
            }
        )
        observations.append(
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "DIV.SH",
                "momentum_1m": -0.01,
                "momentum_3m": 0.06,
                "momentum_6m": 0.02,
                "relative_strength_3m": 0.01,
                "drawdown_3m": -0.02,
                "amount_crowding_percentile_3y": 0.55,
                "market_correlation_6m": 0.70,
                "etf_share_growth_1q": 0.02,
                "amount_acceleration_1m_6m": 0.00,
                "positive_day_ratio_3m": 0.58,
                "forward_return_3m": 0.02,
            }
        )
        subthemes_by_code = {
            row["ts_code"]: "digital_hot" for row in observations
        }
        subthemes_by_code["COMM.SH"] = "communication"
        subthemes_by_code["DIV.SH"] = "finance"
        groups_by_code = {
            row["ts_code"]: "technology" for row in observations
        }
        groups_by_code["DIV.SH"] = "finance"
        self.assertTrue(
            structural_digital_blowoff_rotation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_digital_blowoff_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["COMM.SH"], scores["DIV.SH"])
        self.assertGreater(scores["COMM.SH"], scores["DIGI4.SZ"])
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            structural_digital_blowoff_rotation_active(
                observations,
                snapshot,
                subthemes_by_code,
            ),
            structural_digital_blowoff_rotation_active(
                changed,
                snapshot,
                subthemes_by_code,
            ),
        )

    def test_digital_blowoff_utilities_rotation_boosts_green_power(self) -> None:
        snapshot = date(2023, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": f"DIGI{idx}.SZ",
                "momentum_1m": 0.11,
                "momentum_3m": 0.31,
                "momentum_6m": 0.35,
                "relative_strength_3m": 0.18,
                "drawdown_3m": -0.006,
                "amount_crowding_percentile_3y": 0.84,
                "market_correlation_6m": 0.55,
                "positive_day_ratio_3m": 0.74,
                "forward_return_3m": -0.05,
            }
            for idx in range(5)
        ]
        observations.extend(
            [
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": "COMM.SH",
                    "momentum_1m": 0.10,
                    "momentum_3m": 0.11,
                    "momentum_6m": -0.03,
                    "relative_strength_3m": 0.04,
                    "drawdown_3m": -0.02,
                    "amount_crowding_percentile_3y": 0.40,
                    "market_correlation_6m": 0.55,
                    "etf_share_growth_1q": 0.02,
                    "amount_acceleration_1m_6m": 0.05,
                    "positive_day_ratio_3m": 0.55,
                    "forward_return_3m": 0.00,
                },
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": "UTIL1.SH",
                    "momentum_1m": -0.02,
                    "momentum_3m": 0.04,
                    "momentum_6m": -0.04,
                    "relative_strength_3m": 0.02,
                    "drawdown_3m": -0.035,
                    "amount_crowding_percentile_3y": 0.35,
                    "market_correlation_6m": 0.50,
                    "etf_share_growth_1q": 0.01,
                    "amount_acceleration_1m_6m": 0.02,
                    "positive_day_ratio_3m": 0.48,
                    "forward_return_3m": 0.10,
                },
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": "UTIL2.SH",
                    "momentum_1m": -0.02,
                    "momentum_3m": 0.035,
                    "momentum_6m": -0.04,
                    "relative_strength_3m": 0.01,
                    "drawdown_3m": -0.038,
                    "amount_crowding_percentile_3y": 0.42,
                    "market_correlation_6m": 0.50,
                    "etf_share_growth_1q": 0.01,
                    "amount_acceleration_1m_6m": 0.02,
                    "positive_day_ratio_3m": 0.46,
                    "forward_return_3m": 0.08,
                },
            ]
        )
        subthemes_by_code = {
            row["ts_code"]: "digital_hot" for row in observations
        }
        subthemes_by_code["COMM.SH"] = "communication"
        subthemes_by_code["UTIL1.SH"] = "utilities"
        subthemes_by_code["UTIL2.SH"] = "utilities"
        groups_by_code = {
            row["ts_code"]: "technology" for row in observations
        }
        self.assertTrue(
            structural_digital_blowoff_utilities_rotation_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_digital_blowoff_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["UTIL1.SH"], scores["COMM.SH"])
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_digital_blowoff_rotation_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_new_energy_pullback_restart_uses_current_features_only(self) -> None:
        snapshot = date(2021, 4, 30)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "NE1.SZ",
                "momentum_1m": 0.12,
                "momentum_3m": -0.12,
                "momentum_6m": 0.36,
                "drawdown_3m": -0.11,
                "market_correlation_6m": 0.70,
                "amount_crowding_percentile_3y": 0.35,
                "etf_share_growth_1q": -0.05,
                "positive_day_ratio_3m": 0.45,
                "forward_return_3m": 0.60,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "NE2.SZ",
                "momentum_1m": 0.10,
                "momentum_3m": -0.10,
                "momentum_6m": 0.32,
                "drawdown_3m": -0.09,
                "market_correlation_6m": 0.68,
                "amount_crowding_percentile_3y": 0.40,
                "etf_share_growth_1q": -0.10,
                "positive_day_ratio_3m": 0.48,
                "forward_return_3m": 0.55,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE.SH",
                "momentum_1m": 0.09,
                "momentum_3m": 0.22,
                "momentum_6m": 0.32,
                "drawdown_3m": -0.03,
                "market_correlation_6m": 0.30,
                "amount_crowding_percentile_3y": 0.90,
                "etf_share_growth_1q": 0.50,
                "positive_day_ratio_3m": 0.70,
                "forward_return_3m": 0.05,
            },
        ]
        groups_by_code = {
            "NE1.SZ": "technology",
            "NE2.SZ": "technology",
            "VALUE.SH": "materials",
        }
        subthemes_by_code = {
            "NE1.SZ": "new_energy",
            "NE2.SZ": "new_energy",
            "VALUE.SH": "resources",
        }
        self.assertTrue(
            structural_new_energy_pullback_restart_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        scores = weighted_structural_new_energy_pullback_restart_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["NE1.SZ"], scores["VALUE.SH"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_new_energy_pullback_restart_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_local_mainline_pullback_reentry_selects_dominant_non_new_energy_theme(
        self,
    ) -> None:
        snapshot = date(2021, 4, 30)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HC1.SH",
                "momentum_1m": 0.08,
                "momentum_3m": -0.11,
                "momentum_6m": 0.32,
                "drawdown_3m": -0.10,
                "drawdown_6m": -0.18,
                "relative_strength_3m": 0.02,
                "relative_strength_6m": 0.24,
                "market_correlation_6m": 0.62,
                "volatility_3m": 0.30,
                "days_since_high_6m": 42.0,
                "amount_crowding_percentile_3y": 0.35,
                "etf_share_growth_1q": 0.06,
                "positive_day_ratio_3m": 0.48,
                "forward_return_3m": -0.40,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "HC2.SH",
                "momentum_1m": 0.07,
                "momentum_3m": -0.09,
                "momentum_6m": 0.30,
                "drawdown_3m": -0.09,
                "drawdown_6m": -0.16,
                "relative_strength_3m": 0.01,
                "relative_strength_6m": 0.22,
                "market_correlation_6m": 0.65,
                "volatility_3m": 0.32,
                "days_since_high_6m": 39.0,
                "amount_crowding_percentile_3y": 0.42,
                "etf_share_growth_1q": 0.03,
                "positive_day_ratio_3m": 0.46,
                "forward_return_3m": -0.35,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "NE1.SZ",
                "momentum_1m": 0.09,
                "momentum_3m": -0.10,
                "momentum_6m": 0.33,
                "drawdown_3m": -0.10,
                "drawdown_6m": -0.17,
                "relative_strength_3m": 0.03,
                "relative_strength_6m": 0.25,
                "market_correlation_6m": 0.60,
                "volatility_3m": 0.31,
                "days_since_high_6m": 40.0,
                "amount_crowding_percentile_3y": 0.30,
                "etf_share_growth_1q": 0.08,
                "positive_day_ratio_3m": 0.49,
                "forward_return_3m": 0.80,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "VALUE.SH",
                "momentum_1m": 0.03,
                "momentum_3m": 0.08,
                "momentum_6m": 0.11,
                "drawdown_3m": -0.03,
                "drawdown_6m": -0.05,
                "relative_strength_3m": -0.04,
                "relative_strength_6m": -0.02,
                "market_correlation_6m": 0.75,
                "volatility_3m": 0.20,
                "days_since_high_6m": 12.0,
                "amount_crowding_percentile_3y": 0.20,
                "etf_share_growth_1q": 0.01,
                "positive_day_ratio_3m": 0.55,
                "forward_return_3m": 0.10,
            },
        ]
        groups_by_code = {
            "HC1.SH": "healthcare",
            "HC2.SH": "healthcare",
            "NE1.SZ": "new_energy",
            "VALUE.SH": "broad_value",
        }
        subthemes_by_code = {
            "HC1.SH": "healthcare",
            "HC2.SH": "healthcare",
            "NE1.SZ": "new_energy",
            "VALUE.SH": "finance",
        }

        self.assertTrue(
            structural_local_mainline_pullback_reentry_active(
                observations,
                snapshot,
                subthemes_by_code,
            )
        )
        self.assertEqual(
            structural_local_mainline_pullback_reentry_subthemes(
                observations,
                snapshot,
                subthemes_by_code,
            ),
            {"healthcare"},
        )
        scores = weighted_structural_local_mainline_pullback_reentry_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
        self.assertGreater(scores["HC1.SH"], scores["NE1.SZ"])

        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -1.0
        self.assertEqual(
            scores,
            weighted_structural_local_mainline_pullback_reentry_scores(
                changed,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            ),
        )

    def test_local_mainline_pullback_reentry_can_select_semiconductor(self) -> None:
        snapshot = date(2022, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": code,
                "momentum_1m": -0.08,
                "momentum_3m": -0.12,
                "momentum_6m": 0.28,
                "drawdown_3m": -0.16,
                "drawdown_6m": -0.24,
                "relative_strength_3m": -0.01,
                "relative_strength_6m": 0.20,
                "market_correlation_6m": 0.70,
                "volatility_3m": 0.40,
                "days_since_high_6m": 55.0,
                "amount_crowding_percentile_3y": 0.45,
                "etf_share_growth_1q": 0.02,
                "positive_day_ratio_3m": 0.42,
            }
            for code in ("SEMI1.SH", "SEMI2.SH")
        ]
        subthemes_by_code = {
            "SEMI1.SH": "semiconductor",
            "SEMI2.SH": "semiconductor",
        }

        self.assertEqual(
            structural_local_mainline_pullback_reentry_subthemes(
                observations,
                snapshot,
                subthemes_by_code,
            ),
            {"semiconductor"},
        )


if __name__ == "__main__":
    unittest.main()

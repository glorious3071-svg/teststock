import unittest

from scripts.replay_scorecard_csi_strict_quarterly_etf_direct_policy import (
    macro_market_damage_cap_active,
    option_panic_after_rally_cap_active,
    report_result,
    summarize,
)


class ReplayStrictQuarterlyEtfDirectPolicyTest(unittest.TestCase):
    def _case(self, target_met: bool = True) -> dict:
        return {
            "target_met": target_met,
            "final_capital_wan": 5000.0,
            "max_drawdown": -0.10,
            "average_exposure": 0.5,
            "online_guard_count": 0,
            "direction_risk_gate_rejection_count": 0,
            "selector_dispersion_recovery_count": 0,
            "recovery_count": 0,
            "quality_high_count": 0,
            "quality_low_count": 0,
            "macro_market_damage_cap_count": 0,
        }

    def test_report_result_accepts_backtest_and_screen_shapes(self) -> None:
        backtest_payload = {"results": [{"cases": []}, {"cases": [{"id": 2}]}]}
        self.assertEqual(report_result(backtest_payload, 1), {"cases": [{"id": 2}]})

        screen_payload = {"mode": "fast_screen", "cases": [{"id": 1}]}
        self.assertEqual(report_result(screen_payload, 0), screen_payload)

    def test_partial_summary_can_pass_without_claiming_full_objective(self) -> None:
        summary = summarize([self._case(), self._case()])
        self.assertTrue(summary["screen_passed"])
        self.assertTrue(summary["partial_matrix"])
        self.assertFalse(summary["objective_met"])

    def test_partial_summary_fails_when_any_case_fails(self) -> None:
        summary = summarize([self._case(), self._case(False)])
        self.assertFalse(summary["screen_passed"])
        self.assertEqual(summary["pass_count"], 1)

    def test_macro_market_damage_cap_catches_2018_style_pre_damage(self) -> None:
        row = {
            "active_risk_flags": ["low_vol_mature_trend_flag"],
            "market_state": {
                "cs300_return_3m": 0.06,
                "basket_drawdown_6m": -0.03,
                "basket_vol_3m": 0.15,
                "pboc_outlook_net_tone": -5.2,
                "domestic_m1_m2_scissors_change_3m": -1.3,
            },
        }
        self.assertTrue(macro_market_damage_cap_active(row))

    def test_macro_market_damage_cap_catches_initial_and_continuation_damage(self) -> None:
        initial_damage = {
            "market_state": {
                "cs300_return_3m": -0.12,
                "basket_drawdown_6m": -0.13,
                "basket_vol_3m": 0.22,
                "pboc_outlook_net_tone": -13.0,
                "domestic_m1_m2_scissors_change_3m": -4.8,
            },
        }
        self.assertTrue(macro_market_damage_cap_active(initial_damage))

        continuation_damage = {
            "market_state": {
                "cs300_return_3m": -0.06,
                "cs300_return_6m": -0.19,
                "basket_drawdown_6m": -0.19,
                "basket_vol_3m": 0.21,
                "basket_return_3m_max": -0.04,
                "breadth_return_3m_positive": 0.0,
                "pboc_outlook_net_tone": -13.0,
                "domestic_m1_m2_scissors_change_3m": -0.3,
            },
        }
        self.assertTrue(macro_market_damage_cap_active(continuation_damage))

    def test_macro_market_damage_cap_does_not_block_policy_repair(self) -> None:
        policy_repair = {
            "active_risk_flags": [],
            "market_state": {
                "cs300_return_3m": -0.02,
                "cs300_return_6m": 0.11,
                "basket_drawdown_6m": -0.06,
                "basket_vol_3m": 0.30,
                "basket_return_1m": -0.04,
                "breadth_return_1m_positive": 0.14,
                "pboc_outlook_net_tone": 22.0,
                "domestic_m1_m2_scissors_change_3m": 8.1,
            },
        }
        self.assertFalse(macro_market_damage_cap_active(policy_repair))

    def test_option_panic_after_rally_cap_requires_supported_high_vol_rally(self) -> None:
        row = {
            "active_risk_flags": ["option_panic_after_rally_flag"],
            "market_state": {
                "cs300_return_3m": 0.006,
                "cs300_return_6m": 0.16,
                "basket_return_1m": 0.076,
                "basket_drawdown_6m": -0.064,
                "basket_vol_3m": 0.317,
                "pboc_outlook_net_tone": 25.0,
                "domestic_m1_m2_scissors_change_3m": 7.0,
            },
        }
        self.assertTrue(option_panic_after_rally_cap_active(row))

        unsupported = dict(row)
        unsupported["market_state"] = dict(row["market_state"])
        unsupported["market_state"]["pboc_outlook_net_tone"] = -5.0
        self.assertFalse(option_panic_after_rally_cap_active(unsupported))


if __name__ == "__main__":
    unittest.main()

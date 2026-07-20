from __future__ import annotations

import unittest
from datetime import date

from backtest.structural_adaptation import (
    StructuralAdaptationGate,
    inferred_defense_return,
    validate_case_matrix_adaptation,
    validate_recent_survival,
)
from scripts.validate_scorecard_csi_structural_adaptation import strong_risk_ban
from scripts.validate_scorecard_csi_structural_adaptation import pct_chg_compounded_series
from scripts.validate_scorecard_csi_structural_adaptation import report_result


class StructuralAdaptationTest(unittest.TestCase):
    def _case(self, returns: list[float], mdd: float = -0.02) -> dict:
        rows = []
        capital = 1_000_000.0
        year = 2006
        quarter = 1
        for value in returns:
            rows.append(
                {
                    "decision_date": f"{year:04d}-{quarter * 3:02d}-01",
                    "capital_at_decision": capital,
                    "min_capital_since_decision": capital * (1.0 + mdd),
                    "realized_portfolio_return": value,
                    "realized_risk_return": value * 1.5,
                    "exposure": 0.5,
                }
            )
            capital *= 1.0 + value
            quarter += 1
            if quarter == 5:
                quarter = 1
                year += 1
        return {
            "phase_month_offset": 0,
            "execution_lag_days": 0,
            "decision_rows": rows,
        }

    def test_recent_survival_passes_when_all_windows_clear_thresholds(self) -> None:
        case = self._case([0.03] * 80)
        result = validate_recent_survival(case)
        self.assertTrue(result["passed"])
        self.assertGreater(result["recent_10y_annualized_return"], 0.08)
        self.assertGreater(result["recent_5y_cumulative_return"], 0.30)

    def test_recent_survival_fails_on_recent_stall(self) -> None:
        returns = [0.03] * 60 + [0.0] * 20
        case = self._case(returns)
        result = validate_recent_survival(case)
        self.assertFalse(result["passed"])
        self.assertIn("recent_5y_cumulative_return", result["failures"])
        self.assertIn("rolling_5y_annualized_return", result["failures"])

    def test_consecutive_under_defense_uses_inferred_defensive_leg(self) -> None:
        row = {
            "realized_portfolio_return": 0.02,
            "realized_risk_return": 0.10,
            "exposure": 0.25,
        }
        self.assertAlmostEqual(inferred_defense_return(row), -0.006666666666666668)

    def test_matrix_adaptation_requires_every_case_to_pass(self) -> None:
        good = self._case([0.03] * 80)
        bad = self._case([0.03] * 60 + [0.0] * 20)
        result = validate_case_matrix_adaptation([good, bad])
        self.assertEqual(result["recent_survival_pass_count"], 1)
        self.assertFalse(result["recent_survival_passed"])

    def test_scorecard_hard_risk_cap_counts_as_strong_risk_ban(self) -> None:
        row = {
            "scorecard_context": {
                "allocation_entry": False,
                "rebalance_reasons": [
                    "stagflation_defensive_cap",
                    "scheduled_selector_refresh",
                ],
            },
        }
        self.assertTrue(strong_risk_ban(row))

    def test_scorecard_refresh_alone_is_not_strong_risk_ban(self) -> None:
        row = {
            "scorecard_context": {
                "allocation_entry": False,
                "rebalance_reasons": ["scheduled_selector_refresh"],
            },
        }
        self.assertFalse(strong_risk_ban(row))

    def test_active_allocation_cap_is_not_strong_risk_ban(self) -> None:
        row = {
            "scorecard_context": {
                "allocation_entry": True,
                "rebalance_reasons": ["weak_repair_trap_cap"],
            },
        }
        self.assertFalse(strong_risk_ban(row))

    def test_legacy_systemic_risk_flag_counts_as_strong_risk_ban(self) -> None:
        self.assertTrue(
            strong_risk_ban(
                {"active_risk_flags": ["domestic_liquidity_stress_flag"]}
            )
        )

    def test_active_hard_exit_counts_as_strong_risk_ban(self) -> None:
        row = {
            "exposure_formation": {
                "trace": [
                    {
                        "stage": "hard_exit",
                        "active": True,
                        "details": {
                            "flags": ["theme_macro_contraction_divergence_flag"],
                        },
                    }
                ]
            }
        }
        self.assertTrue(strong_risk_ban(row))

    def test_pct_chg_compounded_series_neutralizes_price_split(self) -> None:
        rows = [
            (date(2021, 1, 22), 0.0),
            (date(2021, 1, 25), 0.7007),
            (date(2021, 1, 26), -1.0),
        ]
        series = pct_chg_compounded_series(rows)
        self.assertAlmostEqual(series[0][1], 100.0)
        self.assertAlmostEqual(series[1][1], 100.7007)
        self.assertAlmostEqual(series[2][1], 99.693693, places=6)

    def test_report_result_accepts_backtest_and_screen_shapes(self) -> None:
        backtest_payload = {"results": [{"cases": []}, {"cases": [{"id": 2}]}]}
        self.assertEqual(report_result(backtest_payload, 1), {"cases": [{"id": 2}]})

        screen_payload = {"mode": "fast_screen", "cases": [{"id": 1}]}
        self.assertEqual(report_result(screen_payload, 0), screen_payload)


if __name__ == "__main__":
    unittest.main()

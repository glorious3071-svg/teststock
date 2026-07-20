from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.screen_scorecard_csi_strict_quarterly_etf_candidate import (
    failed_structural_case_pairs,
    parse_case_pair,
    summarize_screen_cases,
)


class StrictQuarterlyFastScreenTest(unittest.TestCase):
    def _case(self, phase: int = 0, lag: int = 0, target_met: bool = True) -> dict:
        return {
            "phase_month_offset": phase,
            "execution_lag_days": lag,
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
        }

    def test_parse_case_pair_accepts_colon_and_comma(self) -> None:
        self.assertEqual(parse_case_pair("1:3"), (1, 3))
        self.assertEqual(parse_case_pair("2,5"), (2, 5))

    def test_failed_structural_case_pairs_are_deduplicated_and_sorted(self) -> None:
        payload = {
            "structural_capture": {
                "failed_structural_cases": [
                    {"phase_month_offset": 4, "execution_lag_days": 0},
                    {"phase_month_offset": 1, "execution_lag_days": 3},
                    {"phase_month_offset": 4, "execution_lag_days": 0},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "adaptation.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(failed_structural_case_pairs(path), [(1, 3), (4, 0)])

    def test_partial_summary_can_pass_without_claiming_full_objective(self) -> None:
        summary = summarize_screen_cases([self._case(1, 3), self._case(4, 0)])
        self.assertTrue(summary["screen_passed"])
        self.assertTrue(summary["partial_matrix"])
        self.assertFalse(summary["objective_met"])
        self.assertFalse(summary["case_matrix"]["matrix_complete"])

    def test_partial_summary_fails_when_any_case_fails(self) -> None:
        summary = summarize_screen_cases([self._case(1, 3), self._case(4, 0, False)])
        self.assertFalse(summary["screen_passed"])
        self.assertEqual(summary["pass_count"], 1)


if __name__ == "__main__":
    unittest.main()

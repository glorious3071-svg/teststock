import unittest
from datetime import date

from backtest.phase_schedule import shift_month_end
from scripts.search_passive_etf_walkforward_ridge import path_summary


class PassiveEtfStrictPhaseScreenTest(unittest.TestCase):
    def test_offline_screen_keeps_fixed_strict_phase_anchors(self) -> None:
        predictions = []
        current = date(2005, 5, 31)
        end = date(2026, 4, 30)
        while current <= end:
            predictions.append(
                {
                    "snapshot": current.isoformat(),
                    "basket_return": 0.01,
                    "basket_average_drawdown": -0.02,
                }
            )
            current = shift_month_end(current, 1)

        summary = path_summary(predictions)

        self.assertEqual(summary["strict_anchor"], "2005-02-28")
        self.assertEqual(
            [case["start_snapshot"] for case in summary["cases"]],
            [
                shift_month_end(date(2005, 2, 28), phase).isoformat()
                for phase in range(12)
            ],
        )
        self.assertEqual(summary["cases"][0]["missing_snapshots"], ["2005-02-28"])
        self.assertEqual(summary["cases"][1]["missing_snapshots"], ["2005-03-31"])
        self.assertEqual(summary["cases"][2]["missing_snapshots"], ["2005-04-30"])
        self.assertEqual(summary["cases"][3]["missing_snapshot_count"], 0)
        self.assertAlmostEqual(summary["cases"][0]["capital_factor"], 1.01**79)
        self.assertAlmostEqual(summary["cases"][3]["capital_factor"], 1.01**80)

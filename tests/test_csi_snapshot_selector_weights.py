from __future__ import annotations

import unittest

from backtest.csi_snapshot_selector import capped_score_power_weights


class CsiSnapshotSelectorWeightsTest(unittest.TestCase):
    def test_weight_cap_redistributes_excess_proportionally(self) -> None:
        selected = [
            {"ts_code": "A", "selector_score": 10.0},
            {"ts_code": "B", "selector_score": 1.0},
            {"ts_code": "C", "selector_score": 1.0},
        ]
        weights = capped_score_power_weights(selected, 1.0, 0.50)
        self.assertAlmostEqual(weights["A"], 0.50)
        self.assertAlmostEqual(weights["B"], 0.25)
        self.assertAlmostEqual(weights["C"], 0.25)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_infeasible_cap_relaxes_only_to_equal_weight(self) -> None:
        selected = [
            {"ts_code": "A", "selector_score": 10.0},
            {"ts_code": "B", "selector_score": 1.0},
        ]
        weights = capped_score_power_weights(selected, 1.0, 0.40)
        self.assertEqual(weights, {"A": 0.5, "B": 0.5})

    def test_default_cap_preserves_score_power_weighting(self) -> None:
        selected = [
            {"ts_code": "A", "selector_score": 2.0},
            {"ts_code": "B", "selector_score": 1.0},
        ]
        weights = capped_score_power_weights(selected, 2.0, 1.0)
        self.assertAlmostEqual(weights["A"], 0.8)
        self.assertAlmostEqual(weights["B"], 0.2)


if __name__ == "__main__":
    unittest.main()

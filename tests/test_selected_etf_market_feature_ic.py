from __future__ import annotations

import unittest
from datetime import date

from scripts.analyze_selected_etf_market_feature_ic import drift_observations
from scripts.search_passive_etf_absolute_exposure import selected_observations


class SelectedEtfMarketFeatureIcTest(unittest.TestCase):
    def test_unfinished_forward_window_is_not_used_as_a_label(self) -> None:
        row = {
            "snapshot": "2026-06-30",
            "end_snapshot": "2026-09-30",
            "ts_code": "510300.SH",
            "forward_return_3m": None,
            "forward_max_drawdown_3m": None,
        }
        self.assertEqual(selected_observations([row], "v5"), [])

    def test_builds_twelve_exact_three_month_paths(self) -> None:
        rows = []
        for month_index in range(251):
            year = 2005 + (4 + month_index) // 12
            month = (4 + month_index) % 12 + 1
            rows.append(
                {
                    "snapshot": date(year, month, 1).isoformat(),
                    "end_snapshot": date(year, month, 2).isoformat(),
                    "selected_codes": ["510300.SH"],
                    "feature": float(month_index),
                    "forward_return_3m": 0.01,
                    "forward_max_drawdown_3m": -0.02,
                }
            )
        observations = drift_observations(rows)
        self.assertEqual(len(observations), 12 * 80)
        for phase in range(12):
            path = [row for row in observations if row["phase_month_offset"] == phase]
            self.assertEqual(len(path), 80)
            first = date.fromisoformat(path[0]["decision_date"])
            second = date.fromisoformat(path[1]["decision_date"])
            self.assertEqual((second.year - first.year) * 12 + second.month - first.month, 3)

    def test_missing_selected_features_remain_missing_with_explicit_coverage(self) -> None:
        row = {
            "snapshot": "2018-01-31",
            "end_snapshot": "2018-04-30",
            "ts_code": "510270.SH",
            "index_code": "000056.SH",
            "market_regime": "bull",
            "market_return_6m": None,
            "momentum_3m": 0.06,
            "distance_high_12m": -0.03,
            "market_beta_6m": 0.41,
            "return_autocorrelation_3m": -0.15,
            "volatility_3m": 0.16,
            "ulcer_index_6m": 0.02,
            "forward_return_3m": -0.11,
            "forward_max_drawdown_3m": -0.17,
        }
        selected = selected_observations([row], "v5")
        self.assertEqual(len(selected), 1)
        observation = selected[0]
        self.assertIsNone(observation["selected_index_fundamental_roe_proxy"])
        self.assertEqual(
            observation["selected_index_fundamental_roe_proxy_coverage"], 0.0
        )
        self.assertIsNone(observation["median_index_fundamental_roe_proxy"])
        self.assertIsNone(observation["spread_index_fundamental_roe_proxy"])
        self.assertEqual(observation["coverage_index_fundamental_roe_proxy"], 0.0)
        self.assertIsNone(observation["market_return_6m"])


if __name__ == "__main__":
    unittest.main()

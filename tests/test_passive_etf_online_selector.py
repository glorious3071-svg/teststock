from __future__ import annotations

import unittest
from datetime import date

from backtest.passive_etf_online_selector import learned_online_feature_weights


class PassiveEtfOnlineSelectorTest(unittest.TestCase):
    def test_future_holding_window_labels_are_not_used(self) -> None:
        observations = []
        for month in range(1, 7):
            observations.append(
                {
                    "feature": "market_beta_6m",
                    "market_regime": "neutral",
                    "end_snapshot": f"2020-{month:02d}-28",
                    "ic": -0.10,
                }
            )
        observations.append(
            {
                "feature": "market_beta_6m",
                "market_regime": "neutral",
                "end_snapshot": "2021-01-31",
                "ic": 1.0,
            }
        )
        learned = learned_online_feature_weights(
            observations,
            date(2020, 6, 30),
            "neutral",
            min_history_periods=5,
        )
        self.assertEqual(learned["market_beta_6m"]["orientation"], -1.0)
        self.assertEqual(learned["market_beta_6m"]["history_count"], 6.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date, timedelta

from scripts.analyze_strict_quarterly_market_feature_ic import (
    collect_observations,
    feature_screen,
)


class StrictMarketFeatureIcTest(unittest.TestCase):
    def test_forward_label_uses_frozen_shares_not_daily_constant_weights(self) -> None:
        days = [date(2020, 1, 1) + timedelta(days=index) for index in range(41)]
        prices_a = [1.0, 2.0, 1.0, *([1.0] * 38)]
        prices_b = [1.0] * 41
        path = {
            "phase": 0,
            "daily": [
                {
                    "window_start": index == 0,
                    "previous_day": days[index],
                    "day": days[index + 1],
                    "equity_etf_weights": {"ETF_A": 0.5, "ETF_B": 0.5},
                    "market_state": {"signal": 1.0},
                }
                for index in range(40)
            ],
        }
        observations = collect_observations(
            path,
            {
                "ETF_A": list(zip(days, prices_a)),
                "ETF_B": list(zip(days, prices_b)),
            },
        )
        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0]["forward_risk_return_3m"], 0.0)

    def test_monotonic_coverage_proxy_is_flagged_as_time_trend(self) -> None:
        observations = [
            {
                "phase_month_offset": 0,
                "decision_date": (date(2005, 1, 1) + timedelta(days=90 * index)).isoformat(),
                "era": "2005_2012",
                "features": {"candidate_count": float(index)},
                "forward_risk_return_3m": float(index),
            }
            for index in range(10)
        ]
        result = feature_screen(observations, "forward_risk_return_3m")[0]
        self.assertTrue(result["time_trend_leakage_flag"])
        self.assertAlmostEqual(result["median_abs_time_spearman"], 1.0)


if __name__ == "__main__":
    unittest.main()

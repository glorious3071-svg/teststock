import unittest
from datetime import date

from backtest.monthly_online_selector import (
    OnlineCrossSectionSelector,
    OnlineRidgeCrossSectionSelector,
    OnlineRidgeSelectorConfig,
    OnlineSelectorConfig,
)


class MonthlyOnlineSelectorTest(unittest.TestCase):
    def test_future_label_is_not_released_early(self):
        config = OnlineSelectorConfig(
            top_n=1,
            min_history_months=1,
            history_months=2,
            min_abs_median_ic=0.0,
            min_direction_consistency=0.0,
            features=("trend_6m",),
        )
        selector = OnlineCrossSectionSelector(config)
        rows = [
            {"ts_code": str(index), "trend_6m": float(index), "weight": 0.2}
            for index in range(5)
        ]
        selector.queue_observation(
            date(2024, 2, 5),
            rows,
            {str(index): float(index) for index in range(5)},
        )
        selector.release_known(date(2024, 1, 31))
        selected, diagnostics = selector.select(rows, rows[:1])
        self.assertEqual(diagnostics["mode"], "fallback")
        self.assertEqual(selected[0]["ts_code"], "0")

        selector.release_known(date(2024, 2, 5))
        selected, diagnostics = selector.select(rows, rows[:1])
        self.assertEqual(diagnostics["mode"], "online")
        self.assertEqual(selected[0]["ts_code"], "4")

    def test_negative_history_reverses_current_rank(self):
        config = OnlineSelectorConfig(
            top_n=1,
            min_history_months=1,
            history_months=2,
            min_abs_median_ic=0.0,
            min_direction_consistency=0.0,
            features=("trend_6m",),
        )
        selector = OnlineCrossSectionSelector(config)
        rows = [
            {"ts_code": str(index), "trend_6m": float(index), "weight": 0.2}
            for index in range(5)
        ]
        selector.queue_observation(
            date(2024, 1, 31),
            rows,
            {str(index): -float(index) for index in range(5)},
        )
        selector.release_known(date(2024, 1, 31))
        selected, _diagnostics = selector.select(rows, rows[:1])
        self.assertEqual(selected[0]["ts_code"], "0")

    def test_ridge_selector_releases_labels_only_after_period_end(self):
        config = OnlineRidgeSelectorConfig(
            top_n=1,
            min_history_periods=1,
            history_periods=2,
            ridge_alpha=0.1,
            features=("trend_6m",),
        )
        selector = OnlineRidgeCrossSectionSelector(config)
        rows = [
            {"ts_code": str(index), "trend_6m": float(index), "weight": 0.2}
            for index in range(5)
        ]
        selector.queue_observation(
            date(2024, 2, 5),
            rows,
            {str(index): float(index) for index in range(5)},
        )
        selector.release_known(date(2024, 1, 31))
        selected, diagnostics = selector.select(rows, rows[:1])
        self.assertEqual(diagnostics["mode"], "fallback")
        self.assertEqual(selected[0]["ts_code"], "0")

        selector.release_known(date(2024, 2, 5))
        selected, diagnostics = selector.select(rows, rows[:1])
        self.assertEqual(diagnostics["mode"], "online_ridge")
        self.assertEqual(selected[0]["ts_code"], "4")


if __name__ == "__main__":
    unittest.main()

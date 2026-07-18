import unittest
from datetime import date

from scripts.analyze_csi_quarterly_feature_ic import (
    complete_period_returns,
    execution_boundary,
)


class CsiQuarterlyFeatureIcTest(unittest.TestCase):
    def test_execution_boundary_matches_strict_post_signal_lags(self) -> None:
        days = [
            date(2024, 1, 30),
            date(2024, 1, 31),
            date(2024, 2, 1),
            date(2024, 2, 2),
            date(2024, 2, 5),
            date(2024, 2, 6),
            date(2024, 2, 7),
        ]

        self.assertEqual(
            execution_boundary(days, date(2024, 1, 31), 0),
            date(2024, 2, 1),
        )
        self.assertEqual(
            execution_boundary(days, date(2024, 1, 31), 3),
            date(2024, 2, 6),
        )

    def test_missing_boundary_price_is_not_labeled_zero_return(self) -> None:
        start = date(2024, 1, 31)
        end = date(2024, 4, 30)
        series = {
            "COMPLETE": [(start, 100.0), (end, 110.0)],
            "STARTS_LATE": [(end, 100.0)],
            "ENDS_EARLY": [(start, 100.0)],
        }
        rows = [
            {"ts_code": "COMPLETE"},
            {"ts_code": "STARTS_LATE"},
            {"ts_code": "ENDS_EARLY"},
            {"ts_code": "ABSENT"},
        ]

        outcomes = complete_period_returns(series, rows, start, end)

        self.assertEqual(set(outcomes), {"COMPLETE"})
        self.assertAlmostEqual(outcomes["COMPLETE"], 0.10)


if __name__ == "__main__":
    unittest.main()

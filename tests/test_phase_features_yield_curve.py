from __future__ import annotations

import unittest
from datetime import date

from backtest.phase_features import DatedSeries, aligned_difference_series


class PhaseFeaturesYieldCurveTest(unittest.TestCase):
    def test_aligned_difference_drops_unmatched_dates(self) -> None:
        left = DatedSeries.from_rows(
            [(date(2024, 1, 2), 2.5), (date(2024, 1, 3), 2.6)]
        )
        right = DatedSeries.from_rows(
            [(date(2024, 1, 3), 1.8), (date(2024, 1, 4), 1.9)]
        )
        result = aligned_difference_series(left, right)
        self.assertEqual(result.dates, (date(2024, 1, 3),))
        self.assertAlmostEqual(result.values[0], 0.8)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date

from backtest.scorecard_adapter import observable_macro_months


class ScorecardAdapterPointInTimeTest(unittest.TestCase):
    def test_month_end_macro_rows_without_release_dates_use_prior_month(self) -> None:
        self.assertEqual(
            observable_macro_months(date(2025, 3, 31)),
            {"pmi": "202502", "ppi": "202502"},
        )
        self.assertEqual(
            observable_macro_months(date(2025, 1, 31)),
            {"pmi": "202412", "ppi": "202412"},
        )


if __name__ == "__main__":
    unittest.main()

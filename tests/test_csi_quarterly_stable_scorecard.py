import unittest

from scripts.search_csi_quarterly_stable_scorecard import Component, rank


class CsiQuarterlyStableScorecardTest(unittest.TestCase):
    def test_rank_orders_by_feature_value_before_code(self) -> None:
        rows = [
            {"ts_code": "000001.SH", "signal": 10.0},
            {"ts_code": "999999.SH", "signal": 1.0},
            {"ts_code": "500000.SH", "signal": 5.0},
        ]

        higher = rank(rows, Component("signal", 1.0, True))
        lower = rank(rows, Component("signal", 1.0, False))

        self.assertEqual(
            higher,
            {
                "000001.SH": 1.0,
                "999999.SH": 0.0,
                "500000.SH": 0.5,
            },
        )
        self.assertEqual(
            lower,
            {
                "000001.SH": 0.0,
                "999999.SH": 1.0,
                "500000.SH": 0.5,
            },
        )

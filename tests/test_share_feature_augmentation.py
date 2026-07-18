import datetime as dt
import math
import unittest

from scripts.augment_passive_etf_dataset_with_share_features import (
    ReturnLookup,
    adjusted_subscription_flow,
    build_features,
    observations_available,
    prior_observation,
)


class ShareFeatureAugmentationTest(unittest.TestCase):
    def setUp(self):
        self.history = [
            {
                "trade_date": dt.date(2020, 3, 31),
                "available_date": dt.date(2020, 4, 1),
                "total_share_wan": 100.0,
                "total_size_wan": 100.0,
            },
            {
                "trade_date": dt.date(2020, 6, 30),
                "available_date": dt.date(2020, 7, 1),
                "total_share_wan": 120.0,
                "total_size_wan": 132.0,
            },
        ]

    def test_future_next_morning_record_is_not_available(self):
        usable = observations_available(
            self.history, dt.date(2020, 6, 30), dt.date(2020, 1, 1)
        )
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0]["trade_date"], dt.date(2020, 3, 31))

    def test_pre_listing_record_is_not_available(self):
        usable = observations_available(
            self.history, dt.date(2020, 7, 1), dt.date(2020, 5, 1)
        )
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0]["trade_date"], dt.date(2020, 6, 30))

    def test_flow_removes_price_return_from_size_growth(self):
        # Size rose 32%, price rose 10%, leaving 20% subscription growth.
        self.assertAlmostEqual(adjusted_subscription_flow(132.0, 100.0, 0.10), 0.20)

    def test_quarter_lag_ignores_intermediate_monthly_observations(self):
        available = [
            {"trade_date": dt.date(2020, month, 28)}
            for month in (1, 2, 3, 4)
        ]
        self.assertEqual(
            prior_observation(available, 3)["trade_date"],
            dt.date(2020, 1, 28),
        )

    def test_build_features_uses_compounded_pct_change(self):
        returns = ReturnLookup(
            [
                ("510300.SH", dt.date(2020, 4, 1), 5.0),
                ("510300.SH", dt.date(2020, 6, 30), 100.0 / 21.0),
            ]
        )
        features = build_features(
            "510300.SH",
            dt.date(2020, 7, 1),
            dt.date(2020, 1, 1),
            self.history,
            returns,
        )
        self.assertAlmostEqual(features["etf_share_growth_1q"], 0.20)
        self.assertAlmostEqual(features["etf_subscription_flow_1q"], 0.20)
        self.assertTrue(math.isfinite(features["etf_size_log_total_wan"]))


if __name__ == "__main__":
    unittest.main()

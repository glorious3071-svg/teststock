from __future__ import annotations

import unittest
from datetime import date, timedelta

from backtest.phase_features import (
    DatedSeries,
    PhaseFeatureStore,
    aggregate_etf_share_growth_features,
    moving_average_distance,
    realized_volatility,
    rolling_volatility_percentile,
    rolling_drawdown,
    trailing_return,
)


class FakeMacroCursor:
    def __init__(self, rows):
        self.rows = rows
        self.indicator = None

    def execute(self, _sql, params):
        self.indicator = params[0]

    def fetchall(self):
        return self.rows.get(self.indicator, [])


class PhaseFeaturesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.series = DatedSeries.from_rows(
            [
                (date(2024, 1, 2), 100.0),
                (date(2024, 1, 3), 110.0),
                (date(2024, 1, 4), 99.0),
            ]
        )

    def test_domestic_cutoff_can_include_snapshot_close(self) -> None:
        self.assertEqual(
            self.series.trailing(date(2024, 1, 3), 3, include_snapshot=True),
            (100.0, 110.0),
        )

    def test_external_cutoff_excludes_snapshot_close(self) -> None:
        self.assertEqual(
            self.series.trailing(date(2024, 1, 3), 3, include_snapshot=False),
            (100.0,),
        )

    def test_processed_features(self) -> None:
        values = [100.0, 110.0, 99.0]
        self.assertAlmostEqual(trailing_return(values, 2) or 0.0, -0.01)
        self.assertAlmostEqual(rolling_drawdown(values, 3) or 0.0, -0.10)
        self.assertAlmostEqual(moving_average_distance(values, 3) or 0.0, 99.0 / 103.0 - 1.0)
        self.assertIsNotNone(realized_volatility(values, 2))
        self.assertIsNotNone(rolling_volatility_percentile([float(x) for x in range(1, 11)], 2, 5))

    def test_domestic_macro_uses_prior_month_and_rolling_windows(self) -> None:
        rows = {
            "sf_inc_month": [
                (date(2022 + index // 12, index % 12 + 1, 1), 100.0 + index)
                for index in range(36)
            ],
            "pmi_mfg": [
                (date(2024, month, 1), 49.0 + month)
                for month in range(1, 7)
            ],
            "ppi_yoy": [(date(2024, month, 1), float(month)) for month in range(1, 7)],
            "cpi_yoy": [(date(2024, month, 1), float(month) / 2.0) for month in range(1, 7)],
            "margin_balance": [
                (date(2024, month, 1), 100.0 + month)
                for month in range(1, 7)
            ],
        }
        features = PhaseFeatureStore().domestic_macro_features(
            FakeMacroCursor(rows),
            date(2024, 6, 30),
        )
        self.assertEqual(features["domestic_pmi_mfg_level"], 54.0)
        self.assertEqual(features["domestic_pmi_mfg_change_3m"], 3.0)
        self.assertEqual(features["pmi_extreme_growth_reversal_flag"], 1.0)
        self.assertAlmostEqual(features["domestic_ppi_cpi_scissors"], 2.5)
        self.assertIsNotNone(features["domestic_sf_rolling_12m_growth"])
        self.assertAlmostEqual(
            features["domestic_margin_balance_return_1m"],
            105.0 / 104.0 - 1.0,
        )

    def test_extreme_growth_reversal_flag_uses_fixed_pmi_threshold(self) -> None:
        store = PhaseFeatureStore()
        below = store.domestic_macro_features(
            FakeMacroCursor({"pmi_mfg": [(date(2024, 5, 1), 53.9)]}),
            date(2024, 6, 30),
        )
        at_threshold = PhaseFeatureStore().domestic_macro_features(
            FakeMacroCursor({"pmi_mfg": [(date(2024, 5, 1), 54.0)]}),
            date(2024, 6, 30),
        )

        self.assertEqual(below["pmi_extreme_growth_reversal_flag"], 0.0)
        self.assertEqual(at_threshold["pmi_extreme_growth_reversal_flag"], 1.0)

    def test_stale_option_series_is_not_forward_filled(self) -> None:
        start = date(2020, 1, 1)
        series = DatedSeries.from_rows(
            (start + timedelta(days=index), 1.0 + index / 1000.0)
            for index in range(800)
        )
        store = PhaseFeatureStore()
        store._option_put_call_volume = series
        store._option_put_call_oi = series
        snapshot = series.dates[-1] + timedelta(days=46)

        features = store.option_sentiment_features(None, snapshot)

        self.assertIsNone(features["domestic_option_put_call_volume_21d"])
        self.assertEqual(features["domestic_option_put_call_volume_age_days"], 46)

    def test_fund_issuance_uses_only_prior_month(self) -> None:
        store = PhaseFeatureStore()
        store._fund_issuance_monthly = [
            (f"{2021 + index // 12:04d}{index % 12 + 1:02d}", 100.0 + index, 50.0 + index, 1.0)
            for index in range(48)
        ]

        features = store.fund_issuance_features(None, date(2024, 1, 31))

        self.assertEqual(features["fund_active_issuance_billion"], 85.0)
        self.assertEqual(features["fund_active_issuance_percentile_3y"], 1.0)

    def test_etf_share_growth_excludes_same_day_next_morning_record(self) -> None:
        histories = {
            f"51030{index}.SH": (
                (date(2023, 12, 29), date(2024, 3, 29)),
                (date(2023, 12, 30), date(2024, 3, 30)),
                (100.0, 110.0 + index),
            )
            for index in range(30)
        }
        before = aggregate_etf_share_growth_features(
            histories, date(2024, 3, 29)
        )
        after = aggregate_etf_share_growth_features(
            histories, date(2024, 3, 30)
        )

        self.assertIsNone(before["etf_share_growth_1q_positive_ratio"])
        self.assertEqual(after["etf_share_growth_1q_positive_ratio"], 1.0)

    def test_etf_share_growth_uses_three_calendar_month_lag(self) -> None:
        histories = {
            f"51030{index}.SH": (
                (
                    date(2024, 1, 31), date(2024, 2, 29),
                    date(2024, 3, 29), date(2024, 4, 30),
                ),
                (
                    date(2024, 2, 1), date(2024, 3, 1),
                    date(2024, 3, 30), date(2024, 5, 1),
                ),
                (100.0, 80.0, 90.0, 120.0 + index),
            )
            for index in range(30)
        }
        features = aggregate_etf_share_growth_features(
            histories, date(2024, 5, 1)
        )
        self.assertEqual(features["etf_share_growth_1q_positive_ratio"], 1.0)

    def test_etf_share_growth_excludes_stale_cross_section_members(self) -> None:
        histories = {
            f"51030{index}.SH": (
                (date(2023, 12, 29), date(2024, 3, 29)),
                (date(2023, 12, 30), date(2024, 3, 30)),
                (100.0, 110.0 + index),
            )
            for index in range(29)
        }
        histories["510399.SH"] = (
            (date(2023, 9, 29), date(2023, 12, 29)),
            (date(2023, 9, 30), date(2023, 12, 30)),
            (100.0, 120.0),
        )

        features = aggregate_etf_share_growth_features(
            histories, date(2024, 5, 1)
        )

        self.assertIsNone(features["etf_share_growth_1q_positive_ratio"])
        self.assertEqual(features["etf_share_growth_1q_candidate_count"], 29.0)
        self.assertLessEqual(
            features["etf_share_growth_1q_max_observation_age_days"], 45.0
        )

    def test_etf_share_growth_excludes_horizons_longer_than_four_months(self) -> None:
        histories = {
            f"51030{index}.SH": (
                (date(2023, 9, 29), date(2024, 3, 29)),
                (date(2023, 9, 30), date(2024, 3, 30)),
                (100.0, 110.0 + index),
            )
            for index in range(30)
        }

        features = aggregate_etf_share_growth_features(
            histories, date(2024, 3, 31)
        )

        self.assertIsNone(features["etf_share_growth_1q_positive_ratio"])
        self.assertEqual(features["etf_share_growth_1q_candidate_count"], 0.0)


if __name__ == "__main__":
    unittest.main()

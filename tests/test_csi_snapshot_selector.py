from __future__ import annotations

import unittest
from datetime import date, timedelta

from backtest.csi_snapshot_selector import (
    MONTHLY_TREND6_RISK_TOP10,
    SnapshotCSISelector,
    geometric_trend_acceleration,
    percentile_ranks,
)


class SnapshotSelectorTest(unittest.TestCase):
    def test_geometric_trend_acceleration_is_zero_for_equal_quarters(self) -> None:
        quarterly_return = 0.10
        six_month_return = (1.0 + quarterly_return) ** 2 - 1.0
        self.assertAlmostEqual(
            geometric_trend_acceleration(quarterly_return, six_month_return),
            0.0,
        )
        self.assertIsNone(geometric_trend_acceleration(0.10, -1.0))

    def test_percentile_ranks_are_deterministic(self) -> None:
        self.assertEqual(percentile_ranks({"b": 2.0, "a": 1.0, "c": 3.0}), {"a": 0.0, "b": 0.5, "c": 1.0})

    def test_lower_values_can_rank_better(self) -> None:
        self.assertEqual(
            percentile_ranks({"a": 1.0, "b": 2.0}, higher_is_better=False),
            {"a": 1.0, "b": 0.0},
        )

    def test_investable_filter_is_applied_before_ranking(self) -> None:
        selector = SnapshotCSISelector()
        selector.candidate_rows = lambda _cur, _snapshot: [
            {
                "ts_code": code,
                "momentum_12m": 0.1,
                "momentum_6m": 0.1,
                "momentum_3m": 0.1,
                "volatility_3m": volatility,
                "drawdown_6m": drawdown,
                "trend_6m": trend,
            }
            for code, volatility, drawdown, trend in (
                ("A", 0.1, -0.1, 0.9),
                ("B", 0.2, -0.2, 0.6),
                ("C", 0.3, -0.3, 0.3),
            )
        ]

        selected = selector.select(
            None,
            date(2020, 1, 1),
            MONTHLY_TREND6_RISK_TOP10,
            eligible_codes={"B", "C"},
        )

        self.assertEqual([row["ts_code"] for row in selected], ["B", "C"])
        self.assertAlmostEqual(sum(row["weight"] for row in selected), 1.0)

    def test_fundamental_proxies_use_only_rows_at_or_before_snapshot(self) -> None:
        class FakeCursor:
            def __init__(self, rows):
                self.rows = rows
                self.mode = "basic"

            def execute(self, sql, _params):
                self.mode = "price" if "FROM index_daily\n" in sql else "basic"

            def fetchall(self):
                if self.mode == "price":
                    return [(row[0], row[4]) for row in self.rows]
                return [(row[0], row[1], row[2], row[3]) for row in self.rows]

        start = date(2023, 1, 1)
        rows = [
            (
                start + timedelta(days=index),
                10.0 + index / 100.0,
                2.0 + index / 1000.0,
                1.0,
                100.0 + index,
            )
            for index in range(220)
        ]
        snapshot_index = 180
        snapshot = rows[snapshot_index][0]
        expected_current = rows[snapshot_index]
        expected_prior = rows[snapshot_index - 63]
        selector = SnapshotCSISelector()
        features = selector._dailybasic_features(FakeCursor(rows), "TEST", snapshot)

        self.assertAlmostEqual(
            features["fundamental_earnings_yield"],
            1.0 / expected_current[1],
        )
        self.assertAlmostEqual(
            features["fundamental_earnings_growth_3m"],
            (expected_current[4] / expected_current[1])
            / (expected_prior[4] / expected_prior[1])
            - 1.0,
        )

        changed = list(rows)
        changed[-1] = (changed[-1][0], 0.01, 0.01, 99.0, 99999.0)
        perturbed = SnapshotCSISelector()._dailybasic_features(
            FakeCursor(changed), "TEST", snapshot
        )
        self.assertEqual(features, perturbed)

    def test_constituent_fundamentals_ignore_future_snapshots(self) -> None:
        class FakeCursor:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, _sql, _params):
                pass

            def fetchall(self):
                return list(self.rows)

        rows = [
            (date(2022, 3, 31), 0.05, 0.40, 0.125, 0.02, 0.90, 0.02),
            (date(2023, 3, 31), 0.06, 0.42, 0.143, 0.025, 0.92, 0.018),
            (date(2024, 3, 31), 9.99, 9.99, 9.99, 9.99, 9.99, 9.99),
        ]
        snapshot = date(2023, 6, 30)
        features = SnapshotCSISelector()._constituent_fundamental_features(
            FakeCursor(rows), "TEST", snapshot
        )
        self.assertEqual(features["constituent_earnings_yield"], 0.06)
        self.assertAlmostEqual(
            features["constituent_earnings_yield_change_12m"],
            0.06 / 0.05 - 1.0,
        )

        changed = list(rows)
        changed[-1] = (date(2024, 3, 31), -9.99, -9.99, -9.99, -9.99, -9.99, -9.99)
        perturbed = SnapshotCSISelector()._constituent_fundamental_features(
            FakeCursor(changed), "TEST", snapshot
        )
        self.assertEqual(features, perturbed)


if __name__ == "__main__":
    unittest.main()

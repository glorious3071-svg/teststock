import datetime as dt
import unittest

from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    EquityEtfMeta,
    _is_overseas,
    has_recent_etf_price,
    map_indices_to_etfs,
    portfolio_turnover,
    select_direct_equity_etfs,
)


class DomesticEquityEtfTest(unittest.TestCase):
    def test_stale_price_history_is_not_investable(self):
        snapshot = dt.date(2020, 1, 31)
        series = {
            "STALE": [(dt.date(2013, 12, 31), 100.0)],
            "FRESH": [(dt.date(2020, 1, 23), 100.0)],
        }
        self.assertFalse(has_recent_etf_price(series, "STALE", snapshot))
        self.assertTrue(has_recent_etf_price(series, "FRESH", snapshot))

        metas = {
            "INDEX": [
                EquityEtfMeta(
                    "STALE", "stale", "INDEX", "index", dt.date(2010, 1, 1), dt.date(2010, 1, 4)
                ),
                EquityEtfMeta(
                    "FRESH", "fresh", "INDEX", "index", dt.date(2015, 1, 1), dt.date(2015, 1, 2)
                ),
            ]
        }
        self.assertEqual(
            map_indices_to_etfs(
                {"INDEX": 1.0}, snapshot, metas, etf_series=series
            ),
            {"FRESH": 1.0},
        )

    def test_mapping_uses_oldest_point_in_time_tracker(self):
        metas = {
            "INDEX": [
                EquityEtfMeta("OLD", "old", "INDEX", "index", dt.date(2010, 1, 1), dt.date(2010, 1, 4)),
                EquityEtfMeta("NEW", "new", "INDEX", "index", dt.date(2020, 1, 1), dt.date(2020, 1, 2)),
            ]
        }
        self.assertEqual(map_indices_to_etfs({"INDEX": 1.0}, dt.date(2015, 1, 1), metas), {"OLD": 1.0})

    def test_mapping_rejects_future_etf(self):
        metas = {
            "INDEX": [
                EquityEtfMeta("FUTURE", "future", "INDEX", "index", dt.date(2020, 1, 1), dt.date(2020, 1, 2)),
            ]
        }
        with self.assertRaises(RuntimeError):
            map_indices_to_etfs({"INDEX": 1.0}, dt.date(2015, 1, 1), metas)

    def test_early_broad_proxy_requires_explicit_opt_in_and_is_point_in_time(self):
        metas = {
            "000016.SH": [
                EquityEtfMeta(
                    "510050.SH",
                    "SSE 50 ETF",
                    "000016.SH",
                    "SSE 50",
                    dt.date(2005, 2, 1),
                    dt.date(2005, 2, 23),
                )
            ]
        }
        with self.assertRaises(RuntimeError):
            map_indices_to_etfs({"000300.SH": 1.0}, dt.date(2005, 7, 31), metas)
        self.assertEqual(
            map_indices_to_etfs(
                {"000300.SH": 1.0},
                dt.date(2005, 7, 31),
                metas,
                allow_early_broad_proxy=True,
            ),
            {"510050.SH": 1.0},
        )
        with self.assertRaises(RuntimeError):
            map_indices_to_etfs(
                {"000300.SH": 1.0},
                dt.date(2005, 1, 31),
                metas,
                allow_early_broad_proxy=True,
            )

    def test_correlation_proxy_uses_only_live_domestic_tracker(self):
        start = dt.date(2020, 1, 1)
        days = [start + dt.timedelta(days=index) for index in range(50)]
        target = [100.0]
        close_match = [100.0]
        inverse = [100.0]
        for index in range(1, len(days)):
            move = 0.01 if index % 2 else -0.004
            target.append(target[-1] * (1.0 + move))
            close_match.append(close_match[-1] * (1.0 + move))
            inverse.append(inverse[-1] * (1.0 - move))
        metas = {
            "A_INDEX": [
                EquityEtfMeta("A_ETF", "a", "A_INDEX", "a", start, start),
            ],
            "B_INDEX": [
                EquityEtfMeta("B_ETF", "b", "B_INDEX", "b", start, start),
            ],
        }
        index_series = {
            "TARGET": list(zip(days, target)),
            "A_INDEX": list(zip(days, inverse)),
            "B_INDEX": list(zip(days, close_match)),
        }
        self.assertEqual(
            map_indices_to_etfs(
                {"TARGET": 1.0},
                days[-1],
                metas,
                allow_correlation_proxy=True,
                index_series=index_series,
            ),
            {"B_ETF": 1.0},
        )

    def test_turnover_includes_cash_leg(self):
        self.assertAlmostEqual(portfolio_turnover({}, {"A": 1.0}), 1.0)
        self.assertAlmostEqual(portfolio_turnover({"A": 1.0}, {"B": 1.0}), 1.0)
        self.assertAlmostEqual(portfolio_turnover({"A": 0.5}, {"A": 0.5}), 0.0)

    def test_hong_kong_exposure_is_excluded(self):
        self.assertTrue(_is_overseas("517350.SH", "科技ETF", "科技指数", "INDEX"))
        self.assertTrue(_is_overseas("510000.SH", "沪港深ETF", "科技指数", "INDEX"))

    def test_direct_selector_never_uses_future_listed_etf(self):
        start = dt.date(2019, 1, 1)
        days = [start + dt.timedelta(days=index) for index in range(320)]
        metas = {
            "OLD_INDEX": [
                EquityEtfMeta("OLD", "old", "OLD_INDEX", "old", start, start),
            ],
            "FUTURE_INDEX": [
                EquityEtfMeta(
                    "FUTURE",
                    "future",
                    "FUTURE_INDEX",
                    "future",
                    dt.date(2025, 1, 1),
                    dt.date(2025, 1, 2),
                ),
            ],
        }
        series = {
            "OLD": [(day, 100.0 + index) for index, day in enumerate(days)],
            "FUTURE": [(day, 100.0 + 10 * index) for index, day in enumerate(days)],
        }
        weights = select_direct_equity_etfs(
            metas,
            series,
            days[-1],
            DIRECT_ETF_POLICIES[0],
        )
        self.assertEqual(weights, {"OLD": 1.0})

    def test_stable_direct_selector_deduplicates_tracking_index(self):
        start = dt.date(2019, 1, 1)
        days = [start + dt.timedelta(days=index) for index in range(320)]
        metas = {
            "SAME_INDEX": [
                EquityEtfMeta("OLD", "old", "SAME_INDEX", "same", start, start),
                EquityEtfMeta(
                    "NEW",
                    "new",
                    "SAME_INDEX",
                    "same",
                    start + dt.timedelta(days=10),
                    start + dt.timedelta(days=10),
                ),
            ]
        }
        series = {
            "OLD": [(day, 100.0 + index) for index, day in enumerate(days)],
            "NEW": [(day, 100.0 + 2 * index) for index, day in enumerate(days)],
        }
        benchmark = [(day, 100.0 + index) for index, day in enumerate(days)]
        policy = next(
            item for item in DIRECT_ETF_POLICIES if item.name == "direct_stable_beta_top3"
        )
        weights = select_direct_equity_etfs(
            metas,
            series,
            days[-1],
            policy,
            benchmark_series=benchmark,
        )
        self.assertEqual(weights, {"OLD": 1.0})

    def test_regime_selector_ignores_benchmark_values_after_snapshot(self):
        start = dt.date(2019, 1, 1)
        days = [start + dt.timedelta(days=index) for index in range(340)]
        snapshot = days[300]
        metas = {
            "A_INDEX": [EquityEtfMeta("A", "a", "A_INDEX", "a", start, start)],
            "B_INDEX": [EquityEtfMeta("B", "b", "B_INDEX", "b", start, start)],
        }
        series = {
            "A": [(day, 100.0 + index * 0.20) for index, day in enumerate(days)],
            "B": [(day, 100.0 + index * 0.25 + (index % 5)) for index, day in enumerate(days)],
        }
        benchmark_before = [(day, 100.0 + index * 0.10) for index, day in enumerate(days[:301])]
        benchmark_with_future = benchmark_before + [
            (day, 1000.0 + index * 100.0)
            for index, day in enumerate(days[301:], 1)
        ]
        policy = next(item for item in DIRECT_ETF_POLICIES if item.name == "direct_regime_ic_top3")
        truncated = select_direct_equity_etfs(
            metas, series, snapshot, policy, benchmark_series=benchmark_before
        )
        full = select_direct_equity_etfs(
            metas, series, snapshot, policy, benchmark_series=benchmark_with_future
        )
        self.assertEqual(truncated, full)


if __name__ == "__main__":
    unittest.main()

import datetime as dt
import unittest
from unittest.mock import patch

from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    DirectEtfSelectorPolicy,
    EquityEtfMeta,
    _is_overseas,
    direct_blend_share,
    finance_breadth_rotation_market_confirmation_active,
    finance_catchup_market_confirmation_active,
    finance_defensive_market_confirmation_active,
    has_recent_etf_price,
    map_indices_to_etfs,
    portfolio_turnover,
    select_direct_equity_etfs,
    structural_finance_substyles_from_metas,
    structural_price_cold_start_scores,
    value_reflation_market_confirmation_active,
)


class DomesticEquityEtfTest(unittest.TestCase):
    def test_hcblend_policy_raises_direct_share_only_under_healthcare_leadership(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_hcblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_healthcare_leadership_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2017, 12, 31),
                    groups_by_code={},
                ),
                0.85,
            )

        plain_policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_healthcare_leadership_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    plain_policy,
                    {},
                    snapshot=dt.date(2017, 12, 31),
                    groups_by_code={},
                ),
                0.49,
            )

    def test_neblend_policy_no_longer_raises_direct_share(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_neblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_new_energy_pullback_restart_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2021, 4, 30),
                    subthemes_by_code={},
                ),
                0.49,
            )

    def test_lmblend_policy_raises_direct_share_under_generic_local_mainline(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_lmblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_local_mainline_pullback_reentry_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2021, 4, 30),
                    subthemes_by_code={},
                ),
                0.85,
            )

        plain_policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_new_energy_pullback_restart_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    plain_policy,
                    {},
                    snapshot=dt.date(2021, 4, 30),
                    subthemes_by_code={},
                ),
                0.49,
            )

    def test_finance_breadth_rotation_confirmation_uses_broad_low_vol_evidence(self):
        confirmed = {
            "cs300_return_3m": 0.08,
            "cs300_return_6m": 0.16,
            "basket_return_1m": 0.03,
            "basket_return_3m_max": 0.11,
            "breadth_return_1m_positive": 0.90,
            "breadth_return_3m_positive": 0.88,
            "basket_drawdown_6m": -0.02,
            "basket_vol_3m": 0.12,
        }
        self.assertTrue(finance_breadth_rotation_market_confirmation_active(confirmed))
        self.assertFalse(
            finance_breadth_rotation_market_confirmation_active(
                dict(confirmed, basket_vol_3m=0.22)
            )
        )
        self.assertFalse(
            finance_breadth_rotation_market_confirmation_active(
                dict(confirmed, domestic_liquidity_stress_flag=1.0)
            )
        )

    def test_drotblend_policy_raises_direct_share_only_under_digital_rotation(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_drotblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_digital_blowoff_rotation_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2023, 3, 31),
                    subthemes_by_code={},
                ),
                0.85,
            )

        plain_policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.structural_digital_blowoff_rotation_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    plain_policy,
                    {},
                    snapshot=dt.date(2023, 3, 31),
                    subthemes_by_code={},
                ),
                0.49,
            )

    def test_rbblend_policy_requires_market_and_cross_section_confirmation(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rbblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        with patch(
            "backtest.domestic_equity_etf.finance_catchup_market_confirmation_active",
            return_value=True,
        ), patch(
            "backtest.domestic_equity_etf.structural_finance_catchup_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2020, 12, 31),
                    subthemes_by_code={},
                ),
                0.85,
            )

        with patch(
            "backtest.domestic_equity_etf.finance_catchup_market_confirmation_active",
            return_value=True,
        ), patch(
            "backtest.domestic_equity_etf.structural_finance_catchup_active",
            return_value=False,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {},
                    snapshot=dt.date(2020, 12, 31),
                    subthemes_by_code={},
                ),
                0.49,
            )

    def test_finance_defensive_market_confirmation_blocks_broad_digital_risk_on(self):
        confirmed = {
            "pboc_outlook_net_tone": 22.0,
            "domestic_m1_m2_scissors_change_3m": 8.1,
            "cs300_return_3m": -0.02,
            "basket_return_1m": -0.04,
            "breadth_return_1m_positive": 0.14,
            "basket_drawdown_6m": -0.06,
            "basket_vol_3m": 0.30,
        }
        self.assertTrue(finance_defensive_market_confirmation_active(confirmed))

        digital_risk_on = {
            **confirmed,
            "domestic_m1_m2_scissors_change_3m": -2.4,
            "cs300_return_3m": 0.13,
            "basket_return_1m": 0.10,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.01,
        }
        self.assertFalse(
            finance_defensive_market_confirmation_active(digital_risk_on)
        )

    def test_value_reflation_confirmation_targets_constructive_rotation_only(self):
        confirmed = {
            "pboc_outlook_net_tone": 25.0,
            "domestic_m1_m2_scissors_change_3m": 7.0,
            "cs300_return_3m": 0.01,
            "basket_return_1m": 0.08,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.06,
            "basket_vol_3m": 0.31,
        }
        self.assertTrue(value_reflation_market_confirmation_active(confirmed))

        digital_risk_on_without_money_confirmation = {
            **confirmed,
            "domestic_m1_m2_scissors_change_3m": -2.4,
            "cs300_return_3m": 0.13,
        }
        self.assertFalse(
            value_reflation_market_confirmation_active(
                digital_risk_on_without_money_confirmation
            )
        )

        damaged_broad_market = {
            **confirmed,
            "cs300_return_3m": -0.14,
            "basket_return_1m": -0.03,
            "breadth_return_1m_positive": 0.43,
        }
        self.assertFalse(value_reflation_market_confirmation_active(damaged_broad_market))

    def test_valblend_policy_raises_direct_share_only_under_value_confirmation(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_valblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        confirmed = {
            "pboc_outlook_net_tone": 32.0,
            "domestic_m1_m2_scissors_change_3m": 1.7,
            "cs300_return_3m": 0.01,
            "basket_return_1m": 0.08,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.06,
            "basket_vol_3m": 0.31,
        }
        self.assertEqual(direct_blend_share(policy, confirmed), 0.85)
        self.assertEqual(
            direct_blend_share(
                policy,
                {**confirmed, "domestic_m1_m2_scissors_change_3m": -2.4},
            ),
            0.49,
        )

    def test_finance_catchup_confirmation_blocks_overheated_or_unconfirmed(self):
        confirmed = {
            "pboc_outlook_net_tone": 32.0,
            "domestic_m1_m2_scissors_change_3m": 1.7,
            "cs300_return_3m": 0.14,
            "basket_return_1m": 0.11,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.01,
            "basket_vol_3m": 0.25,
        }
        self.assertTrue(finance_catchup_market_confirmation_active(confirmed))

        digital_risk_on_without_money_confirmation = {
            **confirmed,
            "domestic_m1_m2_scissors_change_3m": -2.4,
        }
        self.assertFalse(
            finance_catchup_market_confirmation_active(
                digital_risk_on_without_money_confirmation
            )
        )

        overheated_broad = {
            **confirmed,
            "cs300_return_3m": 0.42,
            "basket_return_1m": 0.14,
        }
        self.assertFalse(finance_catchup_market_confirmation_active(overheated_broad))

    def test_fcblend_policy_raises_direct_share_only_under_confirmed_finance_catchup(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_fcblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        confirmed = {
            "pboc_outlook_net_tone": 32.0,
            "domestic_m1_m2_scissors_change_3m": 1.7,
            "cs300_return_3m": 0.14,
            "basket_return_1m": 0.11,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.01,
            "basket_vol_3m": 0.25,
        }
        with patch(
            "backtest.domestic_equity_etf.structural_finance_catchup_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    confirmed,
                    snapshot=dt.date(2020, 12, 31),
                    subthemes_by_code={},
                ),
                0.85,
            )
        with patch(
            "backtest.domestic_equity_etf.structural_finance_catchup_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    {**confirmed, "domestic_m1_m2_scissors_change_3m": -2.4},
                    snapshot=dt.date(2020, 12, 31),
                    subthemes_by_code={},
                ),
                0.49,
            )

    def test_fcbankblend_policy_uses_independent_finance_catchup_suffix(self):
        policy = DirectEtfSelectorPolicy(
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_fcbankblend85",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            direct_blend_weight=0.49,
        )
        confirmed = {
            "pboc_outlook_net_tone": 32.0,
            "domestic_m1_m2_scissors_change_3m": 1.7,
            "cs300_return_3m": 0.14,
            "basket_return_1m": 0.11,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_6m": -0.01,
            "basket_vol_3m": 0.25,
        }
        with patch(
            "backtest.domestic_equity_etf.structural_finance_catchup_active",
            return_value=True,
        ), patch("backtest.domestic_equity_etf.load_candidate_observations", return_value=[]):
            self.assertEqual(
                direct_blend_share(
                    policy,
                    confirmed,
                    snapshot=dt.date(2020, 12, 31),
                    subthemes_by_code={},
                ),
                0.85,
            )

    def test_finance_substyle_mapping_splits_bank_from_broker(self):
        metas = {
            "bank": [
                EquityEtfMeta(
                    "BANK.SH",
                    "银行ETF",
                    "CSI_BANK",
                    "中证银行指数",
                    dt.date(2017, 1, 1),
                    dt.date(2017, 1, 1),
                )
            ],
            "broker": [
                EquityEtfMeta(
                    "BROKER.SH",
                    "证券保险ETF",
                    "CSI_BROKER",
                    "证券保险红利指数",
                    dt.date(2017, 1, 1),
                    dt.date(2017, 1, 1),
                )
            ],
        }
        self.assertEqual(
            structural_finance_substyles_from_metas(metas),
            {"BANK.SH": "bank_dividend", "BROKER.SH": "broker_insurance"},
        )

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

    def test_utilities_cold_start_requires_explicit_extra_subtheme(self):
        snapshot = dt.date(2023, 3, 31)
        start = snapshot - dt.timedelta(days=90)
        series = {
            "POWER": [
                (start + dt.timedelta(days=index), 100.0 + index * 0.4)
                for index in range(91)
            ]
        }
        metas = {
            "POWER_INDEX": [
                EquityEtfMeta(
                    "POWER",
                    "电力ETF",
                    "POWER_INDEX",
                    "中证全指电力公用事业指数",
                    start,
                    start,
                )
            ]
        }
        self.assertEqual(
            structural_price_cold_start_scores(
                metas,
                series,
                snapshot,
            ),
            {},
        )
        scores = structural_price_cold_start_scores(
            metas,
            series,
            snapshot,
            extra_allowed_subthemes={"utilities"},
        )
        self.assertIn("POWER", scores)
        self.assertGreater(scores["POWER"], 0.0)

    def test_value_cold_start_can_use_pullback_with_positive_medium_trend(self):
        snapshot = dt.date(2021, 1, 4)
        start = snapshot - dt.timedelta(days=150)
        prices = []
        price = 100.0
        for index in range(151):
            if index < 90:
                price *= 1.002
            elif index < 130:
                price *= 1.003
            else:
                price *= 0.998
            prices.append((start + dt.timedelta(days=index), price))
        series = {"BANK": prices}
        metas = {
            "BANK_INDEX": [
                EquityEtfMeta(
                    "BANK",
                    "银行ETF",
                    "BANK_INDEX",
                    "中证银行指数",
                    start,
                    start,
                )
            ]
        }
        self.assertEqual(
            structural_price_cold_start_scores(
                metas,
                series,
                snapshot,
                extra_allowed_subthemes={"finance"},
            ),
            {},
        )
        scores = structural_price_cold_start_scores(
            metas,
            series,
            snapshot,
            extra_allowed_subthemes={"finance"},
            allow_nonpositive_1m_subthemes={"finance"},
        )
        self.assertIn("BANK", scores)
        self.assertGreater(scores["BANK"], 0.0)

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

import datetime as dt
import unittest

from backtest.domestic_defensive_etf import (
    DefensiveEtfMeta,
    DefensivePolicy,
    apply_portfolio_drawdown_guard,
    classify_defensive_etf,
    select_defensive_weights,
)


class DomesticDefensiveEtfTest(unittest.TestCase):
    def test_classification_excludes_convertible_bonds(self):
        self.assertEqual(classify_defensive_etf("511010.SH", "国债ETF", "5年国债"), "bond")
        self.assertEqual(classify_defensive_etf("518880.SH", "黄金ETF", "黄金9999"), "gold")
        self.assertEqual(
            classify_defensive_etf(
                "159985.SZ", "豆粕ETF", "大商所豆粕期货价格指数"
            ),
            "commodity",
        )
        self.assertIsNone(classify_defensive_etf("511380.SH", "可转债ETF", "中证可转债"))

    def test_selection_is_point_in_time_and_respects_gold_cap(self):
        start = dt.date(2020, 1, 1)
        dates = [start + dt.timedelta(days=index) for index in range(7)]
        metas = {
            "BOND": DefensiveEtfMeta("BOND", "bond", "bond", dates[0], "bond"),
            "GOLD": DefensiveEtfMeta("GOLD", "gold", "gold", dates[0], "gold"),
            "FUTURE": DefensiveEtfMeta("FUTURE", "future", "bond", dt.date(2021, 1, 1), "bond"),
        }
        series = {
            "BOND": list(zip(dates, [100, 101, 102, 103, 104, 105, 106])),
            "GOLD": list(zip(dates, [100, 102, 104, 106, 108, 110, 112])),
            "FUTURE": list(zip(dates, [100, 120, 140, 160, 180, 200, 220])),
        }
        policy = DefensivePolicy("test", 5, 21, 0.35, volatility_penalty=0.0)
        weights = select_defensive_weights(metas, series, dates[-1], policy)
        self.assertEqual(set(weights), {"BOND", "GOLD"})
        self.assertAlmostEqual(weights["GOLD"], 0.35)
        self.assertAlmostEqual(weights["BOND"], 0.65)
        self.assertNotIn("FUTURE", weights)

    def test_drawdown_guard_moves_gold_weight_to_bonds(self):
        start = dt.date(2020, 1, 1)
        metas = {
            "BOND": DefensiveEtfMeta("BOND", "bond", "bond", start, "bond"),
            "GOLD": DefensiveEtfMeta("GOLD", "gold", "gold", start, "gold"),
        }
        policy = DefensivePolicy(
            "guarded",
            126,
            21,
            0.35,
            portfolio_drawdown_threshold=-0.05,
            stressed_gold_max_weight=0.20,
        )
        adjusted, active = apply_portfolio_drawdown_guard(
            {"BOND": 0.65, "GOLD": 0.35},
            metas,
            policy,
            -0.06,
        )
        self.assertTrue(active)
        self.assertAlmostEqual(adjusted["GOLD"], 0.20)
        self.assertAlmostEqual(adjusted["BOND"], 0.80)

    def test_domestic_commodity_index_etf_respects_policy_cap(self):
        start = dt.date(2020, 1, 1)
        dates = [start + dt.timedelta(days=index) for index in range(7)]
        metas = {
            "BOND": DefensiveEtfMeta("BOND", "bond", "bond", start, "bond"),
            "COMMODITY": DefensiveEtfMeta(
                "COMMODITY", "commodity", "domestic futures index", start, "commodity"
            ),
        }
        series = {
            "BOND": list(zip(dates, [100, 101, 102, 103, 104, 105, 106])),
            "COMMODITY": list(zip(dates, [100, 102, 104, 106, 108, 110, 112])),
        }
        policy = DefensivePolicy(
            "commodity_cap30",
            5,
            21,
            0.0,
            volatility_penalty=0.0,
            commodity_max_weight=0.30,
        )
        weights = select_defensive_weights(metas, series, dates[-1], policy)
        self.assertAlmostEqual(weights["COMMODITY"], 0.30)
        self.assertAlmostEqual(weights["BOND"], 0.70)

    def test_short_gold_trend_can_move_weight_to_bonds(self):
        start = dt.date(2020, 1, 1)
        dates = [start + dt.timedelta(days=index) for index in range(8)]
        metas = {
            "BOND": DefensiveEtfMeta("BOND", "bond", "bond", start, "bond"),
            "GOLD": DefensiveEtfMeta("GOLD", "gold", "gold", start, "gold"),
        }
        series = {
            "BOND": list(zip(dates, [100, 101, 102, 103, 104, 105, 106, 107])),
            "GOLD": list(zip(dates, [100, 120, 130, 140, 135, 130, 125, 120])),
        }
        policy = DefensivePolicy(
            "gold_short_filter",
            7,
            21,
            0.35,
            volatility_penalty=0.0,
            gold_short_lookback_days=3,
        )
        weights = select_defensive_weights(metas, series, dates[-1], policy)
        self.assertEqual(weights, {"BOND": 1.0})


if __name__ == "__main__":
    unittest.main()

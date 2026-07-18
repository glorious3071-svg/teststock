import datetime as dt
import unittest

from scripts.backtest_calendar_neutral_csi_monthly import cash_return, summarize


class CalendarNeutralMonthlyTest(unittest.TestCase):
    def test_cash_return_uses_period_days(self):
        value = cash_return(dt.date(2020, 1, 1), dt.date(2020, 2, 1))
        self.assertGreater(value, 0.0)

    def test_summary_uses_month_end_drawdown_results(self):
        cases = [
            {
                "target_met": False,
                "final_capital_wan": 500.0,
                "max_drawdown": -0.08,
                "annualized_return": 0.08,
                "average_exposure": 0.2,
                "direction_hit_rate": 0.55,
                "direction_weighted_hit_rate": 0.60,
            },
            {
                "target_met": False,
                "final_capital_wan": 600.0,
                "max_drawdown": -0.09,
                "annualized_return": 0.09,
                "average_exposure": 0.3,
                "direction_hit_rate": 0.57,
                "direction_weighted_hit_rate": 0.62,
            },
        ]
        output = summarize(cases)
        self.assertEqual(output["min_final_capital_wan"], 500.0)
        self.assertEqual(output["worst_max_drawdown"], -0.09)
        self.assertEqual(output["risk_pass_count"], 2)

if __name__ == "__main__":
    unittest.main()

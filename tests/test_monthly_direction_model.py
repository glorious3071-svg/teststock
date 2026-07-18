import unittest

from backtest.monthly_direction_model import (
    MonthlyDirectionPolicy,
    attach_walkforward_predictions,
    predict_binned_direction,
    predict_direction,
    predict_ridge_direction,
)


class MonthlyDirectionModelTest(unittest.TestCase):
    def test_binned_prediction_uses_only_completed_prior_periods(self):
        policy = MonthlyDirectionPolicy(
            "binned_test",
            -2.0,
            99.0,
            99.0,
            99.0,
            99.0,
            min_history=9,
            features=("trend",),
            minimum_vote_count_for_boost=1,
            model_type="binned",
            target_clip=1.0,
            binned_bins=3,
            binned_shrink_count=0.0,
        )
        history = [
            {
                "features": {"trend": float(value)},
                "forward_return": (-0.10 if value < 3 else 0.0 if value < 6 else 0.10),
            }
            for value in range(9)
        ]
        high = predict_binned_direction(history, {"trend": 100.0}, policy)
        low = predict_binned_direction(history, {"trend": -100.0}, policy)
        self.assertAlmostEqual(high["score"], 0.075)
        self.assertAlmostEqual(low["score"], -0.10)
        self.assertEqual(high["thresholds"]["trend"], [2.0, 5.0])
        self.assertEqual(high["predicted_direction"], 1)
        self.assertEqual(low["predicted_direction"], -1)

    def test_prediction_uses_prior_outcomes(self):
        policy = MonthlyDirectionPolicy("test", -0.2, 0.0, 0.0, 0.0, 0.0, min_history=3, min_abs_correlation=0.0)
        history = [
            {"features": {"external_dxy_return_1m": 1.0}, "forward_return": -0.1},
            {"features": {"external_dxy_return_1m": 2.0}, "forward_return": -0.2},
            {"features": {"external_dxy_return_1m": 3.0}, "forward_return": -0.3},
        ]
        output = predict_direction(history, {"external_dxy_return_1m": 4.0}, policy)
        self.assertEqual(output["votes"]["external_dxy_return_1m"], -1)
        self.assertEqual(output["predicted_direction"], -1)

    def test_insufficient_history_has_no_prediction(self):
        policy = MonthlyDirectionPolicy("test", -0.2, 0.0, 0.0, 0.0, 0.0, min_history=3)
        output = predict_direction([], {"external_dxy_return_1m": 1.0}, policy)
        self.assertIsNone(output["score"])

    def test_confirmation_policy_records_two_votes(self):
        policy = MonthlyDirectionPolicy(
            "confirmed",
            -0.2,
            0.0,
            0.0,
            0.0,
            0.0,
            min_history=3,
            min_abs_correlation=0.0,
            features=("external_dxy_return_1m", "cs300_ma_6m_distance"),
            minimum_vote_count_for_cap=2,
        )
        history = [
            {
                "features": {
                    "external_dxy_return_1m": float(value),
                    "cs300_ma_6m_distance": float(4 - value),
                },
                "forward_return": -0.1 * value,
            }
            for value in (1, 2, 3)
        ]
        output = predict_direction(
            history,
            {"external_dxy_return_1m": 4.0, "cs300_ma_6m_distance": 0.0},
            policy,
        )
        self.assertEqual(output["vote_count"], 2)
        self.assertEqual(output["predicted_direction"], -1)

    def test_walkforward_can_start_from_proxy_prehistory(self):
        policy = MonthlyDirectionPolicy(
            "prehistory",
            -0.2,
            0.0,
            0.0,
            0.0,
            0.0,
            min_history=3,
            min_abs_correlation=0.0,
            features=("external_dxy_return_1m",),
        )
        initial_history = [
            {
                "features": {"external_dxy_return_1m": float(value)},
                "forward_return": -0.1 * value,
            }
            for value in (1, 2, 3)
        ]
        months = [
            {
                "features": {"external_dxy_return_1m": 4.0},
                "risk_return": -0.2,
            }
        ]
        attach_walkforward_predictions(months, policy, initial_history)
        self.assertEqual(months[0]["direction_model"]["predicted_direction"], -1)

    def test_ridge_prediction_uses_only_supplied_history(self):
        policy = MonthlyDirectionPolicy(
            "ridge_test",
            -0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            min_history=6,
            features=("trend", "breadth"),
            minimum_vote_count_for_cap=2,
            model_type="ridge",
            history_months=6,
            ridge_alpha=1.0,
        )
        history = [
            {
                "features": {"trend": float(value), "breadth": float(value) / 2.0},
                "forward_return": float(value) / 100.0,
            }
            for value in range(1, 7)
        ]
        output = predict_ridge_direction(
            history,
            {"trend": 7.0, "breadth": 3.5},
            policy,
        )
        self.assertGreater(output["score"], 0.0)
        self.assertEqual(output["predicted_direction"], 1)


if __name__ == "__main__":
    unittest.main()

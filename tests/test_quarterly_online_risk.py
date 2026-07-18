from __future__ import annotations

import unittest

from backtest.quarterly_online_risk import (
    QuarterlyLossGuardConfig,
    QuarterlyWalkForwardLossGuard,
)


class QuarterlyOnlineRiskTest(unittest.TestCase):
    def test_current_period_is_not_in_training_history(self) -> None:
        guard = QuarterlyWalkForwardLossGuard(
            QuarterlyLossGuardConfig(
                "test",
                min_history_periods=6,
                min_loss_periods=3,
                max_features=2,
            )
        )
        for value, outcome in [
            (0.1, 0.05),
            (0.2, 0.04),
            (0.3, 0.02),
            (0.7, -0.05),
            (0.8, -0.06),
            (0.9, -0.07),
        ]:
            guard.observe_completed_period({"observable": value}, outcome)
        before = guard.history_count
        decision = guard.decision({"observable": 0.8})
        self.assertEqual(before, 6)
        self.assertEqual(guard.history_count, 6)
        self.assertEqual(decision["mode"], "online")

        guard.observe_completed_period({"observable": 0.8}, -0.07)
        self.assertEqual(guard.history_count, 7)

    def test_warmup_never_flags(self) -> None:
        guard = QuarterlyWalkForwardLossGuard(
            QuarterlyLossGuardConfig("test", min_history_periods=3, min_loss_periods=1)
        )
        guard.observe_completed_period({"observable": 1.0}, -0.05)
        self.assertFalse(guard.decision({"observable": 2.0})["flagged"])


if __name__ == "__main__":
    unittest.main()

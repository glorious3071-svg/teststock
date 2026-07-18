from __future__ import annotations

import copy
import unittest
from datetime import date

from backtest.domestic_equity_etf import (
    EquityEtfMeta,
    blended_etf_diagnostics,
    selected_score_diagnostics,
)

from backtest.passive_etf_supervised_selector import (
    STABLE_FEATURES,
    SupervisedEtfPolicy,
    select_supervised_etfs,
    select_weighted_stable_combo_v2_top1,
    select_weighted_stable_combo_v3_top1,
    select_weighted_stable_combo_v4_top1,
    select_weighted_stable_combo_v5_top1,
    select_weighted_stable_combo_v6_top1,
    select_weighted_stable_combo_v7_top1,
    select_weighted_stable_combo_v9_top1,
    select_weighted_stable_combo_v10_top1,
)
from backtest.phase_schedule import shift_month_end


class PassiveEtfSupervisedSelectorTest(unittest.TestCase):
    def test_blended_etf_diagnostics_describes_actual_weighted_basket(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(260)]
        first = [(day, 100.0 * (1.01 ** offset)) for offset, day in enumerate(days)]
        second = [(day, 100.0 * (0.995 ** offset)) for offset, day in enumerate(days)]
        benchmark = [(day, 100.0 * (1.002 ** offset)) for offset, day in enumerate(days)]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.75, "510500.SH": 0.25},
            {"510300.SH": first, "510500.SH": second},
            days[-1],
            benchmark,
        )

        expected_daily = 0.75 * 0.01 + 0.25 * -0.005
        self.assertAlmostEqual(
            result["selected_etf_momentum_6m"],
            (1.0 + expected_daily) ** 126 - 1.0,
        )
        self.assertAlmostEqual(
            result["selected_etf_momentum_12m"],
            (1.0 + expected_daily) ** 252 - 1.0,
        )
        self.assertAlmostEqual(result["selected_etf_positive_day_ratio_3m"], 1.0)
        self.assertAlmostEqual(result["selected_etf_negative_day_ratio_3m"], 0.0)
        self.assertAlmostEqual(
            result["selected_etf_maximum_daily_loss_3m"], expected_daily
        )
        self.assertAlmostEqual(result["selected_etf_volatility_3m"], 0.0)
        self.assertAlmostEqual(result["selected_etf_max_drawdown_6m"], 0.0)

    def test_blended_downside_volatility_uses_semideviation(self) -> None:
        start = date(2023, 1, 1)
        returns = [-0.02 if offset % 2 else 0.01 for offset in range(130)]
        price = 100.0
        rows = [(start, price)]
        for offset, value in enumerate(returns, start=1):
            price *= 1.0 + value
            rows.append((date.fromordinal(start.toordinal() + offset), price))

        result = blended_etf_diagnostics(
            {"510300.SH": 1.0},
            {"510300.SH": rows},
            rows[-1][0],
        )
        trailing = returns[-63:]
        expected = (
            sum(min(value, 0.0) ** 2 for value in trailing) / len(trailing)
        ) ** 0.5 * (252.0 ** 0.5)
        self.assertAlmostEqual(
            result["selected_etf_downside_volatility_3m"], expected
        )

    def test_small_new_etf_sleeve_does_not_erase_six_month_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.90, "NEW.SH": 0.10},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
        )

        self.assertIn("selected_etf_volatility_6m", result)

    def test_new_majority_sleeve_does_not_invent_six_month_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]

        result = blended_etf_diagnostics(
            {"510300.SH": 0.20, "NEW.SH": 0.80},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
        )

        self.assertNotIn("selected_etf_volatility_6m", result)

    def test_tracking_index_supplies_prelisting_risk_history(self) -> None:
        start = date(2023, 1, 1)
        days = [date.fromordinal(start.toordinal() + offset) for offset in range(180)]
        mature = [(day, 100.0 * (1.001 ** offset)) for offset, day in enumerate(days)]
        new = [
            (day, 100.0 * (1.002 ** offset))
            for offset, day in enumerate(days[-40:])
        ]
        meta = EquityEtfMeta(
            code="NEW.SH",
            name="new tracker",
            index_code="TRACK.IDX",
            index_name="track index",
            list_date=days[-40],
            first_trade_date=days[-40],
        )

        result = blended_etf_diagnostics(
            {"510300.SH": 0.20, "NEW.SH": 0.80},
            {"510300.SH": mature, "NEW.SH": new},
            days[-1],
            metas_by_index={"TRACK.IDX": [meta]},
            index_series={"TRACK.IDX": mature},
        )

        self.assertIn("selected_etf_volatility_6m", result)

    def test_selected_score_diagnostics_uses_snapshot_fields_not_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = [
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "510300.SH",
                "volatility_3m": 0.18,
                "market_beta_6m": 0.92,
                "forward_return_3m": 99.0,
            },
            {
                "snapshot": snapshot.isoformat(),
                "ts_code": "510500.SH",
                "volatility_3m": 0.25,
                "market_beta_6m": 1.10,
                "forward_return_3m": -99.0,
            },
        ]
        result = selected_score_diagnostics(
            {"510300.SH": 0.8, "510500.SH": 0.7},
            observations,
            {"510300.SH", "510500.SH"},
            snapshot,
        )
        self.assertEqual(result["selected_etf_volatility_3m"], 0.18)
        self.assertEqual(result["selected_etf_market_beta_6m"], 0.92)
        self.assertNotIn("forward_return_3m", result)

    def test_current_and_future_labels_do_not_affect_current_selection(self) -> None:
        observations = []
        start = date(2018, 1, 31)
        codes = ("510050.SH", "510300.SH", "510500.SH")
        for offset in range(20):
            snapshot = shift_month_end(start, offset)
            end_snapshot = shift_month_end(snapshot, 3)
            for code_index, code in enumerate(codes):
                row = {
                    "snapshot": snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "ts_code": code,
                    "forward_return_3m": 0.02 * (code_index - 1) * (-1 if offset % 2 else 1),
                    "forward_max_drawdown_3m": -0.01 * code_index,
                }
                for feature_index, feature in enumerate(STABLE_FEATURES):
                    row[feature] = float((code_index + feature_index + offset) % 3)
                observations.append(row)

        policy = SupervisedEtfPolicy(
            "test", STABLE_FEATURES, 120, 2.0, 1, 1.0
        )
        snapshot = shift_month_end(start, 16)
        baseline = select_supervised_etfs(observations, snapshot, policy)

        changed = copy.deepcopy(observations)
        for row in changed:
            if date.fromisoformat(row["end_snapshot"]) > snapshot:
                row["forward_return_3m"] = 999.0 if row["ts_code"] == "510500.SH" else -999.0
                row["forward_max_drawdown_3m"] = -0.99
        perturbed = select_supervised_etfs(changed, snapshot, policy)
        self.assertEqual(baseline, perturbed)

    def test_static_v2_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v2_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v2_top1(changed, snapshot),
        )

    def test_static_v3_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.08 + index * 0.02,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v3_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v3_top1(changed, snapshot),
        )

    def test_static_v4_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v4_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v4_top1(changed, snapshot),
        )

    def test_static_v5_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v5_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v5_top1(changed, snapshot),
        )

    def test_static_v6_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "distance_high_12m": -0.03 * index,
                    "momentum_12m": -0.05 + index * 0.05,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_pb_change_6m": -0.1 + index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v6_top1(observations, snapshot)
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v6_top1(changed, snapshot),
        )

    def test_static_v7_flow_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "etf_subscription_flow_2q": 0.2 - index * 0.1,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v7_top1(
            observations, snapshot, 0.5
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v7_top1(changed, snapshot, 0.5),
        )

    def test_static_v9_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v9_top1(
            observations, snapshot
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v9_top1(changed, snapshot),
        )

    def test_static_v10_scorecard_does_not_read_forward_labels(self) -> None:
        snapshot = date(2024, 3, 31)
        observations = []
        for index, code in enumerate(("510050.SH", "510300.SH", "510500.SH")):
            observations.append(
                {
                    "snapshot": snapshot.isoformat(),
                    "ts_code": code,
                    "market_beta_6m": 0.8 + index * 0.1,
                    "distance_high_12m": -0.03 * index,
                    "return_autocorrelation_3m": -0.1 + index * 0.1,
                    "volatility_3m": 0.1 + index * 0.05,
                    "ulcer_index_6m": 0.02 + index * 0.02,
                    "index_fundamental_roe_proxy": 0.12 + index * 0.01,
                    "index_fundamental_book_growth_12m": -0.1 + index * 0.1,
                    "index_constituent_earnings_yield": 0.04 + index * 0.01,
                    "index_constituent_weight_hhi": 0.01 + index * 0.001,
                    "forward_return_3m": -0.5 + index * 0.5,
                    "forward_max_drawdown_3m": -0.1 * index,
                }
            )
        baseline = select_weighted_stable_combo_v10_top1(
            observations, snapshot, 0.75
        )
        changed = copy.deepcopy(observations)
        for row in changed:
            row["forward_return_3m"] *= -999.0
            row["forward_max_drawdown_3m"] = -0.99
        self.assertEqual(
            baseline,
            select_weighted_stable_combo_v10_top1(
                changed, snapshot, 0.75
            ),
        )


if __name__ == "__main__":
    unittest.main()

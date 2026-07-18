from __future__ import annotations

import copy
import unittest
from datetime import date

from backtest.passive_etf_boosted_selector import (
    ALL_PRICE_FEATURES,
    BoostedEtfPolicy,
    select_boosted_etfs,
)
from backtest.phase_schedule import shift_month_end


class PassiveEtfBoostedSelectorTest(unittest.TestCase):
    def test_current_year_and_future_labels_do_not_change_frozen_model(self) -> None:
        observations = []
        start = date(2016, 1, 31)
        codes = ("510050.SH", "510300.SH", "510500.SH", "510880.SH")
        for offset in range(28):
            snapshot = shift_month_end(start, offset * 3)
            end_snapshot = shift_month_end(snapshot, 3)
            for code_index, code in enumerate(codes):
                row = {
                    "snapshot": snapshot.isoformat(),
                    "end_snapshot": end_snapshot.isoformat(),
                    "ts_code": code,
                    "forward_return_3m": 0.02 * (code_index - 1) * (-1 if offset % 2 else 1),
                    "forward_max_drawdown_3m": -0.01 * code_index,
                }
                for feature_index, feature in enumerate(ALL_PRICE_FEATURES):
                    row[feature] = float((code_index + feature_index + offset) % 4)
                observations.append(row)

        policy = BoostedEtfPolicy(
            "test_boost", ALL_PRICE_FEATURES, 120, 0.20, 2.0, 8, 3
        )
        snapshot = shift_month_end(start, 24 * 3)
        baseline = select_boosted_etfs(observations, snapshot, policy)
        cutoff = date(snapshot.year, 1, 1)

        changed = copy.deepcopy(observations)
        for row in changed:
            if date.fromisoformat(row["end_snapshot"]) >= cutoff:
                row["forward_return_3m"] = 999.0 if row["ts_code"] == "510880.SH" else -999.0
                row["forward_max_drawdown_3m"] = -0.99
        perturbed = select_boosted_etfs(changed, snapshot, policy)
        self.assertEqual(baseline, perturbed)


if __name__ == "__main__":
    unittest.main()

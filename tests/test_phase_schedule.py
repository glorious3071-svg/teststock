from __future__ import annotations

import unittest
from datetime import date

from backtest.phase_schedule import ScheduleSpec, build_windows
from scripts.generate_scorecard_csi_strict_quarterly_targets import scheduled_snapshot
from scripts.validate_scorecard_csi_generalization import calendar_allocation_review_state


class PhaseScheduleTest(unittest.TestCase):
    def test_annual_scorecard_reset_follows_execution_year_not_drift_phase(self) -> None:
        year, review, entry, midpoint = calendar_allocation_review_state(
            date(2025, 7, 1), None, 0
        )
        self.assertEqual((year, review, entry, midpoint), (2025, 0, True, False))
        year, review, entry, midpoint = calendar_allocation_review_state(
            date(2025, 10, 8), year, review
        )
        self.assertEqual((year, review, entry, midpoint), (2025, 1, False, False))
        year, review, entry, midpoint = calendar_allocation_review_state(
            date(2026, 1, 5), year, review
        )
        self.assertEqual((year, review, entry, midpoint), (2026, 0, True, False))

    def test_february_drift_start_rebalances_every_three_months(self) -> None:
        """A February start means Feb/May/Aug/Nov, not calendar quarters."""

        spec = ScheduleSpec("cycle12m_review3m", 12, 3)
        first_cycle = build_windows(date(2005, 12, 31), spec, 2, 1)
        self.assertEqual(
            [window.start_snapshot.month for window in first_cycle],
            [2, 5, 8, 11],
        )
        self.assertEqual(
            [window.end_snapshot.month for window in first_cycle],
            [5, 8, 11, 2],
        )

        latest, cycle_entry = scheduled_snapshot(date(2024, 12, 15), 2)
        self.assertEqual(latest, date(2024, 11, 30))
        self.assertEqual(cycle_entry.month, 2)

    def test_windows_are_contiguous_for_multiple_phases(self) -> None:
        spec = ScheduleSpec("cycle12m_review3m", 12, 3)
        for phase in (0, 1, 4, 5, 11):
            with self.subTest(phase=phase):
                windows = build_windows(date(2005, 12, 31), spec, phase, 20)
                self.assertEqual(len(windows), 80)
                self.assertTrue(
                    all(
                        previous.end_snapshot == current.start_snapshot
                        for previous, current in zip(windows, windows[1:])
                    )
                )

    def test_relative_roles_do_not_depend_on_calendar_month(self) -> None:
        spec = ScheduleSpec("cycle12m_review3m", 12, 3)
        for phase in range(12):
            with self.subTest(phase=phase):
                first_cycle = build_windows(date(2005, 12, 31), spec, phase, 1)
                self.assertEqual([window.review_index for window in first_cycle], [0, 1, 2, 3])
                self.assertEqual([window.review_index for window in first_cycle if window.cycle_entry], [0])
                self.assertEqual([window.review_index for window in first_cycle if window.cycle_midpoint], [2])

    def test_single_review_cycle_has_no_midpoint_review(self) -> None:
        spec = ScheduleSpec("cycle12m_review12m", 12, 12)
        windows = build_windows(date(2005, 12, 31), spec, 5, 20)
        self.assertEqual(len(windows), 20)
        self.assertTrue(all(window.cycle_entry for window in windows))
        self.assertFalse(any(window.cycle_midpoint for window in windows))

    def test_invalid_schedule_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ScheduleSpec("invalid", 12, 5)


if __name__ == "__main__":
    unittest.main()

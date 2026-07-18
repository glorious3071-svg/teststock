"""Calendar-neutral phase schedule primitives.

This module only knows cycle length, review interval, phase offset, and a date
anchor. Domain strategies decide how a relative cycle position affects signals,
selection, sizing, or execution.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    cycle_months: int
    review_interval_months: int

    def __post_init__(self) -> None:
        if self.cycle_months <= 0 or self.review_interval_months <= 0:
            raise ValueError("cycle and review intervals must be positive")
        if self.cycle_months % self.review_interval_months:
            raise ValueError("cycle_months must be divisible by review_interval_months")

    @property
    def reviews_per_cycle(self) -> int:
        return self.cycle_months // self.review_interval_months


@dataclass(frozen=True)
class ScheduleWindow:
    cycle_index: int
    review_index: int
    start_snapshot: date
    end_snapshot: date
    cycle_entry: bool
    cycle_midpoint: bool


def shift_month_end(boundary: date, offset_months: int) -> date:
    month_index = boundary.year * 12 + boundary.month - 1 + offset_months
    shifted_year = month_index // 12
    shifted_month = month_index % 12 + 1
    return date(
        shifted_year,
        shifted_month,
        monthrange(shifted_year, shifted_month)[1],
    )


def window_bounds(
    anchor: date,
    spec: ScheduleSpec,
    phase_month_offset: int,
    cycle_index: int,
    review_index: int,
) -> tuple[date, date]:
    if phase_month_offset not in range(spec.cycle_months):
        raise ValueError(
            f"phase_month_offset must be in 0..{spec.cycle_months - 1}, "
            f"got {phase_month_offset}"
        )
    if review_index not in range(spec.reviews_per_cycle):
        raise ValueError(
            f"review_index must be in 0..{spec.reviews_per_cycle - 1}, got {review_index}"
        )
    elapsed_months = cycle_index * spec.cycle_months + review_index * spec.review_interval_months
    start = shift_month_end(anchor, phase_month_offset + elapsed_months)
    end = shift_month_end(start, spec.review_interval_months)
    return start, end


def build_windows(
    anchor: date,
    spec: ScheduleSpec,
    phase_month_offset: int,
    cycle_count: int,
) -> list[ScheduleWindow]:
    if cycle_count <= 0:
        raise ValueError("cycle_count must be positive")
    windows: list[ScheduleWindow] = []
    for cycle_index in range(cycle_count):
        for review_index in range(spec.reviews_per_cycle):
            start_snapshot, end_snapshot = window_bounds(
                anchor,
                spec,
                phase_month_offset,
                cycle_index,
                review_index,
            )
            windows.append(
                ScheduleWindow(
                    cycle_index=cycle_index,
                    review_index=review_index,
                    start_snapshot=start_snapshot,
                    end_snapshot=end_snapshot,
                    cycle_entry=review_index == 0,
                    cycle_midpoint=(
                        review_index * spec.review_interval_months * 2 == spec.cycle_months
                    ),
                )
            )
    for previous, current in zip(windows, windows[1:]):
        if previous.end_snapshot != current.start_snapshot:
            raise AssertionError(
                f"non-contiguous schedule: {previous.end_snapshot} != {current.start_snapshot}"
            )
    return windows

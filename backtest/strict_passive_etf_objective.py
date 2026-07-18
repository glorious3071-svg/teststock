"""Hard validation gates for the strict quarterly passive-ETF objective.

The research repository contains strategies whose ETF names change quarterly
while their exposure, defensive sleeve, or stop state changes monthly or daily.
Those strategies are useful diagnostics, but they do not satisfy a mandate in
which *all* portfolio weights may change only at quarterly boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence


EXPECTED_PHASES = tuple(range(12))
EXPECTED_EXECUTION_LAGS = (0, 1, 3, 5)


@dataclass(frozen=True)
class StrictObjectiveGate:
    initial_capital: float = 1_000_000.0
    minimum_final_capital: float = 40_000_000.0
    minimum_max_drawdown: float = -0.20
    rebalance_interval_months: int = 3
    phase_offsets: tuple[int, ...] = EXPECTED_PHASES
    execution_lags: tuple[int, ...] = EXPECTED_EXECUTION_LAGS
    maximum_gross_weight: float = 1.0

    @property
    def expected_case_count(self) -> int:
        return len(self.phase_offsets) * len(self.execution_lags)


STRICT_OBJECTIVE = StrictObjectiveGate()


def month_distance(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _weights(row: Mapping[str, Any]) -> dict[str, float]:
    raw = row.get("target_weights", row.get("weights", {}))
    if not isinstance(raw, Mapping):
        raise ValueError("row weights must be a mapping")
    return {str(code): float(weight) for code, weight in raw.items() if abs(float(weight)) > 1e-12}


def validate_quarterly_weight_path(
    rows: Sequence[Mapping[str, Any]],
    *,
    gate: StrictObjectiveGate = STRICT_OBJECTIVE,
    require_exact_rebalance_spacing: bool = False,
) -> list[str]:
    """Return violations for a dated target-weight path.

    Rows may be daily or monthly valuation observations. Weight changes are
    legal only when at least three calendar months have elapsed since the prior
    change. When ``require_exact_rebalance_spacing`` is true, every row must be
    a scheduled rebalance decision and adjacent rebalance anchors must be
    exactly three calendar months apart. This catches both hidden monthly
    exposure changes and skipped quarterly decisions.
    """

    violations: list[str] = []
    previous_weights: dict[str, float] | None = None
    previous_change_date: date | None = None
    previous_rebalance_anchor: date | None = None
    for index, row in enumerate(rows):
        raw_date = row.get("decision_date", row.get("snapshot", row.get("date")))
        if raw_date is None:
            violations.append(f"row {index}: missing decision date")
            continue
        decision_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        raw_anchor = row.get("rebalance_anchor", raw_date)
        rebalance_anchor = (
            raw_anchor if isinstance(raw_anchor, date) else date.fromisoformat(str(raw_anchor))
        )
        if require_exact_rebalance_spacing and previous_rebalance_anchor is not None:
            elapsed = month_distance(previous_rebalance_anchor, rebalance_anchor)
            if elapsed != gate.rebalance_interval_months:
                violations.append(
                    f"{rebalance_anchor}: rebalance scheduled after {elapsed} months; "
                    f"required exactly {gate.rebalance_interval_months}"
                )
        previous_rebalance_anchor = rebalance_anchor
        weights = _weights(row)
        gross = sum(abs(weight) for weight in weights.values())
        if gross > gate.maximum_gross_weight + 1e-9:
            violations.append(
                f"{decision_date}: gross weight {gross:.6f} exceeds {gate.maximum_gross_weight:.6f}"
            )
        if any(weight < -1e-12 for weight in weights.values()):
            violations.append(f"{decision_date}: short ETF weight is not allowed")
        if previous_weights is None:
            previous_weights = weights
            previous_change_date = decision_date
            continue
        if weights != previous_weights:
            assert previous_change_date is not None
            elapsed = month_distance(previous_change_date, decision_date)
            if elapsed < gate.rebalance_interval_months:
                violations.append(
                    f"{decision_date}: weights changed after {elapsed} months; "
                    f"minimum is {gate.rebalance_interval_months}"
                )
            previous_weights = weights
            previous_change_date = decision_date
    return violations


def validate_case_matrix(
    cases: Iterable[Mapping[str, Any]],
    *,
    gate: StrictObjectiveGate = STRICT_OBJECTIVE,
) -> dict[str, Any]:
    """Validate full phase/lag coverage and the two numerical pass gates."""

    materialized = list(cases)
    expected = {
        (phase, lag)
        for phase in gate.phase_offsets
        for lag in gate.execution_lags
    }
    observed: dict[tuple[int, int], Mapping[str, Any]] = {}
    duplicates: list[tuple[int, int]] = []
    for case in materialized:
        key = (int(case["phase_month_offset"]), int(case["execution_lag_days"]))
        if key in observed:
            duplicates.append(key)
        observed[key] = case
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    failures = []
    for key in sorted(expected & set(observed)):
        case = observed[key]
        final_capital = float(case["final_capital"])
        max_drawdown = float(case["max_drawdown"])
        if final_capital < gate.minimum_final_capital or max_drawdown < gate.minimum_max_drawdown:
            failures.append(
                {
                    "phase_month_offset": key[0],
                    "execution_lag_days": key[1],
                    "final_capital": final_capital,
                    "max_drawdown": max_drawdown,
                    "capital_ok": final_capital >= gate.minimum_final_capital,
                    "drawdown_ok": max_drawdown >= gate.minimum_max_drawdown,
                }
            )
    matrix_complete = not missing and not unexpected and not duplicates and len(materialized) == gate.expected_case_count
    return {
        "expected_case_count": gate.expected_case_count,
        "observed_case_count": len(materialized),
        "matrix_complete": matrix_complete,
        "missing_cases": missing,
        "unexpected_cases": unexpected,
        "duplicate_cases": sorted(set(duplicates)),
        "failed_cases": failures,
        "all_cases_pass": matrix_complete and not failures,
    }


def validate_target_assets(
    targets: Sequence[Mapping[str, Any]],
    passive_etf_rows: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Reject non-domestic, enhanced, QDII, or non-passive target rows."""

    violations: list[str] = []
    total_weight = 0.0
    for row in targets:
        code = str(row.get("ts_code", ""))
        weight = float(row.get("target_weight_pct", 0.0)) / 100.0
        total_weight += weight
        if code == "CASH":
            continue
        if not code.endswith((".SH", ".SZ")):
            violations.append(f"{code}: target is not an SH/SZ listed ETF")
            continue
        meta = passive_etf_rows.get(code)
        if meta is None:
            violations.append(f"{code}: absent from passive_etf point-in-time universe")
            continue
        if str(meta.get("etf_type") or "").upper() == "QDII":
            violations.append(f"{code}: QDII/overseas ETF is not allowed")
        if bool(meta.get("is_enhanced")):
            violations.append(f"{code}: enhanced index ETF is not allowed")
        if not bool(meta.get("listed_by_as_of", True)):
            violations.append(f"{code}: ETF was not listed at the decision date")
    if abs(total_weight - 1.0) > 1e-6:
        violations.append(f"target weights sum to {total_weight:.6f}, expected 1.000000")
    return violations

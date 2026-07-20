"""Structural-market adaptation gates for strict quarterly ETF strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class StructuralAdaptationGate:
    recent_10y_start: date = date(2016, 1, 1)
    recent_10y_end: date = date(2025, 12, 31)
    recent_10y_min_annualized_return: float = 0.08
    recent_5y_start: date = date(2021, 1, 1)
    recent_5y_end: date = date(2025, 12, 31)
    recent_5y_min_cumulative_return: float = 0.30
    recent_5y_min_max_drawdown: float = -0.20
    rolling_5y_quarters: int = 20
    rolling_5y_min_annualized_return: float = 0.05
    rolling_3y_quarters: int = 12
    rolling_3y_min_max_drawdown: float = -0.18
    max_consecutive_quarters_below_defense: int = 7
    structural_broad_return_max: float = 0.05
    structural_cross_section_spread_min: float = 0.08
    structural_top_positive_min_count: int = 6
    systemic_crash_broad_return_min: float = -0.12
    systemic_crash_median_return_min: float = -0.08
    structural_exposure_median_min: float = 0.50
    structural_capture_ratio_min: float = 0.30
    structural_capture_pass_rate_min: float = 1.0
    structural_benchmark_win_rate_min: float = 0.60
    low_exposure_threshold: float = 0.10
    max_consecutive_low_exposure_structural_quarters: int = 1


STRUCTURAL_ADAPTATION_GATE = StructuralAdaptationGate()


def compound_return(returns: Sequence[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + float(item)
    return value - 1.0


def annualized_return(returns: Sequence[float], periods_per_year: float = 4.0) -> float | None:
    if not returns:
        return None
    cumulative = 1.0 + compound_return(returns)
    if cumulative <= 0:
        return -1.0
    return cumulative ** (periods_per_year / len(returns)) - 1.0


def max_drawdown_from_returns(returns: Sequence[float]) -> float:
    value = 1.0
    peak = 1.0
    worst = 0.0
    for item in returns:
        value *= 1.0 + float(item)
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def max_drawdown_from_rows(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    start_capital = float(rows[0].get("capital_at_decision") or 1.0)
    current = start_capital
    peak = start_capital
    worst = 0.0
    for row in rows:
        capital_at_decision = float(row.get("capital_at_decision") or current)
        peak = max(peak, capital_at_decision)
        min_capital = float(row.get("min_capital_since_decision") or capital_at_decision)
        worst = min(worst, min_capital / max(peak, 1e-12) - 1.0)
        period_return = float(row.get("realized_portfolio_return") or 0.0)
        current = capital_at_decision * (1.0 + period_return)
        peak = max(peak, current)
        worst = min(worst, current / max(peak, 1e-12) - 1.0)
    return worst


def max_consecutive_true(values: Sequence[bool]) -> int:
    best = 0
    current = 0
    for value in values:
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def inferred_defense_return(row: Mapping[str, Any]) -> float:
    portfolio_return = float(row.get("realized_portfolio_return") or 0.0)
    exposure = float(row.get("exposure") or 0.0)
    risk_return = row.get("realized_risk_return")
    if risk_return is None or exposure >= 1.0 - 1e-12:
        return 0.0
    return (portfolio_return - exposure * float(risk_return)) / max(1.0 - exposure, 1e-12)


def case_period_rows(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in case.get("decision_rows", [])]
    rows.sort(key=lambda row: str(row.get("decision_date", "")))
    return rows


def rows_between(rows: Sequence[Mapping[str, Any]], start: date, end: date) -> list[Mapping[str, Any]]:
    selected = []
    for row in rows:
        raw_date = row.get("decision_date")
        if raw_date is None:
            continue
        decision_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        if start <= decision_date <= end:
            selected.append(row)
    return selected


def validate_recent_survival(
    case: Mapping[str, Any],
    *,
    gate: StructuralAdaptationGate = STRUCTURAL_ADAPTATION_GATE,
) -> dict[str, Any]:
    rows = case_period_rows(case)
    recent_10y_rows = rows_between(rows, gate.recent_10y_start, gate.recent_10y_end)
    recent_5y_rows = rows_between(rows, gate.recent_5y_start, gate.recent_5y_end)
    recent_10y_returns = [
        float(row.get("realized_portfolio_return") or 0.0) for row in recent_10y_rows
    ]
    recent_5y_returns = [
        float(row.get("realized_portfolio_return") or 0.0) for row in recent_5y_rows
    ]
    recent_10y_ann = annualized_return(recent_10y_returns)
    recent_5y_cum = compound_return(recent_5y_returns) if recent_5y_returns else None
    recent_5y_mdd = max_drawdown_from_rows(recent_5y_rows)
    rolling_5y = []
    for start in range(0, max(0, len(rows) - gate.rolling_5y_quarters + 1)):
        window = rows[start : start + gate.rolling_5y_quarters]
        returns = [float(row.get("realized_portfolio_return") or 0.0) for row in window]
        rolling_5y.append(
            {
                "start": window[0].get("decision_date"),
                "end": window[-1].get("decision_date"),
                "annualized_return": annualized_return(returns),
            }
        )
    rolling_3y = []
    for start in range(0, max(0, len(rows) - gate.rolling_3y_quarters + 1)):
        window = rows[start : start + gate.rolling_3y_quarters]
        rolling_3y.append(
            {
                "start": window[0].get("decision_date"),
                "end": window[-1].get("decision_date"),
                "max_drawdown": max_drawdown_from_rows(window),
            }
        )
    below_defense = [
        float(row.get("realized_portfolio_return") or 0.0) < inferred_defense_return(row)
        for row in rows
    ]
    min_rolling_5y = min(
        (item["annualized_return"] for item in rolling_5y if item["annualized_return"] is not None),
        default=None,
    )
    worst_rolling_3y_mdd = min(
        (item["max_drawdown"] for item in rolling_3y),
        default=0.0,
    )
    failures = []
    if recent_10y_ann is None or recent_10y_ann < gate.recent_10y_min_annualized_return:
        failures.append("recent_10y_annualized_return")
    if recent_5y_cum is None or recent_5y_cum < gate.recent_5y_min_cumulative_return:
        failures.append("recent_5y_cumulative_return")
    if recent_5y_mdd < gate.recent_5y_min_max_drawdown:
        failures.append("recent_5y_max_drawdown")
    if min_rolling_5y is None or min_rolling_5y < gate.rolling_5y_min_annualized_return:
        failures.append("rolling_5y_annualized_return")
    if worst_rolling_3y_mdd < gate.rolling_3y_min_max_drawdown:
        failures.append("rolling_3y_max_drawdown")
    max_under_defense = max_consecutive_true(below_defense)
    if max_under_defense > gate.max_consecutive_quarters_below_defense:
        failures.append("consecutive_quarters_below_defense")
    return {
        "phase_month_offset": case.get("phase_month_offset"),
        "execution_lag_days": case.get("execution_lag_days"),
        "recent_10y_annualized_return": recent_10y_ann,
        "recent_10y_quarters": len(recent_10y_returns),
        "recent_5y_cumulative_return": recent_5y_cum,
        "recent_5y_max_drawdown": recent_5y_mdd,
        "recent_5y_quarters": len(recent_5y_returns),
        "min_rolling_5y_annualized_return": min_rolling_5y,
        "worst_rolling_3y_max_drawdown": worst_rolling_3y_mdd,
        "max_consecutive_quarters_below_defense": max_under_defense,
        "worst_rolling_5y_window": min(
            rolling_5y,
            key=lambda item: item["annualized_return"] if item["annualized_return"] is not None else -999.0,
        )
        if rolling_5y
        else None,
        "worst_rolling_3y_window": min(rolling_3y, key=lambda item: item["max_drawdown"])
        if rolling_3y
        else None,
        "failures": failures,
        "passed": not failures,
    }


def validate_case_matrix_adaptation(
    cases: Sequence[Mapping[str, Any]],
    *,
    gate: StructuralAdaptationGate = STRUCTURAL_ADAPTATION_GATE,
) -> dict[str, Any]:
    recent = [validate_recent_survival(case, gate=gate) for case in cases]
    return {
        "case_count": len(recent),
        "recent_survival_pass_count": sum(1 for item in recent if item["passed"]),
        "recent_survival_passed": bool(recent) and all(item["passed"] for item in recent),
        "worst_recent_10y_annualized_return": min(
            (item["recent_10y_annualized_return"] for item in recent if item["recent_10y_annualized_return"] is not None),
            default=None,
        ),
        "worst_recent_5y_cumulative_return": min(
            (item["recent_5y_cumulative_return"] for item in recent if item["recent_5y_cumulative_return"] is not None),
            default=None,
        ),
        "worst_recent_5y_max_drawdown": min(
            (item["recent_5y_max_drawdown"] for item in recent),
            default=None,
        ),
        "worst_rolling_5y_annualized_return": min(
            (item["min_rolling_5y_annualized_return"] for item in recent if item["min_rolling_5y_annualized_return"] is not None),
            default=None,
        ),
        "worst_rolling_3y_max_drawdown": min(
            (item["worst_rolling_3y_max_drawdown"] for item in recent),
            default=None,
        ),
        "max_consecutive_quarters_below_defense": max(
            (item["max_consecutive_quarters_below_defense"] for item in recent),
            default=0,
        ),
        "failed_recent_cases": [item for item in recent if not item["passed"]],
    }

#!/usr/bin/env python3
"""Diagnose recent-survival failures for strict quarterly CSI/ETF reports."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.structural_adaptation import (  # noqa: E402
    STRUCTURAL_ADAPTATION_GATE,
    StructuralAdaptationGate,
    annualized_return,
    case_period_rows,
    compound_return,
    inferred_defense_return,
    max_drawdown_from_rows,
    validate_recent_survival,
)


def parse_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def rows_between(
    rows: Iterable[Mapping[str, Any]],
    start: date,
    end: date,
) -> list[Mapping[str, Any]]:
    selected = []
    for row in rows:
        decision_date = parse_date(row["decision_date"])
        if start <= decision_date <= end:
            selected.append(row)
    return selected


def realized_returns(rows: Iterable[Mapping[str, Any]], key: str) -> list[float]:
    return [float(row.get(key) or 0.0) for row in rows]


def risk_contribution(row: Mapping[str, Any]) -> float:
    return float(row.get("exposure") or 0.0) * float(row.get("realized_risk_return") or 0.0)


def defense_contribution(row: Mapping[str, Any]) -> float:
    exposure = float(row.get("exposure") or 0.0)
    return max(0.0, 1.0 - exposure) * inferred_defense_return(row)


def window_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    returns = realized_returns(rows, "realized_portfolio_return")
    risk_returns = realized_returns(rows, "realized_risk_return")
    defense_returns = [inferred_defense_return(row) for row in rows]
    risk_contrib = [risk_contribution(row) for row in rows]
    defense_contrib = [defense_contribution(row) for row in rows]
    exposures = [float(row.get("exposure") or 0.0) for row in rows]
    low_exposure_rows = [row for row in rows if float(row.get("exposure") or 0.0) < 0.50]
    negative_rows = [row for row in rows if float(row.get("realized_portfolio_return") or 0.0) < 0.0]
    worst_rows = sorted(
        rows,
        key=lambda row: float(row.get("realized_portfolio_return") or 0.0),
    )[:5]
    binding_stages: dict[str, int] = {}
    active_flags: dict[str, int] = {}
    for row in rows:
        trace = row.get("exposure_formation", {}).get("trace", [])
        for stage in trace:
            if stage.get("effect") != "decrease":
                continue
            binding_stages[str(stage.get("stage"))] = binding_stages.get(str(stage.get("stage")), 0) + 1
        for flag in row.get("active_risk_flags") or []:
            active_flags[str(flag)] = active_flags.get(str(flag), 0) + 1
    return {
        "start": rows[0].get("decision_date") if rows else None,
        "end": rows[-1].get("decision_date") if rows else None,
        "quarter_count": len(rows),
        "cumulative_return": compound_return(returns) if rows else None,
        "annualized_return": annualized_return(returns) if rows else None,
        "max_drawdown": max_drawdown_from_rows(rows),
        "average_exposure": statistics.mean(exposures) if exposures else None,
        "median_exposure": statistics.median(exposures) if exposures else None,
        "low_exposure_count": len(low_exposure_rows),
        "negative_quarter_count": len(negative_rows),
        "compound_risk_return": compound_return(risk_returns) if rows else None,
        "compound_defense_return": compound_return(defense_returns) if rows else None,
        "sum_risk_contribution": sum(risk_contrib),
        "sum_defense_contribution": sum(defense_contrib),
        "binding_decrease_stages": dict(
            sorted(binding_stages.items(), key=lambda item: (-item[1], item[0]))
        ),
        "active_risk_flag_counts": dict(
            sorted(active_flags.items(), key=lambda item: (-item[1], item[0]))
        ),
        "worst_quarters": [
            {
                "decision_date": row.get("decision_date"),
                "portfolio_return": float(row.get("realized_portfolio_return") or 0.0),
                "risk_return": float(row.get("realized_risk_return") or 0.0),
                "defense_return": inferred_defense_return(row),
                "exposure": float(row.get("exposure") or 0.0),
                "active_risk_flags": row.get("active_risk_flags") or [],
                "top_equity_etfs": list((row.get("equity_etf_weights") or {}).keys())[:8],
            }
            for row in worst_rows
        ],
    }


def trailing_window(rows: list[Mapping[str, Any]], end: str, quarters: int) -> list[Mapping[str, Any]]:
    end_date = parse_date(end)
    eligible = [row for row in rows if parse_date(row["decision_date"]) <= end_date]
    return eligible[-quarters:]


def case_recent_diagnostics(
    case: Mapping[str, Any],
    gate: StructuralAdaptationGate,
) -> dict[str, Any]:
    rows = case_period_rows(case)
    recent = validate_recent_survival(case, gate=gate)
    recent_5y = rows_between(rows, gate.recent_5y_start, gate.recent_5y_end)
    worst_rolling = recent.get("worst_rolling_5y_window") or {}
    worst_rolling_rows = rows_between(
        rows,
        parse_date(worst_rolling["start"]),
        parse_date(worst_rolling["end"]),
    ) if worst_rolling else []
    return {
        "phase_month_offset": case.get("phase_month_offset"),
        "execution_lag_days": case.get("execution_lag_days"),
        "recent": recent,
        "recent_5y_window": window_summary(recent_5y),
        "worst_rolling_5y_window": window_summary(worst_rolling_rows),
        "pre_2020_window": window_summary(trailing_window(rows, "2020-03-31", 20)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    result = payload["results"][args.result_index]
    cases = result["cases"]
    diagnostics = [
        case_recent_diagnostics(case, STRUCTURAL_ADAPTATION_GATE)
        for case in cases
    ]
    failures = [
        item
        for item in diagnostics
        if not item["recent"].get("passed")
    ]
    failures.sort(
        key=lambda item: (
            item["recent"].get("min_rolling_5y_annualized_return") or -999.0,
            item["recent"].get("recent_5y_cumulative_return") or -999.0,
        )
    )
    output = {
        "source_report": str(report_path.relative_to(ROOT)),
        "result_index": args.result_index,
        "rule_name": result["rule"]["name"],
        "direct_etf_policy": result.get("direct_etf_policy", {}).get("name"),
        "case_count": len(cases),
        "failed_case_count": len(failures),
        "worst_cases": failures[: args.limit],
        "summary": {
            "worst_recent_5y_cumulative_return": min(
                item["recent"].get("recent_5y_cumulative_return")
                for item in diagnostics
                if item["recent"].get("recent_5y_cumulative_return") is not None
            ),
            "worst_rolling_5y_annualized_return": min(
                item["recent"].get("min_rolling_5y_annualized_return")
                for item in diagnostics
                if item["recent"].get("min_rolling_5y_annualized_return") is not None
            ),
            "median_recent_5y_average_exposure": statistics.median(
                item["recent_5y_window"]["average_exposure"]
                for item in diagnostics
                if item["recent_5y_window"]["average_exposure"] is not None
            ),
            "median_worst_rolling_5y_average_exposure": statistics.median(
                item["worst_rolling_5y_window"]["average_exposure"]
                for item in diagnostics
                if item["worst_rolling_5y_window"]["average_exposure"] is not None
            ),
        },
    }
    if args.output:
        out_path = args.output if args.output.is_absolute() else ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_path}")
    print(
        f"failed={len(failures)}/{len(cases)} "
        f"worst_5y={output['summary']['worst_recent_5y_cumulative_return']:.4f} "
        f"worst_roll5={output['summary']['worst_rolling_5y_annualized_return']:.4f} "
        f"median_recent5y_exposure={output['summary']['median_recent_5y_average_exposure']:.4f}"
    )
    for item in failures[: min(args.limit, 5)]:
        recent = item["recent"]
        rolling = item["worst_rolling_5y_window"]
        recent5 = item["recent_5y_window"]
        print(
            f"phase={item['phase_month_offset']} lag={item['execution_lag_days']} "
            f"recent5={recent['recent_5y_cumulative_return']:.4f} "
            f"roll5={recent['min_rolling_5y_annualized_return']:.4f} "
            f"roll_window={rolling['start']}..{rolling['end']} "
            f"roll_exp={rolling['average_exposure']:.3f} "
            f"recent_exp={recent5['average_exposure']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

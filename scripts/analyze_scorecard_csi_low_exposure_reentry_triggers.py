#!/usr/bin/env python3
"""Evaluate point-in-time re-entry triggers for low-exposure quarters."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_equity_etf import rotation_structural_opportunity_active  # noqa: E402
from backtest.structural_adaptation import inferred_defense_return  # noqa: E402


TriggerFn = Callable[[Mapping[str, Any]], bool]


def number(state: Mapping[str, Any], name: str) -> float | None:
    value = state.get(name)
    return float(value) if isinstance(value, (int, float)) else None


def strong_crisis(state: Mapping[str, Any]) -> bool:
    return bool(
        state.get("crisis_continuation_flag")
        or state.get("domestic_liquidity_stress_flag")
        or state.get("credit_contraction_tightening_flag")
    )


def broad_recovery(state: Mapping[str, Any]) -> bool:
    required = {
        name: number(state, name)
        for name in (
            "cs300_return_3m",
            "basket_return_3m",
            "breadth_return_3m_positive",
            "basket_drawdown_6m",
            "basket_ma_3m_distance",
        )
    }
    if any(value is None for value in required.values()):
        return False
    return (
        not strong_crisis(state)
        and required["cs300_return_3m"] > -0.05
        and required["basket_return_3m"] > 0.0
        and required["breadth_return_3m_positive"] >= 0.50
        and required["basket_drawdown_6m"] > -0.15
        and required["basket_ma_3m_distance"] >= -0.03
    )


def selected_strength(state: Mapping[str, Any]) -> bool:
    required = {
        name: number(state, name)
        for name in (
            "selected_etf_momentum_3m",
            "selected_etf_drawdown_3m",
            "selected_etf_positive_day_ratio_3m",
            "selector_score_margin",
        )
    }
    if any(value is None for value in required.values()):
        return False
    return (
        not strong_crisis(state)
        and required["selected_etf_momentum_3m"] >= 0.04
        and required["selected_etf_drawdown_3m"] > -0.08
        and required["selected_etf_positive_day_ratio_3m"] >= 0.52
        and required["selector_score_margin"] >= 0.01
    )


def broad_or_selected_strength(state: Mapping[str, Any]) -> bool:
    return broad_recovery(state) or selected_strength(state)


def mild_risk_flags(row: Mapping[str, Any]) -> bool:
    flags = set(row.get("active_risk_flags") or [])
    hard = {
        "crisis_continuation_flag",
        "domestic_liquidity_stress_flag",
        "credit_contraction_tightening_flag",
        "leadership_collapse_tightening_flag",
    }
    return not flags.intersection(hard)


def no_hard_exit(row: Mapping[str, Any]) -> bool:
    trace = row.get("exposure_formation", {}).get("trace", [])
    for stage in trace:
        if stage.get("stage") == "hard_exit" and stage.get("effect") == "decrease":
            return False
    return True


def rows_from_report(report: Mapping[str, Any], result_index: int) -> list[dict[str, Any]]:
    rows = []
    for case in report["results"][result_index]["cases"]:
        for row in case["decision_rows"]:
            enriched = dict(row)
            enriched["phase_month_offset"] = case.get("phase_month_offset")
            enriched["execution_lag_days"] = case.get("execution_lag_days")
            rows.append(enriched)
    return rows


def evaluate_trigger(
    name: str,
    trigger: Callable[[dict[str, Any]], bool],
    rows: list[dict[str, Any]],
    *,
    floor: float,
) -> dict[str, Any]:
    low_rows = [row for row in rows if float(row.get("exposure") or 0.0) < floor]
    triggered = [row for row in low_rows if trigger(row)]
    increments = []
    for row in triggered:
        exposure = float(row.get("exposure") or 0.0)
        risk_return = float(row.get("realized_risk_return") or 0.0)
        defense_return = inferred_defense_return(row)
        increment = (floor - exposure) * (risk_return - defense_return)
        increments.append(increment)
    positive = [row for row in triggered if float(row.get("realized_risk_return") or 0.0) > inferred_defense_return(row)]
    loss_rows = sorted(
        triggered,
        key=lambda row: (floor - float(row.get("exposure") or 0.0))
        * (float(row.get("realized_risk_return") or 0.0) - inferred_defense_return(row)),
    )[:10]
    return {
        "name": name,
        "floor": floor,
        "low_exposure_row_count": len(low_rows),
        "trigger_count": len(triggered),
        "trigger_rate": len(triggered) / len(low_rows) if low_rows else None,
        "positive_increment_rate": len(positive) / len(triggered) if triggered else None,
        "average_incremental_return": statistics.mean(increments) if increments else None,
        "median_incremental_return": statistics.median(increments) if increments else None,
        "sum_incremental_return": sum(increments),
        "worst_incremental_return": min(increments) if increments else None,
        "best_incremental_return": max(increments) if increments else None,
        "loss_count": sum(1 for item in increments if item < 0),
        "worst_loss_rows": [
            {
                "phase_month_offset": row.get("phase_month_offset"),
                "execution_lag_days": row.get("execution_lag_days"),
                "decision_date": row.get("decision_date"),
                "exposure": float(row.get("exposure") or 0.0),
                "risk_return": float(row.get("realized_risk_return") or 0.0),
                "defense_return": inferred_defense_return(row),
                "incremental_return": (floor - float(row.get("exposure") or 0.0))
                * (float(row.get("realized_risk_return") or 0.0) - inferred_defense_return(row)),
                "active_risk_flags": row.get("active_risk_flags") or [],
            }
            for row in loss_rows
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--floor", type=float, default=0.50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    rows = rows_from_report(payload, args.result_index)
    triggers: dict[str, Callable[[dict[str, Any]], bool]] = {
        "broad_recovery_no_hard_exit": lambda row: no_hard_exit(row) and broad_recovery(row.get("market_state") or {}),
        "selected_strength_no_hard_exit": lambda row: no_hard_exit(row) and selected_strength(row.get("market_state") or {}),
        "broad_or_selected_no_hard_exit": lambda row: no_hard_exit(row) and broad_or_selected_strength(row.get("market_state") or {}),
        "rotation_no_hard_exit": lambda row: no_hard_exit(row) and rotation_structural_opportunity_active(row.get("market_state") or {}),
        "broad_recovery_mild_flags": lambda row: mild_risk_flags(row) and broad_recovery(row.get("market_state") or {}),
        "selected_strength_mild_flags": lambda row: mild_risk_flags(row) and selected_strength(row.get("market_state") or {}),
    }
    results = [
        evaluate_trigger(name, trigger, rows, floor=args.floor)
        for name, trigger in triggers.items()
    ]
    results.sort(
        key=lambda item: (
            item["sum_incremental_return"],
            item["positive_increment_rate"] if item["positive_increment_rate"] is not None else -1.0,
            item["worst_incremental_return"] if item["worst_incremental_return"] is not None else -1.0,
        ),
        reverse=True,
    )
    output = {
        "source_report": str(report_path.relative_to(ROOT)),
        "result_index": args.result_index,
        "floor": args.floor,
        "results": results,
    }
    if args.output:
        out_path = args.output if args.output.is_absolute() else ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_path}")
    for item in results:
        print(
            f"{item['name']} trigger={item['trigger_count']} "
            f"pos={item['positive_increment_rate'] or 0.0:.3f} "
            f"sum_inc={item['sum_incremental_return']:.4f} "
            f"avg_inc={item['average_incremental_return'] or 0.0:.4f} "
            f"worst={item['worst_incremental_return'] or 0.0:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

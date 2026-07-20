#!/usr/bin/env python3
"""Evaluate point-in-time triggers for future structural ETF quarters."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_equity_etf import (  # noqa: E402
    structural_opportunity_active,
    wide_structural_opportunity_active,
)
from backtest.structural_adaptation import (  # noqa: E402
    STRUCTURAL_ADAPTATION_GATE,
    StructuralAdaptationGate,
    case_period_rows,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE  # noqa: E402
from scripts.validate_scorecard_csi_structural_adaptation import (  # noqa: E402
    load_domestic_passive_etf_series,
    parse_date,
    period_cross_section,
    period_return,
    strong_risk_ban,
)


TriggerFn = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class ThresholdTrigger:
    name: str
    cs300_return_3m_max: float
    dispersion_3m_min: float
    max_return_3m_min: float
    breadth_3m_min: float
    basket_drawdown_6m_min: float

    def __call__(self, market_state: dict[str, Any]) -> bool:
        required = (
            "cs300_return_3m",
            "basket_return_3m_dispersion",
            "basket_return_3m_max",
            "breadth_return_3m_positive",
            "basket_drawdown_6m",
        )
        if any(market_state.get(name) is None for name in required):
            return False
        if _strong_crisis(market_state):
            return False
        return (
            float(market_state["cs300_return_3m"]) < self.cs300_return_3m_max
            and float(market_state["basket_return_3m_dispersion"]) >= self.dispersion_3m_min
            and float(market_state["basket_return_3m_max"]) >= self.max_return_3m_min
            and float(market_state["breadth_return_3m_positive"]) >= self.breadth_3m_min
            and float(market_state["basket_drawdown_6m"]) > self.basket_drawdown_6m_min
        )


def _strong_crisis(market_state: dict[str, Any]) -> bool:
    return bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )


def _number(market_state: dict[str, Any], name: str) -> float | None:
    value = market_state.get(name)
    return None if value is None else float(value)


def weak_broad_early_strength(market_state: dict[str, Any]) -> bool:
    if _strong_crisis(market_state):
        return False
    required = {
        name: _number(market_state, name)
        for name in (
            "cs300_return_3m",
            "basket_return_1m_dispersion",
            "basket_return_1m_max",
            "breadth_return_1m_positive",
            "basket_drawdown_3m",
        )
    }
    if any(value is None for value in required.values()):
        return False
    return (
        required["cs300_return_3m"] < 0.16
        and required["basket_return_1m_dispersion"] >= 0.025
        and required["basket_return_1m_max"] >= 0.06
        and required["breadth_return_1m_positive"] >= 0.45
        and required["basket_drawdown_3m"] > -0.10
    )


def selected_momentum_margin(market_state: dict[str, Any]) -> bool:
    if _strong_crisis(market_state):
        return False
    required = {
        name: _number(market_state, name)
        for name in (
            "cs300_return_3m",
            "basket_return_3m_dispersion",
            "selected_etf_momentum_3m",
            "selector_score_margin",
            "selected_etf_drawdown_3m",
        )
    }
    if any(value is None for value in required.values()):
        return False
    return (
        required["cs300_return_3m"] < 0.16
        and required["basket_return_3m_dispersion"] >= 0.03
        and required["selected_etf_momentum_3m"] >= 0.06
        and required["selector_score_margin"] >= 0.015
        and required["selected_etf_drawdown_3m"] > -0.10
    )


def broad_participation_rotation_setup(market_state: dict[str, Any]) -> bool:
    if _strong_crisis(market_state):
        return False
    required = {
        name: _number(market_state, name)
        for name in (
            "cs300_return_3m",
            "basket_return_3m_max",
            "breadth_return_3m_positive",
            "basket_drawdown_6m",
            "selected_etf_momentum_3m",
            "selector_score_margin",
        )
    }
    if any(value is None for value in required.values()):
        return False
    return (
        required["cs300_return_3m"] < 0.12
        and required["basket_return_3m_max"] >= 0.10
        and required["breadth_return_3m_positive"] >= 0.80
        and required["basket_drawdown_6m"] > -0.05
        and required["selected_etf_momentum_3m"] >= 0.05
        and required["selector_score_margin"] >= 0.01
    )


def build_labeled_rows(
    cases: list[dict[str, Any]],
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[date, float]]],
    gate: StructuralAdaptationGate,
) -> list[dict[str, Any]]:
    labeled = []
    cross_section_cache: dict[tuple[date, date], dict[str, Any]] = {}
    for case in cases:
        rows = case_period_rows(case)
        for idx, row in enumerate(rows):
            start = parse_date(row["decision_date"])
            end = (
                parse_date(rows[idx + 1]["decision_date"])
                if idx + 1 < len(rows)
                else parse_date(case["sample_end"])
            )
            broad_return = period_return(series[CS300_CODE], start, end)
            cache_key = (start, end)
            cross = cross_section_cache.get(cache_key)
            if cross is None:
                cross = period_cross_section(metas, series, start, end)
                cross_section_cache[cache_key] = cross
            if broad_return is None or not cross.get("available"):
                continue
            systemic_crash = (
                broad_return <= gate.systemic_crash_broad_return_min
                and cross["median_return"] <= gate.systemic_crash_median_return_min
            )
            structural = (
                broad_return < gate.structural_broad_return_max
                and cross["top20_minus_median"] >= gate.structural_cross_section_spread_min
                and cross["top10_positive_count"] >= gate.structural_top_positive_min_count
                and not systemic_crash
            )
            banned = strong_risk_ban(row)
            labeled.append(
                {
                    "phase_month_offset": case.get("phase_month_offset"),
                    "execution_lag_days": case.get("execution_lag_days"),
                    "decision_date": row.get("decision_date"),
                    "signal_date": row.get("rebalance_anchor", row.get("decision_date")),
                    "period_end_date": end.isoformat(),
                    "market_state": dict(row.get("market_state") or {}),
                    "broad_return": broad_return,
                    "median_etf_return": cross["median_return"],
                    "top20_minus_median": cross["top20_minus_median"],
                    "top10_equal_return": cross.get("top10_equal_return"),
                    "top10_positive_count": cross["top10_positive_count"],
                    "systemic_crash": systemic_crash,
                    "strong_risk_ban": banned,
                    "structural": structural,
                    "applicable_structural": structural and not banned,
                }
            )
    return labeled


def evaluate_trigger(name: str, trigger: TriggerFn, rows: list[dict[str, Any]]) -> dict[str, Any]:
    trigger_rows = [row for row in rows if trigger(row["market_state"])]
    structural_rows = [row for row in rows if row["structural"]]
    applicable_rows = [row for row in rows if row["applicable_structural"]]
    triggered_applicable = [row for row in trigger_rows if row["applicable_structural"]]
    triggered_structural = [row for row in trigger_rows if row["structural"]]
    false_systemic = [row for row in trigger_rows if row["systemic_crash"]]
    false_nonstructural = [row for row in trigger_rows if not row["structural"]]

    case_totals: dict[tuple[Any, Any], int] = defaultdict(int)
    case_hits: dict[tuple[Any, Any], int] = defaultdict(int)
    for row in applicable_rows:
        key = (row["phase_month_offset"], row["execution_lag_days"])
        case_totals[key] += 1
        if trigger(row["market_state"]):
            case_hits[key] += 1
    case_recalls = [
        case_hits[key] / total for key, total in case_totals.items() if total > 0
    ]

    return {
        "name": name,
        "row_count": len(rows),
        "trigger_count": len(trigger_rows),
        "structural_count": len(structural_rows),
        "applicable_structural_count": len(applicable_rows),
        "trigger_rate": len(trigger_rows) / len(rows) if rows else None,
        "precision_structural": len(triggered_structural) / len(trigger_rows) if trigger_rows else None,
        "precision_applicable_structural": (
            len(triggered_applicable) / len(trigger_rows) if trigger_rows else None
        ),
        "recall_structural": len(triggered_structural) / len(structural_rows) if structural_rows else None,
        "recall_applicable_structural": (
            len(triggered_applicable) / len(applicable_rows) if applicable_rows else None
        ),
        "worst_case_applicable_recall": min(case_recalls) if case_recalls else None,
        "median_case_applicable_recall": statistics.median(case_recalls) if case_recalls else None,
        "zero_hit_case_count": sum(1 for value in case_recalls if value == 0),
        "false_systemic_crash_count": len(false_systemic),
        "false_nonstructural_count": len(false_nonstructural),
        "false_nonstructural_median_broad_return": (
            statistics.median(row["broad_return"] for row in false_nonstructural)
            if false_nonstructural
            else None
        ),
        "tracked_worst_2020q1_triggered": any(
            row["phase_month_offset"] == 10
            and row["execution_lag_days"] == 0
            and row["decision_date"] == "2020-01-02"
            for row in trigger_rows
        ),
    }


def grid_triggers() -> list[ThresholdTrigger]:
    triggers = []
    for cs300_max in (0.08, 0.12, 0.16, 0.20):
        for dispersion_min in (0.00, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.08):
            for max_return_min in (0.04, 0.06, 0.08, 0.10, 0.12):
                for breadth_min in (0.40, 0.50, 0.60):
                    for drawdown_min in (-0.20, -0.15, -0.12, -0.10):
                        triggers.append(
                            ThresholdTrigger(
                                name=(
                                    "grid_3m"
                                    f"_csi{int(cs300_max * 100):02d}"
                                    f"_disp{int(round(dispersion_min * 1000)):03d}"
                                    f"_max{int(max_return_min * 100):02d}"
                                    f"_br{int(breadth_min * 100):02d}"
                                    f"_dd{int(abs(drawdown_min) * 100):02d}"
                                ),
                                cs300_return_3m_max=cs300_max,
                                dispersion_3m_min=dispersion_min,
                                max_return_3m_min=max_return_min,
                                breadth_3m_min=breadth_min,
                                basket_drawdown_6m_min=drawdown_min,
                            )
                        )
    return triggers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--min-rows", type=int, default=60)
    parser.add_argument("--top-grid", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    cases = payload["results"][args.result_index]["cases"]
    metas, series = load_domestic_passive_etf_series(args.min_rows)
    rows = build_labeled_rows(cases, metas, series, STRUCTURAL_ADAPTATION_GATE)

    fixed_triggers: dict[str, TriggerFn] = {
        "existing_narrow": structural_opportunity_active,
        "existing_wide": wide_structural_opportunity_active,
        "weak_broad_early_strength": weak_broad_early_strength,
        "selected_momentum_margin": selected_momentum_margin,
        "broad_participation_rotation_setup": broad_participation_rotation_setup,
    }
    fixed_results = [
        evaluate_trigger(name, trigger, rows)
        for name, trigger in fixed_triggers.items()
    ]
    grid_results = [
        evaluate_trigger(trigger.name, trigger, rows)
        for trigger in grid_triggers()
    ]
    grid_results.sort(
        key=lambda item: (
            item["worst_case_applicable_recall"] if item["worst_case_applicable_recall"] is not None else -1.0,
            item["recall_applicable_structural"] if item["recall_applicable_structural"] is not None else -1.0,
            -(item["false_systemic_crash_count"] or 0),
            item["precision_applicable_structural"] if item["precision_applicable_structural"] is not None else -1.0,
        ),
        reverse=True,
    )
    output = {
        "source_report": str(report_path.relative_to(ROOT)),
        "result_index": args.result_index,
        "row_count": len(rows),
        "structural_count": sum(1 for row in rows if row["structural"]),
        "applicable_structural_count": sum(1 for row in rows if row["applicable_structural"]),
        "systemic_crash_count": sum(1 for row in rows if row["systemic_crash"]),
        "fixed_triggers": fixed_results,
        "top_grid_triggers": grid_results[: args.top_grid],
    }
    if args.output:
        out_path = args.output if args.output.is_absolute() else ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_path}")
    print(
        f"rows={output['row_count']} structural={output['structural_count']} "
        f"applicable={output['applicable_structural_count']} systemic_crash={output['systemic_crash_count']}"
    )
    for item in fixed_results:
        print(
            f"{item['name']} trigger={item['trigger_count']} "
            f"recall={item['recall_applicable_structural'] or 0.0:.3f} "
            f"worst_case={item['worst_case_applicable_recall'] or 0.0:.3f} "
            f"precision={item['precision_applicable_structural'] or 0.0:.3f} "
            f"crash_fp={item['false_systemic_crash_count']} "
            f"tracks_2020q1={item['tracked_worst_2020q1_triggered']}"
        )
    best = grid_results[0] if grid_results else None
    if best:
        print(
            f"best_grid={best['name']} trigger={best['trigger_count']} "
            f"recall={best['recall_applicable_structural'] or 0.0:.3f} "
            f"worst_case={best['worst_case_applicable_recall'] or 0.0:.3f} "
            f"precision={best['precision_applicable_structural'] or 0.0:.3f} "
            f"crash_fp={best['false_systemic_crash_count']} "
            f"tracks_2020q1={best['tracked_worst_2020q1_triggered']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

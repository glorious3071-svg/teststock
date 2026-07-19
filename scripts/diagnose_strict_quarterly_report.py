#!/usr/bin/env python3
"""Diagnose worst paths in a strict quarterly passive-ETF report."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def pct(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def stage_deltas(row: dict[str, Any]) -> list[dict[str, Any]]:
    trace = row.get("exposure_formation", {}).get("trace", [])
    output = []
    for stage in trace:
        if not stage.get("applied"):
            continue
        before = pct(stage.get("before"))
        after = pct(stage.get("after"))
        if before is None or after is None:
            continue
        output.append(
            {
                "stage": stage.get("stage"),
                "before": before,
                "after": after,
                "delta": after - before,
                "details": stage.get("details", {}),
            }
        )
    return output


def summarize_stages(cases: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "applied_count": 0,
            "decrease_count": 0,
            "increase_count": 0,
            "positive_risk_quarters": 0,
            "negative_risk_quarters": 0,
            "exposure_delta_sum": 0.0,
            "risk_return_effect_sum": 0.0,
        }
    )
    for case in cases:
        for row in case.get("decision_rows", []):
            risk_return = float(row.get("realized_risk_return") or 0.0)
            for stage in stage_deltas(row):
                item = stats[str(stage["stage"])]
                delta = float(stage["delta"])
                item["applied_count"] += 1
                item["exposure_delta_sum"] += delta
                item["risk_return_effect_sum"] += delta * risk_return
                if delta < 0:
                    item["decrease_count"] += 1
                elif delta > 0:
                    item["increase_count"] += 1
                if risk_return > 0:
                    item["positive_risk_quarters"] += 1
                elif risk_return < 0:
                    item["negative_risk_quarters"] += 1
    for item in stats.values():
        count = item["applied_count"]
        item["average_delta"] = item["exposure_delta_sum"] / count if count else None
    return dict(sorted(stats.items(), key=lambda kv: kv[1]["risk_return_effect_sum"]))


def row_summary(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    exposure = float(row.get("exposure") or 0.0)
    risk_return = float(row.get("realized_risk_return") or 0.0)
    portfolio_return = row.get("realized_portfolio_return")
    return {
        "phase_month_offset": int(case["phase_month_offset"]),
        "execution_lag_days": int(case["execution_lag_days"]),
        "decision_date": row.get("decision_date"),
        "rebalance_anchor": row.get("rebalance_anchor"),
        "exposure": exposure,
        "cash_weight": float(row.get("target_weights", {}).get("CASH", 0.0)),
        "realized_risk_return": risk_return,
        "realized_risk_max_drawdown": row.get("realized_risk_max_drawdown"),
        "realized_portfolio_return": portfolio_return,
        "missed_return_proxy": max(0.0, risk_return) * max(0.0, 1.0 - exposure),
        "active_risk_clusters": "+".join(row.get("active_risk_clusters", [])),
        "active_risk_flags": "+".join(row.get("active_risk_flags", [])),
        "applied_decrease_stages": "+".join(
            str(stage["stage"]) for stage in stage_deltas(row) if stage["delta"] < 0
        ),
        "direction_score": row.get("direction_model", {}).get("score"),
        "risk_gate_score": row.get("direction_risk_gate", {}).get("score"),
        "pboc_outlook_net_tone": row.get("market_state", {}).get("pboc_outlook_net_tone"),
        "cs300_return_6m": row.get("market_state", {}).get("cs300_return_6m"),
        "basket_drawdown_6m": row.get("market_state", {}).get("basket_drawdown_6m"),
        "selected_etf_volatility_6m": row.get("market_state", {}).get(
            "selected_etf_volatility_6m"
        ),
    }


def diagnose(
    report: dict[str, Any],
    *,
    opportunity_return: float,
    low_exposure: float,
) -> dict[str, Any]:
    cases = [case for result in report["results"] for case in result["cases"]]
    worst_capital = min(cases, key=lambda item: float(item["final_capital"]))
    worst_drawdown = min(cases, key=lambda item: float(item["max_drawdown"]))
    case_rows = [
        {
            "phase_month_offset": int(case["phase_month_offset"]),
            "execution_lag_days": int(case["execution_lag_days"]),
            "final_capital_wan": float(case["final_capital_wan"]),
            "max_drawdown": float(case["max_drawdown"]),
            "average_exposure": float(case.get("average_exposure") or 0.0),
        }
        for case in cases
    ]
    opportunities = []
    high_risk = []
    zero_reasons: Counter[str] = Counter()
    for case in cases:
        for row in case.get("decision_rows", []):
            exposure = float(row.get("exposure") or 0.0)
            risk_return = float(row.get("realized_risk_return") or 0.0)
            risk_drawdown = float(row.get("realized_risk_max_drawdown") or 0.0)
            if exposure <= 1e-12:
                decreases = [stage["stage"] for stage in stage_deltas(row) if stage["delta"] < 0]
                zero_reasons["+".join(decreases[-2:]) if decreases else "initial_zero"] += 1
            if risk_return >= opportunity_return and exposure <= low_exposure:
                opportunities.append(row_summary(case, row))
            if risk_drawdown <= -0.20 and exposure >= low_exposure:
                high_risk.append(row_summary(case, row))
    opportunities.sort(key=lambda item: item["missed_return_proxy"], reverse=True)
    high_risk.sort(key=lambda item: item["realized_risk_max_drawdown"])
    return {
        "source_objective": report.get("objective"),
        "target_capital": report.get("target_capital"),
        "target_mdd": report.get("target_mdd"),
        "case_count": len(cases),
        "path_summary": {
            "min_final_capital_wan": min(row["final_capital_wan"] for row in case_rows),
            "median_final_capital_wan": statistics.median(
                row["final_capital_wan"] for row in case_rows
            ),
            "max_final_capital_wan": max(row["final_capital_wan"] for row in case_rows),
            "worst_max_drawdown": min(row["max_drawdown"] for row in case_rows),
            "median_max_drawdown": statistics.median(row["max_drawdown"] for row in case_rows),
            "worst_capital_case": {
                "phase_month_offset": int(worst_capital["phase_month_offset"]),
                "execution_lag_days": int(worst_capital["execution_lag_days"]),
                "final_capital_wan": worst_capital["final_capital_wan"],
                "max_drawdown": worst_capital["max_drawdown"],
                "average_exposure": worst_capital.get("average_exposure"),
            },
            "worst_drawdown_case": {
                "phase_month_offset": int(worst_drawdown["phase_month_offset"]),
                "execution_lag_days": int(worst_drawdown["execution_lag_days"]),
                "final_capital_wan": worst_drawdown["final_capital_wan"],
                "max_drawdown": worst_drawdown["max_drawdown"],
                "average_exposure": worst_drawdown.get("average_exposure"),
            },
        },
        "stage_summary": summarize_stages(cases),
        "zero_exposure_reasons": dict(zero_reasons.most_common()),
        "missed_opportunity_rule": {
            "risk_return_gte": opportunity_return,
            "exposure_lte": low_exposure,
        },
        "missed_opportunities": opportunities[:50],
        "high_path_risk_rows": high_risk[:50],
        "paths": sorted(case_rows, key=lambda item: item["final_capital_wan"]),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output-prefix", type=Path)
    parser.add_argument("--opportunity-return", type=float, default=0.10)
    parser.add_argument("--low-exposure", type=float, default=0.30)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    summary = diagnose(
        report,
        opportunity_return=args.opportunity_return,
        low_exposure=args.low_exposure,
    )
    prefix = args.output_prefix or args.report.with_suffix("")
    json_path = Path(f"{prefix}_diagnosis.json")
    opportunity_csv = Path(f"{prefix}_missed_opportunities.csv")
    paths_csv = Path(f"{prefix}_paths.csv")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(opportunity_csv, summary["missed_opportunities"])
    write_csv(paths_csv, summary["paths"])
    worst = summary["path_summary"]["worst_capital_case"]
    print(
        f"worst_capital phase={worst['phase_month_offset']} lag={worst['execution_lag_days']} "
        f"final={worst['final_capital_wan']:.2f}w mdd={worst['max_drawdown'] * 100:.2f}%"
    )
    print(f"missed_opportunities={len(summary['missed_opportunities'])} wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

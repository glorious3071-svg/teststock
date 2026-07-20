#!/usr/bin/env python3
"""Validate recent survival and structural-market capture for CSI ETF reports."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.structural_adaptation import (  # noqa: E402
    STRUCTURAL_ADAPTATION_GATE,
    StructuralAdaptationGate,
    case_period_rows,
    max_consecutive_true,
    validate_case_matrix_adaptation,
)
from backtest.passive_etf_supervised_selector import (  # noqa: E402
    SHARE_V5_DATASET,
    load_candidate_observations,
    weighted_structural_mainline_scores,
)
from db.connection import get_connection  # noqa: E402
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE  # noqa: E402
from scripts.search_scorecard_csi_passive_etf_only import is_overseas_etf  # noqa: E402

SCORECARD_STRONG_RISK_REASONS = {
    "cycle_midpoint_scorecard_risk_reduce",
    "cycle_midpoint_weak_pmi_trailing6m_rally_cap",
    "stagflation_defensive_cap",
    "weak_momentum_exhaustion_cap",
    "weak_repair_trap_cap",
}


def parse_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def pct_chg_compounded_series(rows: list[tuple[date, float | None]]) -> list[tuple[date, float]]:
    """Build a split-neutral ETF return index from fund_daily.pct_chg."""

    output: list[tuple[date, float]] = []
    cumulative = 100.0
    for trade_date, pct_chg in rows:
        if output and pct_chg is not None:
            cumulative *= 1.0 + float(pct_chg) / 100.0
        output.append((trade_date, cumulative))
    return output


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    idx = bisect_right(rows, (boundary, float("inf"))) - 1
    return rows[idx][1] if idx >= 0 else None


def period_return(rows: list[tuple[date, float]], start: date, end: date) -> float | None:
    start_px = price_at(rows, start)
    end_px = price_at(rows, end)
    if start_px is None or end_px is None or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def load_domestic_passive_etf_series(min_rows: int) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[date, float]]]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date,
                       COUNT(f.trade_date)
                FROM passive_etf e
                JOIN fund_daily f ON f.ts_code=e.ts_code
                WHERE (e.etf_type IS NULL OR e.etf_type!='QDII')
                  AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
                  AND e.ts_code LIKE '%%.S_'
                  AND f.close IS NOT NULL
                GROUP BY e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
                HAVING COUNT(f.trade_date) >= %s
                ORDER BY e.ts_code
                """,
                (min_rows,),
            )
            metas: dict[str, dict[str, Any]] = {}
            for code, name, index_code, index_name, list_date, _count in cur.fetchall():
                code = str(code)
                if is_overseas_etf(code, str(name or ""), str(index_name or ""), str(index_code or "")):
                    continue
                metas[code] = {
                    "name": str(name or code),
                    "index_code": str(index_code or ""),
                    "index_name": str(index_name or ""),
                    "list_date": list_date,
                }
            series: dict[str, list[tuple[date, float]]] = {code: [] for code in metas}
            codes = list(metas)
            for start in range(0, len(codes), 400):
                chunk = codes[start : start + 400]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT ts_code, trade_date, pct_chg
                    FROM fund_daily
                    WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                    ORDER BY ts_code, trade_date
                    """,
                    chunk,
                )
                daily_by_code: dict[str, list[tuple[date, float | None]]] = {
                    code: [] for code in chunk
                }
                for code, trade_date, pct_chg in cur.fetchall():
                    daily_by_code[str(code)].append(
                        (trade_date, float(pct_chg) if pct_chg is not None else None)
                    )
                for code, rows in daily_by_code.items():
                    series[code].extend(pct_chg_compounded_series(rows))
            cur.execute(
                """
                SELECT trade_date, close
                FROM index_daily
                WHERE ts_code=%s AND close IS NOT NULL
                ORDER BY trade_date
                """,
                (CS300_CODE,),
            )
            series[CS300_CODE] = [(trade_date, float(close)) for trade_date, close in cur.fetchall()]
        return metas, series
    finally:
        conn.close()


def period_cross_section(
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
) -> dict[str, Any]:
    returns = []
    for code, meta in metas.items():
        list_date = meta.get("list_date")
        if list_date is not None and list_date > start:
            continue
        ret = period_return(series.get(code, []), start, end)
        if ret is not None:
            returns.append((ret, code))
    returns.sort(reverse=True)
    values = [item[0] for item in returns]
    if not values:
        return {"available": False, "count": 0}
    top20_count = max(1, int(len(values) * 0.20))
    top20_mean = statistics.mean(values[:top20_count])
    median = statistics.median(values)
    top10 = returns[:10]
    return {
        "available": True,
        "count": len(values),
        "median_return": median,
        "top20_mean_return": top20_mean,
        "top20_minus_median": top20_mean - median,
        "top10_equal_return": statistics.mean(item[0] for item in top10) if top10 else None,
        "top5_equal_return": statistics.mean(item[0] for item in returns[:5]) if len(returns) >= 5 else None,
        "top10_positive_count": sum(1 for item in top10 if item[0] > 0),
        "top10_codes": [code for _ret, code in top10],
    }


def strong_risk_ban(row: dict[str, Any]) -> bool:
    flags = set(row.get("active_risk_flags") or [])
    flag_ban = bool(row.get("bear_state")) or bool(
        flags
        & {
            "crisis_continuation_flag",
            "credit_contraction_tightening_flag",
            "domestic_liquidity_stress_flag",
        }
    )
    if flag_ban:
        return True
    for stage in (row.get("exposure_formation") or {}).get("trace", []):
        if stage.get("stage") != "hard_exit" or not stage.get("active"):
            continue
        details = stage.get("details") or {}
        if details.get("flags"):
            return True
    context = row.get("scorecard_context") or {}
    if context.get("allocation_entry") is not False:
        return False
    reasons = set(context.get("rebalance_reasons") or [])
    return bool(reasons & SCORECARD_STRONG_RISK_REASONS)


def classify_capture_failure(row: dict[str, Any], structural: dict[str, Any]) -> str:
    if not structural.get("cross_section_available", True):
        return "etf_pool_coverage_insufficient"
    exposure = float(row.get("exposure") or 0.0)
    if exposure < 0.50:
        return "risk_control_low_exposure"
    selected = set(row.get("equity_etf_weights") or {})
    top10 = set(structural.get("top10_codes") or [])
    if selected and selected.isdisjoint(top10):
        return "scorecard_missed_mainline"
    return "rebalance_or_weighting_lag"


def structural_rows_for_case(
    case: dict[str, Any],
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[date, float]]],
    gate: StructuralAdaptationGate,
    mainline_observations: list[dict[str, Any]] | None = None,
    cross_section_cache: dict[tuple[date, date], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = case_period_rows(case)
    annotated = []
    for idx, row in enumerate(rows):
        start = parse_date(row["decision_date"])
        end = parse_date(rows[idx + 1]["decision_date"]) if idx + 1 < len(rows) else parse_date(case["sample_end"])
        broad_return = period_return(series[CS300_CODE], start, end)
        cache_key = (start, end)
        if cross_section_cache is not None and cache_key in cross_section_cache:
            cross = cross_section_cache[cache_key]
        else:
            cross = period_cross_section(metas, series, start, end)
            if cross_section_cache is not None:
                cross_section_cache[cache_key] = cross
        if broad_return is None or not cross.get("available"):
            continue
        top10_equal = cross.get("top10_equal_return")
        structural = (
            broad_return < gate.structural_broad_return_max
            and cross["top20_minus_median"] >= gate.structural_cross_section_spread_min
            and cross["top10_positive_count"] >= gate.structural_top_positive_min_count
            and broad_return > gate.systemic_crash_broad_return_min
            and cross["median_return"] > gate.systemic_crash_median_return_min
        )
        if not structural:
            continue
        signal_date = parse_date(row.get("rebalance_anchor", row["decision_date"]))
        mainline_codes: list[str] = []
        if mainline_observations is not None:
            scores = weighted_structural_mainline_scores(
                mainline_observations,
                signal_date,
            )
            mainline_codes = sorted(
                scores,
                key=lambda code: (round(scores[code], 12), code),
                reverse=True,
            )[:5]
        portfolio_return = float(row.get("realized_portfolio_return") or 0.0)
        capture_ratio = (
            portfolio_return / top10_equal
            if top10_equal is not None and top10_equal > 0
            else None
        )
        ban = strong_risk_ban(row)
        enriched = {
            "phase_month_offset": case.get("phase_month_offset"),
            "execution_lag_days": case.get("execution_lag_days"),
            "decision_date": row.get("decision_date"),
            "signal_date": signal_date.isoformat(),
            "period_end_date": end.isoformat(),
            "broad_return": broad_return,
            "portfolio_return": portfolio_return,
            "exposure": float(row.get("exposure") or 0.0),
            "strong_risk_ban": ban,
            "capture_ratio_vs_top10": capture_ratio,
            "top10_equal_return": top10_equal,
            "top5_equal_return": cross.get("top5_equal_return"),
            "top20_minus_median": cross["top20_minus_median"],
            "median_etf_return": cross["median_return"],
            "top10_positive_count": cross["top10_positive_count"],
            "top10_codes": cross["top10_codes"],
            "cross_section_available": True,
            "structural_mainline_top5_codes": mainline_codes,
            "structural_mainline_top5_overlap_count": len(
                set(mainline_codes).intersection(cross["top10_codes"])
            ),
            "selected_codes": list((row.get("equity_etf_weights") or {}).keys()),
            "outperformed_broad": portfolio_return > broad_return,
            "low_exposure": float(row.get("exposure") or 0.0) < gate.low_exposure_threshold,
        }
        capture_failed = (
            capture_ratio is None
            or capture_ratio < gate.structural_capture_ratio_min
        )
        if capture_failed and not ban:
            enriched["failure_reason"] = classify_capture_failure(row, enriched)
        annotated.append(enriched)
    return annotated


def validate_structural_capture(
    cases: list[dict[str, Any]],
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[date, float]]],
    gate: StructuralAdaptationGate,
    mainline_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    per_case = []
    all_structural_rows = []
    cross_section_cache: dict[tuple[date, date], dict[str, Any]] = {}
    for case in cases:
        rows = structural_rows_for_case(
            case,
            metas,
            series,
            gate,
            mainline_observations,
            cross_section_cache,
        )
        all_structural_rows.extend(rows)
        applicable = [row for row in rows if not row["strong_risk_ban"]]
        exposures = [row["exposure"] for row in applicable]
        capture_checks = [
            row
            for row in applicable
            if row["capture_ratio_vs_top10"] is not None
            and row["capture_ratio_vs_top10"] >= gate.structural_capture_ratio_min
        ]
        wins = [row for row in applicable if row["outperformed_broad"]]
        low_exposure_streak = max_consecutive_true([row["low_exposure"] for row in applicable])
        exposure_median = statistics.median(exposures) if exposures else None
        capture_pass_rate = len(capture_checks) / len(applicable) if applicable else None
        win_rate = len(wins) / len(applicable) if applicable else None
        failures = []
        if applicable and (exposure_median is None or exposure_median < gate.structural_exposure_median_min):
            failures.append("structural_exposure_median")
        if applicable and (capture_pass_rate is None or capture_pass_rate < gate.structural_capture_pass_rate_min):
            failures.append("structural_capture_ratio")
        if applicable and (win_rate is None or win_rate < gate.structural_benchmark_win_rate_min):
            failures.append("structural_benchmark_win_rate")
        if low_exposure_streak > gate.max_consecutive_low_exposure_structural_quarters:
            failures.append("structural_low_exposure_streak")
        per_case.append(
            {
                "phase_month_offset": case.get("phase_month_offset"),
                "execution_lag_days": case.get("execution_lag_days"),
                "structural_quarters": len(rows),
                "applicable_structural_quarters": len(applicable),
                "median_structural_exposure": exposure_median,
                "capture_pass_rate": capture_pass_rate,
                "benchmark_win_rate": win_rate,
                "max_consecutive_low_exposure_structural_quarters": low_exposure_streak,
                "structural_mainline_top5_hit_rate": (
                    sum(
                        1
                        for row in applicable
                        if row.get("structural_mainline_top5_overlap_count", 0) > 0
                    )
                    / len(applicable)
                    if applicable
                    else None
                ),
                "worst_capture_quarter": min(
                    applicable,
                    key=lambda row: row["capture_ratio_vs_top10"]
                    if row["capture_ratio_vs_top10"] is not None
                    else -999.0,
                )
                if applicable
                else None,
                "failures": failures,
                "passed": not failures and bool(applicable),
            }
        )
    failed_cases = [item for item in per_case if not item["passed"]]
    failure_reasons: dict[str, int] = {}
    for row in all_structural_rows:
        reason = row.get("failure_reason")
        if reason:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    return {
        "case_count": len(per_case),
        "structural_capture_pass_count": sum(1 for item in per_case if item["passed"]),
        "structural_capture_passed": bool(per_case) and all(item["passed"] for item in per_case),
        "total_structural_quarters": len(all_structural_rows),
        "failed_structural_cases": failed_cases,
        "failure_reason_counts": failure_reasons,
        "structural_mainline_top5_hit_rate": (
            sum(
                1
                for row in all_structural_rows
                if row.get("structural_mainline_top5_overlap_count", 0) > 0
                and not row["strong_risk_ban"]
            )
            / max(sum(1 for row in all_structural_rows if not row["strong_risk_ban"]), 1)
            if all_structural_rows
            else None
        ),
        "worst_structural_case": min(
            per_case,
            key=lambda item: (
                item["capture_pass_rate"] if item["capture_pass_rate"] is not None else -1.0,
                item["median_structural_exposure"] if item["median_structural_exposure"] is not None else -1.0,
            ),
        )
        if per_case
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--result-index", type=int, default=0)
    parser.add_argument("--min-rows", type=int, default=60)
    parser.add_argument("--output-prefix", type=Path)
    args = parser.parse_args()

    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    result = report_result(payload, args.result_index)
    cases = result["cases"]
    if not all(case.get("decision_rows") for case in cases):
        raise ValueError("report must include decision_rows; rerun source backtest with --include-decision-rows")
    metas, series = load_domestic_passive_etf_series(args.min_rows)
    mainline_observations = load_candidate_observations(SHARE_V5_DATASET)
    gate = STRUCTURAL_ADAPTATION_GATE
    recent = validate_case_matrix_adaptation(cases, gate=gate)
    structural = validate_structural_capture(
        cases,
        metas,
        series,
        gate,
        mainline_observations,
    )
    objective_summary = {
        "original_hard_gate_passed": bool(result["summary"].get("objective_met")),
        "recent_survival_passed": recent["recent_survival_passed"],
        "structural_capture_passed": structural["structural_capture_passed"],
    }
    objective_summary["adaptation_objective_met"] = all(objective_summary.values())
    output = {
        "source_report": str(report_path.relative_to(ROOT)),
        "rule_name": result["rule"]["name"],
        "selector_policy": result["selector_policy"]["name"],
        "direct_etf_policy": result.get("direct_etf_policy", {}).get("name"),
        "defensive_policy": result["defensive_policy"]["name"],
        "gate": gate.__dict__,
        "base_summary": result["summary"],
        "objective_summary": objective_summary,
        "recent_survival": recent,
        "structural_capture": structural,
    }
    prefix = args.output_prefix or report_path.with_name(report_path.stem.replace("_report", "_structural_adaptation"))
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    out_path = Path(f"{prefix}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(
        "adaptation_objective_met="
        f"{objective_summary['adaptation_objective_met']} "
        f"recent={recent['recent_survival_pass_count']}/{recent['case_count']} "
        f"structural={structural['structural_capture_pass_count']}/{structural['case_count']} "
        f"worst_10y_ann={recent['worst_recent_10y_annualized_return']:.4f} "
        f"worst_5y_cum={recent['worst_recent_5y_cumulative_return']:.4f} "
        f"worst_roll5y={recent['worst_rolling_5y_annualized_return']:.4f}"
    )
    print(f"Wrote {out_path}")
    return 0 if objective_summary["adaptation_objective_met"] else 1


def report_result(payload: dict[str, Any], result_index: int) -> dict[str, Any]:
    if "results" in payload:
        return payload["results"][result_index]
    if "cases" in payload:
        return payload
    raise ValueError("report must contain either results[] or top-level cases")


if __name__ == "__main__":
    raise SystemExit(main())

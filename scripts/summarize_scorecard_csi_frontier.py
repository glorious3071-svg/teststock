#!/usr/bin/env python3
"""Summarize the scorecard + CSI strict-search frontier.

The goal target is intentionally hard: every timing variant must finish above
4000w with max drawdown no worse than -10%.  This script does not search a new
strategy.  It consolidates the existing experiment outputs so the next research
step is driven by the observed risk/return frontier instead of another isolated
backtest.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

BACKTEST_DIR = ROOT / "data" / "backtests"
DOC_DIR = ROOT / "docs" / "design"

DEFAULT_TARGET_FINAL_WAN = 4000.0
DEFAULT_TARGET_MDD = -0.10

OUT_JSON = BACKTEST_DIR / "scorecard_csi_frontier_summary.json"
OUT_CSV = BACKTEST_DIR / "scorecard_csi_frontier_summary.csv"
OUT_MD = DOC_DIR / "scorecard_csi_frontier_summary.md"
PORTFOLIO_DIR = ROOT / "data" / "portfolio"


@dataclass(frozen=True)
class Candidate:
    experiment: str
    name: str
    source_file: str
    pass_count: int
    case_count: int
    min_final_capital_wan: float
    median_final_capital_wan: float | None
    worst_max_drawdown: float
    median_max_drawdown: float | None
    min_annualized_return: float | None
    capital_gap_wan: float
    drawdown_gap_pct: float
    pass_rate: float
    strict_target_met: bool
    pareto_frontier: bool
    extra: dict[str, Any]


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def experiment_name(path: Path) -> str:
    name = path.stem
    if name == "scorecard_csi_overlay_search":
        return "overlay"
    prefix = "scorecard_csi_"
    suffix = "_search"
    if name.startswith(prefix):
        name = name[len(prefix) :]
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def candidate_from_row(path: Path, row: dict[str, str], target_final_wan: float, target_mdd: float) -> Candidate | None:
    min_final = parse_float(row.get("min_final_capital_wan"))
    worst_mdd = parse_float(row.get("worst_max_drawdown"))
    if min_final is None or worst_mdd is None:
        return None

    pass_count = parse_int(row.get("pass_count", row.get("strict_pass_count")))
    case_count = parse_int(row.get("count", row.get("strict_case_count")))
    strict_target_met = (
        case_count > 0
        and pass_count == case_count
        and min_final >= target_final_wan
        and worst_mdd >= target_mdd
    )
    extra = {
        key: value
        for key, value in row.items()
        if key
        not in {
            "name",
            "pass_count",
            "count",
            "strict_pass_count",
            "strict_case_count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "target_met",
        }
        and value not in (None, "")
    }
    return Candidate(
        experiment=experiment_name(path),
        name=row.get("name") or path.stem,
        source_file=str(path.relative_to(ROOT)),
        pass_count=pass_count,
        case_count=case_count,
        min_final_capital_wan=min_final,
        median_final_capital_wan=parse_float(row.get("median_final_capital_wan")),
        worst_max_drawdown=worst_mdd,
        median_max_drawdown=parse_float(row.get("median_max_drawdown")),
        min_annualized_return=parse_float(row.get("min_annualized_return")),
        capital_gap_wan=max(0.0, target_final_wan - min_final),
        drawdown_gap_pct=max(0.0, target_mdd - worst_mdd) * 100.0,
        pass_rate=(pass_count / case_count) if case_count else 0.0,
        strict_target_met=strict_target_met,
        pareto_frontier=False,
        extra=extra,
    )


def load_candidates(target_final_wan: float, target_mdd: float) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path in sorted(BACKTEST_DIR.glob("scorecard_csi_*_search.csv")):
        if path.name == OUT_CSV.name:
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                candidate = candidate_from_row(path, row, target_final_wan, target_mdd)
                if candidate is not None:
                    candidates.append(candidate)
    return candidates


def with_frontier_flags(candidates: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    for item in candidates:
        dominated = False
        for other in candidates:
            if other is item:
                continue
            better_or_equal = (
                other.min_final_capital_wan >= item.min_final_capital_wan
                and other.worst_max_drawdown >= item.worst_max_drawdown
            )
            strictly_better = (
                other.min_final_capital_wan > item.min_final_capital_wan
                or other.worst_max_drawdown > item.worst_max_drawdown
            )
            if better_or_equal and strictly_better:
                dominated = True
                break
        values = asdict(item)
        values["pareto_frontier"] = not dominated
        out.append(Candidate(**values))
    return out


def candidate_sort_key(item: Candidate) -> tuple[int, float, float, float]:
    return (
        int(item.strict_target_met),
        item.min_final_capital_wan,
        item.worst_max_drawdown,
        item.pass_rate,
    )


def summarize_group(items: list[Candidate], target_final_wan: float, target_mdd: float) -> dict[str, Any]:
    best_final = max(items, key=lambda item: (item.min_final_capital_wan, item.worst_max_drawdown))
    best_drawdown = max(items, key=lambda item: (item.worst_max_drawdown, item.min_final_capital_wan))
    best_balance = min(
        items,
        key=lambda item: (
            item.capital_gap_wan / target_final_wan + item.drawdown_gap_pct / abs(target_mdd * 100.0),
            -item.min_final_capital_wan,
            -item.worst_max_drawdown,
        ),
    )
    return {
        "count": len(items),
        "strict_passes": sum(1 for item in items if item.strict_target_met),
        "best_min_final": slim_candidate(best_final),
        "best_drawdown": slim_candidate(best_drawdown),
        "best_balance": slim_candidate(best_balance),
    }


def slim_candidate(item: Candidate) -> dict[str, Any]:
    return {
        "experiment": item.experiment,
        "name": item.name,
        "pass_count": item.pass_count,
        "case_count": item.case_count,
        "min_final_capital_wan": item.min_final_capital_wan,
        "worst_max_drawdown": item.worst_max_drawdown,
        "capital_gap_wan": item.capital_gap_wan,
        "drawdown_gap_pct": item.drawdown_gap_pct,
        "strict_target_met": item.strict_target_met,
        "source_file": item.source_file,
    }


def threshold_summary(candidates: list[Candidate], target_final_wan: float, target_mdd: float) -> dict[str, Any]:
    mdd_thresholds = [target_mdd, -0.12, -0.20, -0.30, -0.40]
    capital_thresholds = [target_final_wan, 3000.0, 2000.0, 1000.0]
    by_mdd = {}
    for threshold in mdd_thresholds:
        eligible = [item for item in candidates if item.worst_max_drawdown >= threshold]
        by_mdd[f"mdd_ge_{threshold:.0%}"] = {
            "eligible_count": len(eligible),
            "best_min_final": slim_candidate(max(eligible, key=lambda item: item.min_final_capital_wan)) if eligible else None,
        }
    by_capital = {}
    for threshold in capital_thresholds:
        eligible = [item for item in candidates if item.min_final_capital_wan >= threshold]
        by_capital[f"min_final_ge_{threshold:.0f}w"] = {
            "eligible_count": len(eligible),
            "best_drawdown": slim_candidate(max(eligible, key=lambda item: (item.worst_max_drawdown, item.min_final_capital_wan))) if eligible else None,
        }
    return {"by_mdd_threshold": by_mdd, "by_capital_threshold": by_capital}


def load_strict_report() -> dict[str, Any] | None:
    path = BACKTEST_DIR / "scorecard_csi_generalization_report.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path.relative_to(ROOT)),
        "stable": bool(payload.get("validation", {}).get("stable")),
        "checks": payload.get("validation", {}).get("checks", {}),
        "summaries": payload.get("summaries", {}),
    }


def latest_json(pattern: str) -> tuple[Path, dict[str, Any]] | None:
    paths = sorted(PORTFOLIO_DIR.glob(pattern))
    if not paths:
        return None
    path = paths[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def latest_backtest_json(pattern: str) -> tuple[Path, dict[str, Any]] | None:
    paths = sorted(BACKTEST_DIR.glob(pattern))
    if not paths:
        return None
    path = paths[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def pre_option_regime_target_ready(
    report_pair: tuple[Path, dict[str, Any]] | None,
    target_pair: tuple[Path, dict[str, Any]] | None,
) -> tuple[bool, dict[str, Any]]:
    if not report_pair or not target_pair:
        return False, {
            "status": "missing_report_or_target",
            "report_path": str(report_pair[0].relative_to(ROOT)) if report_pair else None,
            "target_path": str(target_pair[0].relative_to(ROOT)) if target_pair else None,
        }

    report_path, report = report_pair
    target_path, target = target_pair
    target_rule = target.get("rule_name")
    matching = [
        item
        for item in report.get("results", [])
        if (item.get("rule") or {}).get("name") == target_rule
    ]
    target_summary = target.get("validated_summary") or {}
    report_summary = (matching[0].get("summary") if matching else None) or target_summary
    rows = target.get("rows", [])
    asset_types = {row.get("asset_type") for row in rows}
    strict_pass = (
        int(report_summary.get("pass_count") or 0) == int(report_summary.get("count") or -1)
        and float(report_summary.get("min_final_capital_wan") or 0.0) >= DEFAULT_TARGET_FINAL_WAN
        and float(report_summary.get("worst_max_drawdown") or -1.0) >= DEFAULT_TARGET_MDD
    )
    concrete_rows = (
        target.get("model_status") == "strict_backtest_pass_execution_targets_generated"
        and target.get("qqq_option_package", {}).get("status") == "selected"
        and target.get("cn_option_package", {}).get("status") == "selected"
        and "external_etf_underlying" in asset_types
        and "us_option_package_leg" in asset_types
        and "cn_etf_option_package_leg" in asset_types
        and "option_protected_sleeve" not in asset_types
        and 99.0 <= float(target.get("net_position_weight_pct") or 0.0) <= 101.0
    )
    ready = bool(strict_pass and concrete_rows)
    return ready, {
        "status": "ready" if ready else "not_ready",
        "report_path": str(report_path.relative_to(ROOT)),
        "target_path": str(target_path.relative_to(ROOT)),
        "experiment": "pre_option_regime_defense_switch_50_to_300_misszero",
        "rule_name": target_rule,
        "strict_pass": strict_pass,
        "concrete_rows": concrete_rows,
        "asset_types": sorted(asset_types),
        "net_position_weight_pct": target.get("net_position_weight_pct"),
        "summary": report_summary,
        "qqq_option_package": target.get("qqq_option_package"),
        "cn_option_package": target.get("cn_option_package"),
    }


def load_cffex_index_future_coverage() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for ts_code, name in [
                ("IF.CFX", "沪深300股指期货连续"),
                ("IH.CFX", "上证50股指期货连续"),
                ("IC.CFX", "中证500股指期货连续"),
                ("IM.CFX", "中证1000股指期货连续"),
            ]:
                cur.execute(
                    """
                    SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
                    FROM fut_daily
                    WHERE ts_code = %s
                    """,
                    (ts_code,),
                )
                count, min_date, max_date = cur.fetchone()
                rows.append(
                    {
                        "ts_code": ts_code,
                        "name": name,
                        "rows": int(count or 0),
                        "min_date": str(min_date) if min_date else None,
                        "max_date": str(max_date) if max_date else None,
                    }
                )
    finally:
        conn.close()
    return rows


def load_execution_readiness() -> dict[str, Any]:
    audit_pair = latest_json("csi_defined_loss_execution_audit_*.json")
    stress_pair = latest_json("csi_defined_loss_replication_stress_*.json")
    ranking_pair = latest_json("option_package_stress_ranking_*.json")
    floor_cost_pair = (
        latest_json("option_package_floor_cost_csihedge_*.json")
        or latest_json("option_package_floor_cost_overhedge_*.json")
        or latest_json("option_package_floor_cost_*.json")
    )
    cn_put_pair = latest_json("cn_etf_put_hedge_search_*.json")
    cn_package_pair = latest_json("cn_etf_option_package_hedge_search_*.json")
    cn_history_coverage_pair = latest_json("cn_option_history_coverage_audit_*.json")
    cn_real_zero_pair = (
        latest_backtest_json("scorecard_csi_cn_option_package_real_history_switch_50_to_300_misszero.json")
        or latest_backtest_json("scorecard_csi_cn_option_package_real_history_misszero.json")
    )
    cn_real_proxy_pair = (
        latest_backtest_json("scorecard_csi_cn_option_package_real_history_switch_50_to_300_missproxy.json")
        or latest_backtest_json("scorecard_csi_cn_option_package_real_history_missproxy.json")
    )
    cn_real_tipp_pair = latest_backtest_json("scorecard_csi_cn_option_package_real_tipp_switch_50_to_300_misszero_report.json")
    cn_real_preguard_pair = latest_backtest_json(
        "scorecard_csi_cn_option_package_real_pre_guard_switch_50_to_300_misszero_report.json"
    )
    cn_real_daily_stop_pair = latest_backtest_json(
        "scorecard_csi_cn_option_package_real_daily_stop_proxy_switch_50_to_300_misszero_report.json"
    )
    cn_daily_mtm_stop_pair = latest_backtest_json(
        "scorecard_csi_cn_option_package_daily_mtm_stop_switch_50_to_300_misszero_report.json"
    )
    pre_option_report_pair = latest_backtest_json(
        "scorecard_csi_pre_option_regime_defense_switch_50_to_300_misszero_report.json"
    )
    pre_option_target_pair = latest_json("csi_pre_option_regime_targets_*.json")

    audit = audit_pair[1] if audit_pair else None
    stress = stress_pair[1] if stress_pair else None
    ranking = ranking_pair[1] if ranking_pair else None
    floor_cost = floor_cost_pair[1] if floor_cost_pair else None
    cn_put = cn_put_pair[1] if cn_put_pair else None
    cn_package = cn_package_pair[1] if cn_package_pair else None
    cn_history_coverage = cn_history_coverage_pair[1] if cn_history_coverage_pair else None
    cn_real_zero = cn_real_zero_pair[1] if cn_real_zero_pair else None
    cn_real_proxy = cn_real_proxy_pair[1] if cn_real_proxy_pair else None
    cn_real_tipp = cn_real_tipp_pair[1] if cn_real_tipp_pair else None
    cn_real_preguard = cn_real_preguard_pair[1] if cn_real_preguard_pair else None
    cn_real_daily_stop = cn_real_daily_stop_pair[1] if cn_real_daily_stop_pair else None
    cn_daily_mtm_stop = cn_daily_mtm_stop_pair[1] if cn_daily_mtm_stop_pair else None
    pre_option_ready, pre_option_target = pre_option_regime_target_ready(pre_option_report_pair, pre_option_target_pair)
    execution_validated = bool(audit and audit.get("execution_validated"))
    replication_floor_validated = bool(stress and stress.get("status") == "replication_floor_validated")
    budget_option_packages_all_pass = bool(ranking and int(ranking.get("all_stress_floor_pass_count") or 0) > 0)
    legacy_production_ready = execution_validated and replication_floor_validated
    production_ready = legacy_production_ready or pre_option_ready
    cffex_coverage = load_cffex_index_future_coverage()
    return {
        "production_ready": production_ready,
        "legacy_production_ready": legacy_production_ready,
        "pre_option_regime_target_ready": pre_option_ready,
        "pre_option_regime_target": pre_option_target,
        "execution_validated": execution_validated,
        "replication_floor_validated": replication_floor_validated,
        "budget_option_packages_all_pass": budget_option_packages_all_pass,
        "latest_audit_path": str(audit_pair[0].relative_to(ROOT)) if audit_pair else None,
        "latest_replication_stress_path": str(stress_pair[0].relative_to(ROOT)) if stress_pair else None,
        "latest_option_package_stress_ranking_path": str(ranking_pair[0].relative_to(ROOT)) if ranking_pair else None,
        "latest_option_package_floor_cost_path": str(floor_cost_pair[0].relative_to(ROOT)) if floor_cost_pair else None,
        "latest_cn_etf_put_hedge_search_path": str(cn_put_pair[0].relative_to(ROOT)) if cn_put_pair else None,
        "latest_cn_etf_option_package_hedge_search_path": str(cn_package_pair[0].relative_to(ROOT)) if cn_package_pair else None,
        "latest_cn_option_history_coverage_audit_path": str(cn_history_coverage_pair[0].relative_to(ROOT)) if cn_history_coverage_pair else None,
        "latest_cn_option_package_real_history_zero_path": str(cn_real_zero_pair[0].relative_to(ROOT)) if cn_real_zero_pair else None,
        "latest_cn_option_package_real_history_proxy_path": str(cn_real_proxy_pair[0].relative_to(ROOT)) if cn_real_proxy_pair else None,
        "latest_cn_option_package_real_tipp_path": str(cn_real_tipp_pair[0].relative_to(ROOT)) if cn_real_tipp_pair else None,
        "latest_cn_option_package_real_preguard_path": str(cn_real_preguard_pair[0].relative_to(ROOT)) if cn_real_preguard_pair else None,
        "latest_cn_option_package_real_daily_stop_proxy_path": (
            str(cn_real_daily_stop_pair[0].relative_to(ROOT)) if cn_real_daily_stop_pair else None
        ),
        "latest_cn_option_package_daily_mtm_stop_path": (
            str(cn_daily_mtm_stop_pair[0].relative_to(ROOT)) if cn_daily_mtm_stop_pair else None
        ),
        "latest_pre_option_regime_report_path": (
            str(pre_option_report_pair[0].relative_to(ROOT)) if pre_option_report_pair else None
        ),
        "latest_pre_option_regime_target_path": (
            str(pre_option_target_pair[0].relative_to(ROOT)) if pre_option_target_pair else None
        ),
        "target_rule": (
            pre_option_target.get("rule_name")
            if pre_option_ready
            else (audit or stress or ranking or {}).get("target_rule") or (audit or {}).get("target", {}).get("rule_name")
        ),
        "audit_status": audit.get("status") if audit else None,
        "audit_blockers": audit.get("blockers", []) if audit else [],
        "replication_stress_status": stress.get("status") if stress else None,
        "replication_floor_pass_count": stress.get("floor_pass_count") if stress else None,
        "replication_scenario_count": stress.get("scenario_count") if stress else None,
        "replication_worst_return_pct": (
            (stress.get("worst_scenario") or {}).get("total_return_pct") if stress else None
        ),
        "ranking_candidate_count": ranking.get("candidate_count") if ranking else None,
        "ranking_all_stress_floor_pass_count": ranking.get("all_stress_floor_pass_count") if ranking else None,
        "ranking_best_candidate": ranking.get("best_candidate") if ranking else None,
        "floor_cost_candidate_count": floor_cost.get("candidate_count") if floor_cost else None,
        "floor_cost_all_stress_floor_pass_count": floor_cost.get("all_stress_floor_pass_count") if floor_cost else None,
        "floor_cost_cheapest_all_pass": floor_cost.get("cheapest_all_stress_floor_pass") if floor_cost else None,
        "floor_cost_best_stress_candidate": floor_cost.get("best_stress_candidate") if floor_cost else None,
        "floor_cost_best_max_csi_gross_candidate": floor_cost.get("best_max_csi_gross_candidate") if floor_cost else None,
        "cn_etf_put_status": cn_put.get("status") if cn_put else None,
        "cn_etf_put_candidate_count": cn_put.get("candidate_count") if cn_put else None,
        "cn_etf_put_all_pass_count": cn_put.get("all_pass_count") if cn_put else None,
        "cn_etf_put_budget_all_pass_count": cn_put.get("budget_all_pass_count") if cn_put else None,
        "cn_etf_put_cheapest_all_pass": cn_put.get("cheapest_all_pass") if cn_put else None,
        "cn_etf_put_cheapest_budget_all_pass": cn_put.get("cheapest_budget_all_pass") if cn_put else None,
        "cn_etf_option_package_status": cn_package.get("status") if cn_package else None,
        "cn_etf_option_package_candidate_count": cn_package.get("candidate_count") if cn_package else None,
        "cn_etf_option_package_all_pass_count": cn_package.get("all_pass_count") if cn_package else None,
        "cn_etf_option_package_budget_all_pass_count": cn_package.get("budget_all_pass_count") if cn_package else None,
        "cn_etf_option_package_cheapest_budget_all_pass": cn_package.get("cheapest_budget_all_pass") if cn_package else None,
        "cn_etf_option_package_best_candidate": cn_package.get("best_candidate") if cn_package else None,
        "cn_option_history_sample_count": cn_history_coverage.get("sample_count") if cn_history_coverage else None,
        "cn_option_history_samples_with_daily": cn_history_coverage.get("samples_with_option_daily") if cn_history_coverage else None,
        "cn_option_history_samples_with_terms": (
            cn_history_coverage.get("samples_with_any_contract_terms")
            if cn_history_coverage and cn_history_coverage.get("samples_with_any_contract_terms") is not None
            else cn_history_coverage.get("samples_with_contract_terms")
            if cn_history_coverage
            else None
        ),
        "cn_option_history_contract_terms_gap": cn_history_coverage.get("contract_terms_gap") if cn_history_coverage else None,
        "cn_real_zero_quote_dates_available": cn_real_zero.get("quote_dates_available") if cn_real_zero else None,
        "cn_real_zero_quote_dates_used": cn_real_zero.get("quote_dates_used") if cn_real_zero else None,
        "cn_real_zero_missing_reasons": cn_real_zero.get("missing_reasons") if cn_real_zero else None,
        "cn_real_zero_best": cn_real_zero.get("results", [{}])[0] if cn_real_zero and cn_real_zero.get("results") else None,
        "cn_real_proxy_best": cn_real_proxy.get("results", [{}])[0] if cn_real_proxy and cn_real_proxy.get("results") else None,
        "cn_real_tipp_best": cn_real_tipp.get("results", [{}])[0] if cn_real_tipp and cn_real_tipp.get("results") else None,
        "cn_real_tipp_raw_diag": cn_real_tipp.get("raw_return_diagnostics") if cn_real_tipp else None,
        "cn_real_preguard_best": cn_real_preguard.get("results", [{}])[0] if cn_real_preguard and cn_real_preguard.get("results") else None,
        "cn_real_daily_stop_best": (
            cn_real_daily_stop.get("results", [{}])[0]
            if cn_real_daily_stop and cn_real_daily_stop.get("results")
            else None
        ),
        "cn_real_daily_stop_diag": cn_real_daily_stop.get("proxy_stop_diagnostics") if cn_real_daily_stop else None,
        "cn_daily_mtm_stop_best": (
            cn_daily_mtm_stop.get("results", [{}])[0]
            if cn_daily_mtm_stop and cn_daily_mtm_stop.get("results")
            else None
        ),
        "cn_daily_mtm_stop_coverage": cn_daily_mtm_stop.get("daily_mtm_coverage") if cn_daily_mtm_stop else None,
        "cn_daily_mtm_stop_diag": cn_daily_mtm_stop.get("daily_mtm_stop_diagnostics") if cn_daily_mtm_stop else None,
        "cffex_index_future_coverage": cffex_coverage,
        "if_continuous_available": bool(
            next((row for row in cffex_coverage if row["ts_code"] == "IF.CFX" and row["rows"] > 0), None)
        ),
    }


def write_csv(candidates: list[Candidate], path: Path) -> None:
    fields = [
        "pareto_frontier",
        "strict_target_met",
        "experiment",
        "name",
        "pass_count",
        "case_count",
        "pass_rate",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "min_annualized_return",
        "capital_gap_wan",
        "drawdown_gap_pct",
        "source_file",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in sorted(candidates, key=candidate_sort_key, reverse=True):
            row = asdict(item)
            row.pop("extra", None)
            writer.writerow({key: row.get(key) for key in fields})


def write_markdown(payload: dict[str, Any], path: Path) -> None:
    best_final = payload["best_overall"]["best_min_final"]
    best_mdd = payload["best_overall"]["best_drawdown"]
    best_balance = payload["best_overall"]["best_balance"]
    strict = payload.get("strict_generalization")
    readiness = payload.get("execution_readiness", {})
    frontier = payload["pareto_frontier"][:12]
    lines = [
        "# Scorecard + CSI Frontier Summary",
        "",
        "This file is generated by `scripts/summarize_scorecard_csi_frontier.py`.",
        "",
        "## Target",
        "",
        f"- minimum final capital: {payload['target']['min_final_capital_wan']:.0f}w",
        f"- worst max drawdown: >= {payload['target']['max_drawdown_floor']:.1%}",
        "- pass condition: every strict timing case passes",
        "",
        "## Current Frontier",
        "",
        f"- candidates consolidated: {payload['candidate_count']}",
        f"- strict target passes: {payload['strict_pass_count']}",
        f"- production-ready passes: {payload['production_ready_pass_count']}",
        (
            f"- best capital floor: `{best_final['experiment']}/{best_final['name']}` "
            f"at {best_final['min_final_capital_wan']:.1f}w, "
            f"{best_final['worst_max_drawdown']:.1%} worst drawdown"
        ),
        (
            f"- best drawdown candidate: `{best_mdd['experiment']}/{best_mdd['name']}` "
            f"at {best_mdd['min_final_capital_wan']:.1f}w, "
            f"{best_mdd['worst_max_drawdown']:.1%} worst drawdown"
        ),
        (
            f"- best balanced gap: `{best_balance['experiment']}/{best_balance['name']}` "
            f"capital gap {best_balance['capital_gap_wan']:.1f}w, "
            f"drawdown gap {best_balance['drawdown_gap_pct']:.1f} percentage points"
        ),
        "",
        "## Strict Validator",
        "",
    ]
    if strict:
        lines.append(f"- stable: `{strict['stable']}`")
        for name, ok in strict.get("checks", {}).items():
            lines.append(f"- {name}: `{ok}`")
    else:
        lines.append("- strict report missing")
    lines.extend(["", "## Execution Readiness", ""])
    lines.append(f"- production_ready: `{readiness.get('production_ready')}`")
    lines.append(f"- legacy_production_ready: `{readiness.get('legacy_production_ready')}`")
    lines.append(f"- pre_option_regime_target_ready: `{readiness.get('pre_option_regime_target_ready')}`")
    lines.append(f"- execution_validated: `{readiness.get('execution_validated')}`")
    lines.append(f"- replication_floor_validated: `{readiness.get('replication_floor_validated')}`")
    lines.append(f"- budget_option_packages_all_pass: `{readiness.get('budget_option_packages_all_pass')}`")
    lines.append(f"- IF continuous futures available: `{readiness.get('if_continuous_available')}`")
    lines.append(f"- target_rule: `{readiness.get('target_rule')}`")
    if readiness.get("latest_audit_path"):
        lines.append(f"- latest_audit_path: `{readiness.get('latest_audit_path')}`")
    if readiness.get("latest_replication_stress_path"):
        lines.append(f"- latest_replication_stress_path: `{readiness.get('latest_replication_stress_path')}`")
    if readiness.get("latest_option_package_stress_ranking_path"):
        lines.append(
            f"- latest_option_package_stress_ranking_path: `{readiness.get('latest_option_package_stress_ranking_path')}`"
        )
    if readiness.get("latest_option_package_floor_cost_path"):
        lines.append(
            f"- latest_option_package_floor_cost_path: `{readiness.get('latest_option_package_floor_cost_path')}`"
        )
    if readiness.get("latest_cn_etf_put_hedge_search_path"):
        lines.append(
            f"- latest_cn_etf_put_hedge_search_path: `{readiness.get('latest_cn_etf_put_hedge_search_path')}`"
        )
    if readiness.get("latest_cn_etf_option_package_hedge_search_path"):
        lines.append(
            "- latest_cn_etf_option_package_hedge_search_path: "
            f"`{readiness.get('latest_cn_etf_option_package_hedge_search_path')}`"
        )
    if readiness.get("latest_cn_option_history_coverage_audit_path"):
        lines.append(
            "- latest_cn_option_history_coverage_audit_path: "
            f"`{readiness.get('latest_cn_option_history_coverage_audit_path')}`"
        )
    if readiness.get("latest_cn_option_package_real_history_zero_path"):
        lines.append(
            "- latest_cn_option_package_real_history_zero_path: "
            f"`{readiness.get('latest_cn_option_package_real_history_zero_path')}`"
        )
    if readiness.get("latest_cn_option_package_real_history_proxy_path"):
        lines.append(
            "- latest_cn_option_package_real_history_proxy_path: "
            f"`{readiness.get('latest_cn_option_package_real_history_proxy_path')}`"
        )
    if readiness.get("latest_cn_option_package_real_tipp_path"):
        lines.append(
            "- latest_cn_option_package_real_tipp_path: "
            f"`{readiness.get('latest_cn_option_package_real_tipp_path')}`"
        )
    if readiness.get("latest_cn_option_package_real_preguard_path"):
        lines.append(
            "- latest_cn_option_package_real_preguard_path: "
            f"`{readiness.get('latest_cn_option_package_real_preguard_path')}`"
        )
    if readiness.get("latest_cn_option_package_real_daily_stop_proxy_path"):
        lines.append(
            "- latest_cn_option_package_real_daily_stop_proxy_path: "
            f"`{readiness.get('latest_cn_option_package_real_daily_stop_proxy_path')}`"
        )
    if readiness.get("latest_cn_option_package_daily_mtm_stop_path"):
        lines.append(
            "- latest_cn_option_package_daily_mtm_stop_path: "
            f"`{readiness.get('latest_cn_option_package_daily_mtm_stop_path')}`"
        )
    if readiness.get("latest_pre_option_regime_report_path"):
        lines.append(
            "- latest_pre_option_regime_report_path: "
            f"`{readiness.get('latest_pre_option_regime_report_path')}`"
        )
    if readiness.get("latest_pre_option_regime_target_path"):
        lines.append(
            "- latest_pre_option_regime_target_path: "
            f"`{readiness.get('latest_pre_option_regime_target_path')}`"
        )
    pre_option_target = readiness.get("pre_option_regime_target") or {}
    if pre_option_target:
        summary = pre_option_target.get("summary") or {}
        qqq = pre_option_target.get("qqq_option_package") or {}
        cn_pkg = pre_option_target.get("cn_option_package") or {}
        lines.append(
            "- pre-option regime executable target: "
            f"status={pre_option_target.get('status')}, "
            f"{summary.get('pass_count')}/{summary.get('count')} pass, "
            f"min {float(summary.get('min_final_capital_wan') or 0):.1f}w, "
            f"worst MDD {float(summary.get('worst_max_drawdown') or 0):.1%}, "
            f"net {float(pre_option_target.get('net_position_weight_pct') or 0):.2f}%"
        )
        if qqq:
            lines.append(
                "- pre-option QQQ listed legs: "
                f"{qqq.get('long_put_contract')}, {qqq.get('short_put_contract')}, "
                f"{qqq.get('short_call_contract')}, quote {qqq.get('quote_date')}"
            )
        if cn_pkg:
            lines.append(
                "- pre-option CN option package: "
                f"{cn_pkg.get('underlying_option_code')}, quote {cn_pkg.get('quote_date')}, "
                f"contracts {cn_pkg.get('contracts')}, net debit {cn_pkg.get('net_debit_pct')}"
            )
    if readiness.get("replication_scenario_count") is not None:
        lines.append(
            "- replication stress: "
            f"{readiness.get('replication_floor_pass_count')}/{readiness.get('replication_scenario_count')} "
            f"floor-pass, worst return {readiness.get('replication_worst_return_pct'):.2f}%"
        )
    if readiness.get("ranking_candidate_count") is not None:
        lines.append(
            "- option package stress ranking: "
            f"{readiness.get('ranking_all_stress_floor_pass_count')}/"
            f"{readiness.get('ranking_candidate_count')} candidates pass the full stress grid"
        )
    if readiness.get("floor_cost_candidate_count") is not None:
        best_floor_cost = readiness.get("floor_cost_best_stress_candidate") or {}
        lines.append(
            "- option package floor-cost diagnostic: "
            f"{readiness.get('floor_cost_all_stress_floor_pass_count')}/"
            f"{readiness.get('floor_cost_candidate_count')} candidates pass unrestricted stress grid"
        )
        if best_floor_cost:
            lines.append(
                "- best floor-cost stress candidate: "
                f"{best_floor_cost.get('stress_pass_count')}/{best_floor_cost.get('stress_scenario_count')} stress-pass, "
                f"worst return {float(best_floor_cost.get('worst_total_return_pct')):.2f}%, "
                f"net debit {float(best_floor_cost.get('net_debit_pct_capital')):.2f}%"
            )
            if best_floor_cost.get("max_csi_gross_pct_for_floor") is not None:
                lines.append(
                    "- best floor-cost stress candidate CSI capacity: "
                    f"max CSI gross {float(best_floor_cost.get('max_csi_gross_pct_for_floor')):.2f}% "
                    f"vs current {float(best_floor_cost.get('current_csi_gross_pct')):.2f}%"
                )
        best_csi_capacity = readiness.get("floor_cost_best_max_csi_gross_candidate") or {}
        if best_csi_capacity:
            lines.append(
                "- best listed-option CSI capacity candidate: "
                f"max CSI gross {float(best_csi_capacity.get('max_csi_gross_pct_for_floor')):.2f}%, "
                f"net debit {float(best_csi_capacity.get('net_debit_pct_capital')):.2f}%"
            )
    if readiness.get("cn_etf_put_candidate_count") is not None:
        lines.append(
            "- CN ETF put hedge search: "
            f"status={readiness.get('cn_etf_put_status')}, "
            f"all-pass {readiness.get('cn_etf_put_all_pass_count')}/"
            f"{readiness.get('cn_etf_put_candidate_count')}, "
            f"budget all-pass {readiness.get('cn_etf_put_budget_all_pass_count')}"
        )
        cheapest_cn = readiness.get("cn_etf_put_cheapest_all_pass") or {}
        if cheapest_cn:
            lines.append(
                "- cheapest CN ETF put all-pass: "
                f"{cheapest_cn.get('contract')} contracts={cheapest_cn.get('contract_count')}, "
                f"premium {float(cheapest_cn.get('premium_cost_pct')):.2f}%, "
                f"cover {float(cheapest_cn.get('protected_weight_pct')):.2f}%"
            )
    if readiness.get("cn_etf_option_package_candidate_count") is not None:
        lines.append(
            "- CN ETF option package hedge search: "
            f"status={readiness.get('cn_etf_option_package_status')}, "
            f"all-pass {readiness.get('cn_etf_option_package_all_pass_count')}/"
            f"{readiness.get('cn_etf_option_package_candidate_count')}, "
            f"budget all-pass {readiness.get('cn_etf_option_package_budget_all_pass_count')}"
        )
        cheapest_pkg = readiness.get("cn_etf_option_package_cheapest_budget_all_pass") or {}
        if cheapest_pkg:
            lines.append(
                "- cheapest CN ETF option package budget all-pass: "
                f"long_put {cheapest_pkg.get('long_put_contract')} x{cheapest_pkg.get('contracts')}, "
                f"short_call {cheapest_pkg.get('short_call_contract')} x{cheapest_pkg.get('short_call_contracts')}, "
                f"net {float(cheapest_pkg.get('total_net_debit_pct')):.2f}%, "
                f"worst {float(cheapest_pkg.get('worst_total_return_pct')):.2f}%, "
                f"margin proxy {float(cheapest_pkg.get('margin_proxy_pct_capital') or 0.0):.2f}%"
            )
    if readiness.get("cn_option_history_sample_count") is not None:
        lines.append(
            "- CN option history coverage audit: "
            f"daily samples {readiness.get('cn_option_history_samples_with_daily')}/"
            f"{readiness.get('cn_option_history_sample_count')}, "
            f"contract-term samples {readiness.get('cn_option_history_samples_with_terms')}, "
            f"terms gap `{readiness.get('cn_option_history_contract_terms_gap')}`"
        )
    if readiness.get("cn_real_zero_quote_dates_available") is not None:
        zero_best = readiness.get("cn_real_zero_best") or {}
        zero_summary = zero_best.get("summary") or {}
        proxy_best = readiness.get("cn_real_proxy_best") or {}
        proxy_summary = proxy_best.get("summary") or {}
        missing_reasons = readiness.get("cn_real_zero_missing_reasons") or {}
        lines.append(
            "- CN option real-history listed-contract diagnostic: "
            f"quote dates {readiness.get('cn_real_zero_quote_dates_used')}/"
            f"{readiness.get('cn_real_zero_quote_dates_available')} used, "
            f"median listed months {float(zero_summary.get('median_listed_package_months') or 0):.1f}, "
            f"median missing months {float(zero_summary.get('median_missing_package_months') or 0):.1f}, "
            f"missing quote hits {missing_reasons.get('missing_quote_date')}"
        )
        if zero_summary:
            lines.append(
                "- CN real-history misszero best: "
                f"{zero_best.get('rule', {}).get('name')} "
                f"{zero_summary.get('pass_count')}/{zero_summary.get('count')} pass, "
                f"min {float(zero_summary.get('min_final_capital_wan')):.1f}w, "
                f"worst MDD {float(zero_summary.get('worst_max_drawdown')):.1%}"
            )
        if proxy_summary:
            lines.append(
                "- CN real-history missproxy best: "
                f"{proxy_best.get('rule', {}).get('name')} "
                f"{proxy_summary.get('pass_count')}/{proxy_summary.get('count')} pass, "
                f"min {float(proxy_summary.get('min_final_capital_wan')):.1f}w, "
                f"worst MDD {float(proxy_summary.get('worst_max_drawdown')):.1%}"
            )
    if readiness.get("cn_real_tipp_best"):
        tipp_best = readiness.get("cn_real_tipp_best") or {}
        tipp_summary = tipp_best.get("summary") or {}
        raw_diag = readiness.get("cn_real_tipp_raw_diag") or {}
        lines.append(
            "- CN raw real-package TIPP/CPPI diagnostic: "
            f"{tipp_summary.get('pass_count')}/{tipp_summary.get('count')} pass for best rule "
            f"{tipp_best.get('rule', {}).get('name')}, "
            f"min {float(tipp_summary.get('min_final_capital_wan')):.1f}w, "
            f"worst MDD {float(tipp_summary.get('worst_max_drawdown')):.1%}; "
            f"raw worst month {float(raw_diag.get('global_worst_monthly_return') or 0.0):.1%}"
        )
    if readiness.get("cn_real_preguard_best"):
        pg_best = readiness.get("cn_real_preguard_best") or {}
        pg_summary = pg_best.get("summary") or {}
        lines.append(
            "- CN raw real-package pre-guard diagnostic: "
            f"{pg_summary.get('pass_count')}/{pg_summary.get('count')} pass for best rule "
            f"{pg_best.get('rule', {}).get('name')}, "
            f"min {float(pg_summary.get('min_final_capital_wan')):.1f}w, "
            f"worst MDD {float(pg_summary.get('worst_max_drawdown')):.1%}"
        )
    if readiness.get("cn_real_daily_stop_best"):
        ds_best = readiness.get("cn_real_daily_stop_best") or {}
        ds_summary = ds_best.get("summary") or {}
        ds_diag = readiness.get("cn_real_daily_stop_diag") or {}
        capture = ((ds_diag.get("by_threshold") or {}).get("-5%") or {}).get("severe_loss_capture_rate")
        capture_text = f", -5% proxy severe-loss capture {float(capture):.1%}" if capture is not None else ""
        lines.append(
            "- CN raw real-package proxy daily-stop diagnostic: "
            f"{ds_summary.get('pass_count')}/{ds_summary.get('count')} pass for best rule "
            f"{ds_best.get('rule', {}).get('name')}, "
            f"min {float(ds_summary.get('min_final_capital_wan')):.1f}w, "
            f"worst MDD {float(ds_summary.get('worst_max_drawdown')):.1%}"
            f"{capture_text}"
        )
    if readiness.get("cn_daily_mtm_stop_best"):
        mtm_best = readiness.get("cn_daily_mtm_stop_best") or {}
        mtm_summary = mtm_best.get("summary") or {}
        mtm_coverage = readiness.get("cn_daily_mtm_stop_coverage") or {}
        mtm_diag = readiness.get("cn_daily_mtm_stop_diag") or {}
        capture = ((mtm_diag.get("by_threshold") or {}).get("-5%") or {}).get("severe_loss_capture_rate")
        capture_text = f", -5% MTM severe-loss capture {float(capture):.1%}" if capture is not None else ""
        coverage_text = (
            f", MTM points median/max {mtm_coverage.get('median_daily_mtm_points')}/"
            f"{mtm_coverage.get('max_daily_mtm_points')}"
        )
        lines.append(
            "- CN real option-leg daily-MTM stop diagnostic: "
            f"{mtm_summary.get('pass_count')}/{mtm_summary.get('count')} pass for best rule "
            f"{mtm_best.get('rule', {}).get('name')}, "
            f"min {float(mtm_summary.get('min_final_capital_wan')):.1f}w, "
            f"worst MDD {float(mtm_summary.get('worst_max_drawdown')):.1%}"
            f"{capture_text}{coverage_text}"
        )
    if readiness.get("cffex_index_future_coverage"):
        lines.append("- CFFEX index-futures coverage:")
        for row in readiness["cffex_index_future_coverage"]:
            lines.append(
                f"  - {row['ts_code']}: rows={row['rows']}, range={row['min_date']} ~ {row['max_date']}"
            )
    lines.extend(["", "## Pareto Frontier", "", "| Experiment | Rule | Min final | Worst MDD | Passes |", "| --- | --- | ---: | ---: | ---: |"])
    for item in frontier:
        lines.append(
            f"| {item['experiment']} | `{item['name']}` | "
            f"{item['min_final_capital_wan']:.1f}w | {item['worst_max_drawdown']:.1%} | "
            f"{item['pass_count']}/{item['case_count']} |"
        )
    lines.extend(["", "## Readout", ""])
    if payload["strict_pass_count"] > 0:
        if payload["production_ready_pass_count"] > 0:
            readout = (
                f"{payload['strict_pass_count']} tested candidates satisfy both sides of the numerical "
                "target across all timing variants; "
                f"{payload['production_ready_pass_count']} candidates are production-ready after execution "
                "readiness gates. Modeled-only protection candidates remain research evidence until their "
                "execution audits also pass."
            )
        else:
            readout = (
                f"{payload['strict_pass_count']} tested candidates satisfy both sides of the numerical "
                "target across all timing variants, but "
                f"{payload['production_ready_pass_count']} candidates are production-ready after execution "
                "readiness gates. Treat modeled protection candidates as research evidence until execution "
                "audit and replication stress also pass."
            )
        lines.extend(
            [
                readout,
                "",
            ]
        )
    else:
        lines.extend(
            [
                "No tested candidate currently satisfies both sides of the target across all timing variants. "
                "The experiments show a structural tradeoff: candidates that preserve enough compounding still "
                "carry deep drawdowns, while candidates near the 10% drawdown limit do not compound anywhere "
                "near 4000w. The next research step should prioritize a new alpha or hedge data source with "
                "full-window coverage rather than another narrow exposure cap on the same features.",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_payload(candidates: list[Candidate], target_final_wan: float, target_mdd: float) -> dict[str, Any]:
    groups: dict[str, list[Candidate]] = {}
    for item in candidates:
        groups.setdefault(item.experiment, []).append(item)
    frontier = sorted([item for item in candidates if item.pareto_frontier], key=candidate_sort_key, reverse=True)
    readiness = load_execution_readiness()
    strict_pass_count = sum(1 for item in candidates if item.strict_target_met)
    production_ready_pass_count = 0
    if readiness.get("legacy_production_ready"):
        production_ready_pass_count = strict_pass_count
    elif readiness.get("pre_option_regime_target_ready"):
        production_ready_experiment = (
            (readiness.get("pre_option_regime_target") or {}).get("experiment")
            or "pre_option_regime_defense_switch_50_to_300_misszero"
        )
        production_ready_pass_count = sum(
            1
            for item in candidates
            if item.strict_target_met and item.experiment == production_ready_experiment
        )
    return {
        "target": {
            "min_final_capital_wan": target_final_wan,
            "max_drawdown_floor": target_mdd,
        },
        "candidate_count": len(candidates),
        "strict_pass_count": strict_pass_count,
        "production_ready_pass_count": production_ready_pass_count,
        "best_overall": summarize_group(candidates, target_final_wan, target_mdd),
        "experiments": {name: summarize_group(items, target_final_wan, target_mdd) for name, items in sorted(groups.items())},
        "thresholds": threshold_summary(candidates, target_final_wan, target_mdd),
        "pareto_frontier": [slim_candidate(item) for item in frontier],
        "strict_generalization": load_strict_report(),
        "execution_readiness": readiness,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize scorecard + CSI search result frontier")
    parser.add_argument("--target-final-wan", type=float, default=DEFAULT_TARGET_FINAL_WAN)
    parser.add_argument("--target-mdd", type=float, default=DEFAULT_TARGET_MDD)
    args = parser.parse_args()

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    candidates = with_frontier_flags(load_candidates(args.target_final_wan, args.target_mdd))
    if not candidates:
        raise RuntimeError(f"no scorecard_csi search CSV files found under {BACKTEST_DIR}")
    payload = build_payload(candidates, args.target_final_wan, args.target_mdd)

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    write_csv(candidates, OUT_CSV)
    write_markdown(payload, OUT_MD)
    best = payload["best_overall"]["best_balance"]
    print(
        "frontier_summary: "
        f"candidates={payload['candidate_count']} "
        f"strict_passes={payload['strict_pass_count']} "
        f"production_ready={payload['production_ready_pass_count']} "
        f"best_balance={best['experiment']}/{best['name']} "
        f"min_final={best['min_final_capital_wan']:.1f}w "
        f"worst_mdd={best['worst_max_drawdown']:.1%} "
        f"json={OUT_JSON}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

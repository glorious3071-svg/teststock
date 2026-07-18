#!/usr/bin/env python3
"""Run the domestic passive ETF-only validation and target-generation pipeline."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_midyear_risk import TARGET_CAPITAL
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.generate_scorecard_csi_passive_etf_only_targets import build_targets

OUT_DIR = ROOT / "data" / "portfolio"
BACKTEST_DIR = ROOT / "data" / "backtests"
DEFAULT_RULE_NAME = "etftipp_i1_top1_m3_tr100_dd06_sgn05_f95_k20"


VALIDATION_SOURCES = [
    {
        "name": "strict_passive_etf_only",
        "path": BACKTEST_DIR / "scorecard_csi_passive_etf_only_strict_refreshed_quick_search.csv",
        "description": "Strict domestic passive ETF-only search.",
    },
    {
        "name": "cash_defense_scoregate",
        "path": BACKTEST_DIR / "scorecard_csi_passive_etf_only_cashdef_scoregate_focused_search.csv",
        "description": "ETF-only holdings with uninvested cash risk-off state and ETF score gates.",
    },
    {
        "name": "trend_state_daily_gate",
        "path": BACKTEST_DIR / "scorecard_csi_passive_etf_trend_state_gate_quick_search.csv",
        "description": "Trend-state ETF rotation with daily capital curve and global re-entry gate.",
    },
    {
        "name": "phase_tipp_passive_etf_only",
        "path": BACKTEST_DIR / "scorecard_csi_passive_etf_tipp_quick_search.csv",
        "description": "Phase-ensemble CSI selector plus TIPP sizing using only domestic SH/SZ passive ETF returns.",
    },
    {
        "name": "csi_si_proxy_etf",
        "path": BACKTEST_DIR / "scorecard_csi_proxy_etf_quick_search.csv",
        "description": "CSI/SI annual scorecard recommendations mapped to domestic SH/SZ ETF proxies.",
    },
]


def parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def run_cmd(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def maybe_run_validation() -> None:
    python = str(ROOT / ".venv" / "bin" / "python")
    run_cmd(
        [
            python,
            "scripts/search_scorecard_csi_passive_etf_only.py",
            "--quick",
            "--summary-only",
            "--output-prefix",
            "data/backtests/scorecard_csi_passive_etf_only_pipeline_strict_quick",
        ]
    )
    run_cmd(
        [
            python,
            "scripts/search_scorecard_csi_passive_etf_only.py",
            "--cash-focused",
            "--allow-cash-defense",
            "--summary-only",
            "--output-prefix",
            "data/backtests/scorecard_csi_passive_etf_only_pipeline_cashdef_focused",
        ]
    )
    run_cmd(
        [
            python,
            "scripts/search_scorecard_csi_passive_etf_trend_state.py",
            "--quick",
            "--summary-only",
            "--output-prefix",
            "data/backtests/scorecard_csi_passive_etf_trend_state_pipeline_quick",
        ]
    )
    run_cmd(
        [
            python,
            "scripts/search_scorecard_csi_passive_etf_tipp.py",
            "--quick",
            "--summary-only",
            "--output-prefix",
            "data/backtests/scorecard_csi_passive_etf_tipp_quick",
        ]
    )


def read_search_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def to_float(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value in (None, ""):
        return float("nan")
    return float(value)


def summarize_source(source: dict[str, Any]) -> dict[str, Any]:
    rows = read_search_csv(source["path"])
    if not rows:
        return {
            "name": source["name"],
            "description": source["description"],
            "path": str(source["path"].relative_to(ROOT)),
            "available": False,
            "pass_count": 0,
            "case_count": 0,
            "target_met": False,
            "note": "validation output missing",
        }
    best_by_pass = max(
        rows,
        key=lambda row: (
            int(float(row.get("pass_count", 0) or 0)),
            to_float(row, "worst_max_drawdown"),
            to_float(row, "min_final_capital_wan"),
        ),
    )
    best_by_capital = max(rows, key=lambda row: to_float(row, "min_final_capital_wan"))
    mdd_ok_rows = [row for row in rows if to_float(row, "worst_max_drawdown") >= TARGET_MDD]
    best_mdd_ok = max(mdd_ok_rows, key=lambda row: to_float(row, "min_final_capital_wan")) if mdd_ok_rows else None
    cap_ok_rows = [row for row in rows if to_float(row, "min_final_capital_wan") >= TARGET_CAPITAL / 10_000.0]
    best_cap_ok = max(cap_ok_rows, key=lambda row: to_float(row, "worst_max_drawdown")) if cap_ok_rows else None
    pass_count = int(float(best_by_pass.get("pass_count", 0) or 0))
    case_count = int(float(best_by_pass.get("count", 0) or 0))
    return {
        "name": source["name"],
        "description": source["description"],
        "path": str(source["path"].relative_to(ROOT)),
        "available": True,
        "rule_count": len(rows),
        "pass_count": pass_count,
        "case_count": case_count,
        "target_met": pass_count == case_count and case_count > 0,
        "best_by_pass_then_mdd": {
            "name": best_by_pass.get("name"),
            "min_final_capital_wan": to_float(best_by_pass, "min_final_capital_wan"),
            "worst_max_drawdown": to_float(best_by_pass, "worst_max_drawdown"),
            "median_final_capital_wan": to_float(best_by_pass, "median_final_capital_wan"),
            "median_max_drawdown": to_float(best_by_pass, "median_max_drawdown"),
        },
        "best_by_min_capital": {
            "name": best_by_capital.get("name"),
            "min_final_capital_wan": to_float(best_by_capital, "min_final_capital_wan"),
            "worst_max_drawdown": to_float(best_by_capital, "worst_max_drawdown"),
        },
        "best_with_mdd_gate": {
            "name": best_mdd_ok.get("name") if best_mdd_ok else None,
            "min_final_capital_wan": to_float(best_mdd_ok, "min_final_capital_wan") if best_mdd_ok else None,
            "worst_max_drawdown": to_float(best_mdd_ok, "worst_max_drawdown") if best_mdd_ok else None,
            "rule_count": len(mdd_ok_rows),
        },
        "best_with_capital_gate": {
            "name": best_cap_ok.get("name") if best_cap_ok else None,
            "min_final_capital_wan": to_float(best_cap_ok, "min_final_capital_wan") if best_cap_ok else None,
            "worst_max_drawdown": to_float(best_cap_ok, "worst_max_drawdown") if best_cap_ok else None,
            "rule_count": len(cap_ok_rows),
        },
    }


def write_targets_csv(path: Path, targets: list[dict[str, Any]]) -> None:
    fields = [
        "rank",
        "asset_type",
        "ts_code",
        "name",
        "index_code",
        "index_name",
        "category",
        "target_weight_pct",
        "target_amount",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(targets)


def write_validation_csv(path: Path, validation: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "available",
        "target_met",
        "pass_count",
        "case_count",
        "best_min_final_capital_wan",
        "best_worst_max_drawdown",
        "best_mdd_gate_min_final_capital_wan",
        "best_capital_gate_worst_max_drawdown",
        "path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in validation:
            best = item.get("best_by_pass_then_mdd") or {}
            best_mdd = item.get("best_with_mdd_gate") or {}
            best_cap = item.get("best_with_capital_gate") or {}
            writer.writerow(
                {
                    "name": item["name"],
                    "available": item["available"],
                    "target_met": item["target_met"],
                    "pass_count": item["pass_count"],
                    "case_count": item["case_count"],
                    "best_min_final_capital_wan": best.get("min_final_capital_wan"),
                    "best_worst_max_drawdown": best.get("worst_max_drawdown"),
                    "best_mdd_gate_min_final_capital_wan": best_mdd.get("min_final_capital_wan"),
                    "best_capital_gate_worst_max_drawdown": best_cap.get("worst_max_drawdown"),
                    "path": item["path"],
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ETF-only validation and target generation pipeline.")
    parser.add_argument("--run-validation", action="store_true", help="Re-run expensive search validations before summarizing.")
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--peak-capital", type=float, default=1_000_000.0)
    parser.add_argument("--rule-name", default=DEFAULT_RULE_NAME)
    parser.add_argument("--allow-cash-defense", action="store_true", default=True)
    parser.add_argument("--include-money-etf-defense", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    if args.run_validation:
        maybe_run_validation()

    validation = [summarize_source(source) for source in VALIDATION_SOURCES]
    objective_met = any(item["target_met"] for item in validation)
    targets = build_targets(
        as_of=args.as_of,
        rule_name=args.rule_name,
        capital=args.capital,
        peak_capital=args.peak_capital,
        include_money_etf_defense=args.include_money_etf_defense,
        allow_cash_defense=args.allow_cash_defense,
        min_rows=120,
    )
    targets["objective_validation"] = {
        "target_capital": TARGET_CAPITAL,
        "target_capital_wan": TARGET_CAPITAL / 10_000.0,
        "target_mdd": TARGET_MDD,
        "objective_met": objective_met,
        "validation_sources": validation,
    }
    targets["readiness_note"] = (
        "Pipeline generated current holdings and validation evidence. "
        "The original 4000w plus -10% max-drawdown gate is not passed by current validations."
    )

    as_of_slug = targets["as_of"].replace("-", "")
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"scorecard_csi_passive_etf_only_pipeline_{as_of_slug}"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)

    json_path = Path(f"{prefix}.json")
    target_csv_path = Path(f"{prefix}_targets.csv")
    validation_csv_path = Path(f"{prefix}_validation.csv")
    json_path.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
    write_targets_csv(target_csv_path, targets["targets"])
    write_validation_csv(validation_csv_path, validation)

    print(f"as_of={targets['as_of']} objective_met={objective_met}")
    for item in validation:
        best = item.get("best_by_pass_then_mdd") or {}
        print(
            f"{item['name']:<28} pass={item['pass_count']}/{item['case_count']} "
            f"min={best.get('min_final_capital_wan')}w mdd={best.get('worst_max_drawdown')}"
        )
    for row in targets["targets"]:
        print(f"{row['rank']:>2} {row['ts_code']:<10} {row['target_weight_pct']:6.2f}% {row['target_amount']:,.2f}")
    print(f"Wrote {json_path}")
    print(f"Wrote {target_csv_path}")
    print(f"Wrote {validation_csv_path}")
    return 0 if objective_met else 1


if __name__ == "__main__":
    raise SystemExit(main())

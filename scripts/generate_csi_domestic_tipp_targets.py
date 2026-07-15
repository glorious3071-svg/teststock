#!/usr/bin/env python3
"""Generate domestic-only TIPP target holdings for the CSI scorecard strategy."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_dynamic_defense import month_end_shift
from scripts.generate_csi_phase_ensemble_targets import build_targets as build_phase_targets
from scripts.generate_csi_phase_ensemble_targets import previous_month_end
from scripts.generate_csi_pre_option_regime_targets import cn_option_package_rows

OUT_DIR = ROOT / "data" / "portfolio"
BACKTEST_DIR = ROOT / "data" / "backtests"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_VALIDATION_REPORT = (
    BACKTEST_DIR / "scorecard_csi_domestic_only_tipp_aggressive_guard_focused_fixed_quick_report.json"
)
DEFAULT_SELECTOR = "max_mdd_margin"


def load_validated_rule(report_path: Path, selector: str) -> dict[str, Any]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    passing = [
        item
        for item in payload["results"]
        if item["summary"]["pass_count"] == item["summary"]["count"]
        and item["summary"]["min_final_capital_wan"] >= 4000.0
        and item["summary"]["worst_max_drawdown"] >= -0.10
    ]
    if not passing:
        raise RuntimeError(f"No domestic-only strict-pass rule found in {report_path}")
    if selector == "max_mdd_margin":
        return max(
            passing,
            key=lambda item: (
                item["summary"]["worst_max_drawdown"],
                item["summary"]["min_final_capital_wan"],
            ),
        )
    if selector == "max_min_capital":
        return max(
            passing,
            key=lambda item: (
                item["summary"]["min_final_capital_wan"],
                item["summary"]["worst_max_drawdown"],
            ),
        )
    matches = [item for item in passing if item["rule"]["name"] == selector]
    if not matches:
        raise ValueError(f"Unknown or non-passing domestic TIPP rule selector: {selector}")
    return matches[0]


def tipp_exposure(rule: dict[str, Any], strategy_drawdown_pct: float) -> float:
    drawdown = strategy_drawdown_pct / 100.0
    floor_pct = float(rule["floor_pct"])
    multiplier = float(rule["multiplier"])
    max_exposure = float(rule["max_exposure"])
    min_exposure = float(rule["min_exposure"])
    capital_to_peak = 1.0 + drawdown
    if capital_to_peak <= 0:
        return 0.0
    exposure = multiplier * max(0.0, capital_to_peak - floor_pct) / capital_to_peak
    exposure = min(max_exposure, max(min_exposure, exposure))
    if drawdown <= float(rule["drawdown_guard_lte"]):
        exposure = min(exposure, float(rule["drawdown_guard_exposure"]))
    return exposure


def scale_row(row: dict[str, Any], scale: float, capital: float, source_component: str) -> dict[str, Any]:
    item = dict(row)
    item["target_weight_pct"] = float(item["target_weight_pct"]) * scale
    item["target_amount"] = capital * item["target_weight_pct"] / 100.0
    item["source_component"] = source_component
    return item


def rescale_option_rows(rows: list[dict[str, Any]], sleeve_capital: float, total_capital: float) -> list[dict[str, Any]]:
    scaled = []
    for row in rows:
        item = dict(row)
        amount = float(item["target_amount"])
        item["target_amount"] = amount
        item["target_weight_pct"] = amount / total_capital * 100.0
        item["source_component"] = "domestic_etf_option_package"
        scaled.append(item)
    return scaled


def build_targets(
    rule_selector: str,
    validation_report: Path,
    as_of: date,
    snapshot: date,
    capital: float,
    top_per_sleeve: int,
    strategy_drawdown_pct: float,
) -> dict[str, Any]:
    validated = load_validated_rule(validation_report, rule_selector)
    rule = validated["rule"]
    base_rule = rule["base_rule"]
    wrapper_exposure = tipp_exposure(rule, strategy_drawdown_pct)
    base_leverage = float(base_rule["listed_normal_exposure"])
    active_scale = wrapper_exposure * base_leverage
    phase_report = build_phase_targets(
        rule_name=base_rule["phase_rule_name"],
        as_of=as_of,
        snapshot=snapshot,
        capital=capital,
        top_per_sleeve=top_per_sleeve,
        portfolio_drawdown_pct=strategy_drawdown_pct,
    )

    rows: list[dict[str, Any]] = [
        scale_row(row, active_scale, capital, "domestic_phase_ensemble_csi")
        for row in phase_report["rows"]
    ]

    option_sleeve_capital = capital * active_scale
    option_rows: list[dict[str, Any]] = []
    option_meta: dict[str, Any] = {"status": "not_selected_zero_exposure"}
    if option_sleeve_capital > 1e-9:
        raw_option_rows, option_meta = cn_option_package_rows(
            option_sleeve_capital,
            as_of,
            month_end_shift(as_of, 1),
        )
        if option_meta.get("status") != "selected":
            raise RuntimeError(f"CN option package target generation failed: {option_meta}")
        option_rows = rescale_option_rows(raw_option_rows, option_sleeve_capital, capital)
        rows.extend(option_rows)

    inner_safe_pct = wrapper_exposure * (1.0 - base_leverage) * 100.0
    outer_safe_pct = (1.0 - wrapper_exposure) * 100.0
    safe_pct = inner_safe_pct + outer_safe_pct
    if abs(safe_pct) > 1e-9:
        rows.append(
            {
                "rank": None,
                "asset_type": "cash_or_financing",
                "index_code": "CASH_FINANCING",
                "index_name": "domestic cash if positive, financing if negative",
                "target_weight_pct": safe_pct,
                "target_amount": capital * safe_pct / 100.0,
                "source_component": "tipp_safe_residual",
                "execution_note": "Negative weight is strategy financing implied by the validated leverage rule.",
            }
        )

    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    net_weight_pct = sum(float(row.get("target_weight_pct") or 0.0) for row in rows)
    return {
        "strategy": "scorecard_csi_domestic_only_tipp_targets",
        "model_status": "domestic_only_strict_backtest_pass_targets_generated",
        "no_lookahead_rule": (
            "CSI rows reuse the phase-ensemble generator with the previous month-end snapshot. "
            "The domestic ETF option package uses option quotes on or before as_of and a one-month holding horizon. "
            "No overseas assets, overseas ETFs, crypto, or US rates are used."
        ),
        "rule_selector": rule_selector,
        "validation_report": str(validation_report.relative_to(ROOT)),
        "validated_rule_name": rule["name"],
        "validated_summary": validated["summary"],
        "validated_rule": rule,
        "as_of": as_of.isoformat(),
        "snapshot": snapshot.isoformat(),
        "capital": capital,
        "strategy_drawdown_pct": strategy_drawdown_pct,
        "wrapper_exposure": wrapper_exposure,
        "base_leverage": base_leverage,
        "active_scale": active_scale,
        "phase_target_equity_pct": phase_report["target_equity_pct"],
        "phase_report_rule": phase_report["rule_name"],
        "cn_option_package": option_meta,
        "no_overseas_assets": True,
        "allowed_assets": [
            "CSI index baskets and their domestic listed ETF mappings",
            "domestic cash or financing",
            "SSE ETF option package on 510050/510300 option universe",
        ],
        "excluded_assets": ["QQQ", "SHY", "BTC-USD", "ETH-USD", "US10Y_PROXY", "SPX.US"],
        "net_weight_pct": net_weight_pct,
        "rows": rows,
    }


def write_outputs(report: dict[str, Any]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(report["as_of"]).replace("-", "")
    json_path = OUT_DIR / f"csi_domestic_tipp_targets_{stamp}.json"
    csv_path = OUT_DIR / f"csi_domestic_tipp_targets_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames: list[str] = []
    for row in report["rows"]:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report["rows"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate domestic-only TIPP CSI target weights")
    parser.add_argument("--rule-selector", default=DEFAULT_SELECTOR)
    parser.add_argument("--validation-report", default=str(DEFAULT_VALIDATION_REPORT))
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Decision date, YYYY-MM-DD")
    parser.add_argument("--snapshot", help="Scorecard/selection snapshot date, YYYY-MM-DD. Default: previous month end.")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--top-per-sleeve", type=int, default=0)
    parser.add_argument("--strategy-drawdown-pct", type=float, default=0.0)
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    snapshot = date.fromisoformat(args.snapshot) if args.snapshot else previous_month_end(as_of)
    report = build_targets(
        rule_selector=args.rule_selector,
        validation_report=Path(args.validation_report),
        as_of=as_of,
        snapshot=snapshot,
        capital=args.capital,
        top_per_sleeve=args.top_per_sleeve,
        strategy_drawdown_pct=args.strategy_drawdown_pct,
    )
    json_path, csv_path = write_outputs(report)
    print("Domestic-only TIPP CSI targets")
    print(
        f"  rule={report['validated_rule_name']} as_of={report['as_of']} snapshot={report['snapshot']} "
        f"wrapper={report['wrapper_exposure']:.3f} base_leverage={report['base_leverage']:.2f} "
        f"active_scale={report['active_scale']:.3f} net_weight={report['net_weight_pct']:.2f}%"
    )
    summary = report["validated_summary"]
    print(
        f"  validation pass={summary['pass_count']}/{summary['count']} "
        f"min={summary['min_final_capital_wan']:.1f}w worst_mdd={summary['worst_max_drawdown']:.1%}"
    )
    print(f"  option_package_status={report['cn_option_package'].get('status')}")
    for row in report["rows"][:20]:
        print(
            f"  {row['rank']:>2}. {row['asset_type']} {row['index_code']} "
            f"weight={float(row['target_weight_pct']):.2f}% amount={float(row['target_amount']):,.0f}"
        )
    if len(report["rows"]) > 20:
        print(f"  ... {len(report['rows']) - 20} more rows")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

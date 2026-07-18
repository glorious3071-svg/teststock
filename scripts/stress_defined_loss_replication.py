#!/usr/bin/env python3
"""Stress-test whether listed option legs can replicate the defined-loss floor.

The defined-loss backtest uses a modeled monthly portfolio floor.  This script
uses the current target rows and the execution audit's mapped option contracts
to test a narrower claim: whether the listed QQQ option sleeve by itself can
support the portfolio-level monthly loss floor under simple CSI/QQQ shock grids
after conservative transaction frictions.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "portfolio"


def default_target_path(as_of: str) -> Path:
    return OUT_DIR / f"csi_defined_loss_overlay_targets_{as_of.replace('-', '')}.json"


def default_audit_path(as_of: str) -> Path:
    return OUT_DIR / f"csi_defined_loss_execution_audit_{as_of.replace('-', '')}.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pct_grid(start: float, stop: float, step: float) -> list[float]:
    values = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 10))
        current += step
    return values


def target_weights(target: dict[str, Any]) -> dict[str, float]:
    rows = target["rows"]
    csi_gross = sum(float(row.get("target_weight_pct") or 0.0) for row in rows if row.get("asset_type") == "csi_index")
    financing = sum(float(row.get("target_weight_pct") or 0.0) for row in rows if "financing" in str(row.get("asset_type")))
    safe = sum(
        float(row.get("target_weight_pct") or 0.0)
        for row in rows
        if row.get("asset_type") in {"core_safe_asset", "satellite_safe_asset"}
    )
    option_row = next(row for row in rows if row.get("asset_type") == "option_protected_sleeve")
    overlay_row = next(row for row in rows if row.get("asset_type") == "defined_loss_overlay")
    return {
        "csi_gross_pct": csi_gross,
        "financing_pct": financing,
        "safe_pct": safe,
        "option_target_weight_pct": float(option_row.get("target_weight_pct") or 0.0),
        "option_underlying_notional_pct": float(option_row.get("underlying_notional_pct") or 0.0),
        "monthly_floor_pct": float(overlay_row.get("monthly_loss_floor_pct") or 0.0),
        "premium_budget_pct": float(overlay_row.get("monthly_premium_budget_pct") or 0.0),
    }


def option_package(audit: dict[str, Any]) -> dict[str, Any]:
    evidence = audit["available_evidence"]["target_option_chain_evidence"]
    if not evidence:
        raise RuntimeError("audit has no target option-chain evidence")
    package = evidence[0].get("estimated_option_package")
    if not package:
        raise RuntimeError("audit has no estimated option package; run execution audit first")
    return {
        "underlying_price": float(evidence[0]["underlying_price"]["price"]),
        "underlying_units": float(package["underlying_units"]),
        "underlying_notional": float(package["underlying_notional"]),
        "net_debit": float(package["net_debit"]),
        "net_debit_pct_capital": float(package["net_debit_pct_capital"]),
        "legs": package["legs"],
    }


def option_payoff(legs: list[dict[str, Any]], final_spot: float) -> float:
    total = 0.0
    for leg in legs:
        contract = leg.get("contract_symbol", "")
        if len(contract) < 9:
            continue
        strike = float(contract[-8:]) / 1000.0
        units = float(leg["units"])
        option_type = leg["option_type"]
        side = leg["side"]
        if option_type == "put":
            payoff = max(strike - final_spot, 0.0) * units
        elif option_type == "call":
            payoff = max(final_spot - strike, 0.0) * units
        else:
            payoff = 0.0
        total += payoff if side == "buy" else -payoff
    return total


def scenario_rows(
    target: dict[str, Any],
    audit: dict[str, Any],
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
    margin_reserve_pct_notional: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    capital = float(target["capital"])
    weights = target_weights(target)
    package = option_package(audit)
    floor_pct = weights["monthly_floor_pct"]
    safe_return = safe_return_pct / 100.0
    financing_monthly = financing_rate_annual_pct / 100.0 / 12.0
    slippage = (
        package["underlying_notional"]
        * len(package["legs"])
        * slippage_bps_per_leg
        / 10000.0
    )
    margin_reserve = package["underlying_notional"] * margin_reserve_pct_notional / 100.0
    rows: list[dict[str, Any]] = []
    for qqq_ret in qqq_shocks:
        final_spot = package["underlying_price"] * (1.0 + qqq_ret / 100.0)
        listed_option_pnl = option_payoff(package["legs"], final_spot) - package["net_debit"] - slippage
        qqq_underlying_pnl = package["underlying_notional"] * qqq_ret / 100.0
        option_sleeve_pnl = qqq_underlying_pnl + listed_option_pnl
        for csi_ret in csi_shocks:
            csi_pnl = capital * weights["csi_gross_pct"] / 100.0 * csi_ret / 100.0
            safe_pnl = capital * weights["safe_pct"] / 100.0 * safe_return
            financing_cost = abs(capital * weights["financing_pct"] / 100.0) * financing_monthly
            total_pnl = csi_pnl + safe_pnl - financing_cost + option_sleeve_pnl
            total_return_pct = total_pnl / capital * 100.0
            floor_gap_pct = total_return_pct - floor_pct
            rows.append(
                {
                    "qqq_return_pct": qqq_ret,
                    "csi_return_pct": csi_ret,
                    "safe_return_pct": safe_return_pct,
                    "financing_rate_annual_pct": financing_rate_annual_pct,
                    "slippage_bps_per_leg": slippage_bps_per_leg,
                    "margin_reserve_pct_notional": margin_reserve_pct_notional,
                    "final_spot": final_spot,
                    "csi_pnl": csi_pnl,
                    "safe_pnl": safe_pnl,
                    "financing_cost": financing_cost,
                    "qqq_underlying_pnl": qqq_underlying_pnl,
                    "listed_option_pnl": listed_option_pnl,
                    "option_sleeve_pnl": option_sleeve_pnl,
                    "total_pnl": total_pnl,
                    "total_return_pct": total_return_pct,
                    "floor_pct": floor_pct,
                    "floor_pass": total_return_pct >= floor_pct,
                    "floor_gap_pct": floor_gap_pct,
                    "margin_reserve_amount": margin_reserve,
                    "cash_pressure_amount": max(0.0, margin_reserve + package["net_debit"] + slippage),
                }
            )
    worst = min(rows, key=lambda row: row["total_return_pct"])
    closest_fail = max(
        [row for row in rows if not row["floor_pass"]],
        key=lambda row: row["total_return_pct"],
        default=None,
    )
    summary = {
        "scenario_count": len(rows),
        "floor_pass_count": sum(1 for row in rows if row["floor_pass"]),
        "floor_fail_count": sum(1 for row in rows if not row["floor_pass"]),
        "all_scenarios_floor_pass": all(row["floor_pass"] for row in rows),
        "worst_scenario": worst,
        "closest_failed_scenario": closest_fail,
        "weights": weights,
        "option_package": package,
        "assumptions": {
            "safe_return_pct": safe_return_pct,
            "financing_rate_annual_pct": financing_rate_annual_pct,
            "slippage_bps_per_leg": slippage_bps_per_leg,
            "margin_reserve_pct_notional": margin_reserve_pct_notional,
            "qqq_shocks_pct": qqq_shocks,
            "csi_shocks_pct": csi_shocks,
        },
    }
    return rows, summary


def write_outputs(report: dict[str, Any], rows: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress-test listed-option replication of the defined-loss floor.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--audit-json")
    parser.add_argument("--qqq-min-pct", type=float, default=-50.0)
    parser.add_argument("--qqq-max-pct", type=float, default=30.0)
    parser.add_argument("--csi-min-pct", type=float, default=-40.0)
    parser.add_argument("--csi-max-pct", type=float, default=25.0)
    parser.add_argument("--shock-step-pct", type=float, default=5.0)
    parser.add_argument("--safe-return-pct", type=float, default=0.0)
    parser.add_argument("--financing-rate-annual-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--margin-reserve-pct-notional", type=float, default=15.0)
    parser.add_argument("--output-prefix")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    target_path = Path(args.target_json) if args.target_json else default_target_path(args.as_of)
    audit_path = Path(args.audit_json) if args.audit_json else default_audit_path(args.as_of)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    if not audit_path.is_absolute():
        audit_path = ROOT / audit_path
    target = load_json(target_path)
    audit = load_json(audit_path)
    rows, summary = scenario_rows(
        target=target,
        audit=audit,
        qqq_shocks=pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct),
        csi_shocks=pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct),
        safe_return_pct=args.safe_return_pct,
        financing_rate_annual_pct=args.financing_rate_annual_pct,
        slippage_bps_per_leg=args.slippage_bps_per_leg,
        margin_reserve_pct_notional=args.margin_reserve_pct_notional,
    )
    report = {
        "strategy": "scorecard_csi_defined_loss_replication_stress",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "audit_json": str(audit_path),
        "target_rule": target.get("rule_name"),
        "status": "replication_floor_validated" if summary["all_scenarios_floor_pass"] else "replication_floor_failed",
        **summary,
    }
    output_prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"csi_defined_loss_replication_stress_{args.as_of.replace('-', '')}"
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, rows, output_prefix)
    worst = summary["worst_scenario"]
    print("Defined-loss listed-option replication stress")
    print(
        f"  rule={report['target_rule']} status={report['status']} "
        f"pass={summary['floor_pass_count']}/{summary['scenario_count']}"
    )
    print(
        f"  worst qqq={worst['qqq_return_pct']:.1f}% csi={worst['csi_return_pct']:.1f}% "
        f"return={worst['total_return_pct']:.2f}% floor={worst['floor_pct']:.2f}% "
        f"gap={worst['floor_gap_pct']:.2f}pp"
    )
    if summary["closest_failed_scenario"]:
        fail = summary["closest_failed_scenario"]
        print(
            f"  closest_fail qqq={fail['qqq_return_pct']:.1f}% csi={fail['csi_return_pct']:.1f}% "
            f"return={fail['total_return_pct']:.2f}% gap={fail['floor_gap_pct']:.2f}pp"
        )
    print(f"  option_net_debit={summary['option_package']['net_debit']:,.0f}")
    print(f"  cash_pressure={worst['cash_pressure_amount']:,.0f}")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    if args.strict and not summary["all_scenarios_floor_pass"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rank executable option-package candidates by portfolio stress performance."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.stress_defined_loss_replication import (
    default_target_path,
    option_payoff,
    pct_grid,
    target_weights,
)

OUT_DIR = ROOT / "data" / "portfolio"


def default_candidate_path(as_of: str) -> Path:
    return OUT_DIR / f"executable_option_package_search_{as_of.replace('-', '')}.csv"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_candidates(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def candidate_legs(candidate: dict[str, str], put_cover_pct: float, call_cover_pct: float) -> list[dict[str, Any]]:
    underlying_units = float(candidate["underlying_units"])
    legs = [
        {
            "side": "buy",
            "option_type": "put",
            "role": "long_put",
            "contract_symbol": candidate["put_contract"],
            "units": underlying_units * put_cover_pct / 100.0,
        }
    ]
    if candidate.get("short_put_contract"):
        legs.append(
            {
                "side": "sell",
                "option_type": "put",
                "role": "short_put",
                "contract_symbol": candidate["short_put_contract"],
                "units": underlying_units * put_cover_pct / 100.0,
            }
        )
    if candidate.get("call_contract"):
        legs.append(
            {
                "side": "sell",
                "option_type": "call",
                "role": "short_call",
                "contract_symbol": candidate["call_contract"],
                "units": underlying_units * call_cover_pct / 100.0,
            }
        )
    return legs


def score_candidate(
    candidate: dict[str, str],
    target: dict[str, Any],
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
) -> dict[str, Any]:
    capital = float(target["capital"])
    weights = target_weights(target)
    option_row = next(row for row in target["rows"] if row.get("asset_type") == "option_protected_sleeve")
    put_cover_pct = float(option_row.get("long_put_cover_pct") or 0.0)
    call_cover_pct = float(option_row.get("call_cover_pct") or 0.0)
    underlying_price = float(target["available_underlying_price"]) if target.get("available_underlying_price") else None
    if underlying_price is None:
        # The search candidate's strike percentages were produced from the same
        # spot.  Reverse the long put target to avoid requiring another DB read.
        underlying_price = float(candidate["put_strike"]) / (float(candidate["put_strike_pct"]) / 100.0)
    underlying_notional = float(candidate["underlying_notional"])
    net_debit = float(candidate["net_debit"])
    legs = candidate_legs(candidate, put_cover_pct, call_cover_pct)
    slippage = underlying_notional * len(legs) * slippage_bps_per_leg / 10000.0
    safe_return = safe_return_pct / 100.0
    financing_monthly = financing_rate_annual_pct / 100.0 / 12.0
    floor_pct = weights["monthly_floor_pct"]
    scenario_count = 0
    pass_count = 0
    worst_return = 1e9
    worst_row: dict[str, Any] | None = None
    for qqq_ret in qqq_shocks:
        final_spot = underlying_price * (1.0 + qqq_ret / 100.0)
        listed_option_pnl = option_payoff(legs, final_spot) - net_debit - slippage
        qqq_underlying_pnl = underlying_notional * qqq_ret / 100.0
        option_sleeve_pnl = qqq_underlying_pnl + listed_option_pnl
        for csi_ret in csi_shocks:
            csi_pnl = capital * weights["csi_gross_pct"] / 100.0 * csi_ret / 100.0
            safe_pnl = capital * weights["safe_pct"] / 100.0 * safe_return
            financing_cost = abs(capital * weights["financing_pct"] / 100.0) * financing_monthly
            total_return_pct = (
                csi_pnl + safe_pnl - financing_cost + option_sleeve_pnl
            ) / capital * 100.0
            scenario_count += 1
            if total_return_pct >= floor_pct:
                pass_count += 1
            if total_return_pct < worst_return:
                worst_return = total_return_pct
                worst_row = {
                    "qqq_return_pct": qqq_ret,
                    "csi_return_pct": csi_ret,
                    "total_return_pct": total_return_pct,
                }
    assert worst_row is not None
    return {
        "expiration_date": candidate["expiration_date"],
        "dte": int(float(candidate["dte"])),
        "put_strike_pct": float(candidate["put_strike_pct"]),
        "short_put_strike_pct": float(candidate["short_put_strike_pct"]) if candidate.get("short_put_strike_pct") else None,
        "call_strike_pct": float(candidate["call_strike_pct"]),
        "put_contract": candidate["put_contract"],
        "short_put_contract": candidate.get("short_put_contract") or None,
        "call_contract": candidate["call_contract"],
        "net_debit_pct_capital": float(candidate["net_debit_pct_capital"]),
        "premium_budget_pass": candidate["premium_budget_pass"] == "True",
        "stress_pass_count": pass_count,
        "stress_scenario_count": scenario_count,
        "stress_pass_rate": pass_count / scenario_count if scenario_count else 0.0,
        "all_stress_floor_pass": pass_count == scenario_count,
        "worst_total_return_pct": worst_return,
        "worst_floor_gap_pct": worst_return - floor_pct,
        "worst_qqq_return_pct": worst_row["qqq_return_pct"],
        "worst_csi_return_pct": worst_row["csi_return_pct"],
    }


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
    parser = argparse.ArgumentParser(description="Rank option-package candidates by defined-loss stress performance.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--candidate-csv")
    parser.add_argument("--budget-only", action="store_true", default=True)
    parser.add_argument("--qqq-min-pct", type=float, default=-50.0)
    parser.add_argument("--qqq-max-pct", type=float, default=30.0)
    parser.add_argument("--csi-min-pct", type=float, default=-40.0)
    parser.add_argument("--csi-max-pct", type=float, default=25.0)
    parser.add_argument("--shock-step-pct", type=float, default=5.0)
    parser.add_argument("--safe-return-pct", type=float, default=0.0)
    parser.add_argument("--financing-rate-annual-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    target_path = Path(args.target_json) if args.target_json else default_target_path(args.as_of)
    candidate_path = Path(args.candidate_csv) if args.candidate_csv else default_candidate_path(args.as_of)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    if not candidate_path.is_absolute():
        candidate_path = ROOT / candidate_path
    target = load_json(target_path)
    candidates = load_candidates(candidate_path)
    if args.budget_only:
        candidates = [row for row in candidates if row.get("premium_budget_pass") == "True"]
    scores = [
        score_candidate(
            candidate=row,
            target=target,
            qqq_shocks=pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct),
            csi_shocks=pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct),
            safe_return_pct=args.safe_return_pct,
            financing_rate_annual_pct=args.financing_rate_annual_pct,
            slippage_bps_per_leg=args.slippage_bps_per_leg,
        )
        for row in candidates
    ]
    scores.sort(
        key=lambda row: (
            row["all_stress_floor_pass"],
            row["stress_pass_count"],
            row["worst_total_return_pct"],
            -abs(row["net_debit_pct_capital"]),
        ),
        reverse=True,
    )
    report = {
        "strategy": "scorecard_csi_option_package_stress_ranking",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "candidate_csv": str(candidate_path),
        "target_rule": target.get("rule_name"),
        "candidate_count": len(scores),
        "all_stress_floor_pass_count": sum(1 for row in scores if row["all_stress_floor_pass"]),
        "best_candidate": scores[0] if scores else None,
        "top_candidates": scores[: args.top],
        "assumptions": {
            "budget_only": args.budget_only,
            "qqq_min_pct": args.qqq_min_pct,
            "qqq_max_pct": args.qqq_max_pct,
            "csi_min_pct": args.csi_min_pct,
            "csi_max_pct": args.csi_max_pct,
            "shock_step_pct": args.shock_step_pct,
            "safe_return_pct": args.safe_return_pct,
            "financing_rate_annual_pct": args.financing_rate_annual_pct,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
        },
    }
    output_prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"option_package_stress_ranking_{args.as_of.replace('-', '')}"
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, scores, output_prefix)
    best = report["best_candidate"]
    print("Option package stress ranking")
    print(
        f"  rule={report['target_rule']} candidates={len(scores)} "
        f"all_pass={report['all_stress_floor_pass_count']}"
    )
    if best:
        print(
            f"  best expiry={best['expiration_date']} put={best['put_strike_pct']:.1f}% "
            f"short_put={best['short_put_strike_pct'] or 0:.1f}% call={best['call_strike_pct']:.1f}% "
            f"stress={best['stress_pass_count']}/{best['stress_scenario_count']} "
            f"worst={best['worst_total_return_pct']:.2f}% net={best['net_debit_pct_capital']:.2f}%"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

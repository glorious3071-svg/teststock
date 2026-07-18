#!/usr/bin/env python3
"""Stress-test a China ETF put candidate as the small-account CSI hedge bridge."""

from __future__ import annotations

import argparse
import csv
import copy
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.stress_defined_loss_replication import default_target_path, pct_grid, target_weights

OUT_DIR = ROOT / "data" / "portfolio"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_json(pattern: str) -> Path:
    paths = sorted(OUT_DIR.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"no files match {OUT_DIR / pattern}")
    return paths[-1]


def option_payoff(strike: float, option_type: str, side: str, units: float, final_spot: float) -> float:
    if option_type == "put":
        payoff = max(strike - final_spot, 0.0) * units
    elif option_type == "call":
        payoff = max(final_spot - strike, 0.0) * units
    else:
        payoff = 0.0
    return payoff if side == "buy" else -payoff


def qqq_package_pnl(package: dict[str, Any], qqq_ret_pct: float, slippage_bps_per_leg: float) -> float:
    spot = float(package["put_strike"]) / (float(package["put_strike_pct"]) / 100.0)
    final_spot = spot * (1.0 + qqq_ret_pct / 100.0)
    underlying_notional = float(package["underlying_notional"])
    underlying_pnl = underlying_notional * qqq_ret_pct / 100.0
    underlying_units = float(package["underlying_units"])
    put_units = underlying_units * float(package["put_cover_pct"]) / 100.0
    call_units = underlying_units * float(package["call_cover_pct"] or 0.0) / 100.0
    legs = [
        option_payoff(float(package["put_strike"]), "put", "buy", put_units, final_spot),
    ]
    if package.get("short_put_contract") and package.get("short_put_strike"):
        legs.append(option_payoff(float(package["short_put_strike"]), "put", "sell", put_units, final_spot))
    if package.get("call_contract") and package.get("call_strike"):
        legs.append(option_payoff(float(package["call_strike"]), "call", "sell", call_units, final_spot))
    slippage = underlying_notional * len(legs) * slippage_bps_per_leg / 10000.0
    return underlying_pnl + sum(legs) - float(package["net_debit"]) - slippage


def underlying_from_opt_code(opt_code: str) -> str:
    if not opt_code.startswith("OP"):
        return opt_code
    return opt_code[2:]


def latest_fund_price(ts_code: str, as_of: date) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, close
                FROM fund_daily
                WHERE ts_code=%s AND trade_date <= %s AND close IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (ts_code, as_of),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"trade_date": row[0].isoformat(), "price": float(row[1]), "source": "fund_daily"}


def load_cn_put_candidate(target: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in target["rows"] if row.get("asset_type") == "cn_etf_put_hedge_candidate"]
    if not rows:
        raise RuntimeError("target has no cn_etf_put_hedge_candidate row")
    return rows[0]


def cn_put_pnl(
    row: dict[str, Any],
    csi_ret_pct: float,
    etf_spot: float,
    slippage_bps_per_leg: float,
) -> float:
    contracts = int(row["contract_count_candidate"])
    per_unit = float(row["per_unit"])
    strike = float(row["exercise_price"])
    option_close = float(row["option_close"])
    final_spot = etf_spot * (1.0 + csi_ret_pct / 100.0)
    gross_payoff = max(strike - final_spot, 0.0) * per_unit * contracts
    premium = option_close * per_unit * contracts
    notional = strike * per_unit * contracts
    slippage = notional * slippage_bps_per_leg / 10000.0
    return gross_payoff - premium - slippage


def with_contract_count(row: dict[str, Any], contracts: int, capital: float) -> dict[str, Any]:
    out = copy.deepcopy(row)
    strike = float(out["exercise_price"])
    per_unit = float(out["per_unit"])
    close = float(out["option_close"])
    contract_notional = strike * per_unit
    protected_notional = contracts * contract_notional
    premium_cost = contracts * close * per_unit
    out["contract_count_candidate"] = contracts
    out["protected_notional_candidate"] = protected_notional
    out["protected_weight_pct_candidate"] = protected_notional / capital * 100.0 if capital > 0 else 0.0
    out["premium_cost_candidate"] = premium_cost
    out["premium_cost_pct_candidate"] = premium_cost / capital * 100.0 if capital > 0 else 0.0
    return out


def scenario_rows(
    target: dict[str, Any],
    qqq_package: dict[str, Any],
    cn_put: dict[str, Any],
    etf_spot: float,
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    capital = float(target["capital"])
    weights = target_weights(target)
    floor_pct = weights["monthly_floor_pct"]
    safe_return = safe_return_pct / 100.0
    financing_monthly = financing_rate_annual_pct / 100.0 / 12.0
    rows: list[dict[str, Any]] = []
    for qqq_ret in qqq_shocks:
        qqq_pnl = qqq_package_pnl(qqq_package, qqq_ret, slippage_bps_per_leg)
        for csi_ret in csi_shocks:
            csi_pnl = capital * weights["csi_gross_pct"] / 100.0 * csi_ret / 100.0
            safe_pnl = capital * weights["safe_pct"] / 100.0 * safe_return
            financing_cost = abs(capital * weights["financing_pct"] / 100.0) * financing_monthly
            cn_put_hedge_pnl = cn_put_pnl(cn_put, csi_ret, etf_spot, slippage_bps_per_leg)
            total_pnl = qqq_pnl + csi_pnl + safe_pnl - financing_cost + cn_put_hedge_pnl
            total_return_pct = total_pnl / capital * 100.0
            rows.append(
                {
                    "qqq_return_pct": qqq_ret,
                    "csi_return_pct": csi_ret,
                    "qqq_package_pnl": qqq_pnl,
                    "csi_pnl": csi_pnl,
                    "cn_put_hedge_pnl": cn_put_hedge_pnl,
                    "safe_pnl": safe_pnl,
                    "financing_cost": financing_cost,
                    "total_pnl": total_pnl,
                    "total_return_pct": total_return_pct,
                    "floor_pct": floor_pct,
                    "floor_gap_pct": total_return_pct - floor_pct,
                    "floor_pass": total_return_pct >= floor_pct,
                }
            )
    worst = min(rows, key=lambda row: row["total_return_pct"])
    return rows, {
        "scenario_count": len(rows),
        "floor_pass_count": sum(1 for row in rows if row["floor_pass"]),
        "floor_fail_count": sum(1 for row in rows if not row["floor_pass"]),
        "all_scenarios_floor_pass": all(row["floor_pass"] for row in rows),
        "worst_scenario": worst,
        "weights": weights,
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
    parser = argparse.ArgumentParser(description="Stress China ETF put candidate as CSI hedge substitute.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--floor-cost-json")
    parser.add_argument("--floor-cost-key", default="best_stress_candidate")
    parser.add_argument("--qqq-min-pct", type=float, default=-50.0)
    parser.add_argument("--qqq-max-pct", type=float, default=30.0)
    parser.add_argument("--csi-min-pct", type=float, default=-40.0)
    parser.add_argument("--csi-max-pct", type=float, default=25.0)
    parser.add_argument("--shock-step-pct", type=float, default=5.0)
    parser.add_argument("--safe-return-pct", type=float, default=0.0)
    parser.add_argument("--financing-rate-annual-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--contract-min", type=int)
    parser.add_argument("--contract-max", type=int)
    parser.add_argument("--output-prefix")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    target_path = Path(args.target_json) if args.target_json else default_target_path(args.as_of)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    floor_cost_path = Path(args.floor_cost_json) if args.floor_cost_json else latest_json("option_package_floor_cost_csihedge_*.json")
    if not floor_cost_path.is_absolute():
        floor_cost_path = ROOT / floor_cost_path
    target = load_json(target_path)
    floor_cost = load_json(floor_cost_path)
    qqq_package = floor_cost.get(args.floor_cost_key)
    if not qqq_package:
        raise RuntimeError(f"floor-cost file has no {args.floor_cost_key}")
    cn_put = load_cn_put_candidate(target)
    capital = float(target["capital"])
    underlying_code = underlying_from_opt_code(cn_put["underlying_option_code"])
    fund_price = latest_fund_price(underlying_code, as_of)
    if not fund_price:
        raise RuntimeError(f"missing fund_daily close for {underlying_code} <= {as_of}")

    base_contracts = int(cn_put["contract_count_candidate"])
    contract_min = args.contract_min if args.contract_min is not None else max(1, base_contracts - 3)
    contract_max = args.contract_max if args.contract_max is not None else base_contracts + 10
    qqq_shocks = pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct)
    csi_shocks = pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct)
    search: list[dict[str, Any]] = []
    rows_by_contract: dict[int, list[dict[str, Any]]] = {}
    summary_by_contract: dict[int, dict[str, Any]] = {}
    for contracts in range(contract_min, contract_max + 1):
        candidate = with_contract_count(cn_put, contracts, capital)
        case_rows, case_summary = scenario_rows(
            target=target,
            qqq_package=qqq_package,
            cn_put=candidate,
            etf_spot=float(fund_price["price"]),
            qqq_shocks=qqq_shocks,
            csi_shocks=csi_shocks,
            safe_return_pct=args.safe_return_pct,
            financing_rate_annual_pct=args.financing_rate_annual_pct,
            slippage_bps_per_leg=args.slippage_bps_per_leg,
        )
        rows_by_contract[contracts] = case_rows
        summary_by_contract[contracts] = case_summary
        search.append(
            {
                "contract_count": contracts,
                "protected_weight_pct": candidate["protected_weight_pct_candidate"],
                "premium_cost_pct": candidate["premium_cost_pct_candidate"],
                "premium_budget_pass": candidate["premium_cost_pct_candidate"]
                <= float(target["defined_loss_terms"]["monthly_premium_budget_pct"]),
                "floor_pass_count": case_summary["floor_pass_count"],
                "scenario_count": case_summary["scenario_count"],
                "all_scenarios_floor_pass": case_summary["all_scenarios_floor_pass"],
                "worst_total_return_pct": case_summary["worst_scenario"]["total_return_pct"],
                "worst_floor_gap_pct": case_summary["worst_scenario"]["floor_gap_pct"],
                "worst_qqq_return_pct": case_summary["worst_scenario"]["qqq_return_pct"],
                "worst_csi_return_pct": case_summary["worst_scenario"]["csi_return_pct"],
            }
        )
    all_pass = [row for row in search if row["all_scenarios_floor_pass"]]
    cheapest_all_pass = min(all_pass, key=lambda row: (row["premium_cost_pct"], row["contract_count"])) if all_pass else None
    best_search = max(
        search,
        key=lambda row: (
            row["floor_pass_count"],
            row["worst_total_return_pct"],
            -row["premium_cost_pct"],
        ),
    )
    selected_contracts = int((cheapest_all_pass or best_search)["contract_count"])
    cn_put_selected = with_contract_count(cn_put, selected_contracts, capital)
    rows = rows_by_contract[selected_contracts]
    summary = summary_by_contract[selected_contracts]
    report = {
        "strategy": "scorecard_csi_cn_etf_put_hedge_candidate_stress",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "floor_cost_json": str(floor_cost_path),
        "floor_cost_key": args.floor_cost_key,
        "target_rule": target.get("rule_name"),
        "status": "cn_etf_put_floor_validated" if summary["all_scenarios_floor_pass"] else "cn_etf_put_floor_failed",
        "cn_put_candidate": cn_put_selected,
        "base_contract_count": base_contracts,
        "selected_contract_count": selected_contracts,
        "contract_search": search,
        "cheapest_all_pass": cheapest_all_pass,
        "best_search_candidate": best_search,
        "underlying_price": fund_price,
        "qqq_package": qqq_package,
        "assumptions": {
            "safe_return_pct": args.safe_return_pct,
            "financing_rate_annual_pct": args.financing_rate_annual_pct,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "qqq_shocks_pct": qqq_shocks,
            "csi_shocks_pct": csi_shocks,
            "contract_min": contract_min,
            "contract_max": contract_max,
            "mapping_note": "CSI shock is applied one-for-one to the ETF option underlying; basis and intramonth path are not modeled.",
        },
        **summary,
    }
    output_prefix = (
        Path(args.output_prefix)
        if args.output_prefix
        else OUT_DIR / f"cn_etf_put_hedge_stress_{args.as_of.replace('-', '')}"
    )
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, rows, output_prefix)

    worst = summary["worst_scenario"]
    print("CN ETF put hedge candidate stress")
    print(
        f"  rule={report['target_rule']} status={report['status']} "
        f"pass={summary['floor_pass_count']}/{summary['scenario_count']}"
    )
    print(
        f"  cn_put={cn_put_selected['index_code']} contracts={cn_put_selected['contract_count_candidate']} "
        f"cover={float(cn_put_selected['protected_weight_pct_candidate']):.2f}% "
        f"premium={float(cn_put_selected['premium_cost_pct_candidate']):.2f}%"
    )
    if cheapest_all_pass:
        print(
            f"  cheapest_all_pass contracts={cheapest_all_pass['contract_count']} "
            f"cover={cheapest_all_pass['protected_weight_pct']:.2f}% "
            f"premium={cheapest_all_pass['premium_cost_pct']:.2f}% "
            f"budget_pass={cheapest_all_pass['premium_budget_pass']}"
        )
    else:
        print("  cheapest_all_pass=None")
    print(
        f"  worst qqq={worst['qqq_return_pct']:.1f}% csi={worst['csi_return_pct']:.1f}% "
        f"return={worst['total_return_pct']:.2f}% floor={worst['floor_pct']:.2f}% "
        f"gap={worst['floor_gap_pct']:.2f}pp"
    )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    if args.strict and not summary["all_scenarios_floor_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

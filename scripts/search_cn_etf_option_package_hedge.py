#!/usr/bin/env python3
"""Search China CSI300 ETF option packages as executable CSI hedge substitutes."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.stress_cn_etf_put_hedge_candidate import latest_json, qqq_package_pnl, underlying_from_opt_code
from scripts.stress_defined_loss_replication import default_target_path, pct_grid, target_weights

OUT_DIR = ROOT / "data" / "portfolio"
UNDERLYINGS = ("OP510300.SH", "OP159919.SZ")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fund_prices(as_of: date) -> dict[str, dict[str, Any]]:
    codes = [underlying_from_opt_code(code) for code in UNDERLYINGS]
    conn = get_connection()
    out: dict[str, dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            for code in codes:
                cur.execute(
                    """
                    SELECT trade_date, close
                    FROM fund_daily
                    WHERE ts_code=%s AND trade_date <= %s AND close IS NOT NULL
                    ORDER BY trade_date DESC
                    LIMIT 1
                    """,
                    (code, as_of),
                )
                row = cur.fetchone()
                if row:
                    out[code] = {"trade_date": row[0].isoformat(), "price": float(row[1])}
    finally:
        conn.close()
    return out


def load_options(as_of: date) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM cn_option_daily WHERE trade_date <= %s", (as_of,))
            quote_date = cur.fetchone()[0]
            if not quote_date:
                return []
            cur.execute(
                """
                SELECT
                    b.ts_code, b.opt_code, b.name, b.call_put, b.exercise_price,
                    b.per_unit, b.maturity_date, d.trade_date, d.close, d.settle,
                    d.vol, d.oi
                FROM cn_option_basic b
                JOIN cn_option_daily d ON d.ts_code=b.ts_code
                WHERE d.trade_date=%s
                  AND b.opt_code IN ('OP510300.SH','OP159919.SZ')
                  AND b.maturity_date >= %s
                  AND d.vol > 0
                  AND d.close IS NOT NULL
                  AND b.exercise_price IS NOT NULL
                  AND b.per_unit IS NOT NULL
                ORDER BY b.opt_code, b.maturity_date, b.call_put, b.exercise_price
                """,
                (quote_date, as_of),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        (
            ts_code,
            opt_code,
            name,
            call_put,
            exercise_price,
            per_unit,
            maturity_date,
            quote_date,
            close,
            settle,
            vol,
            oi,
        ) = row
        out.append(
            {
                "ts_code": ts_code,
                "opt_code": opt_code,
                "name": name,
                "call_put": call_put,
                "strike": float(exercise_price),
                "per_unit": float(per_unit),
                "maturity_date": maturity_date.isoformat(),
                "quote_date": quote_date.isoformat(),
                "close": float(close),
                "settle": float(settle) if settle is not None else None,
                "vol": float(vol),
                "oi": float(oi) if oi is not None else None,
            }
        )
    return out


def option_payoff(option_type: str, side: str, strike: float, units: float, final_spot: float) -> float:
    if option_type == "P":
        payoff = max(strike - final_spot, 0.0) * units
    elif option_type == "C":
        payoff = max(final_spot - strike, 0.0) * units
    else:
        payoff = 0.0
    return payoff if side == "buy" else -payoff


def package_pnl(package: dict[str, Any], csi_ret_pct: float, spot: float, slippage_bps_per_leg: float) -> float:
    final_spot = spot * (1.0 + csi_ret_pct / 100.0)
    pnl = 0.0
    leg_count = 0
    notional_for_slippage = 0.0
    for leg in package["legs"]:
        units = float(leg["contracts"]) * float(leg["per_unit"])
        pnl += option_payoff(leg["call_put"], leg["side"], float(leg["strike"]), units, final_spot)
        leg_count += 1
        notional_for_slippage += abs(float(leg["strike"]) * units)
    return pnl - float(package["net_debit"]) - notional_for_slippage * slippage_bps_per_leg / 10000.0


def build_package(
    long_put: dict[str, Any],
    contracts: int,
    short_put: dict[str, Any] | None,
    short_call: dict[str, Any] | None,
    short_call_multiplier: float,
) -> dict[str, Any]:
    legs = [
        {
            "role": "long_put",
            "side": "buy",
            "call_put": "P",
            "contract": long_put["ts_code"],
            "strike": long_put["strike"],
            "close": long_put["close"],
            "per_unit": long_put["per_unit"],
            "contracts": contracts,
        }
    ]
    net_debit = long_put["close"] * long_put["per_unit"] * contracts
    if short_put:
        legs.append(
            {
                "role": "short_put",
                "side": "sell",
                "call_put": "P",
                "contract": short_put["ts_code"],
                "strike": short_put["strike"],
                "close": short_put["close"],
                "per_unit": short_put["per_unit"],
                "contracts": contracts,
            }
        )
        net_debit -= short_put["close"] * short_put["per_unit"] * contracts
    short_call_contracts = int(round(contracts * short_call_multiplier)) if short_call else 0
    if short_call and short_call_contracts > 0:
        legs.append(
            {
                "role": "short_call",
                "side": "sell",
                "call_put": "C",
                "contract": short_call["ts_code"],
                "strike": short_call["strike"],
                "close": short_call["close"],
                "per_unit": short_call["per_unit"],
                "contracts": short_call_contracts,
            }
        )
        net_debit -= short_call["close"] * short_call["per_unit"] * short_call_contracts
    return {
        "underlying_option_code": long_put["opt_code"],
        "maturity_date": long_put["maturity_date"],
        "long_put_contract": long_put["ts_code"],
        "long_put_strike": long_put["strike"],
        "long_put_close": long_put["close"],
        "short_put_contract": short_put["ts_code"] if short_put else None,
        "short_put_strike": short_put["strike"] if short_put else None,
        "short_put_close": short_put["close"] if short_put else None,
        "short_call_contract": short_call["ts_code"] if short_call and short_call_contracts > 0 else None,
        "short_call_strike": short_call["strike"] if short_call and short_call_contracts > 0 else None,
        "short_call_close": short_call["close"] if short_call and short_call_contracts > 0 else None,
        "contracts": contracts,
        "short_call_contracts": short_call_contracts,
        "per_unit": long_put["per_unit"],
        "protected_notional": long_put["strike"] * long_put["per_unit"] * contracts,
        "net_debit": net_debit,
        "legs": legs,
        "min_leg_volume": min(float(leg_source["vol"]) for leg_source in [long_put, *( [short_put] if short_put else [] ), *( [short_call] if short_call and short_call_contracts > 0 else [] )]),
        "min_leg_oi": min(float(leg_source["oi"] or 0.0) for leg_source in [long_put, *( [short_put] if short_put else [] ), *( [short_call] if short_call and short_call_contracts > 0 else [] )]),
    }


def score_package(
    target: dict[str, Any],
    qqq_package: dict[str, Any],
    cn_package: dict[str, Any],
    spot: float,
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
) -> dict[str, Any]:
    capital = float(target["capital"])
    weights = target_weights(target)
    floor_pct = weights["monthly_floor_pct"]
    safe_return = safe_return_pct / 100.0
    financing_monthly = financing_rate_annual_pct / 100.0 / 12.0
    pass_count = 0
    scenario_count = 0
    worst: dict[str, Any] | None = None
    max_csi_ret = max(csi_shocks) if csi_shocks else 25.0
    short_call_stress_loss = 0.0
    short_call_underlying_notional = 0.0
    stressed_spot = spot * (1.0 + max_csi_ret / 100.0)
    for leg in cn_package["legs"]:
        if leg["side"] == "sell" and leg["call_put"] == "C":
            units = float(leg["contracts"]) * float(leg["per_unit"])
            short_call_stress_loss += max(stressed_spot - float(leg["strike"]), 0.0) * units
            short_call_underlying_notional += spot * units
    margin_proxy = max(short_call_stress_loss, short_call_underlying_notional * 0.12)
    for qqq_ret in qqq_shocks:
        qqq_pnl = qqq_package_pnl(qqq_package, qqq_ret, slippage_bps_per_leg)
        for csi_ret in csi_shocks:
            csi_pnl = capital * weights["csi_gross_pct"] / 100.0 * csi_ret / 100.0
            safe_pnl = capital * weights["safe_pct"] / 100.0 * safe_return
            financing_cost = abs(capital * weights["financing_pct"] / 100.0) * financing_monthly
            cn_pnl = package_pnl(cn_package, csi_ret, spot, slippage_bps_per_leg)
            total_return_pct = (qqq_pnl + csi_pnl + safe_pnl - financing_cost + cn_pnl) / capital * 100.0
            scenario_count += 1
            if total_return_pct >= floor_pct:
                pass_count += 1
            row = {
                "qqq_return_pct": qqq_ret,
                "csi_return_pct": csi_ret,
                "total_return_pct": total_return_pct,
                "floor_gap_pct": total_return_pct - floor_pct,
            }
            if worst is None or total_return_pct < worst["total_return_pct"]:
                worst = row
    assert worst is not None
    total_net_debit_pct = (float(qqq_package["net_debit"]) + float(cn_package["net_debit"])) / capital * 100.0
    cn_net_debit_pct = float(cn_package["net_debit"]) / capital * 100.0
    premium_budget_pct = float(target["defined_loss_terms"]["monthly_premium_budget_pct"])
    return {
        **{key: value for key, value in cn_package.items() if key != "legs"},
        "cn_net_debit_pct": cn_net_debit_pct,
        "total_net_debit_pct": total_net_debit_pct,
        "premium_budget_pct": premium_budget_pct,
        "premium_budget_pass": total_net_debit_pct <= premium_budget_pct,
        "protected_weight_pct": float(cn_package["protected_notional"]) / capital * 100.0,
        "short_call_stress_up_pct": max_csi_ret if short_call_underlying_notional > 0 else None,
        "short_call_stress_loss_amount": short_call_stress_loss if short_call_underlying_notional > 0 else None,
        "short_call_stress_loss_pct_capital": short_call_stress_loss / capital * 100.0
        if short_call_underlying_notional > 0
        else None,
        "margin_proxy_amount": margin_proxy if short_call_underlying_notional > 0 else None,
        "margin_proxy_pct_capital": margin_proxy / capital * 100.0 if short_call_underlying_notional > 0 else None,
        "stress_pass_count": pass_count,
        "stress_scenario_count": scenario_count,
        "all_stress_floor_pass": pass_count == scenario_count,
        "stress_pass_rate": pass_count / scenario_count if scenario_count else 0.0,
        "worst_total_return_pct": worst["total_return_pct"],
        "worst_floor_gap_pct": worst["floor_gap_pct"],
        "worst_qqq_return_pct": worst["qqq_return_pct"],
        "worst_csi_return_pct": worst["csi_return_pct"],
        "legs": cn_package["legs"],
    }


def write_outputs(report: dict[str, Any], rows: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            fieldnames = [key for key in rows[0] if key != "legs"]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: value for key, value in row.items() if key != "legs"})
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search China ETF option packages under CSI hedge stress.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--floor-cost-json")
    parser.add_argument("--floor-cost-key", default="best_stress_candidate")
    parser.add_argument("--contract-extra", type=int, default=12)
    parser.add_argument("--short-call-multipliers", default="0,0.5,1")
    parser.add_argument("--qqq-min-pct", type=float, default=-50.0)
    parser.add_argument("--qqq-max-pct", type=float, default=30.0)
    parser.add_argument("--csi-min-pct", type=float, default=-40.0)
    parser.add_argument("--csi-max-pct", type=float, default=25.0)
    parser.add_argument("--shock-step-pct", type=float, default=5.0)
    parser.add_argument("--safe-return-pct", type=float, default=0.0)
    parser.add_argument("--financing-rate-annual-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
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

    prices = load_fund_prices(as_of)
    options = load_options(as_of)
    groups: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"P": [], "C": []})
    for option in options:
        groups[(option["opt_code"], option["maturity_date"])][option["call_put"]].append(option)

    qqq_shocks = pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct)
    csi_shocks = pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct)
    short_call_multipliers = [float(item) for item in args.short_call_multipliers.split(",") if item.strip()]
    capital = float(target["capital"])
    target_notional = capital * float(target["defined_loss_terms"]["csi_hedge_pct"]) / 100.0
    scores: list[dict[str, Any]] = []
    for (opt_code, _maturity), legs in groups.items():
        underlying = underlying_from_opt_code(opt_code)
        price = prices.get(underlying)
        if not price:
            continue
        puts = sorted(legs["P"], key=lambda row: row["strike"])
        calls = sorted(legs["C"], key=lambda row: row["strike"])
        for long_put in puts:
            contract_notional = long_put["strike"] * long_put["per_unit"]
            base_contracts = max(1, math.ceil(target_notional / contract_notional))
            short_put_choices = [None] + [put for put in puts if put["strike"] < long_put["strike"]]
            short_call_choices = [None] + [call for call in calls if call["strike"] >= float(price["price"])]
            for contracts in range(max(1, base_contracts - 3), base_contracts + args.contract_extra + 1):
                for short_put in short_put_choices:
                    for short_call in short_call_choices:
                        for short_call_multiplier in short_call_multipliers:
                            if short_call is None and short_call_multiplier > 0:
                                continue
                            package = build_package(long_put, contracts, short_put, short_call, short_call_multiplier)
                            scores.append(
                                score_package(
                                    target=target,
                                    qqq_package=qqq_package,
                                    cn_package=package,
                                    spot=float(price["price"]),
                                    qqq_shocks=qqq_shocks,
                                    csi_shocks=csi_shocks,
                                    safe_return_pct=args.safe_return_pct,
                                    financing_rate_annual_pct=args.financing_rate_annual_pct,
                                    slippage_bps_per_leg=args.slippage_bps_per_leg,
                                )
                            )
    scores.sort(
        key=lambda row: (
            row["all_stress_floor_pass"],
            row["premium_budget_pass"],
            row["stress_pass_count"],
            row["worst_total_return_pct"],
            -row["total_net_debit_pct"],
        ),
        reverse=True,
    )
    all_pass = [row for row in scores if row["all_stress_floor_pass"]]
    budget_all_pass = [row for row in all_pass if row["premium_budget_pass"]]
    cheapest_all_pass = min(all_pass, key=lambda row: (row["total_net_debit_pct"], row["cn_net_debit_pct"])) if all_pass else None
    cheapest_budget_all_pass = (
        min(budget_all_pass, key=lambda row: (row["total_net_debit_pct"], row["cn_net_debit_pct"]))
        if budget_all_pass
        else None
    )
    best = scores[0] if scores else None
    report = {
        "strategy": "scorecard_csi_cn_etf_option_package_hedge_search",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "floor_cost_json": str(floor_cost_path),
        "floor_cost_key": args.floor_cost_key,
        "target_rule": target.get("rule_name"),
        "status": "budget_all_pass_found" if cheapest_budget_all_pass else ("all_pass_found" if cheapest_all_pass else "no_all_pass"),
        "candidate_count": len(scores),
        "all_pass_count": len(all_pass),
        "budget_all_pass_count": len(budget_all_pass),
        "best_candidate": best,
        "cheapest_all_pass": cheapest_all_pass,
        "cheapest_budget_all_pass": cheapest_budget_all_pass,
        "top_by_stress": scores[:50],
        "assumptions": {
            "short_call_multipliers": short_call_multipliers,
            "contract_extra": args.contract_extra,
            "safe_return_pct": args.safe_return_pct,
            "financing_rate_annual_pct": args.financing_rate_annual_pct,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "qqq_shocks_pct": qqq_shocks,
            "csi_shocks_pct": csi_shocks,
            "pricing_note": "Uses option close as execution price for both buys and sells plus slippage; bid/ask not available.",
        },
    }
    output_prefix = (
        Path(args.output_prefix)
        if args.output_prefix
        else OUT_DIR / f"cn_etf_option_package_hedge_search_{args.as_of.replace('-', '')}"
    )
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, scores[:200], output_prefix)
    print("CN ETF option package hedge stress search")
    print(
        f"  rule={target.get('rule_name')} status={report['status']} candidates={len(scores)} "
        f"all_pass={len(all_pass)} budget_all_pass={len(budget_all_pass)}"
    )
    if best:
        print(
            f"  best long_put={best['long_put_contract']} contracts={best['contracts']} "
            f"short_put={best['short_put_contract']} short_call={best['short_call_contract']} "
            f"total_net={best['total_net_debit_pct']:.2f}% pass={best['stress_pass_count']}/{best['stress_scenario_count']} "
            f"worst={best['worst_total_return_pct']:.2f}%"
        )
    if cheapest_budget_all_pass:
        print(
            f"  cheapest_budget_all_pass long_put={cheapest_budget_all_pass['long_put_contract']} "
            f"contracts={cheapest_budget_all_pass['contracts']} "
            f"short_put={cheapest_budget_all_pass['short_put_contract']} "
            f"short_call={cheapest_budget_all_pass['short_call_contract']} "
            f"total_net={cheapest_budget_all_pass['total_net_debit_pct']:.2f}%"
        )
    else:
        print("  cheapest_budget_all_pass=None")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    if args.strict and not cheapest_budget_all_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

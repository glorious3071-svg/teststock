#!/usr/bin/env python3
"""Find the minimum listed-option package cost needed to pass floor stress.

This diagnostic relaxes the modeled premium budget and asks a narrower
execution question: using the current cached option chain, is there any listed
QQQ option package that can support the target portfolio monthly floor under
the same simple QQQ/CSI shock grid?  It includes collars, put spreads, and
pure protective-put packages.
"""

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

from db.connection import get_connection
from scripts.audit_defined_loss_execution_feasibility import latest_external_price
from scripts.search_executable_option_package_candidates import option_rows
from scripts.stress_defined_loss_replication import default_target_path, option_payoff, pct_grid, target_weights

OUT_DIR = ROOT / "data" / "portfolio"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def nearest(rows: list[dict[str, Any]], expiry: date, option_type: str, target_strike: float) -> dict[str, Any] | None:
    candidates = [row for row in rows if row["expiration_date"] == expiry and row["option_type"] == option_type]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(row["strike"] - target_strike))


def maybe_leg(
    rows: list[dict[str, Any]],
    expiry: date,
    option_type: str,
    spot: float,
    strike_pct: float,
    max_gap: float,
) -> dict[str, Any] | None:
    if strike_pct <= 0.0:
        return None
    row = nearest(rows, expiry, option_type, spot * strike_pct / 100.0)
    if row is None:
        return None
    gap = abs(row["strike"] / (spot * strike_pct / 100.0) - 1.0)
    return row if gap <= max_gap else None


def build_candidates(
    rows: list[dict[str, Any]],
    spot: float,
    capital: float,
    quote_date: date,
    target: dict[str, Any],
    min_dte: int,
    max_dte: int,
    put_strike_pcts: list[float],
    short_put_strike_pcts: list[float],
    call_strike_pcts: list[float],
    put_cover_multipliers: list[float],
    call_cover_multipliers: list[float],
    csi_hedge_pcts: list[float],
    max_strike_gap: float,
) -> list[dict[str, Any]]:
    option_row = next(row for row in target["rows"] if row.get("asset_type") == "option_protected_sleeve")
    underlying_notional_pct = float(option_row["underlying_notional_pct"])
    base_put_cover_pct = float(option_row["long_put_cover_pct"])
    base_call_cover_pct = float(option_row["call_cover_pct"])
    underlying_notional = capital * underlying_notional_pct / 100.0
    underlying_units = underlying_notional / spot
    expiries = sorted({
        row["expiration_date"] for row in rows
        if min_dte <= (row["expiration_date"] - quote_date).days <= max_dte
    })
    out: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for expiry in expiries:
        dte = (expiry - quote_date).days
        for put_pct in put_strike_pcts:
            put = maybe_leg(rows, expiry, "put", spot, put_pct, max_strike_gap)
            if put is None:
                continue
            for short_put_pct in short_put_strike_pcts:
                if short_put_pct >= put_pct:
                    continue
                short_put = maybe_leg(rows, expiry, "put", spot, short_put_pct, max_strike_gap)
                if short_put_pct > 0.0 and short_put is None:
                    continue
                for call_pct in call_strike_pcts:
                    call = maybe_leg(rows, expiry, "call", spot, call_pct, max_strike_gap)
                    if call_pct > 0.0 and call is None:
                        continue
                    key = (
                        put["contract_symbol"],
                        short_put["contract_symbol"] if short_put else None,
                        call["contract_symbol"] if call else None,
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    for put_cover_multiplier in put_cover_multipliers:
                        put_cover_pct = base_put_cover_pct * put_cover_multiplier
                        for call_cover_multiplier in call_cover_multipliers:
                            call_cover_pct = base_call_cover_pct * call_cover_multiplier if call else 0.0
                            long_put_cost = put["ask"] * underlying_units * put_cover_pct / 100.0
                            short_put_credit = short_put["bid"] * underlying_units * put_cover_pct / 100.0 if short_put else 0.0
                            short_call_credit = call["bid"] * underlying_units * call_cover_pct / 100.0 if call else 0.0
                            net_debit = long_put_cost - short_put_credit - short_call_credit
                            base_candidate = {
                                "expiration_date": expiry.isoformat(),
                                "dte": dte,
                                "put_strike_pct": put["strike"] / spot * 100.0,
                                "short_put_strike_pct": short_put["strike"] / spot * 100.0 if short_put else None,
                                "call_strike_pct": call["strike"] / spot * 100.0 if call else None,
                                "put_contract": put["contract_symbol"],
                                "short_put_contract": short_put["contract_symbol"] if short_put else None,
                                "call_contract": call["contract_symbol"] if call else None,
                                "put_strike": put["strike"],
                                "short_put_strike": short_put["strike"] if short_put else None,
                                "call_strike": call["strike"] if call else None,
                                "put_bid": put["bid"],
                                "put_ask": put["ask"],
                                "short_put_bid": short_put["bid"] if short_put else None,
                                "short_put_ask": short_put["ask"] if short_put else None,
                                "call_bid": call["bid"] if call else None,
                                "call_ask": call["ask"] if call else None,
                                "underlying_notional": underlying_notional,
                                "underlying_units": underlying_units,
                                "put_cover_multiplier": put_cover_multiplier,
                                "call_cover_multiplier": call_cover_multiplier if call else 0.0,
                                "put_cover_pct": put_cover_pct,
                                "call_cover_pct": call_cover_pct,
                                "long_put_cost": long_put_cost,
                                "short_put_credit": short_put_credit,
                                "short_call_credit": short_call_credit,
                                "net_debit": net_debit,
                                "net_debit_pct_capital": net_debit / capital * 100.0,
                                "premium_budget_pct_capital": target["defined_loss_terms"]["monthly_premium_budget_pct"],
                                "premium_budget_pass": net_debit <= capital * float(target["defined_loss_terms"]["monthly_premium_budget_pct"]) / 100.0,
                            }
                            for csi_hedge_pct in csi_hedge_pcts:
                                out.append({**base_candidate, "csi_hedge_pct": csi_hedge_pct})
    return out


def candidate_legs(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    underlying_units = float(candidate["underlying_units"])
    put_cover_pct = float(candidate["put_cover_pct"])
    call_cover_pct = float(candidate["call_cover_pct"])
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
    candidate: dict[str, Any],
    target: dict[str, Any],
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
    csi_hedge_cost_annual_pct: float,
) -> dict[str, Any]:
    capital = float(target["capital"])
    weights = target_weights(target)
    underlying_price = float(candidate["put_strike"]) / (float(candidate["put_strike_pct"]) / 100.0)
    underlying_notional = float(candidate["underlying_notional"])
    net_debit = float(candidate["net_debit"])
    legs = candidate_legs(candidate)
    slippage = underlying_notional * len(legs) * slippage_bps_per_leg / 10000.0
    safe_return = safe_return_pct / 100.0
    financing_monthly = financing_rate_annual_pct / 100.0 / 12.0
    csi_hedge_pct = float(candidate.get("csi_hedge_pct") or 0.0)
    csi_hedge_monthly_cost = csi_hedge_pct * csi_hedge_cost_annual_pct / 100.0 / 12.0
    floor_pct = weights["monthly_floor_pct"]
    current_csi_gross_pct = weights["csi_gross_pct"]
    scenario_count = 0
    pass_count = 0
    worst_return = 1e9
    worst_row: dict[str, Any] | None = None
    max_csi_gross_pct = float("inf")
    max_csi_constraint_row: dict[str, Any] | None = None
    for qqq_ret in qqq_shocks:
        final_spot = underlying_price * (1.0 + qqq_ret / 100.0)
        listed_option_pnl = option_payoff(legs, final_spot) - net_debit - slippage
        qqq_underlying_pnl = underlying_notional * qqq_ret / 100.0
        option_sleeve_pnl = qqq_underlying_pnl + listed_option_pnl
        for csi_ret in csi_shocks:
            csi_pnl = capital * current_csi_gross_pct / 100.0 * csi_ret / 100.0
            safe_pnl = capital * weights["safe_pct"] / 100.0 * safe_return
            financing_cost = abs(capital * weights["financing_pct"] / 100.0) * financing_monthly
            csi_hedge_return_pct = -csi_hedge_pct * csi_ret / 100.0 - csi_hedge_monthly_cost
            base_return_pct = (safe_pnl - financing_cost + option_sleeve_pnl) / capital * 100.0 + csi_hedge_return_pct
            total_return_pct = base_return_pct + current_csi_gross_pct * csi_ret / 100.0
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
            if csi_ret < 0:
                allowed = (floor_pct - base_return_pct) * 100.0 / csi_ret
                if allowed < max_csi_gross_pct:
                    max_csi_gross_pct = allowed
                    max_csi_constraint_row = {
                        "qqq_return_pct": qqq_ret,
                        "csi_return_pct": csi_ret,
                        "base_return_without_csi_pct": base_return_pct,
                        "max_csi_gross_pct": allowed,
                    }
    assert worst_row is not None
    if max_csi_gross_pct == float("inf"):
        max_csi_gross_pct = 0.0
    clamped_max_csi_gross_pct = max(0.0, max_csi_gross_pct)
    return {
        **candidate,
        "stress_pass_count": pass_count,
        "stress_scenario_count": scenario_count,
        "stress_pass_rate": pass_count / scenario_count if scenario_count else 0.0,
        "all_stress_floor_pass": pass_count == scenario_count,
        "worst_total_return_pct": worst_return,
        "worst_floor_gap_pct": worst_return - floor_pct,
        "worst_qqq_return_pct": worst_row["qqq_return_pct"],
        "worst_csi_return_pct": worst_row["csi_return_pct"],
        "current_csi_gross_pct": current_csi_gross_pct,
        "csi_hedge_pct": csi_hedge_pct,
        "csi_hedge_cost_annual_pct": csi_hedge_cost_annual_pct,
        "max_csi_gross_pct_for_floor": clamped_max_csi_gross_pct,
        "csi_gross_reduction_required_pct": max(0.0, current_csi_gross_pct - clamped_max_csi_gross_pct),
        "max_csi_constraint_qqq_return_pct": (
            max_csi_constraint_row or {}
        ).get("qqq_return_pct"),
        "max_csi_constraint_csi_return_pct": (
            max_csi_constraint_row or {}
        ).get("csi_return_pct"),
        "max_csi_constraint_base_return_without_csi_pct": (
            max_csi_constraint_row or {}
        ).get("base_return_without_csi_pct"),
        "leg_count": len(legs),
    }


def write_outputs(report: dict[str, Any], rows: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if rows:
        fields = list(rows[0])
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    return json_path, csv_path


def pct_grid_with_zero(start: float, stop: float, step: float, include_zero: bool) -> list[float]:
    values = pct_grid(start, stop, step)
    return ([0.0] + values) if include_zero else values


def parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose minimum listed option cost needed to pass floor stress.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--source", default="cboe_delayed_quotes")
    parser.add_argument("--min-dte", type=int, default=5)
    parser.add_argument("--max-dte", type=int, default=65)
    parser.add_argument("--put-min-pct", type=float, default=80.0)
    parser.add_argument("--put-max-pct", type=float, default=105.0)
    parser.add_argument("--short-put-min-pct", type=float, default=70.0)
    parser.add_argument("--short-put-max-pct", type=float, default=100.0)
    parser.add_argument("--call-min-pct", type=float, default=100.0)
    parser.add_argument("--call-max-pct", type=float, default=140.0)
    parser.add_argument("--strike-step-pct", type=float, default=2.5)
    parser.add_argument("--put-cover-multipliers", default="1.0")
    parser.add_argument("--call-cover-multipliers", default="1.0")
    parser.add_argument("--csi-hedge-pcts", default="0")
    parser.add_argument("--csi-hedge-cost-annual-pct", type=float, default=1.0)
    parser.add_argument("--max-strike-gap-pct", type=float, default=2.0)
    parser.add_argument("--qqq-min-pct", type=float, default=-50.0)
    parser.add_argument("--qqq-max-pct", type=float, default=30.0)
    parser.add_argument("--csi-min-pct", type=float, default=-40.0)
    parser.add_argument("--csi-max-pct", type=float, default=25.0)
    parser.add_argument("--shock-step-pct", type=float, default=5.0)
    parser.add_argument("--safe-return-pct", type=float, default=0.0)
    parser.add_argument("--financing-rate-annual-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    target_path = Path(args.target_json) if args.target_json else default_target_path(args.as_of)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    target = load_json(target_path)
    option_row = next(row for row in target["rows"] if row.get("asset_type") == "option_protected_sleeve")
    symbol = option_row["index_code"]
    capital = float(target["capital"])

    conn = get_connection()
    try:
        price = latest_external_price(conn, symbol, as_of)
        rows = option_rows(conn, symbol, as_of, args.source)
    finally:
        conn.close()
    if not price:
        raise RuntimeError(f"missing external price for {symbol} <= {as_of}")
    if not rows:
        raise RuntimeError(f"missing option-chain rows for {symbol} source={args.source} quote_date={as_of}")

    candidates = build_candidates(
        rows=rows,
        spot=float(price["price"]),
        capital=capital,
        quote_date=as_of,
        target=target,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        put_strike_pcts=pct_grid(args.put_min_pct, args.put_max_pct, args.strike_step_pct),
        short_put_strike_pcts=pct_grid_with_zero(args.short_put_min_pct, args.short_put_max_pct, args.strike_step_pct, True),
        call_strike_pcts=pct_grid_with_zero(args.call_min_pct, args.call_max_pct, args.strike_step_pct, True),
        put_cover_multipliers=parse_float_list(args.put_cover_multipliers),
        call_cover_multipliers=parse_float_list(args.call_cover_multipliers),
        csi_hedge_pcts=parse_float_list(args.csi_hedge_pcts),
        max_strike_gap=args.max_strike_gap_pct / 100.0,
    )
    scores = [
        score_candidate(
            candidate=row,
            target=target,
            qqq_shocks=pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct),
            csi_shocks=pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct),
            safe_return_pct=args.safe_return_pct,
            financing_rate_annual_pct=args.financing_rate_annual_pct,
            slippage_bps_per_leg=args.slippage_bps_per_leg,
            csi_hedge_cost_annual_pct=args.csi_hedge_cost_annual_pct,
        )
        for row in candidates
    ]
    all_pass = [row for row in scores if row["all_stress_floor_pass"]]
    all_pass_by_cost = sorted(all_pass, key=lambda row: (row["net_debit_pct_capital"], abs(row["dte"] - 30)))
    best_by_stress = sorted(
        scores,
        key=lambda row: (
            row["all_stress_floor_pass"],
            row["stress_pass_count"],
            row["worst_total_return_pct"],
            -row["net_debit_pct_capital"],
        ),
        reverse=True,
    )
    best_by_cost = sorted(scores, key=lambda row: (row["net_debit_pct_capital"], -row["stress_pass_count"]))
    report = {
        "strategy": "scorecard_csi_option_package_floor_cost_diagnostic",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "target_rule": target.get("rule_name"),
        "source": args.source,
        "underlying_symbol": symbol,
        "underlying_price": price,
        "candidate_count": len(scores),
        "all_stress_floor_pass_count": len(all_pass),
        "cheapest_all_stress_floor_pass": all_pass_by_cost[0] if all_pass_by_cost else None,
        "best_stress_candidate": best_by_stress[0] if best_by_stress else None,
            "top_all_stress_floor_pass_by_cost": all_pass_by_cost[: args.top],
            "top_by_stress": best_by_stress[: args.top],
            "top_by_lowest_net_debit": best_by_cost[: args.top],
            "best_max_csi_gross_candidate": max(scores, key=lambda row: row["max_csi_gross_pct_for_floor"]) if scores else None,
            "assumptions": {
            "min_dte": args.min_dte,
            "max_dte": args.max_dte,
            "put_min_pct": args.put_min_pct,
            "put_max_pct": args.put_max_pct,
            "short_put_min_pct": args.short_put_min_pct,
            "short_put_max_pct": args.short_put_max_pct,
            "call_min_pct": args.call_min_pct,
            "call_max_pct": args.call_max_pct,
            "strike_step_pct": args.strike_step_pct,
            "put_cover_multipliers": parse_float_list(args.put_cover_multipliers),
            "call_cover_multipliers": parse_float_list(args.call_cover_multipliers),
            "csi_hedge_pcts": parse_float_list(args.csi_hedge_pcts),
            "csi_hedge_cost_annual_pct": args.csi_hedge_cost_annual_pct,
            "max_strike_gap_pct": args.max_strike_gap_pct,
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
    output_prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"option_package_floor_cost_{args.as_of.replace('-', '')}"
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    # Keep CSV compact and decision-useful.
    best_by_max_csi = sorted(scores, key=lambda row: row["max_csi_gross_pct_for_floor"], reverse=True)
    csv_rows = all_pass_by_cost[: args.top] + best_by_stress[: args.top] + best_by_cost[: args.top] + best_by_max_csi[: args.top]
    deduped = []
    seen = set()
    for row in csv_rows:
        key = (row.get("put_contract"), row.get("short_put_contract"), row.get("call_contract"))
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    json_path, csv_path = write_outputs(report, deduped, output_prefix)

    print("Option package floor-cost diagnostic")
    print(
        f"  rule={target.get('rule_name')} source={args.source} as_of={args.as_of} "
        f"candidates={len(scores)} all_pass={len(all_pass)}"
    )
    best = report["best_stress_candidate"]
    if best:
        print(
            f"  best_stress expiry={best['expiration_date']} dte={best['dte']} "
            f"put={best['put_strike_pct']:.1f}% short_put={best['short_put_strike_pct'] or 0:.1f}% "
            f"call={best['call_strike_pct'] or 0:.1f}% stress={best['stress_pass_count']}/{best['stress_scenario_count']} "
            f"worst={best['worst_total_return_pct']:.2f}% net={best['net_debit_pct_capital']:.2f}% "
            f"put_cover={best['put_cover_multiplier']:.2f}x "
            f"csi_hedge={best['csi_hedge_pct']:.2f}% "
            f"max_csi={best['max_csi_gross_pct_for_floor']:.2f}%"
        )
    best_csi = report["best_max_csi_gross_candidate"]
    if best_csi:
        print(
            f"  best_max_csi expiry={best_csi['expiration_date']} dte={best_csi['dte']} "
            f"put={best_csi['put_strike_pct']:.1f}% short_put={best_csi['short_put_strike_pct'] or 0:.1f}% "
            f"call={best_csi['call_strike_pct'] or 0:.1f}% "
            f"max_csi={best_csi['max_csi_gross_pct_for_floor']:.2f}% "
            f"net={best_csi['net_debit_pct_capital']:.2f}% "
            f"csi_hedge={best_csi['csi_hedge_pct']:.2f}%"
        )
    cheapest = report["cheapest_all_stress_floor_pass"]
    if cheapest:
        print(
            f"  cheapest_all_pass expiry={cheapest['expiration_date']} dte={cheapest['dte']} "
            f"put={cheapest['put_strike_pct']:.1f}% short_put={cheapest['short_put_strike_pct'] or 0:.1f}% "
            f"call={cheapest['call_strike_pct'] or 0:.1f}% net={cheapest['net_debit_pct_capital']:.2f}% "
            f"put_cover={cheapest['put_cover_multiplier']:.2f}x "
            f"csi_hedge={cheapest['csi_hedge_pct']:.2f}%"
        )
    else:
        print("  cheapest_all_pass=None")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Search China CSI300 ETF puts as small-account CSI hedge substitutes."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.stress_cn_etf_put_hedge_candidate import (
    latest_json,
    qqq_package_pnl,
    scenario_rows,
    underlying_from_opt_code,
    with_contract_count,
)
from scripts.stress_defined_loss_replication import default_target_path, pct_grid

OUT_DIR = ROOT / "data" / "portfolio"
UNDERLYINGS = ("OP510300.SH", "OP159919.SZ")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_fund_prices(as_of: date) -> dict[str, dict[str, Any]]:
    conn = get_connection()
    out: dict[str, dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            for code in [underlying_from_opt_code(item) for item in UNDERLYINGS]:
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
                    out[code] = {"trade_date": row[0].isoformat(), "price": float(row[1]), "source": "fund_daily"}
    finally:
        conn.close()
    return out


def load_puts(as_of: date) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(trade_date)
                FROM cn_option_daily
                WHERE trade_date <= %s
                """,
                (as_of,),
            )
            quote_date = cur.fetchone()[0]
            if not quote_date:
                return []
            cur.execute(
                """
                SELECT
                    b.ts_code, b.opt_code, b.name, b.exercise_price, b.per_unit,
                    b.maturity_date, d.trade_date, d.close, d.settle, d.vol, d.oi
                FROM cn_option_basic b
                JOIN cn_option_daily d ON d.ts_code = b.ts_code
                WHERE d.trade_date = %s
                  AND b.opt_code IN ('OP510300.SH', 'OP159919.SZ')
                  AND b.call_put = 'P'
                  AND b.maturity_date >= %s
                  AND d.vol > 0
                  AND d.close IS NOT NULL
                  AND b.exercise_price IS NOT NULL
                  AND b.per_unit IS NOT NULL
                ORDER BY b.opt_code, b.maturity_date, b.exercise_price
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
                "rank": None,
                "asset_type": "cn_etf_put_hedge_candidate",
                "index_code": ts_code,
                "index_name": name,
                "target_weight_pct": 0.0,
                "target_amount": 0.0,
                "source_component": "cn_etf_option_substitute_search",
                "underlying_option_code": opt_code,
                "option_quote_date": quote_date.isoformat(),
                "maturity_date": maturity_date.isoformat(),
                "exercise_price": float(exercise_price),
                "per_unit": float(per_unit),
                "option_close": float(close),
                "option_settle": float(settle) if settle is not None else None,
                "vol": float(vol),
                "oi": float(oi) if oi is not None else None,
                "execution_note": "Search candidate; not validated until stress, liquidity, and fill audits pass.",
            }
        )
    return out


def score_candidate(
    target: dict[str, Any],
    qqq_package: dict[str, Any],
    put: dict[str, Any],
    contracts: int,
    etf_spot: float,
    qqq_shocks: list[float],
    csi_shocks: list[float],
    safe_return_pct: float,
    financing_rate_annual_pct: float,
    slippage_bps_per_leg: float,
) -> dict[str, Any]:
    capital = float(target["capital"])
    candidate = with_contract_count(put, contracts, capital)
    rows, summary = scenario_rows(
        target=target,
        qqq_package=qqq_package,
        cn_put=candidate,
        etf_spot=etf_spot,
        qqq_shocks=qqq_shocks,
        csi_shocks=csi_shocks,
        safe_return_pct=safe_return_pct,
        financing_rate_annual_pct=financing_rate_annual_pct,
        slippage_bps_per_leg=slippage_bps_per_leg,
    )
    worst = summary["worst_scenario"]
    return {
        "contract": candidate["index_code"],
        "underlying_option_code": candidate["underlying_option_code"],
        "maturity_date": candidate["maturity_date"],
        "exercise_price": candidate["exercise_price"],
        "option_close": candidate["option_close"],
        "vol": candidate["vol"],
        "oi": candidate["oi"],
        "contract_count": contracts,
        "protected_weight_pct": candidate["protected_weight_pct_candidate"],
        "premium_cost_pct": candidate["premium_cost_pct_candidate"],
        "premium_budget_pass": candidate["premium_cost_pct_candidate"]
        <= float(target["defined_loss_terms"]["monthly_premium_budget_pct"]),
        "floor_pass_count": summary["floor_pass_count"],
        "scenario_count": summary["scenario_count"],
        "all_scenarios_floor_pass": summary["all_scenarios_floor_pass"],
        "worst_total_return_pct": worst["total_return_pct"],
        "worst_floor_gap_pct": worst["floor_gap_pct"],
        "worst_qqq_return_pct": worst["qqq_return_pct"],
        "worst_csi_return_pct": worst["csi_return_pct"],
    }


def write_outputs(report: dict[str, Any], rows: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search China CSI300 ETF put hedge candidates under stress.")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--target-json")
    parser.add_argument("--floor-cost-json")
    parser.add_argument("--floor-cost-key", default="best_stress_candidate")
    parser.add_argument("--contract-extra", type=int, default=12)
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
    fund_prices = latest_fund_prices(as_of)
    puts = load_puts(as_of)
    qqq_shocks = pct_grid(args.qqq_min_pct, args.qqq_max_pct, args.shock_step_pct)
    csi_shocks = pct_grid(args.csi_min_pct, args.csi_max_pct, args.shock_step_pct)
    rows: list[dict[str, Any]] = []
    capital = float(target["capital"])
    target_notional = capital * float(target["defined_loss_terms"]["csi_hedge_pct"]) / 100.0
    for put in puts:
        underlying = underlying_from_opt_code(put["underlying_option_code"])
        price = fund_prices.get(underlying)
        if not price:
            continue
        contract_notional = float(put["exercise_price"]) * float(put["per_unit"])
        base_contracts = max(1, math.ceil(target_notional / contract_notional))
        for contracts in range(max(1, base_contracts - 3), base_contracts + args.contract_extra + 1):
            rows.append(
                score_candidate(
                    target=target,
                    qqq_package=qqq_package,
                    put=put,
                    contracts=contracts,
                    etf_spot=float(price["price"]),
                    qqq_shocks=qqq_shocks,
                    csi_shocks=csi_shocks,
                    safe_return_pct=args.safe_return_pct,
                    financing_rate_annual_pct=args.financing_rate_annual_pct,
                    slippage_bps_per_leg=args.slippage_bps_per_leg,
                )
            )
    rows.sort(
        key=lambda row: (
            row["all_scenarios_floor_pass"],
            row["premium_budget_pass"],
            row["floor_pass_count"],
            row["worst_total_return_pct"],
            -row["premium_cost_pct"],
        ),
        reverse=True,
    )
    all_pass = [row for row in rows if row["all_scenarios_floor_pass"]]
    budget_all_pass = [row for row in all_pass if row["premium_budget_pass"]]
    cheapest_all_pass = min(all_pass, key=lambda row: (row["premium_cost_pct"], row["contract_count"])) if all_pass else None
    cheapest_budget_all_pass = (
        min(budget_all_pass, key=lambda row: (row["premium_cost_pct"], row["contract_count"]))
        if budget_all_pass
        else None
    )
    best = rows[0] if rows else None
    report = {
        "strategy": "scorecard_csi_cn_etf_put_hedge_search",
        "as_of": args.as_of,
        "target_json": str(target_path),
        "floor_cost_json": str(floor_cost_path),
        "floor_cost_key": args.floor_cost_key,
        "target_rule": target.get("rule_name"),
        "status": "budget_all_pass_found" if cheapest_budget_all_pass else ("all_pass_found" if cheapest_all_pass else "no_all_pass"),
        "candidate_count": len(rows),
        "all_pass_count": len(all_pass),
        "budget_all_pass_count": len(budget_all_pass),
        "best_candidate": best,
        "cheapest_all_pass": cheapest_all_pass,
        "cheapest_budget_all_pass": cheapest_budget_all_pass,
        "top_by_stress": rows[:50],
        "assumptions": {
            "contract_extra": args.contract_extra,
            "safe_return_pct": args.safe_return_pct,
            "financing_rate_annual_pct": args.financing_rate_annual_pct,
            "slippage_bps_per_leg": args.slippage_bps_per_leg,
            "qqq_shocks_pct": qqq_shocks,
            "csi_shocks_pct": csi_shocks,
        },
    }
    output_prefix = (
        Path(args.output_prefix)
        if args.output_prefix
        else OUT_DIR / f"cn_etf_put_hedge_search_{args.as_of.replace('-', '')}"
    )
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, rows[:200], output_prefix)
    print("CN ETF put hedge stress search")
    print(
        f"  rule={report['target_rule']} status={report['status']} "
        f"candidates={len(rows)} all_pass={len(all_pass)} budget_all_pass={len(budget_all_pass)}"
    )
    if best:
        print(
            f"  best={best['contract']} contracts={best['contract_count']} "
            f"premium={best['premium_cost_pct']:.2f}% cover={best['protected_weight_pct']:.2f}% "
            f"pass={best['floor_pass_count']}/{best['scenario_count']} "
            f"worst={best['worst_total_return_pct']:.2f}%"
        )
    if cheapest_all_pass:
        print(
            f"  cheapest_all_pass={cheapest_all_pass['contract']} contracts={cheapest_all_pass['contract_count']} "
            f"premium={cheapest_all_pass['premium_cost_pct']:.2f}% "
            f"budget_pass={cheapest_all_pass['premium_budget_pass']}"
        )
    else:
        print("  cheapest_all_pass=None")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    if args.strict and not cheapest_budget_all_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

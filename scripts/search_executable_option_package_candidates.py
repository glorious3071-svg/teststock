#!/usr/bin/env python3
"""Search executable option-package candidates around a defined-loss target.

The modeled frontier can pass with abstract premium assumptions.  This script
uses cached option-chain bid/ask snapshots to find concrete put/call packages
that fit the current premium budget, so production target generation can move
toward executable terms instead of relying on a synthetic Black-Scholes package.
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

OUT_DIR = ROOT / "data" / "portfolio"


def load_target(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def option_rows(conn, symbol: str, quote_date: date, source: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT expiration_date, option_type, strike, contract_symbol, bid, ask,
                   mark, last_price, implied_volatility, volume, open_interest
            FROM us_option_chain_snapshot
            WHERE underlying_symbol=%s
              AND quote_date=%s
              AND source=%s
              AND bid > 0
              AND ask > 0
            ORDER BY expiration_date, option_type, strike
            """,
            (symbol, quote_date, source),
        )
        rows = cur.fetchall()
    return [
        {
            "expiration_date": row[0],
            "option_type": row[1],
            "strike": float(row[2]),
            "contract_symbol": row[3],
            "bid": float(row[4]),
            "ask": float(row[5]),
            "mark": float(row[6]) if row[6] is not None else None,
            "last_price": float(row[7]) if row[7] is not None else None,
            "implied_volatility": float(row[8]) if row[8] is not None else None,
            "volume": int(row[9]) if row[9] is not None else None,
            "open_interest": int(row[10]) if row[10] is not None else None,
            "source": source,
        }
        for row in rows
    ]


def nearest(rows: list[dict[str, Any]], expiry: date, option_type: str, target_strike: float) -> dict[str, Any] | None:
    candidates = [
        row for row in rows
        if row["expiration_date"] == expiry and row["option_type"] == option_type
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(row["strike"] - target_strike))


def build_candidates(
    rows: list[dict[str, Any]],
    spot: float,
    capital: float,
    underlying_notional_pct: float,
    put_cover_pct: float,
    call_cover_pct: float,
    premium_budget_pct: float,
    quote_date: date,
    min_dte: int,
    max_dte: int,
    put_strike_pcts: list[float],
    short_put_strike_pcts: list[float],
    call_strike_pcts: list[float],
) -> list[dict[str, Any]]:
    underlying_notional = capital * underlying_notional_pct / 100.0
    underlying_units = underlying_notional / spot
    budget_amount = capital * premium_budget_pct / 100.0
    expiries = sorted({
        row["expiration_date"] for row in rows
        if min_dte <= (row["expiration_date"] - quote_date).days <= max_dte
    })
    candidates: list[dict[str, Any]] = []
    for expiry in expiries:
        dte = (expiry - quote_date).days
        for put_pct in put_strike_pcts:
            put = nearest(rows, expiry, "put", spot * put_pct / 100.0)
            if put is None:
                continue
            put_gap = abs(put["strike"] / (spot * put_pct / 100.0) - 1.0)
            if put_gap > 0.02:
                continue
            for short_put_pct in short_put_strike_pcts:
                if short_put_pct > 0 and short_put_pct >= put_pct:
                    continue
                short_put = None
                short_put_gap = None
                if short_put_pct > 0:
                    short_put = nearest(rows, expiry, "put", spot * short_put_pct / 100.0)
                    if short_put is None:
                        continue
                    short_put_gap = abs(short_put["strike"] / (spot * short_put_pct / 100.0) - 1.0)
                    if short_put_gap > 0.02:
                        continue
                for call_pct in call_strike_pcts:
                    call = nearest(rows, expiry, "call", spot * call_pct / 100.0)
                    if call is None:
                        continue
                    call_gap = abs(call["strike"] / (spot * call_pct / 100.0) - 1.0)
                    if call_gap > 0.02:
                        continue
                    long_put_cost = put["ask"] * underlying_units * put_cover_pct / 100.0
                    short_put_credit = (
                        short_put["bid"] * underlying_units * put_cover_pct / 100.0
                        if short_put
                        else 0.0
                    )
                    short_call_credit = call["bid"] * underlying_units * call_cover_pct / 100.0
                    net_debit = long_put_cost - short_put_credit - short_call_credit
                    candidates.append(
                        {
                            "expiration_date": expiry.isoformat(),
                            "dte": dte,
                            "put_strike_pct": put_pct,
                            "short_put_strike_pct": short_put_pct or None,
                            "call_strike_pct": call_pct,
                            "put_contract": put["contract_symbol"],
                            "short_put_contract": short_put["contract_symbol"] if short_put else None,
                            "call_contract": call["contract_symbol"],
                            "put_strike": put["strike"],
                            "short_put_strike": short_put["strike"] if short_put else None,
                            "call_strike": call["strike"],
                            "put_bid": put["bid"],
                            "put_ask": put["ask"],
                            "short_put_bid": short_put["bid"] if short_put else None,
                            "short_put_ask": short_put["ask"] if short_put else None,
                            "call_bid": call["bid"],
                            "call_ask": call["ask"],
                            "put_volume": put["volume"],
                            "put_open_interest": put["open_interest"],
                            "short_put_volume": short_put["volume"] if short_put else None,
                            "short_put_open_interest": short_put["open_interest"] if short_put else None,
                            "call_volume": call["volume"],
                            "call_open_interest": call["open_interest"],
                            "underlying_notional": underlying_notional,
                            "underlying_units": underlying_units,
                            "long_put_cost": long_put_cost,
                            "short_put_credit": short_put_credit,
                            "short_call_credit": short_call_credit,
                            "net_debit": net_debit,
                            "net_debit_pct_capital": net_debit / capital * 100.0,
                            "premium_budget_amount": budget_amount,
                            "premium_budget_pct_capital": premium_budget_pct,
                            "premium_budget_pass": net_debit <= budget_amount,
                            "budget_gap": net_debit - budget_amount,
                            "estimated_downside_floor_before_premium_pct": put["strike"] / spot * 100.0 - 100.0,
                            "estimated_short_put_exhaustion_pct": (
                                short_put["strike"] / spot * 100.0 - 100.0 if short_put else None
                            ),
                            "estimated_put_spread_width_pct": (
                                (put["strike"] - short_put["strike"]) / spot * 100.0 if short_put else None
                            ),
                            "estimated_upside_cap_pct": call["strike"] / spot * 100.0 - 100.0,
                        }
                    )
    candidates.sort(
        key=lambda row: (
            not row["premium_budget_pass"],
            -row["put_strike_pct"],
            -(row["short_put_strike_pct"] or 0.0),
            row["net_debit_pct_capital"],
            abs(row["dte"] - 30),
            -row["call_strike_pct"],
        )
    )
    return candidates


def write_outputs(report: dict[str, Any], candidates: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if candidates:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(candidates[0]))
            writer.writeheader()
            writer.writerows(candidates)
    else:
        csv_path.write_text("", encoding="utf-8")
    return json_path, csv_path


def pct_grid(start: float, stop: float, step: float) -> list[float]:
    values = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 4))
        current += step
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Search executable option packages against the target premium budget.")
    parser.add_argument("--target-json", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--source", default="cboe_delayed_quotes")
    parser.add_argument("--min-dte", type=int, default=14)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--put-min-pct", type=float, default=90.0)
    parser.add_argument("--put-max-pct", type=float, default=100.0)
    parser.add_argument("--short-put-min-pct", type=float, default=90.0)
    parser.add_argument("--short-put-max-pct", type=float, default=98.0)
    parser.add_argument("--call-min-pct", type=float, default=102.0)
    parser.add_argument("--call-max-pct", type=float, default=120.0)
    parser.add_argument("--strike-step-pct", type=float, default=1.0)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    target_path = Path(args.target_json)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    target = load_target(target_path)
    as_of = date.fromisoformat(args.as_of)
    option_row = next(row for row in target["rows"] if row.get("asset_type") == "option_protected_sleeve")
    symbol = option_row["index_code"]
    capital = float(target["capital"])
    terms = target["defined_loss_terms"]
    premium_budget_pct = float(terms["monthly_premium_budget_pct"])
    target_short_put_pct = float(option_row.get("short_put_strike_pct") or 0.0)

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
        underlying_notional_pct=float(option_row["underlying_notional_pct"]),
        put_cover_pct=float(option_row["long_put_cover_pct"]),
        call_cover_pct=float(option_row["call_cover_pct"]),
        premium_budget_pct=premium_budget_pct,
        quote_date=as_of,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        put_strike_pcts=pct_grid(args.put_min_pct, args.put_max_pct, args.strike_step_pct),
        short_put_strike_pcts=(
            pct_grid(args.short_put_min_pct, args.short_put_max_pct, args.strike_step_pct)
            if target_short_put_pct > 0.0
            else [0.0]
        ),
        call_strike_pcts=pct_grid(args.call_min_pct, args.call_max_pct, args.strike_step_pct),
    )
    top_candidates = candidates[: args.top]
    pass_count = sum(1 for item in candidates if item["premium_budget_pass"])
    report = {
        "strategy": "executable_option_package_search",
        "target_json": str(target_path),
        "as_of": args.as_of,
        "source": args.source,
        "underlying_symbol": symbol,
        "underlying_price": price,
        "capital": capital,
        "premium_budget_pct": premium_budget_pct,
        "premium_budget_amount": capital * premium_budget_pct / 100.0,
        "search_space": {
            "min_dte": args.min_dte,
            "max_dte": args.max_dte,
            "put_min_pct": args.put_min_pct,
            "put_max_pct": args.put_max_pct,
            "short_put_min_pct": args.short_put_min_pct if target_short_put_pct > 0.0 else None,
            "short_put_max_pct": args.short_put_max_pct if target_short_put_pct > 0.0 else None,
            "call_min_pct": args.call_min_pct,
            "call_max_pct": args.call_max_pct,
            "strike_step_pct": args.strike_step_pct,
        },
        "candidate_count": len(candidates),
        "premium_budget_pass_count": pass_count,
        "top_candidates": top_candidates,
    }
    output_prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"executable_option_package_search_{args.as_of.replace('-', '')}"
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, candidates, output_prefix)

    print("Executable option package search")
    print(
        f"  symbol={symbol} source={args.source} as_of={args.as_of} "
        f"candidates={len(candidates)} budget_pass={pass_count}"
    )
    for idx, row in enumerate(top_candidates[:10], 1):
        print(
            f"  {idx:>2}. expiry={row['expiration_date']} dte={row['dte']} "
            f"put={row['put_strike_pct']:.1f}% "
            f"short_put={row['short_put_strike_pct'] or 0:.1f}% "
            f"call={row['call_strike_pct']:.1f}% "
            f"net={row['net_debit_pct_capital']:.2f}% pass={row['premium_budget_pass']} "
            f"contracts={row['put_contract']}/{row['short_put_contract'] or '-'}/{row['call_contract']}"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

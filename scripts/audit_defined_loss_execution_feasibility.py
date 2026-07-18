#!/usr/bin/env python3
"""Audit whether the modeled defined-loss CSI target has execution evidence.

The defined-loss overlay can pass the numerical backtest while still lacking
real option-chain or structured-product terms.  This script makes that boundary
explicit and writes a repeatable audit artifact next to the target files.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

OUT_DIR = ROOT / "data" / "portfolio"
CBOE_STRATEGY_SYMBOLS = ["PPUT", "PUT", "BXM", "BXMD", "CLLZ", "VXTH", "VPD"]
VOL_SYMBOLS = ["^VIX", "VIX3M", "VVIX"]
TARGET_SUPPORT_SYMBOLS = ["QQQ", "SHY", "BTC-USD", "ETH-USD", *VOL_SYMBOLS, *CBOE_STRATEGY_SYMBOLS]
OPTION_CHAIN_KEYWORDS = ("option", "contract", "chain", "quote", "greek")
OPTION_CHAIN_REQUIRED_GROUPS = {
    "underlying": ("underlying", "symbol", "ticker"),
    "expiry": ("expiry", "expiration", "expire", "maturity"),
    "strike": ("strike",),
    "right": ("option_type", "call_put", "right", "cp_flag", "put_call"),
    "price": ("bid", "ask", "mark", "mid", "last", "close"),
}


def default_target_path(as_of: date) -> Path:
    return OUT_DIR / f"csi_defined_loss_overlay_targets_{as_of.isoformat().replace('-', '')}.json"


def table_names(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        return [row[0] for row in cur.fetchall()]


def table_columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        return [row[0] for row in cur.fetchall()]


def has_column_group(columns: list[str], needles: tuple[str, ...]) -> bool:
    lowered = [column.lower() for column in columns]
    return any(any(needle in column for needle in needles) for column in lowered)


def inspect_option_tables(conn, tables: list[str]) -> list[dict[str, Any]]:
    candidates = []
    for table in tables:
        low = table.lower()
        if not any(keyword in low for keyword in OPTION_CHAIN_KEYWORDS):
            continue
        columns = table_columns(conn, table)
        groups = {
            name: has_column_group(columns, needles)
            for name, needles in OPTION_CHAIN_REQUIRED_GROUPS.items()
        }
        candidates.append(
            {
                "table": table,
                "columns": columns,
                "required_groups": groups,
                "looks_like_listed_option_chain": all(groups.values()),
            }
        )
    return candidates


def external_asset_coverage(conn, symbols: list[str], as_of: date) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(symbols))
    sql = f"""
        SELECT symbol, COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM external_asset_daily
        WHERE symbol IN ({placeholders})
          AND trade_date <= %s
        GROUP BY symbol
        ORDER BY symbol
    """
    with conn.cursor() as cur:
        cur.execute(sql, [*symbols, as_of])
        rows = cur.fetchall()
    found = {
        row[0]: {
            "symbol": row[0],
            "rows": int(row[1]),
            "min_date": row[2].isoformat() if row[2] else None,
            "max_date": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    }
    return [
        found.get(
            symbol,
            {"symbol": symbol, "rows": 0, "min_date": None, "max_date": None},
        )
        for symbol in symbols
    ]


def cboe_vix_coverage(conn, as_of: date) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'cboe_vix_daily'")
        if not cur.fetchone():
            return None
        cur.execute(
            """
            SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM cboe_vix_daily
            WHERE trade_date <= %s
            """,
            (as_of,),
        )
        row = cur.fetchone()
    return {
        "table": "cboe_vix_daily",
        "rows": int(row[0] or 0),
        "min_date": row[1].isoformat() if row[1] else None,
        "max_date": row[2].isoformat() if row[2] else None,
    }


def latest_external_price(conn, symbol: str, as_of: date) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, COALESCE(adj_close, close)
            FROM external_asset_daily
            WHERE symbol=%s AND trade_date <= %s
              AND COALESCE(adj_close, close) IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol, as_of),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"trade_date": row[0].isoformat(), "price": float(row[1])}


def latest_option_quote_date(conn, symbol: str, as_of: date) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(quote_date)
            FROM us_option_chain_snapshot
            WHERE underlying_symbol=%s AND quote_date <= %s
            """,
            (symbol, as_of),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def nearest_option_contract(
    conn,
    symbol: str,
    quote_date: date,
    option_type: str,
    target_strike: float,
    min_dte: int = 14,
    max_dte: int = 65,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, expiration_date, strike, contract_symbol, bid, ask, mark,
                   last_price, implied_volatility, volume, open_interest
            FROM us_option_chain_snapshot
            WHERE underlying_symbol=%s
              AND quote_date=%s
              AND option_type=%s
              AND DATEDIFF(expiration_date, quote_date) BETWEEN %s AND %s
            ORDER BY ABS(strike - %s),
                     CASE WHEN bid > 0 AND ask > 0 THEN 0 ELSE 1 END,
                     expiration_date,
                     CASE source WHEN 'cboe_delayed_quotes' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (symbol, quote_date, option_type, min_dte, max_dte, target_strike),
        )
        row = cur.fetchone()
        if row:
            return {
                "source": row[0],
                "expiration_date": row[1].isoformat(),
                "dte": (row[1] - quote_date).days,
                "strike": float(row[2]),
                "contract_symbol": row[3],
                "bid": float(row[4]) if row[4] is not None else None,
                "ask": float(row[5]) if row[5] is not None else None,
                "mark": float(row[6]) if row[6] is not None else None,
                "last_price": float(row[7]) if row[7] is not None else None,
                "implied_volatility": float(row[8]) if row[8] is not None else None,
                "volume": int(row[9]) if row[9] is not None else None,
                "open_interest": int(row[10]) if row[10] is not None else None,
            }
        cur.execute(
            """
            SELECT source, expiration_date, strike, contract_symbol, bid, ask, mark,
                   last_price, implied_volatility, volume, open_interest
            FROM us_option_chain_snapshot
            WHERE underlying_symbol=%s
              AND quote_date=%s
              AND option_type=%s
            ORDER BY ABS(strike - %s),
                     CASE WHEN bid > 0 AND ask > 0 THEN 0 ELSE 1 END,
                     expiration_date,
                     CASE source WHEN 'cboe_delayed_quotes' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (symbol, quote_date, option_type, target_strike),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "source": row[0],
        "expiration_date": row[1].isoformat(),
        "dte": (row[1] - quote_date).days,
        "strike": float(row[2]),
        "contract_symbol": row[3],
        "bid": float(row[4]) if row[4] is not None else None,
        "ask": float(row[5]) if row[5] is not None else None,
        "mark": float(row[6]) if row[6] is not None else None,
        "last_price": float(row[7]) if row[7] is not None else None,
        "implied_volatility": float(row[8]) if row[8] is not None else None,
        "volume": int(row[9]) if row[9] is not None else None,
        "open_interest": int(row[10]) if row[10] is not None else None,
    }


def target_option_chain_evidence(conn, target_info: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    evidence = []
    capital = float(target_info.get("capital") or 0.0)
    terms = target_info.get("defined_loss_terms") or {}
    premium_budget_pct = float(terms.get("monthly_premium_budget_pct") or 0.0)
    premium_budget_amount = capital * premium_budget_pct / 100.0
    for row in target_info.get("option_rows", []):
        symbol = row.get("index_code")
        if not symbol:
            continue
        price = latest_external_price(conn, symbol, as_of)
        quote_date = latest_option_quote_date(conn, symbol, as_of)
        item: dict[str, Any] = {
            "underlying_symbol": symbol,
            "underlying_price": price,
            "option_quote_date": quote_date.isoformat() if quote_date else None,
            "checks": [],
            "estimated_option_package": None,
        }
        if not price or not quote_date:
            item["status"] = "missing_option_chain_or_underlying_price"
            evidence.append(item)
            continue
        leg_specs = [
            {
                "role": "long_put",
                "option_type": "put",
                "strike_field": "long_put_strike_pct",
                "cover_field": "long_put_cover_pct",
            },
            {
                "role": "short_put",
                "option_type": "put",
                "strike_field": "short_put_strike_pct",
                "cover_field": "long_put_cover_pct",
            },
            {
                "role": "short_call",
                "option_type": "call",
                "strike_field": "call_strike_pct",
                "cover_field": "call_cover_pct",
            },
        ]
        for spec in leg_specs:
            option_type = spec["option_type"]
            strike_pct = row.get(spec["strike_field"])
            cover_pct = row.get(spec["cover_field"])
            if strike_pct is None or float(strike_pct) <= 0 or float(cover_pct or 0.0) <= 0:
                continue
            target_strike = float(price["price"]) * float(strike_pct) / 100.0
            nearest = nearest_option_contract(conn, symbol, quote_date, option_type, target_strike)
            if nearest:
                rel_gap = abs(nearest["strike"] / target_strike - 1.0) if target_strike else None
                has_mark = nearest.get("mark") is not None or nearest.get("last_price") is not None
                has_executable_bid_ask = (
                    nearest.get("bid") is not None
                    and nearest.get("ask") is not None
                    and float(nearest.get("bid") or 0.0) > 0.0
                    and float(nearest.get("ask") or 0.0) > 0.0
                )
                item["checks"].append(
                    {
                        "option_type": option_type,
                        "role": spec["role"],
                        "target_strike_pct": float(strike_pct),
                        "target_strike": target_strike,
                        "nearest_contract": nearest,
                        "relative_strike_gap": rel_gap,
                        "contract_available": True,
                        "mark_or_last_available": has_mark,
                        "executable_bid_ask_available": has_executable_bid_ask,
                        "within_2pct_strike": rel_gap is not None and rel_gap <= 0.02,
                    }
                )
            else:
                item["checks"].append(
                    {
                        "option_type": option_type,
                        "role": spec["role"],
                        "target_strike_pct": float(strike_pct),
                        "target_strike": target_strike,
                        "nearest_contract": None,
                        "relative_strike_gap": None,
                        "contract_available": False,
                        "mark_or_last_available": False,
                        "executable_bid_ask_available": False,
                        "within_2pct_strike": False,
                    }
                )
        contracts_available = item["checks"] and all(
            check["contract_available"] and check["within_2pct_strike"] for check in item["checks"]
        )
        executable_bid_ask_available = contracts_available and all(
            check["executable_bid_ask_available"] for check in item["checks"]
        )
        if executable_bid_ask_available:
            item["status"] = "target_option_executable_bid_ask_available"
        elif contracts_available:
            item["status"] = "target_option_contracts_available_bid_ask_incomplete"
        else:
            item["status"] = "target_option_contracts_incomplete"
        if executable_bid_ask_available and price and capital > 0:
            underlying_notional = capital * float(row.get("underlying_notional_pct") or 0.0) / 100.0
            underlying_units = underlying_notional / float(price["price"]) if float(price["price"]) > 0 else 0.0
            long_put_cost = 0.0
            short_put_credit = 0.0
            short_call_credit = 0.0
            legs = []
            for check in item["checks"]:
                contract = check["nearest_contract"]
                role = check["role"]
                if role == "long_put":
                    cover = float(row.get("long_put_cover_pct") or 0.0) / 100.0
                    notional_units = underlying_units * cover
                    cash_flow = -float(contract["ask"]) * notional_units
                    long_put_cost += -cash_flow
                    side = "buy"
                elif role == "short_put":
                    cover = float(row.get("long_put_cover_pct") or 0.0) / 100.0
                    notional_units = underlying_units * cover
                    cash_flow = float(contract["bid"]) * notional_units
                    short_put_credit += cash_flow
                    side = "sell"
                elif role == "short_call":
                    cover = float(row.get("call_cover_pct") or 0.0) / 100.0
                    notional_units = underlying_units * cover
                    cash_flow = float(contract["bid"]) * notional_units
                    short_call_credit += cash_flow
                    side = "sell"
                else:
                    continue
                legs.append(
                    {
                        "side": side,
                        "option_type": check["option_type"],
                        "role": role,
                        "contract_symbol": contract["contract_symbol"],
                        "units": notional_units,
                        "contracts_100_share": notional_units / 100.0,
                        "cash_flow": cash_flow,
                    }
                )
            net_debit = long_put_cost - short_put_credit - short_call_credit
            item["estimated_option_package"] = {
                "underlying_notional": underlying_notional,
                "underlying_units": underlying_units,
                "long_put_cost": long_put_cost,
                "short_put_credit": short_put_credit,
                "short_call_credit": short_call_credit,
                "net_debit": net_debit,
                "net_debit_pct_capital": net_debit / capital * 100.0,
                "premium_budget_amount": premium_budget_amount,
                "premium_budget_pct_capital": premium_budget_pct,
                "premium_budget_pass": net_debit <= premium_budget_amount,
                "premium_budget_gap": net_debit - premium_budget_amount,
                "legs": legs,
                "pricing_rule": "long legs use ask; short legs use bid; contract multiplier cancels because units are underlying shares",
            }
        evidence.append(item)
    return evidence


def load_target(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def target_checks(target: dict[str, Any] | None) -> dict[str, Any]:
    if target is None:
        return {
            "present": False,
            "rule_name": None,
            "model_status": None,
            "has_option_protected_sleeve": False,
            "has_defined_loss_overlay": False,
            "option_rows": [],
            "overlay_rows": [],
            "defined_loss_terms": {},
            "capital": None,
        }
    rows = target.get("rows", [])
    option_rows = [row for row in rows if row.get("asset_type") == "option_protected_sleeve"]
    overlay_rows = [row for row in rows if row.get("asset_type") == "defined_loss_overlay"]
    return {
        "present": True,
        "rule_name": target.get("rule_name"),
        "model_status": target.get("model_status"),
        "as_of": target.get("as_of"),
        "snapshot": target.get("snapshot"),
        "has_option_protected_sleeve": bool(option_rows),
        "has_defined_loss_overlay": bool(overlay_rows),
        "option_rows": option_rows,
        "overlay_rows": overlay_rows,
        "defined_loss_terms": target.get("defined_loss_terms", {}),
        "component_budgets": target.get("component_budgets", {}),
        "execution_validation": target.get("execution_validation", {}),
        "capital": target.get("capital"),
    }


def build_audit(target_path: Path, as_of: date) -> dict[str, Any]:
    target = load_target(target_path)
    conn = get_connection()
    try:
        tables = table_names(conn)
        option_candidates = inspect_option_tables(conn, tables)
        coverage = external_asset_coverage(conn, TARGET_SUPPORT_SYMBOLS, as_of)
        vix_table = cboe_vix_coverage(conn, as_of)
        target_info = target_checks(target)
        target_option_evidence = target_option_chain_evidence(conn, target_info, as_of)
    finally:
        conn.close()

    listed_option_tables = [
        item for item in option_candidates if item["looks_like_listed_option_chain"]
    ]
    cboe_indices = [
        item for item in coverage
        if item["symbol"] in CBOE_STRATEGY_SYMBOLS and item["rows"] > 0
    ]
    support_latest = {
        item["symbol"]: item["max_date"]
        for item in coverage
        if item["rows"] > 0
    }
    execution_validation = target_info.get("execution_validation") or {}
    option_terms_validated = bool(execution_validation.get("option_terms_validated"))
    total_floor_validated = bool(execution_validation.get("total_floor_validated"))
    friction_validated = bool(execution_validation.get("friction_validated"))

    blockers = []
    if not target_info["present"]:
        blockers.append(f"target file is missing: {target_path}")
    if not target_info["has_option_protected_sleeve"]:
        blockers.append("target file has no option_protected_sleeve row")
    if not target_info["has_defined_loss_overlay"]:
        blockers.append("target file has no defined_loss_overlay row")
    if not listed_option_tables and not option_terms_validated:
        blockers.append("no listed option-chain table with underlying, expiry, strike, right, and quote columns")
    incomplete_option_evidence = [
        item for item in target_option_evidence
        if item.get("status") == "target_option_contracts_incomplete"
    ]
    bid_ask_incomplete_option_evidence = [
        item for item in target_option_evidence
        if item.get("status") == "target_option_contracts_available_bid_ask_incomplete"
    ]
    if incomplete_option_evidence and not option_terms_validated:
        blockers.append("target option sleeve cannot be mapped to quoted near-strike contracts")
    if bid_ask_incomplete_option_evidence and not option_terms_validated:
        blockers.append("target option sleeve lacks non-zero bid/ask on the mapped near-strike contracts")
    cost_exceeded_option_evidence = [
        item for item in target_option_evidence
        if (item.get("estimated_option_package") or {}).get("premium_budget_pass") is False
    ]
    if cost_exceeded_option_evidence and not option_terms_validated:
        blockers.append("current quoted option package net debit exceeds the modeled monthly premium budget")
    if not total_floor_validated:
        blockers.append("no broker or structured-product quote proving the -1.0% monthly total-portfolio loss floor")
    if not friction_validated:
        blockers.append("no skew, fill/slippage, margin, tax, or intramonth mark-to-market validation for the overlay")

    execution_validated = (
        target_info["present"]
        and target_info["has_option_protected_sleeve"]
        and target_info["has_defined_loss_overlay"]
        and (bool(listed_option_tables) or option_terms_validated)
        and not blockers
    )
    status = "execution_validated" if execution_validated else "not_execution_validated"
    return {
        "strategy": "scorecard_csi_defined_loss_overlay_execution_audit",
        "as_of": as_of.isoformat(),
        "target_json": str(target_path),
        "status": status,
        "execution_validated": execution_validated,
        "target": target_info,
        "available_evidence": {
            "external_asset_daily_symbols": coverage,
            "cboe_strategy_indices_available": bool(cboe_indices),
            "cboe_strategy_indices_count": len(cboe_indices),
            "cboe_vix_daily": vix_table,
            "support_latest_dates": support_latest,
            "target_option_chain_evidence": target_option_evidence,
        },
        "schema_audit": {
            "relevant_tables": [
                table for table in tables
                if any(keyword in table.lower() for keyword in (*OPTION_CHAIN_KEYWORDS, "cboe", "external_asset"))
            ],
            "option_table_candidates": option_candidates,
            "listed_option_chain_available": bool(listed_option_tables),
            "listed_option_chain_tables": [item["table"] for item in listed_option_tables],
            "required_option_chain_fields": OPTION_CHAIN_REQUIRED_GROUPS,
        },
        "blockers": blockers,
        "next_required_data": {
            "listed_option_chain_minimum_fields": [
                "underlying",
                "quote_time_or_trade_date",
                "expiry",
                "strike",
                "option_type",
                "bid",
                "ask",
                "mark_or_mid",
                "implied_volatility",
                "delta",
                "open_interest",
                "volume",
                "source",
            ],
            "execution_terms_needed": [
                "monthly reset date rule",
                "exact put/call strikes and expiries for the QQQ sleeve",
                "total portfolio floor provider or replication recipe",
                "premium debit or financing schedule",
                "assignment, margin, tax, and slippage assumptions",
            ],
        },
    }


def write_audit(report: dict[str, Any], output: Path | None) -> Path:
    if output is None:
        stamp = str(report["as_of"]).replace("-", "")
        output = OUT_DIR / f"csi_defined_loss_execution_audit_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit execution evidence for the modeled defined-loss CSI target.")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Audit date, YYYY-MM-DD")
    parser.add_argument("--target-json", help="Target JSON to audit. Default: data/portfolio/csi_defined_loss_overlay_targets_YYYYMMDD.json")
    parser.add_argument("--output-json", help="Audit JSON output path.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when execution is not validated.")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    target_path = Path(args.target_json) if args.target_json else default_target_path(as_of)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    output = Path(args.output_json) if args.output_json else None
    if output is not None and not output.is_absolute():
        output = ROOT / output

    report = build_audit(target_path, as_of)
    out_path = write_audit(report, output)

    target = report["target"]
    terms = target.get("defined_loss_terms") or {}
    print("Defined-loss execution feasibility audit")
    print(f"  status={report['status']} as_of={report['as_of']}")
    print(f"  target={report['target_json']}")
    print(f"  rule={target.get('rule_name')} model_status={target.get('model_status')}")
    if terms:
        print(
            "  modeled_terms="
            f"floor={float(terms.get('monthly_loss_floor_pct', 0.0)):.2f}% "
            f"premium={float(terms.get('monthly_premium_budget_pct', 0.0)):.2f}%/month "
            f"upside_haircut={float(terms.get('upside_haircut_pct', 0.0)):.1f}%"
        )
    print(
        "  evidence="
        f"listed_option_chain={report['schema_audit']['listed_option_chain_available']} "
        f"cboe_strategy_indices={report['available_evidence']['cboe_strategy_indices_count']} "
        f"cboe_vix_daily_rows={(report['available_evidence']['cboe_vix_daily'] or {}).get('rows', 0)}"
    )
    for item in report["available_evidence"]["target_option_chain_evidence"]:
        estimate = item.get("estimated_option_package") or {}
        premium_text = (
            f" net_debit={float(estimate['net_debit']):,.0f} "
            f"({float(estimate['net_debit_pct_capital']):.2f}% capital) "
            f"budget_pass={estimate.get('premium_budget_pass')}"
            if estimate
            else ""
        )
        print(
            f"  target_option_chain {item['underlying_symbol']}: "
            f"status={item['status']} quote_date={item.get('option_quote_date')} "
            f"checks={len(item.get('checks', []))}{premium_text}"
        )
    for blocker in report["blockers"]:
        print(f"  blocker: {blocker}")
    print(f"Wrote {out_path}")

    if args.strict and not report["execution_validated"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

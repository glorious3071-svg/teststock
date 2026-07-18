#!/usr/bin/env python3
"""Generate target rows for the strict-pass pre-option regime strategy."""

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
from scripts.audit_defined_loss_execution_feasibility import latest_external_price
from scripts.backtest_scorecard_csi_crypto_satellite_mix import CORE_RULE_BY_NAME, SATELLITE_RULE_BY_NAME
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME
from scripts.backtest_scorecard_csi_dynamic_defense import month_end_shift
from scripts.generate_csi_defined_loss_overlay_targets import (
    EXECUTABLE_SPREAD_RULE_NAME,
    OPTION_RULE_BY_NAME,
    cppi_exposure,
    rule_by_name as defined_loss_rule_by_name,
    satellite_rows,
)
from scripts.generate_csi_phase_ensemble_targets import build_targets as build_phase_targets, previous_month_end
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape

OUT_DIR = ROOT / "data" / "portfolio"
BACKTEST_DIR = ROOT / "data" / "backtests"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_RULE_NAME = "best_balance"
PRE_OPTION_REPORT = BACKTEST_DIR / "scorecard_csi_pre_option_regime_defense_switch_50_to_300_misszero_report.json"
CORE_RULE_NAME = "core_xbcppi_sub12_spread95_call108"
SATELLITE_RULE_NAME = "sat_crypto_cppi"
US_OPTION_SOURCE = "cboe_delayed_quotes"


def load_validated_rule(selector: str) -> dict[str, Any]:
    payload = json.loads(PRE_OPTION_REPORT.read_text(encoding="utf-8"))
    passing = [item for item in payload["results"] if item["summary"]["pass_count"] == item["summary"]["count"]]
    if not passing:
        raise RuntimeError(f"No strict-pass pre-option regime rule found in {PRE_OPTION_REPORT}")
    if selector == "best_drawdown":
        item = max(
            passing,
            key=lambda row: (
                row["summary"]["worst_max_drawdown"],
                row["summary"]["min_final_capital_wan"],
            ),
        )
    elif selector == "best_balance":
        item = max(
            passing,
            key=lambda row: (
                row["summary"]["min_final_capital_wan"],
                row["summary"]["worst_max_drawdown"],
            ),
        )
    else:
        matches = [item for item in passing if item["rule"]["name"] == selector]
        if not matches:
            raise ValueError(f"Unknown or non-passing pre-option rule selector: {selector}")
        item = matches[0]
    return item


def cn_option_package_rows(capital: float, as_of: date, holding_end: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = get_connection()
    try:
        package = load_package_shape()
        pricer = HistoricalCnPackagePricer(conn, package, "switch_50_to_300", 10, 5.0, "zero")
        selected = pricer.select_quote_legs_for_period(as_of, holding_end)
        if selected is None:
            return [], {
                "status": "missing_current_option_package",
                "as_of": as_of.isoformat(),
                "holding_end": holding_end.isoformat(),
            }
        underlying, quote_date, spot, selection = selected
        long_put, short_call = selection
    finally:
        conn.close()

    target_notional = capital * package.long_put_notional_pct
    contracts = max(1, math.ceil(target_notional / max(long_put.strike * long_put.per_unit, 1.0)))
    put_units = contracts * long_put.per_unit
    call_units = contracts * short_call.per_unit
    long_cost = long_put.close * put_units
    short_credit = short_call.close * call_units
    net_debit = long_cost - short_credit
    stress_up_pct = 25.0
    stressed_spot = spot * (1.0 + stress_up_pct / 100.0)
    short_call_loss = max(stressed_spot - short_call.strike, 0.0) * call_units
    margin_proxy = max(short_call_loss, spot * call_units * 0.12)
    rows = [
        {
            "rank": None,
            "asset_type": "cn_etf_option_package_leg",
            "index_code": long_put.ts_code,
            "index_name": f"buy P {underlying.opt_code} {long_put.strike}",
            "target_weight_pct": long_cost / capital * 100.0,
            "target_amount": long_cost,
            "source_component": "pre_option_regime_cn_option_package",
            "option_side": "buy",
            "call_put": "P",
            "strike": long_put.strike,
            "close": long_put.close,
            "per_unit": long_put.per_unit,
            "contract_count": contracts,
            "maturity_date": long_put.maturity_date.isoformat(),
            "underlying_option_code": underlying.opt_code,
            "quote_date": quote_date.isoformat(),
            "underlying_spot": spot,
            "protected_notional": long_put.strike * put_units,
            "protected_weight_pct": long_put.strike * put_units / capital * 100.0,
            "execution_note": "Strict-pass target package leg; use broker executable quotes before trading.",
        },
        {
            "rank": None,
            "asset_type": "cn_etf_option_package_leg",
            "index_code": short_call.ts_code,
            "index_name": f"sell C {underlying.opt_code} {short_call.strike}",
            "target_weight_pct": -short_credit / capital * 100.0,
            "target_amount": -short_credit,
            "source_component": "pre_option_regime_cn_option_package",
            "option_side": "sell",
            "call_put": "C",
            "strike": short_call.strike,
            "close": short_call.close,
            "per_unit": short_call.per_unit,
            "contract_count": -contracts,
            "maturity_date": short_call.maturity_date.isoformat(),
            "underlying_option_code": underlying.opt_code,
            "quote_date": quote_date.isoformat(),
            "underlying_spot": spot,
            "covered_notional": short_call.strike * call_units,
            "covered_weight_pct": short_call.strike * call_units / capital * 100.0,
            "stress_up_pct_for_short_call": stress_up_pct,
            "stress_short_call_loss_amount": short_call_loss,
            "margin_proxy_amount": margin_proxy,
            "margin_proxy_pct_capital": margin_proxy / capital * 100.0,
            "execution_note": "Short call leg requires broker margin, liquidity, and assignment audit.",
        },
    ]
    metadata = {
        "status": "selected",
        "underlying_option_code": underlying.opt_code,
        "underlying_fund_code": underlying.fund_code,
        "quote_date": quote_date.isoformat(),
        "holding_end": holding_end.isoformat(),
        "spot": spot,
        "contracts": contracts,
        "net_debit": net_debit,
        "net_debit_pct": net_debit / capital * 100.0,
        "package_shape_source": package.source_json,
        "long_put_notional_pct": package.long_put_notional_pct * 100.0,
        "short_call_notional_pct": package.short_call_notional_pct * 100.0,
    }
    return rows, metadata


def load_us_option_rows(conn, symbol: str, quote_date: date, source: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT expiration_date, option_type, strike, contract_symbol, bid, ask,
                   mark, last_price, volume, open_interest
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
                "volume": int(row[8]) if row[8] is not None else None,
                "open_interest": int(row[9]) if row[9] is not None else None,
            }
            for row in cur.fetchall()
        ]


def latest_us_option_quote_date(conn, symbol: str, as_of: date, source: str) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(quote_date)
            FROM us_option_chain_snapshot
            WHERE underlying_symbol=%s
              AND quote_date <= %s
              AND source=%s
            """,
            (symbol, as_of, source),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def nearest_option(
    rows: list[dict[str, Any]],
    expiry: date,
    option_type: str,
    target_strike: float,
) -> dict[str, Any] | None:
    candidates = [
        row for row in rows
        if row["expiration_date"] == expiry and row["option_type"] == option_type
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(row["strike"] - target_strike))


def qqq_option_package_rows(
    option_rule: Any,
    capital: float,
    option_budget_pct: float,
    as_of: date,
    holding_end: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = get_connection()
    try:
        price = latest_external_price(conn, option_rule.underlying, as_of)
        if not price:
            return [], {"status": "missing_underlying_price", "symbol": option_rule.underlying}
        quote_date = latest_us_option_quote_date(conn, option_rule.underlying, as_of, US_OPTION_SOURCE)
        if quote_date is None:
            return [], {
                "status": "missing_option_quote_date",
                "symbol": option_rule.underlying,
                "as_of": as_of.isoformat(),
                "source": US_OPTION_SOURCE,
            }
        rows = load_us_option_rows(conn, option_rule.underlying, quote_date, US_OPTION_SOURCE)
    finally:
        conn.close()
    if not rows:
        return [], {
            "status": "missing_option_chain",
            "symbol": option_rule.underlying,
            "quote_date": quote_date.isoformat(),
            "source": US_OPTION_SOURCE,
        }

    expiries = sorted({
        row["expiration_date"]
        for row in rows
        if 5 <= (row["expiration_date"] - quote_date).days <= 75
    })
    if not expiries:
        return [], {"status": "missing_expiry", "symbol": option_rule.underlying, "quote_date": quote_date.isoformat()}
    expiry = min(expiries, key=lambda item: abs((item - holding_end).days))
    spot = float(price["price"])
    underlying_notional = capital * option_budget_pct / 100.0 * option_rule.leverage
    underlying_units = underlying_notional / spot
    contract_unit = 100.0

    long_put = nearest_option(rows, expiry, "put", spot * option_rule.put_strike_pct)
    short_put = (
        nearest_option(rows, expiry, "put", spot * option_rule.short_put_strike_pct)
        if option_rule.short_put_strike_pct > 0
        else None
    )
    short_call = (
        nearest_option(rows, expiry, "call", spot * option_rule.call_strike_pct)
        if option_rule.call_strike_pct > 0
        else None
    )
    if long_put is None or (option_rule.short_put_strike_pct > 0 and short_put is None) or (
        option_rule.call_strike_pct > 0 and short_call is None
    ):
        return [], {
            "status": "missing_required_option_leg",
            "symbol": option_rule.underlying,
            "quote_date": quote_date.isoformat(),
            "expiry": expiry.isoformat(),
        }

    put_contracts = max(1, math.ceil(underlying_units * option_rule.put_cover / contract_unit))
    short_put_contracts = put_contracts if short_put else 0
    call_contracts = max(1, math.ceil(underlying_units * option_rule.call_cover / contract_unit)) if short_call else 0
    long_put_cost = put_contracts * contract_unit * long_put["ask"]
    short_put_credit = short_put_contracts * contract_unit * short_put["bid"] if short_put else 0.0
    short_call_credit = call_contracts * contract_unit * short_call["bid"] if short_call else 0.0
    net_option_cost = long_put_cost - short_put_credit - short_call_credit
    option_sleeve_financing_pct = option_budget_pct - (underlying_notional / capital * 100.0) - (net_option_cost / capital * 100.0)

    out = [
        {
            "rank": None,
            "asset_type": "external_etf_underlying",
            "index_code": option_rule.underlying,
            "index_name": f"{option_rule.underlying} underlying for listed option sleeve",
            "target_weight_pct": underlying_notional / capital * 100.0,
            "target_amount": underlying_notional,
            "source_component": option_rule.name,
            "underlying_price_date": price["trade_date"],
            "underlying_price": spot,
            "underlying_units": underlying_units,
            "execution_note": "Underlying notional for the QQQ listed option sleeve.",
        },
        {
            "rank": None,
            "asset_type": "us_option_package_leg",
            "index_code": long_put["contract_symbol"],
            "index_name": f"buy P {option_rule.underlying} {long_put['strike']}",
            "target_weight_pct": long_put_cost / capital * 100.0,
            "target_amount": long_put_cost,
            "source_component": option_rule.name,
            "option_side": "buy",
            "call_put": "P",
            "strike": long_put["strike"],
            "bid": long_put["bid"],
            "ask": long_put["ask"],
            "contract_count": put_contracts,
            "contract_unit": contract_unit,
            "expiration_date": expiry.isoformat(),
            "quote_date": quote_date.isoformat(),
            "source": US_OPTION_SOURCE,
            "volume": long_put["volume"],
            "open_interest": long_put["open_interest"],
            "execution_note": "Mapped from synthetic QQQ long put target; verify live bid/ask before trading.",
        },
    ]
    if short_put:
        out.append(
            {
                "rank": None,
                "asset_type": "us_option_package_leg",
                "index_code": short_put["contract_symbol"],
                "index_name": f"sell P {option_rule.underlying} {short_put['strike']}",
                "target_weight_pct": -short_put_credit / capital * 100.0,
                "target_amount": -short_put_credit,
                "source_component": option_rule.name,
                "option_side": "sell",
                "call_put": "P",
                "strike": short_put["strike"],
                "bid": short_put["bid"],
                "ask": short_put["ask"],
                "contract_count": -short_put_contracts,
                "contract_unit": contract_unit,
                "expiration_date": expiry.isoformat(),
                "quote_date": quote_date.isoformat(),
                "source": US_OPTION_SOURCE,
                "volume": short_put["volume"],
                "open_interest": short_put["open_interest"],
                "execution_note": "Short put spread leg; requires margin and assignment audit.",
            }
        )
    if short_call:
        out.append(
            {
                "rank": None,
                "asset_type": "us_option_package_leg",
                "index_code": short_call["contract_symbol"],
                "index_name": f"sell C {option_rule.underlying} {short_call['strike']}",
                "target_weight_pct": -short_call_credit / capital * 100.0,
                "target_amount": -short_call_credit,
                "source_component": option_rule.name,
                "option_side": "sell",
                "call_put": "C",
                "strike": short_call["strike"],
                "bid": short_call["bid"],
                "ask": short_call["ask"],
                "contract_count": -call_contracts,
                "contract_unit": contract_unit,
                "expiration_date": expiry.isoformat(),
                "quote_date": quote_date.isoformat(),
                "source": US_OPTION_SOURCE,
                "volume": short_call["volume"],
                "open_interest": short_call["open_interest"],
                "execution_note": "Short call overwrite leg; requires margin and assignment audit.",
            }
        )
    if abs(option_sleeve_financing_pct) > 1e-9:
        out.append(
            {
                "rank": None,
                "asset_type": "option_sleeve_financing",
                "index_code": "OPTION_SLEEVE_FINANCING",
                "index_name": "QQQ listed option sleeve financing",
                "target_weight_pct": option_sleeve_financing_pct,
                "target_amount": capital * option_sleeve_financing_pct / 100.0,
                "source_component": option_rule.name,
                "execution_note": "Residual financing/cash required to reconcile option sleeve budget to listed legs.",
            }
        )
    metadata = {
        "status": "selected",
        "source": US_OPTION_SOURCE,
        "symbol": option_rule.underlying,
        "quote_date": quote_date.isoformat(),
        "underlying_price_date": price["trade_date"],
        "expiration_date": expiry.isoformat(),
        "holding_end": holding_end.isoformat(),
        "spot": spot,
        "underlying_notional_pct": underlying_notional / capital * 100.0,
        "long_put_contract": long_put["contract_symbol"],
        "short_put_contract": short_put["contract_symbol"] if short_put else None,
        "short_call_contract": short_call["contract_symbol"] if short_call else None,
        "long_put_contracts": put_contracts,
        "short_put_contracts": short_put_contracts,
        "short_call_contracts": call_contracts,
        "net_option_cost": net_option_cost,
        "net_option_cost_pct": net_option_cost / capital * 100.0,
        "option_sleeve_financing_pct": option_sleeve_financing_pct,
    }
    return out, metadata


def build_targets(
    rule_selector: str,
    as_of: date,
    snapshot: date,
    capital: float,
    top_per_sleeve: int,
    portfolio_drawdown_pct: float,
    core_drawdown_pct: float,
    satellite_drawdown_pct: float,
) -> dict[str, Any]:
    validated = load_validated_rule(rule_selector)
    pre_rule = validated["rule"]
    core_rule = CORE_RULE_BY_NAME[CORE_RULE_NAME]
    satellite_rule = SATELLITE_RULE_BY_NAME[SATELLITE_RULE_NAME]
    blend_rule = BLEND_RULE_BY_NAME[core_rule.base_blend_name]
    defined_loss_rule = defined_loss_rule_by_name(EXECUTABLE_SPREAD_RULE_NAME)

    core_exposure = cppi_exposure(
        core_rule.floor_pct,
        core_rule.multiplier,
        core_rule.max_exposure,
        core_drawdown_pct,
    )
    core_weight = 0.95
    satellite_weight = 0.08
    core_active_pct = core_weight * 100.0 * core_exposure
    csi_budget_pct = core_active_pct * blend_rule.csi_weight
    qqq_option_sleeve_budget_pct = core_active_pct * blend_rule.option_weight
    core_safe_pct = core_weight * 100.0 * (1.0 - core_exposure)
    satellite_budget_pct = satellite_weight * 100.0
    structural_financing_pct = min(0.0, 100.0 - core_weight * 100.0 - satellite_budget_pct)

    phase_report = build_phase_targets(
        rule_name=blend_rule.phase_rule_name,
        as_of=as_of,
        snapshot=snapshot,
        capital=capital,
        top_per_sleeve=top_per_sleeve,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
    )
    rows: list[dict[str, Any]] = []
    for row in phase_report["rows"]:
        item = dict(row)
        item["target_weight_pct"] = float(item["target_weight_pct"]) * csi_budget_pct / 100.0
        item["target_amount"] = capital * item["target_weight_pct"] / 100.0
        item["source_component"] = "phase_ensemble_csi_core"
        rows.append(item)

    option_rule = OPTION_RULE_BY_NAME[defined_loss_rule.option_rule_name]
    qqq_rows, qqq_option_meta = qqq_option_package_rows(
        option_rule,
        capital,
        qqq_option_sleeve_budget_pct,
        as_of,
        month_end_shift(as_of, 1),
    )
    if qqq_option_meta.get("status") != "selected":
        raise RuntimeError(f"QQQ option package target generation failed: {qqq_option_meta}")
    rows.extend(qqq_rows)
    sat_rows, sat_meta = satellite_rows(defined_loss_rule, as_of, capital, satellite_budget_pct, satellite_drawdown_pct)
    rows.extend(sat_rows)
    option_package_rows, option_package_meta = cn_option_package_rows(capital, as_of, month_end_shift(as_of, 1))
    if option_package_meta.get("status") != "selected":
        raise RuntimeError(f"CN option package target generation failed: {option_package_meta}")
    rows.extend(option_package_rows)

    if core_safe_pct > 1e-9:
        rows.append(
            {
                "rank": None,
                "asset_type": "core_safe_asset",
                "index_code": "SHY",
                "index_name": "SHY",
                "target_weight_pct": core_safe_pct,
                "target_amount": capital * core_safe_pct / 100.0,
                "source_component": "core_cppi_residual",
            }
        )
    if structural_financing_pct < -1e-9:
        rows.append(
            {
                "rank": None,
                "asset_type": "strategy_financing",
                "index_code": "FINANCING",
                "index_name": "strategy financing from core/satellite mix",
                "target_weight_pct": structural_financing_pct,
                "target_amount": capital * structural_financing_pct / 100.0,
                "source_component": "pre_option_regime_gross_exposure",
            }
        )

    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    net_weight_pct = sum(float(row.get("target_weight_pct") or 0.0) for row in rows)

    return {
        "strategy": "scorecard_csi_pre_option_regime_targets",
        "rule_selector": rule_selector,
        "rule_name": pre_rule["name"],
        "validated_rule": pre_rule,
        "validated_summary": validated["summary"],
        "as_of": as_of.isoformat(),
        "snapshot": snapshot.isoformat(),
        "capital": capital,
        "portfolio_drawdown_pct": portfolio_drawdown_pct,
        "core_drawdown_pct": core_drawdown_pct,
        "satellite_drawdown_pct": satellite_drawdown_pct,
        "model_status": "strict_backtest_pass_execution_targets_generated",
        "no_lookahead_rule": (
            "CSI rows reuse the phase-ensemble target generator with the previous month-end snapshot. "
            "Satellite selection uses cached prices available on or before as_of. "
            "QQQ and CN ETF option packages use option quotes on or before as_of and a one-month holding horizon."
        ),
        "component_budgets": {
            "core_weight_pct": core_weight * 100.0,
            "core_cppi_exposure_pct": core_exposure * 100.0,
            "csi_budget_pct": csi_budget_pct,
            "qqq_option_sleeve_budget_pct": qqq_option_sleeve_budget_pct,
            "qqq_underlying_notional_pct": qqq_option_meta.get("underlying_notional_pct"),
            "qqq_option_net_cost_pct": qqq_option_meta.get("net_option_cost_pct"),
            "qqq_option_sleeve_financing_pct": qqq_option_meta.get("option_sleeve_financing_pct"),
            "cn_option_package_net_debit_pct": option_package_meta.get("net_debit_pct"),
            "core_safe_pct": core_safe_pct,
            "satellite_budget_pct": satellite_budget_pct,
            "structural_financing_pct": structural_financing_pct,
            **sat_meta,
        },
        "listed_option_risk_control": {
            "listed_stop_loss_pct": pre_rule["listed_stop_loss_pct"] * 100.0,
            "post_stop_exposure_pct": 0.0,
        },
        "bubble_reversal_signal": {
            "cs300_12m_gte_pct": 100.0,
            "cs300_3m_lte_pct": -3.0,
        },
        "cn_option_package": option_package_meta,
        "qqq_option_package": qqq_option_meta,
        "phase_ensemble": {
            "rule_name": phase_report["rule_name"],
            "target_equity_pct": phase_report["target_equity_pct"],
            "scorecard": phase_report["scorecard"],
            "rebalance_reasons": phase_report["rebalance_reasons"],
            "sleeves": phase_report["sleeves"],
        },
        "net_position_weight_pct": net_weight_pct,
        "rows": rows,
    }


def write_outputs(report: dict[str, Any]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(report["as_of"]).replace("-", "")
    json_path = OUT_DIR / f"csi_pre_option_regime_targets_{stamp}.json"
    csv_path = OUT_DIR / f"csi_pre_option_regime_targets_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames = sorted({key for row in report["rows"] for key in row})
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report["rows"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate strict-pass pre-option regime strategy targets.")
    parser.add_argument("--rule", default=DEFAULT_RULE_NAME, help="best_balance, best_drawdown, or exact strict-pass rule name.")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Decision date, YYYY-MM-DD")
    parser.add_argument("--snapshot", help="Scorecard/selection snapshot date, YYYY-MM-DD. Default: previous month end.")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--top-per-sleeve", type=int, default=0)
    parser.add_argument("--portfolio-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--core-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--satellite-drawdown-pct", type=float, default=0.0)
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    snapshot = date.fromisoformat(args.snapshot) if args.snapshot else previous_month_end(as_of)
    report = build_targets(
        rule_selector=args.rule,
        as_of=as_of,
        snapshot=snapshot,
        capital=args.capital,
        top_per_sleeve=args.top_per_sleeve,
        portfolio_drawdown_pct=args.portfolio_drawdown_pct,
        core_drawdown_pct=args.core_drawdown_pct,
        satellite_drawdown_pct=args.satellite_drawdown_pct,
    )
    json_path, csv_path = write_outputs(report)
    budgets = report["component_budgets"]
    print("Pre-option regime CSI strategy targets")
    print(
        f"  rule={report['rule_name']} as_of={report['as_of']} snapshot={report['snapshot']} "
        f"min_final={report['validated_summary']['min_final_capital_wan']:.1f}w "
        f"worst_mdd={report['validated_summary']['worst_max_drawdown'] * 100:.1f}%"
    )
    print(
        f"  csi={budgets['csi_budget_pct']:.2f}% qqq_option_sleeve={budgets['qqq_option_sleeve_budget_pct']:.2f}% "
        f"qqq_underlying={budgets.get('qqq_underlying_notional_pct')}% "
        f"qqq_option_net_cost={budgets.get('qqq_option_net_cost_pct')}% "
        f"satellite_risky={budgets['satellite_risky_budget_pct']:.2f}% "
        f"satellite_safe={budgets['satellite_safe_budget_pct']:.2f}% "
        f"cn_option_net_debit={budgets.get('cn_option_package_net_debit_pct')}"
    )
    print(f"  rows={len(report['rows'])} net_weight={report['net_position_weight_pct']:.2f}%")
    print(f"  json={json_path}")
    print(f"  csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

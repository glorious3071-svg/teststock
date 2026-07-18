#!/usr/bin/env python3
"""Generate target rows for the modeled defined-loss CSI strategy.

This is a production-shape artifact for the first strict-pass modeled frontier:
CSI phase sleeves + QQQ option sleeve + small crypto CPPI satellite + a monthly
defined-loss overlay.  The overlay row is an execution requirement, not proof
that an executable listed option package has been sourced.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_blend_tipp_overlay import BLEND_RULE_BY_NAME
from scripts.backtest_scorecard_csi_defined_loss_overlay import DefinedLossRule
from scripts.backtest_scorecard_csi_synthetic_option_hedge import RULES as OPTION_RULES
from scripts.generate_csi_phase_ensemble_targets import (
    DEFAULT_RULE_NAME as DEFAULT_PHASE_RULE_NAME,
    build_targets as build_phase_targets,
    previous_month_end,
)
from scripts.stress_cn_etf_put_hedge_candidate import latest_fund_price, underlying_from_opt_code

OUT_DIR = ROOT / "data" / "portfolio"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_RULE_NAME = "defloss_mix95_8_floor010_prem075_up10"
EXECUTABLE_SPREAD_RULE_NAME = "defloss_spread95call108_mix95_8_floor010_prem075_up0"
OPTION_RULE_BY_NAME = {rule.name: rule for rule in OPTION_RULES}


@dataclass(frozen=True)
class DefinedLossTargetRule:
    name: str
    research_rule_name: str
    phase_rule_name: str
    base_blend_name: str
    option_rule_name: str
    satellite_rule_name: str
    satellite_risk_key: str
    core_weight: float
    satellite_weight: float
    core_cppi_floor_pct: float
    core_cppi_multiplier: float
    core_cppi_max_exposure: float
    satellite_cppi_floor_pct: float
    satellite_cppi_multiplier: float
    satellite_cppi_max_exposure: float
    monthly_loss_floor: float
    premium_monthly: float
    upside_haircut: float
    csi_hedge_pct: float = 0.0
    csi_hedge_cost_annual: float = 0.0
    hedge_future_code: str = "IF.CFX"
    hedge_future_multiplier: float = 300.0
    hedge_future_margin_rate: float = 0.12


RULES = [
    DefinedLossTargetRule(
        name=DEFAULT_RULE_NAME,
        research_rule_name=DEFAULT_RULE_NAME,
        phase_rule_name=DEFAULT_PHASE_RULE_NAME,
        base_blend_name="blend_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80",
        option_rule_name="qqq_put98_call108_lev125",
        satellite_rule_name="sat_crypto_cppi",
        satellite_risk_key="crypto",
        core_weight=0.95,
        satellite_weight=0.08,
        core_cppi_floor_pct=0.86,
        core_cppi_multiplier=8.0,
        core_cppi_max_exposure=1.0,
        satellite_cppi_floor_pct=0.90,
        satellite_cppi_multiplier=6.0,
        satellite_cppi_max_exposure=1.0,
        monthly_loss_floor=-0.01,
        premium_monthly=0.0075,
        upside_haircut=0.10,
    ),
    DefinedLossTargetRule(
        name=EXECUTABLE_SPREAD_RULE_NAME,
        research_rule_name=EXECUTABLE_SPREAD_RULE_NAME,
        phase_rule_name=DEFAULT_PHASE_RULE_NAME,
        base_blend_name="blend_phase12_lever120_us10y_qqq_put98_95spread_call108_lev125_c20_o80",
        option_rule_name="qqq_put98_95spread_call108_lev125",
        satellite_rule_name="sat_crypto_cppi",
        satellite_risk_key="crypto",
        core_weight=0.95,
        satellite_weight=0.08,
        core_cppi_floor_pct=0.86,
        core_cppi_multiplier=8.0,
        core_cppi_max_exposure=1.0,
        satellite_cppi_floor_pct=0.90,
        satellite_cppi_multiplier=6.0,
        satellite_cppi_max_exposure=1.0,
        monthly_loss_floor=-0.01,
        premium_monthly=0.0075,
        upside_haircut=0.0,
        csi_hedge_pct=0.23,
        csi_hedge_cost_annual=0.01,
    )
]
RULE_BY_NAME = {rule.name: rule for rule in RULES}


def rule_by_name(name: str) -> DefinedLossTargetRule:
    if name not in RULE_BY_NAME:
        available = ", ".join(sorted(RULE_BY_NAME))
        raise ValueError(f"Unknown defined-loss target rule {name!r}. Available rules: {available}")
    return RULE_BY_NAME[name]


def cppi_exposure(floor_pct: float, multiplier: float, max_exposure: float, drawdown_pct: float) -> float:
    capital = max(0.01, 1.0 + drawdown_pct / 100.0)
    cushion = max(0.0, capital - floor_pct)
    return min(max_exposure, multiplier * cushion / capital)


def scale_rows(rows: list[dict[str, Any]], scale: float, capital: float, source_component: str) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["target_weight_pct"] = float(item["target_weight_pct"]) * scale
        item["target_amount"] = capital * item["target_weight_pct"] / 100.0
        item["source_component"] = source_component
        out.append(item)
    return out


def load_external_rows(symbol: str, as_of: date) -> list[tuple[date, float]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, COALESCE(adj_close, close)
                FROM external_asset_daily
                WHERE symbol=%s AND trade_date <= %s
                ORDER BY trade_date
                """,
                (symbol, as_of),
            )
            return [(row[0], float(row[1])) for row in cur.fetchall() if row[1] is not None]
    finally:
        conn.close()


def current_asset_features(symbol: str, as_of: date) -> dict[str, Any]:
    rows = load_external_rows(symbol, as_of)
    if len(rows) < 253:
        return {"symbol": symbol, "latest_date": rows[-1][0].isoformat() if rows else None}
    prices = [price for _day, price in rows]
    returns = [0.0]
    for prev, current in zip(prices, prices[1:]):
        returns.append(0.0 if prev <= 0 else current / prev - 1.0)
    window = returns[-63:]
    mean = sum(window) / len(window)
    vol_63 = (sum((value - mean) ** 2 for value in window) / len(window)) ** 0.5 * (252.0 ** 0.5)
    latest = prices[-1]
    ret_6m = latest / prices[-127] - 1.0 if prices[-127] > 0 else None
    ret_12m = latest / prices[-253] - 1.0 if prices[-253] > 0 else None
    return {
        "symbol": symbol,
        "latest_date": rows[-1][0].isoformat(),
        "ret_6m": ret_6m,
        "ret_12m": ret_12m,
        "vol_63": vol_63,
    }


def current_satellite_weights(symbols: list[str], as_of: date, top_n: int) -> tuple[list[tuple[str, float]], str, float, dict[str, Any]]:
    features = {symbol: current_asset_features(symbol, as_of) for symbol in symbols}
    scored = []
    for symbol, feat in features.items():
        ret_6m = feat.get("ret_6m")
        ret_12m = feat.get("ret_12m")
        vol = feat.get("vol_63")
        if ret_6m is None or ret_12m is None or vol is None:
            continue
        score = 0.65 * ret_12m + 0.35 * ret_6m
        if score <= 0:
            continue
        scored.append((score / max(vol, 0.08), vol, symbol, score))
    scored.sort(reverse=True)
    picks = scored[:top_n]
    if not picks:
        return [("SHY", 1.0)], "fallback", 0.0, {"features": features}
    inv_vol = [1.0 / max(item[1], 0.08) for item in picks]
    denom = sum(inv_vol)
    weights = [(item[2], weight / denom) for weight, item in zip(inv_vol, picks)]
    trend = sum(item[3] for item in picks) / len(picks)
    return weights, ",".join(symbol for symbol, _weight in weights), trend, {"features": features}


def satellite_rows(
    rule: DefinedLossTargetRule,
    as_of: date,
    capital: float,
    satellite_budget_pct: float,
    satellite_drawdown_pct: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    exposure = cppi_exposure(
        rule.satellite_cppi_floor_pct,
        rule.satellite_cppi_multiplier,
        rule.satellite_cppi_max_exposure,
        satellite_drawdown_pct,
    )
    universe = ["BTC-USD", "ETH-USD"] if rule.satellite_risk_key == "crypto" else ["BTC-USD"]
    weights, selected, trend, feature_meta = current_satellite_weights(universe, as_of, top_n=2)
    is_fallback = selected == "fallback"
    risky_budget_pct = 0.0 if is_fallback else satellite_budget_pct * exposure
    rows = []
    if not is_fallback:
        for _rank, (symbol, weight) in enumerate(weights, 1):
            target_weight_pct = risky_budget_pct * weight
            rows.append(
                {
                    "rank": None,
                    "asset_type": "crypto_satellite",
                    "index_code": symbol,
                    "index_name": symbol,
                    "target_weight_pct": target_weight_pct,
                    "target_amount": capital * target_weight_pct / 100.0,
                    "source_component": rule.satellite_rule_name,
                    "selection": selected,
                    "trend_score": trend,
                }
            )
    safe_pct = satellite_budget_pct if is_fallback else satellite_budget_pct * (1.0 - exposure)
    if safe_pct > 1e-9:
        rows.append(
            {
                "rank": None,
                "asset_type": "satellite_safe_asset",
                "index_code": "SHY",
                "index_name": "SHY",
                "target_weight_pct": safe_pct,
                "target_amount": capital * safe_pct / 100.0,
                "source_component": rule.satellite_rule_name,
                "selection": "satellite_cppi_residual",
                "trend_score": trend,
            }
        )
    metadata = {
        "as_of_external_date": max(
            feat.get("latest_date") or "0001-01-01"
            for feat in feature_meta["features"].values()
        ),
        "satellite_exposure_pct": exposure * 100.0,
        "satellite_risky_budget_pct": risky_budget_pct,
        "satellite_safe_budget_pct": safe_pct,
        "satellite_selection": selected,
        "satellite_trend_score": trend,
        "satellite_features": feature_meta["features"],
    }
    return rows, metadata


def option_row(rule: DefinedLossTargetRule, capital: float, option_budget_pct: float) -> dict[str, Any]:
    option_rule = OPTION_RULE_BY_NAME[rule.option_rule_name]
    underlying_notional_pct = option_budget_pct * option_rule.leverage
    return {
        "rank": None,
        "asset_type": "option_protected_sleeve",
        "index_code": option_rule.underlying,
        "index_name": f"{option_rule.name} synthetic sleeve",
        "target_weight_pct": option_budget_pct,
        "target_amount": capital * option_budget_pct / 100.0,
        "source_component": rule.option_rule_name,
        "underlying_notional_pct": underlying_notional_pct,
        "long_put_strike_pct": option_rule.put_strike_pct * 100.0,
        "long_put_cover_pct": option_rule.put_cover * 100.0,
        "short_put_strike_pct": option_rule.short_put_strike_pct * 100.0,
        "call_strike_pct": option_rule.call_strike_pct * 100.0,
        "call_cover_pct": option_rule.call_cover * 100.0,
        "reset_trading_days": option_rule.reset_trading_days,
        "execution_note": "Requires broker option-chain implementation; target row is not a filled position.",
    }


def load_future_quote(ts_code: str, as_of: date) -> tuple[date, float] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, close
                FROM fut_daily
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
    return row[0], float(row[1])


def futures_hedge_row(rule: DefinedLossTargetRule, capital: float, as_of: date) -> dict[str, Any] | None:
    if rule.csi_hedge_pct <= 0:
        return None

    quote = load_future_quote(rule.hedge_future_code, as_of)
    target_notional = capital * rule.csi_hedge_pct
    base = {
        "rank": None,
        "asset_type": "index_futures_hedge",
        "index_code": rule.hedge_future_code,
        "index_name": "CSI-linked short futures hedge",
        "source_component": "csi_linked_hedge",
        "desired_hedge_notional": -target_notional,
        "desired_hedge_weight_pct": -rule.csi_hedge_pct * 100.0,
        "hedge_cost_annual_pct": rule.csi_hedge_cost_annual * 100.0,
        "margin_rate_pct": rule.hedge_future_margin_rate * 100.0,
    }
    if quote is None:
        return {
            **base,
            "target_weight_pct": 0.0,
            "target_amount": 0.0,
            "contract_count": 0,
            "execution_note": "No futures quote available on or before as_of; hedge cannot be sized.",
        }

    quote_date, close = quote
    contract_notional = close * rule.hedge_future_multiplier
    contracts_float = target_notional / contract_notional if contract_notional > 0 else 0.0
    rounded_contracts = int(round(contracts_float))
    rounded_notional = -rounded_contracts * contract_notional
    rounded_weight_pct = rounded_notional / capital * 100.0 if capital > 0 else 0.0
    min_contract_weight_pct = -contract_notional / capital * 100.0 if capital > 0 else 0.0
    margin_amount = abs(rounded_notional) * rule.hedge_future_margin_rate

    if rounded_contracts == 0:
        execution_note = (
            "Target hedge is below one IF contract; live execution needs a larger account, "
            "ETF/options substitute, or acceptance of no futures hedge."
        )
    elif abs(rounded_weight_pct + rule.csi_hedge_pct * 100.0) > 5.0:
        execution_note = "Rounded IF contract count materially differs from target hedge notional."
    else:
        execution_note = "Short IF continuous hedge target; map to current main contract before trading."

    return {
        **base,
        "target_weight_pct": rounded_weight_pct,
        "target_amount": rounded_notional,
        "contract_count": -rounded_contracts,
        "contracts_float": contracts_float,
        "contract_multiplier": rule.hedge_future_multiplier,
        "future_quote_date": quote_date.isoformat(),
        "future_close": close,
        "contract_notional": contract_notional,
        "min_one_contract_weight_pct": min_contract_weight_pct,
        "notional_rounding_error_pct": rounded_weight_pct + rule.csi_hedge_pct * 100.0,
        "estimated_margin_amount": margin_amount,
        "execution_note": execution_note,
    }


def cn_etf_put_hedge_candidate_row(rule: DefinedLossTargetRule, capital: float, as_of: date) -> dict[str, Any] | None:
    if rule.csi_hedge_pct <= 0:
        return None
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
            latest = cur.fetchone()[0]
            if not latest:
                return None
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
                ORDER BY
                  CASE b.opt_code WHEN 'OP510300.SH' THEN 0 ELSE 1 END,
                  d.vol DESC,
                  b.maturity_date ASC
                LIMIT 1
                """,
                (latest, as_of),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

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
    exercise_price = float(exercise_price)
    per_unit = float(per_unit)
    close = float(close)
    contract_notional = exercise_price * per_unit
    desired_notional = capital * rule.csi_hedge_pct
    contracts = int(math.ceil(desired_notional / contract_notional)) if contract_notional > 0 else 0
    protected_notional = contracts * contract_notional
    premium_cost = contracts * close * per_unit
    premium_cost_pct = premium_cost / capital * 100.0 if capital > 0 else 0.0
    return {
        "rank": None,
        "asset_type": "cn_etf_put_hedge_candidate",
        "index_code": ts_code,
        "index_name": name,
        "target_weight_pct": 0.0,
        "target_amount": 0.0,
        "source_component": "cn_etf_option_substitute_candidate",
        "underlying_option_code": opt_code,
        "option_quote_date": quote_date.isoformat(),
        "maturity_date": maturity_date.isoformat(),
        "exercise_price": exercise_price,
        "per_unit": per_unit,
        "option_close": close,
        "option_settle": float(settle) if settle is not None else None,
        "vol": float(vol),
        "oi": float(oi) if oi is not None else None,
        "desired_hedge_notional": desired_notional,
        "contract_notional": contract_notional,
        "contract_count_candidate": contracts,
        "protected_notional_candidate": protected_notional,
        "protected_weight_pct_candidate": protected_notional / capital * 100.0 if capital > 0 else 0.0,
        "premium_cost_candidate": premium_cost,
        "premium_cost_pct_candidate": premium_cost_pct,
        "execution_note": (
            "Candidate substitute for the IF hedge granularity problem. "
            "Not validated as equivalent protection; requires stress replication and liquidity/fill audit."
        ),
    }


def latest_json(pattern: str) -> tuple[Path, dict[str, Any]] | None:
    paths = sorted(OUT_DIR.glob(pattern))
    if not paths:
        return None
    path = paths[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def cn_etf_option_package_rows(capital: float, as_of: date) -> list[dict[str, Any]]:
    pair = latest_json("cn_etf_option_package_hedge_search_*.json")
    if pair is None:
        return []
    path, payload = pair
    candidate = payload.get("cheapest_budget_all_pass")
    if not candidate:
        return []
    if str(payload.get("as_of")) > as_of.isoformat():
        return []

    rows: list[dict[str, Any]] = []
    underlying_code = underlying_from_opt_code(candidate.get("underlying_option_code", ""))
    underlying_price = latest_fund_price(underlying_code, as_of) if underlying_code else None
    stress_up_pct = 25.0
    for leg in candidate.get("legs", []):
        side = leg["side"]
        contracts = int(leg["contracts"])
        close = float(leg["close"])
        per_unit = float(leg["per_unit"])
        strike = float(leg["strike"])
        signed_cost = close * per_unit * contracts * (1.0 if side == "buy" else -1.0)
        stress_short_loss = None
        margin_proxy = None
        if side == "sell" and leg.get("call_put") == "C" and underlying_price:
            stressed_spot = float(underlying_price["price"]) * (1.0 + stress_up_pct / 100.0)
            stress_short_loss = max(stressed_spot - strike, 0.0) * per_unit * contracts
            margin_proxy = max(stress_short_loss, float(underlying_price["price"]) * per_unit * contracts * 0.12)
        rows.append(
            {
                "rank": None,
                "asset_type": "cn_etf_option_package_leg",
                "index_code": leg["contract"],
                "index_name": f"{leg['side']} {leg['call_put']} {candidate['underlying_option_code']} {leg['strike']}",
                "target_weight_pct": signed_cost / capital * 100.0 if capital > 0 else 0.0,
                "target_amount": signed_cost,
                "source_component": "cn_etf_option_package_latest_validated_candidate",
                "source_json": str(path.relative_to(ROOT)),
                "package_status": payload.get("status"),
                "package_role": leg["role"],
                "option_side": side,
                "call_put": leg["call_put"],
                "strike": strike,
                "close": close,
                "per_unit": per_unit,
                "contract_count": contracts,
                "maturity_date": candidate.get("maturity_date"),
                "underlying_option_code": candidate.get("underlying_option_code"),
                "protected_notional": candidate.get("protected_notional"),
                "protected_weight_pct": candidate.get("protected_weight_pct"),
                "cn_net_debit_pct": candidate.get("cn_net_debit_pct"),
                "total_net_debit_pct": candidate.get("total_net_debit_pct"),
                "premium_budget_pct": candidate.get("premium_budget_pct"),
                "premium_budget_pass": candidate.get("premium_budget_pass"),
                "stress_pass_count": candidate.get("stress_pass_count"),
                "stress_scenario_count": candidate.get("stress_scenario_count"),
                "worst_total_return_pct": candidate.get("worst_total_return_pct"),
                "min_leg_volume": candidate.get("min_leg_volume"),
                "min_leg_oi": candidate.get("min_leg_oi"),
                "underlying_price_date": underlying_price.get("trade_date") if underlying_price else None,
                "underlying_price": underlying_price.get("price") if underlying_price else None,
                "stress_up_pct_for_short_call": stress_up_pct if stress_short_loss is not None else None,
                "stress_short_call_loss_amount": stress_short_loss,
                "stress_short_call_loss_pct_capital": stress_short_loss / capital * 100.0
                if stress_short_loss is not None and capital > 0
                else None,
                "margin_proxy_amount": margin_proxy,
                "margin_proxy_pct_capital": margin_proxy / capital * 100.0
                if margin_proxy is not None and capital > 0
                else None,
                "execution_note": (
                    "Latest budget-pass stress candidate. Uses option close, not bid/ask; "
                    "short legs require broker margin, exercise, liquidity, and fill audit."
                ),
            }
        )
    return rows


def overlay_row(rule: DefinedLossTargetRule, capital: float) -> dict[str, Any]:
    return {
        "rank": None,
        "asset_type": "defined_loss_overlay",
        "index_code": "DEFINED_LOSS_OVERLAY",
        "index_name": rule.name,
        "target_weight_pct": 0.0,
        "target_amount": 0.0,
        "source_component": rule.name,
        "protected_notional_pct": 100.0,
        "monthly_loss_floor_pct": rule.monthly_loss_floor * 100.0,
        "monthly_premium_budget_pct": rule.premium_monthly * 100.0,
        "monthly_premium_budget_amount": capital * rule.premium_monthly,
        "upside_haircut_pct": rule.upside_haircut * 100.0,
        "execution_note": "Modeled strict-pass assumption; must be mapped to executable protection terms before live adoption.",
    }


def build_targets(
    rule_name: str,
    as_of: date,
    snapshot: date,
    capital: float,
    top_per_sleeve: int,
    portfolio_drawdown_pct: float,
    core_drawdown_pct: float,
    satellite_drawdown_pct: float,
) -> dict[str, Any]:
    rule = rule_by_name(rule_name)
    blend_rule = BLEND_RULE_BY_NAME[rule.base_blend_name]
    core_exposure = cppi_exposure(
        rule.core_cppi_floor_pct,
        rule.core_cppi_multiplier,
        rule.core_cppi_max_exposure,
        core_drawdown_pct,
    )
    core_active_pct = rule.core_weight * 100.0 * core_exposure
    csi_budget_pct = core_active_pct * blend_rule.csi_weight
    option_budget_pct = core_active_pct * blend_rule.option_weight
    core_safe_pct = rule.core_weight * 100.0 * (1.0 - core_exposure)
    satellite_budget_pct = rule.satellite_weight * 100.0
    structural_financing_pct = min(0.0, 100.0 - rule.core_weight * 100.0 - satellite_budget_pct)

    phase_report = build_phase_targets(
        rule_name=rule.phase_rule_name,
        as_of=as_of,
        snapshot=snapshot,
        capital=capital,
        top_per_sleeve=top_per_sleeve,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
    )
    rows = scale_rows(phase_report["rows"], csi_budget_pct / 100.0, capital, "phase_ensemble_csi_core")
    rows.append(option_row(rule, capital, option_budget_pct))
    sat_rows, sat_meta = satellite_rows(rule, as_of, capital, satellite_budget_pct, satellite_drawdown_pct)
    rows.extend(sat_rows)
    hedge_row = futures_hedge_row(rule, capital, as_of)
    if hedge_row is not None:
        rows.append(hedge_row)
    cn_option_candidate = cn_etf_put_hedge_candidate_row(rule, capital, as_of)
    if cn_option_candidate is not None:
        rows.append(cn_option_candidate)
    rows.extend(cn_etf_option_package_rows(capital, as_of))
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
                "source_component": "defined_loss_mix_gross_exposure",
            }
        )
    rows.append(overlay_row(rule, capital))
    position_rows = [row for row in rows if row.get("asset_type") != "defined_loss_overlay"]
    for rank, row in enumerate(position_rows, 1):
        row["rank"] = rank
    rows[-1]["rank"] = len(position_rows) + 1
    net_weight_pct = sum(float(row.get("target_weight_pct") or 0.0) for row in position_rows)

    return {
        "strategy": "scorecard_csi_defined_loss_overlay_targets",
        "rule_name": rule.name,
        "research_rule_name": rule.research_rule_name,
        "rule": asdict(rule),
        "as_of": as_of.isoformat(),
        "snapshot": snapshot.isoformat(),
        "capital": capital,
        "portfolio_drawdown_pct": portfolio_drawdown_pct,
        "core_drawdown_pct": core_drawdown_pct,
        "satellite_drawdown_pct": satellite_drawdown_pct,
        "model_status": "cost_boundary_not_execution_validated",
        "no_lookahead_rule": (
            "CSI rows reuse the phase-ensemble target generator with the previous month-end snapshot. "
            "Crypto satellite selection uses cached external prices available on or before as_of. "
            "Defined-loss overlay terms are modeled assumptions and require execution validation."
        ),
        "component_budgets": {
            "core_weight_pct": rule.core_weight * 100.0,
            "core_cppi_exposure_pct": core_exposure * 100.0,
            "csi_budget_pct": csi_budget_pct,
            "option_budget_pct": option_budget_pct,
            "core_safe_pct": core_safe_pct,
            "satellite_budget_pct": satellite_budget_pct,
            "structural_financing_pct": structural_financing_pct,
            "csi_hedge_target_pct": rule.csi_hedge_pct * 100.0,
            **sat_meta,
        },
        "defined_loss_terms": {
            "monthly_loss_floor_pct": rule.monthly_loss_floor * 100.0,
            "monthly_premium_budget_pct": rule.premium_monthly * 100.0,
            "upside_haircut_pct": rule.upside_haircut * 100.0,
            "csi_hedge_pct": rule.csi_hedge_pct * 100.0,
            "csi_hedge_cost_annual_pct": rule.csi_hedge_cost_annual * 100.0,
            "hedge_future_code": rule.hedge_future_code,
        },
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
    json_path = OUT_DIR / f"csi_defined_loss_overlay_targets_{stamp}.json"
    csv_path = OUT_DIR / f"csi_defined_loss_overlay_targets_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fieldnames = sorted({key for row in report["rows"] for key in row})
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report["rows"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate modeled defined-loss CSI strategy target rows")
    parser.add_argument("--rule", default=DEFAULT_RULE_NAME)
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Decision date, YYYY-MM-DD")
    parser.add_argument("--snapshot", help="Scorecard/selection snapshot date, YYYY-MM-DD. Default: previous month end.")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--top-per-sleeve", type=int, default=0)
    parser.add_argument("--portfolio-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--core-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--satellite-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--list-rules", action="store_true")
    args = parser.parse_args()

    if args.list_rules:
        for rule in RULES:
            print(rule.name)
        return 0

    as_of = date.fromisoformat(args.as_of)
    snapshot = date.fromisoformat(args.snapshot) if args.snapshot else previous_month_end(as_of)
    report = build_targets(
        rule_name=args.rule,
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
    terms = report["defined_loss_terms"]
    print("Defined-loss CSI strategy targets")
    print(
        f"  rule={report['rule_name']} as_of={report['as_of']} snapshot={report['snapshot']} "
        f"model_status={report['model_status']}"
    )
    print(
        f"  csi={budgets['csi_budget_pct']:.2f}% option={budgets['option_budget_pct']:.2f}% "
        f"satellite_risky={budgets['satellite_risky_budget_pct']:.2f}% "
        f"satellite_safe={budgets['satellite_safe_budget_pct']:.2f}% "
        f"financing={budgets['structural_financing_pct']:.2f}%"
    )
    print(
        f"  defined_loss floor={terms['monthly_loss_floor_pct']:.2f}% "
        f"premium_budget={terms['monthly_premium_budget_pct']:.2f}%/month "
        f"upside_haircut={terms['upside_haircut_pct']:.1f}%"
    )
    print(f"  net_position_weight={report['net_position_weight_pct']:.2f}% capital={report['capital']:,.0f}")
    display_limit = 24
    for row in report["rows"][:display_limit]:
        amount = float(row.get("target_amount") or 0.0)
        weight = float(row.get("target_weight_pct") or 0.0)
        extra = ""
        if row.get("asset_type") == "index_futures_hedge":
            extra = (
                f" contracts={row.get('contract_count')} "
                f"desired={float(row.get('desired_hedge_weight_pct') or 0.0):.2f}% "
                f"min1={float(row.get('min_one_contract_weight_pct') or 0.0):.2f}%"
            )
        elif row.get("asset_type") == "cn_etf_put_hedge_candidate":
            extra = (
                f" contracts={row.get('contract_count_candidate')} "
                f"cover={float(row.get('protected_weight_pct_candidate') or 0.0):.2f}% "
                f"premium={float(row.get('premium_cost_pct_candidate') or 0.0):.2f}%"
            )
        elif row.get("asset_type") == "cn_etf_option_package_leg":
            extra = (
                f" {row.get('option_side')} {row.get('call_put')} x{row.get('contract_count')} "
                f"net={float(row.get('total_net_debit_pct') or 0.0):.2f}% "
                f"stress={row.get('stress_pass_count')}/{row.get('stress_scenario_count')}"
            )
            if row.get("margin_proxy_pct_capital") is not None:
                extra += f" margin_proxy={float(row.get('margin_proxy_pct_capital')):.2f}%"
        print(
            f"  {int(row['rank']):>2}. {row['asset_type']} {row['index_code']} "
            f"weight={weight:.2f}% amount={amount:,.0f}{extra}"
        )
        if row.get("asset_type") in {
            "index_futures_hedge",
            "cn_etf_put_hedge_candidate",
            "cn_etf_option_package_leg",
        } and row.get("execution_note"):
            print(f"      note: {row['execution_note']}")
    if len(report["rows"]) > display_limit:
        print(f"  ... {len(report['rows']) - display_limit} more rows")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

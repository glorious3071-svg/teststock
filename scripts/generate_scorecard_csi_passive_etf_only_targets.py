#!/usr/bin/env python3
"""Generate domestic passive ETF-only target holdings from the search rules."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from backtest.strict_passive_etf_objective import validate_target_assets
from scripts.backtest_scorecard_csi_dynamic_defense import month_end_shift
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL
from scripts.search_scorecard_csi_passive_etf_only import (
    EtfOnlyRule,
    build_rules,
    choose_portfolio,
    load_etf_universe,
    period_return,
    portfolio_return,
)

OUT_DIR = ROOT / "data" / "portfolio"
DEFAULT_RULE_NAME = "etftipp_i1_top1_m3_tr100_dd06_sgn05_f95_k20"
DEFAULT_CAPITAL = 1_000_000.0


def parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def previous_month_end(day: dt.date) -> dt.date:
    first = dt.date(day.year, day.month, 1)
    return first - dt.timedelta(days=1)


def latest_fund_date(series: dict[str, list[tuple[dt.date, float]]]) -> dt.date:
    dates = [rows[-1][0] for code, rows in series.items() if code != CS300_CODE and rows]
    if not dates:
        raise RuntimeError("No ETF price series available")
    return max(dates)


def rule_by_name(name: str) -> EtfOnlyRule:
    candidates = [
        *build_rules(quick=True, cash_focused=False),
        *build_rules(quick=False, cash_focused=True),
    ]
    for rule in candidates:
        if rule.name == name:
            return rule
    available = ", ".join(rule.name for rule in candidates[:30])
    raise ValueError(f"Unknown rule {name!r}. First available rules: {available}")


def target_rows(
    metas,
    series,
    rule: EtfOnlyRule,
    snapshot: dt.date,
    start_exec: dt.date,
    end_exec: dt.date,
    capital: float,
    peak_capital: float,
    allow_cash_defense: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    drawdown = capital / peak_capital - 1.0 if peak_capital > 0 else 0.0
    trend = period_return(series[CS300_CODE], month_end_shift(snapshot, -rule.trend_months), snapshot) or 0.0
    defensive = trend <= rule.trend_lte or drawdown <= rule.drawdown_lte
    codes = choose_portfolio(metas, series, snapshot, rule, defensive, allow_cash_defense)
    defense_codes = choose_portfolio(metas, series, snapshot, rule, True, allow_cash_defense)

    if rule.floor_pct > 0:
        floor = peak_capital * rule.floor_pct
        cushion = max(0.0, capital - floor)
        risk_weight = min(rule.max_risk_weight, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
        if defensive:
            risk_weight = min(risk_weight, 0.25)
    else:
        risk_weight = 0.0 if not codes else 1.0

    rows: list[dict[str, Any]] = []
    if codes and risk_weight > 0:
        per_weight = risk_weight / len(codes)
        for rank, code in enumerate(codes, 1):
            meta = metas[code]
            rows.append(
                {
                    "rank": rank,
                    "asset_type": "domestic_passive_etf",
                    "ts_code": code,
                    "name": meta.name,
                    "index_code": meta.index_code,
                    "index_name": meta.index_name,
                    "category": meta.category,
                    "target_weight_pct": per_weight * 100.0,
                    "target_amount": capital * per_weight,
                }
            )

    defensive_weight = 1.0 - sum(row["target_weight_pct"] for row in rows) / 100.0
    if defensive_weight > 1e-9 and defense_codes:
        per_weight = defensive_weight / len(defense_codes)
        offset = len(rows)
        for i, code in enumerate(defense_codes, 1):
            meta = metas[code]
            rows.append(
                {
                    "rank": offset + i,
                    "asset_type": "defensive_etf",
                    "ts_code": code,
                    "name": meta.name,
                    "index_code": meta.index_code,
                    "index_name": meta.index_name,
                    "category": meta.category,
                    "target_weight_pct": per_weight * 100.0,
                    "target_amount": capital * per_weight,
                }
            )
    elif defensive_weight > 1e-9:
        rows.append(
            {
                "rank": len(rows) + 1,
                "asset_type": "uninvested_cash",
                "ts_code": "CASH",
                "name": "空仓现金",
                "index_code": "",
                "index_name": "未投资资金，不是非 ETF 投资工具",
                "category": "cash",
                "target_weight_pct": defensive_weight * 100.0,
                "target_amount": capital * defensive_weight,
            }
        )

    expected_period_return = (
        risk_weight * portfolio_return(codes, series, start_exec, end_exec)
        + defensive_weight * portfolio_return(defense_codes, series, start_exec, end_exec)
    )
    state = {
        "snapshot": snapshot.isoformat(),
        "start_exec": start_exec.isoformat(),
        "end_exec": end_exec.isoformat(),
        "trend_return": trend,
        "capital_drawdown": drawdown,
        "defensive": defensive,
        "risk_weight_pct": risk_weight * 100.0,
        "defensive_or_cash_weight_pct": defensive_weight * 100.0,
        "expected_period_return_if_held_to_next_month_end": expected_period_return,
    }
    return rows, state


def build_targets(
    as_of: dt.date | None,
    rule_name: str,
    capital: float,
    peak_capital: float,
    include_money_etf_defense: bool,
    allow_cash_defense: bool,
    min_rows: int,
) -> dict[str, Any]:
    conn = get_connection()
    try:
        metas, series = load_etf_universe(conn, min_rows, include_money_etf_defense)
    finally:
        conn.close()

    rule = rule_by_name(rule_name)
    as_of = as_of or latest_fund_date(series)
    snapshot = previous_month_end(as_of)
    start_exec = as_of
    end_exec = previous_month_end(as_of + dt.timedelta(days=40))
    rows, state = target_rows(
        metas,
        series,
        rule,
        snapshot,
        start_exec,
        end_exec,
        capital,
        peak_capital,
        allow_cash_defense,
    )
    target_asset_violations = validate_target_assets(
        rows,
        {
            code: {
                "etf_type": None,
                "is_enhanced": False,
                "listed_by_as_of": meta.list_date is None or meta.list_date <= as_of,
            }
            for code, meta in metas.items()
        },
    )
    return {
        "as_of": as_of.isoformat(),
        "initial_capital_reference": INITIAL_CAPITAL,
        "capital": capital,
        "peak_capital": peak_capital,
        "rule": asdict(rule),
        "constraints": {
            "domestic_passive_etf_only": True,
            "no_overseas_assets": True,
            "no_options": True,
            "no_futures": True,
            "no_crypto": True,
            "include_money_etf_defense": include_money_etf_defense,
            "allow_uninvested_cash": allow_cash_defense,
        },
        "state": state,
        "targets": rows,
        "strict_asset_validation": {
            "passed": not target_asset_violations,
            "violations": target_asset_violations,
        },
        "readiness_note": (
            "This generator automates holdings for the selected ETF-only rule. "
            "The current rule has not passed the 4000w and 10% max-drawdown objective."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate domestic passive ETF-only target holdings.")
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument("--rule-name", default=DEFAULT_RULE_NAME)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--peak-capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--include-money-etf-defense", action="store_true")
    parser.add_argument("--allow-cash-defense", action="store_true")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    payload = build_targets(
        as_of=args.as_of,
        rule_name=args.rule_name,
        capital=args.capital,
        peak_capital=args.peak_capital,
        include_money_etf_defense=args.include_money_etf_defense,
        allow_cash_defense=args.allow_cash_defense,
        min_rows=args.min_rows,
    )
    as_of_slug = payload["as_of"].replace("-", "")
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / f"scorecard_csi_passive_etf_only_targets_{as_of_slug}"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)

    json_path = Path(f"{prefix}.json")
    csv_path = Path(f"{prefix}.csv")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "rank",
        "asset_type",
        "ts_code",
        "name",
        "index_code",
        "index_name",
        "category",
        "target_weight_pct",
        "target_amount",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(payload["targets"])

    print(f"as_of={payload['as_of']} rule={payload['rule']['name']}")
    for row in payload["targets"]:
        print(
            f"{row['rank']:>2} {row['asset_type']:<20} {row['ts_code']:<10} "
            f"{row['target_weight_pct']:6.2f}% {row['target_amount']:,.2f}"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

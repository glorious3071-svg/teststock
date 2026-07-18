#!/usr/bin/env python3
"""Generate current domestic passive-ETF targets from the validated monthly model."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SELECTOR_POLICIES
from backtest.domestic_defensive_etf import DEFENSIVE_POLICIES, load_defensive_etf_universe
from backtest.monthly_direction_model import MONTHLY_DIRECTION_POLICIES
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_monthly import (
    RULES,
    build_monthly_path,
    enrich_monthly_paths,
    evaluate_path,
    load_price_series,
    load_selector_price_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import shifted_boundary
from backtest.phase_schedule import shift_month_end
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE
from scripts.map_csi_to_etf_proxy import resolve_etf_proxy
from scripts.validate_scorecard_csi_generalization import FORMAL_SCHEDULES

OUT_JSON = ROOT / "data" / "portfolio" / "calendar_neutral_csi_targets_latest.json"
OUT_CSV = ROOT / "data" / "portfolio" / "calendar_neutral_csi_targets_latest.csv"
RULE_NAME = "monthly_f90_m10_s125_bc25"
SELECTOR_NAME = "monthly_trend6_risk_top10"
DEFENSE_NAME = "bond_gold45_252d_gold42"
DIRECTION_NAME = "dxy_local_theme1c25_fund_dist_max100"


def named(items, name):
    return next(item for item in items if item.name == name)


def main() -> int:
    selector = named(SELECTOR_POLICIES, SELECTOR_NAME)
    rule = named(RULES, RULE_NAME)
    defense = named(DEFENSIVE_POLICIES, DEFENSE_NAME)
    direction = named(MONTHLY_DIRECTION_POLICIES, DIRECTION_NAME)
    schedule = next(item for item in FORMAL_SCHEDULES if "review1m" in item.name)

    conn = get_connection()
    try:
        price_series = load_price_series(conn)
        load_selector_price_series(conn, price_series)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
    finally:
        conn.close()
    trade_dates = [day for day, _value in price_series[CS300_CODE]]
    path = build_monthly_path(schedule, 0, 3, trade_dates, selector)
    latest_complete_month = date.today().replace(day=1) - timedelta(days=1)
    cursor = path["months"][-1]["next_snapshot"]
    template = path["months"][-1]
    while cursor < latest_complete_month:
        next_snapshot = min(shift_month_end(cursor, 1), latest_complete_month)
        path["months"].append(
            {
                "snapshot": cursor,
                "next_snapshot": next_snapshot,
                "start_exec": shifted_boundary(trade_dates, cursor, 3),
                "end_exec": shifted_boundary(trade_dates, next_snapshot, 3),
                "holding_codes": list(template["holding_codes"]),
                "holding_weights": dict(template["holding_weights"]),
                "base_weight": float(template["base_weight"]),
                "review_interval_months": 1,
            }
        )
        cursor = next_snapshot
    if cursor == latest_complete_month:
        path["months"].append(
            {
                "snapshot": cursor,
                "next_snapshot": shift_month_end(cursor, 1),
                "start_exec": shifted_boundary(trade_dates, cursor, 3),
                "end_exec": trade_dates[-1],
                "holding_codes": list(template["holding_codes"]),
                "holding_weights": dict(template["holding_weights"]),
                "base_weight": float(template["base_weight"]),
                "review_interval_months": 1,
            }
        )
    enrich_monthly_paths(
        [path],
        price_series,
        trade_dates,
        selector,
        monthly_selector_refresh=True,
        online_selector=False,
        direction_prehistory_months=0,
    )
    result = evaluate_path(
        path,
        rule,
        price_series,
        defensive_series,
        defensive_metas,
        defense,
        {},
        direction,
    )
    latest = result["rows"][-1]
    targets: dict[str, dict] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for index_code, index_weight in latest["holding_weights"].items():
                proxy = resolve_etf_proxy(
                    cur,
                    index_code,
                    date.fromisoformat(latest["snapshot"]),
                    504,
                    0.80,
                )
                if not proxy:
                    continue
                code = proxy["etf_code"]
                row = targets.setdefault(
                    code,
                    {"asset_type": "equity_etf", "etf_code": code, "etf_name": proxy["etf_name"], "target_weight": 0.0},
                )
                row["target_weight"] += latest["exposure"] * float(index_weight)
    finally:
        conn.close()
    for code, weight in latest["defensive_weights"].items():
        meta = defensive_metas[code]
        targets[code] = {
            "asset_type": f"defensive_{meta.category}_etf",
            "etf_code": code,
            "etf_name": meta.name,
            "target_weight": (1.0 - latest["exposure"]) * float(weight),
        }
    invested = sum(row["target_weight"] for row in targets.values())
    rows = sorted(targets.values(), key=lambda row: row["target_weight"], reverse=True)
    rows.append({"asset_type": "cash", "etf_code": "CASH", "etf_name": "现金", "target_weight": max(0.0, 1.0 - invested)})
    payload = {
        "snapshot": latest["snapshot"],
        "execution_start": latest["start_exec"],
        "frequency": "monthly",
        "domestic_passive_only": True,
        "policy": {"rule": RULE_NAME, "selector": SELECTOR_NAME, "defense": DEFENSE_NAME, "direction": DIRECTION_NAME},
        "model_exposure": latest["exposure"],
        "targets": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("asset_type", "etf_code", "etf_name", "target_weight"))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {OUT_JSON} and {OUT_CSV}; snapshot={latest['snapshot']} exposure={latest['exposure']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate current holdings for the strict quarterly passive-ETF frontier."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.csi_snapshot_selector import SELECTOR_POLICIES, SNAPSHOT_CSI_SELECTOR
from backtest.domestic_defensive_etf import (
    DEFENSIVE_POLICIES,
    load_defensive_etf_universe,
    select_defensive_weights,
)
from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    direct_selector_diagnostics,
    load_equity_etf_return_universe,
    map_indices_to_etfs,
    select_direct_equity_etfs,
    direct_blend_share,
)
from backtest.phase_features import PHASE_FEATURE_STORE
from backtest.phase_schedule import shift_month_end
from backtest.strict_passive_etf_objective import validate_target_assets
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_tipp import (
    build_daily_path,
    load_selector_price_series,
)
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (
    ANNUAL_MARKET_SCORECARD,
    EXECUTION_LAGS,
    RULES,
    evaluate_path,
)
from scripts.validate_scorecard_csi_generalization import (
    DEFAULT_RULE,
    DIRECTION_MATCHED_FEATURE_POLICY,
    MONTH_DRIFT_PHASES,
    SCHEDULE_12M_3M,
    apply_feature_policy,
    scorecard_detail,
)


RULE_NAME = "q_mdd20_qfree_stack_highdist800"
SELECTOR_NAME = "expanded_value_risk_top7_power8_cap45"
DIRECT_POLICY_NAME = "blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92"
DEFENSIVE_POLICY_NAME = "bondfine_91d_vp41_top1_min-50"
OUT_PREFIX = ROOT / "data/portfolio/scorecard_csi_strict_quarterly_targets_latest"


def named(items, name):
    return next(item for item in items if item.name == name)


def latest_complete_month(as_of: date) -> date:
    return as_of.replace(day=1) - timedelta(days=1)


def scheduled_snapshot(as_of: date, phase: int) -> tuple[date, date]:
    anchor = date(2004, 12, 31)
    boundary = latest_complete_month(as_of)
    elapsed = (boundary.year - anchor.year) * 12 + boundary.month - anchor.month
    elapsed -= (elapsed - phase) % 3
    snapshot = shift_month_end(anchor, elapsed)
    quarter_index = (elapsed - phase) // 3
    cycle_entry = shift_month_end(anchor, phase + (quarter_index // 4) * 12)
    return snapshot, cycle_entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--phase-month-offset", type=int, default=0, choices=range(12))
    parser.add_argument("--execution-lag-days", type=int, default=3, choices=(0, 1, 3, 5))
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--budget-peak", type=float, default=1_000_000.0)
    parser.add_argument("--output-prefix", type=Path, default=OUT_PREFIX)
    args = parser.parse_args()

    rule = named(RULES, RULE_NAME)
    selector = named(SELECTOR_POLICIES, SELECTOR_NAME)
    direct_policy = named(DIRECT_ETF_POLICIES, DIRECT_POLICY_NAME)
    defensive_policy = named(DEFENSIVE_POLICIES, DEFENSIVE_POLICY_NAME)

    conn = get_connection()
    try:
        index_series = load_price_series(conn)
        load_selector_price_series(conn, index_series)
        equity_metas, equity_series = load_equity_etf_return_universe(conn)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
        trade_dates = [day for day, _value in index_series[CS300_CODE]]
        data_as_of = min(args.as_of or trade_dates[-1], trade_dates[-1])
        path = build_daily_path(
            index_series,
            trade_dates,
            SCHEDULE_12M_3M,
            args.phase_month_offset,
            args.execution_lag_days,
            equity_metas,
            equity_series,
            ANNUAL_MARKET_SCORECARD,
            True,
            True,
            selector,
            direct_policy,
            False,
            False,
            True,
            max(MONTH_DRIFT_PHASES),
            max(EXECUTION_LAGS),
            date(2005, 2, 28),
            "execution",
        )
        evaluated = evaluate_path(
            path,
            rule,
            equity_series,
            defensive_metas,
            defensive_series,
            defensive_policy,
            include_decision_rows=True,
        )
        decisions = [
            row
            for row in evaluated["decision_rows"]
            if date.fromisoformat(str(row["decision_date"])) <= data_as_of
        ]
        if not decisions:
            raise RuntimeError(f"No generated decision on or before {data_as_of}")
        latest_decision = decisions[-1]
        target_weights = {
            str(code): float(weight)
            for code, weight in latest_decision["target_weights"].items()
        }
        snapshot = date.fromisoformat(str(latest_decision["rebalance_anchor"]))
        execution_date = date.fromisoformat(str(latest_decision["decision_date"]))
        _scheduled_snapshot, cycle_entry = scheduled_snapshot(
            execution_date, args.phase_month_offset
        )
        index_weights = {
            str(code): float(weight)
            for code, weight in latest_decision.get("index_target_weights", {}).items()
        }
        exposure = float(latest_decision["exposure"])
        active_risk_flags = list(latest_decision.get("active_risk_flags", []))
        direct_share = direct_blend_share(
            direct_policy,
            latest_decision.get("market_state", {}),
        )
        allocation_reasons = latest_decision.get("scorecard_context", {}).get(
            "rebalance_reasons", []
        )
        base_pct = (
            float(latest_decision["exposure_formation"]["raw_base_weight"])
            * 100.0
        )
        bear = bool(latest_decision.get("bear_state"))
        recovery = latest_decision.get("market_recovery", {})
        recovery_flagged = bool(recovery.get("flagged"))
        recovery_applied = bool(recovery.get("applied"))

        codes = [
            code
            for code, weight in target_weights.items()
            if code != "CASH" and abs(weight) > 1e-12
        ]
        meta_rows = {}
        if codes:
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(codes))
                cur.execute(
                    f"""
                    SELECT ts_code, extname, index_ts_code, index_name, etf_type,
                           is_enhanced, list_date
                    FROM passive_etf WHERE ts_code IN ({placeholders})
                    """,
                    codes,
                )
                meta_rows = {
                    str(code): {
                        "name": str(name or code),
                        "index_code": str(index_code or ""),
                        "index_name": str(index_name or ""),
                        "etf_type": etf_type,
                        "is_enhanced": bool(is_enhanced),
                        "listed_by_as_of": list_date is None or list_date <= snapshot,
                    }
                    for code, name, index_code, index_name, etf_type, is_enhanced, list_date in cur.fetchall()
                }
    finally:
        conn.close()

    rows = []
    for rank, (code, weight) in enumerate(
        (
            (code, weight)
            for code, weight in sorted(
                target_weights.items(), key=lambda item: (-item[1], item[0])
            )
            if abs(weight) > 1e-12
        ),
        1,
    ):
        meta = meta_rows.get(code, {})
        rows.append(
            {
                "rank": rank,
                "asset_type": "cash" if code == "CASH" else "domestic_passive_etf",
                "ts_code": code,
                "name": "现金" if code == "CASH" else meta.get("name", code),
                "index_code": meta.get("index_code", ""),
                "index_name": meta.get("index_name", ""),
                "target_weight_pct": weight * 100.0,
                "target_amount": args.capital * weight,
            }
        )
    violations = validate_target_assets(rows, meta_rows)
    payload = {
        "as_of": data_as_of.isoformat(),
        "snapshot": snapshot.isoformat(),
        "cycle_entry_snapshot": cycle_entry.isoformat(),
        "execution_date": execution_date.isoformat(),
        "frequency": "exactly_every_three_months_from_phase_start",
        "phase_month_offset": args.phase_month_offset,
        "execution_lag_days": args.execution_lag_days,
        "capital": args.capital,
        "budget_peak": args.budget_peak,
        "policy": {
            "market_scorecard": ANNUAL_MARKET_SCORECARD.name,
            "csi_selector": SELECTOR_NAME,
            "direct_etf_selector": DIRECT_POLICY_NAME,
            "risk_rule": RULE_NAME,
            "defensive_policy": DEFENSIVE_POLICY_NAME,
        },
        "state": {
            "allocation_signal_year": execution_date.year,
            "allocation_signal_snapshot": snapshot.isoformat(),
            "base_equity_pct": base_pct,
            "model_exposure": exposure,
            "bear_state": bear,
            "active_risk_flags": active_risk_flags,
            "market_recovery_flagged": recovery_flagged,
            "market_recovery_applied": recovery_applied,
            "direct_etf_blend_share": direct_share,
            "market_recovery_thresholds": {
                "cs300_return_3m": rule.recovery_market_return_threshold,
                "cs300_return_6m": rule.recovery_market_return_6m_threshold,
                "cs300_ma_6m_distance": (
                    rule.recovery_market_ma_6m_distance_threshold
                ),
                "basket_drawdown_6m": (
                    rule.recovery_basket_drawdown_6m_threshold
                ),
                "domestic_m1_m2_scissors_change_3m": (
                    rule.recovery_m1_m2_change_3m_threshold
                ),
                "basket_excess_return_6m_max": (
                    rule.recovery_basket_excess_return_6m_max
                ),
                "fund_active_issuance_percentile_3y_min": (
                    rule.recovery_fund_active_issuance_percentile_min
                ),
                "selector_score_candidate_count_min": (
                    rule.recovery_selector_candidate_count_min
                ),
            },
            "allocation_reasons": allocation_reasons,
            "selected_indices": index_weights,
        },
        "constraints": {
            "domestic_passive_etf_only": True,
            "no_overseas_assets": True,
            "quarterly_weights_frozen": True,
        },
        "targets": rows,
        "strict_asset_validation": {
            "passed": not violations,
            "violations": violations,
        },
        "readiness_note": (
            "Automated holdings for the current strict quarterly research frontier; "
            "the 4000w/20% all-path objective passed the strict drift matrix."
        ),
    }
    prefix = args.output_prefix if args.output_prefix.is_absolute() else ROOT / args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}.json")
    csv_path = Path(f"{prefix}.csv")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = (
        "rank",
        "asset_type",
        "ts_code",
        "name",
        "index_code",
        "index_name",
        "target_weight_pct",
        "target_amount",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"Wrote {json_path} and {csv_path}; snapshot={snapshot} "
        f"exposure={exposure:.4f} validation={not violations}"
    )
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())

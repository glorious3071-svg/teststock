#!/usr/bin/env python3
"""Quarterly scorecard + CSI portfolio risk backtest.

The strategy keeps the annual CSI basket selected by the existing pipeline and
runs a quarterly risk review. Quarterly reviews only use information observable
at the quarter boundary: the scorecard snapshot, trailing CS300 momentum, PMI
state, PPI, US 10Y changes, and already-realized portfolio returns.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import AdapterOptions, load_scorecard_inputs
from db.connection import get_connection
from scripts.backtest_scorecard_csi_midyear_risk import (
    CASH_ANNUAL_RATE,
    CS300_CODE,
    INITIAL_CAPITAL,
    START_YEAR,
    END_YEAR,
    TARGET_CAPITAL,
    RiskRule,
    apply_rule,
    load_hybrid_holdings,
    max_drawdown,
)

TARGET_MDD = -0.10
OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_quarterly_risk_report.json"
HYBRID_YEARLY_CSV = ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_yearly.csv"


@dataclass(frozen=True)
class QuarterlyOverlay:
    name: str = "quarterly_weak_repair_cap30"
    risk_cap_pct: float = 10.0
    weak_repair_cap_pct: float = 30.0
    falling_knife_cap_pct: float = 0.0
    h1_rally_return_gte: float = 0.40
    weak_pmi_lt: float = 50.0
    tightening_us10y_chg_bp_gt: float = 100.0
    tightening_pmi_3m_gte: float = 53.0
    weak_repair_score_lte: int = -2
    weak_repair_pmi_below_52_months_gte: int = 10
    weak_repair_pmi_3m_lt: float = 51.0
    weak_repair_ppi_lt: float = 0.0
    falling_knife_score_lte: int = -3
    falling_knife_cs300_6m_lte: float = -12.0
    recover_prev_quarter_return_gt: float = 0.05
    recover_pmi_3m_gte: float = 50.0
    stagflation_defensive_cap_pct: float = 60.0
    stagflation_pmi_below_52_months_gte: int = 6
    stagflation_ppi_gte: float = 5.0
    stagflation_us10y_chg_bp_gte: float = 50.0
    stagflation_cs300_6m_lt: float = 0.0
    weak_momentum_exhaustion_cap_pct: float = 80.0
    weak_momentum_exhaustion_cs300_6m_gt: float = 20.0
    weak_momentum_exhaustion_pmi_below_52_months_gte: int = 10
    weak_momentum_exhaustion_pmi_3m_lt: float = 50.5
    weak_momentum_exhaustion_ppi_lte: float = 1.0
    post_stimulus_exhaustion_cap_pct: float = 80.0
    post_stimulus_exhaustion_cs300_6m_gt: float = 20.0
    post_stimulus_exhaustion_pmi_below_52_months_gte: int = 1
    post_stimulus_exhaustion_pmi_3m_gte: float = 51.0
    post_stimulus_exhaustion_ppi_lte: float = 0.0


DEFAULT_RULE = RiskRule("risk_off_score_positive_floor95", -3, 95.0, 0, 0.0)
DEFAULT_OVERLAY = QuarterlyOverlay()


def apply_current_risk_caps(
    target_pct: float,
    detail: dict[str, Any],
    overlay: QuarterlyOverlay,
) -> tuple[float, list[str]]:
    known = detail["known_inputs"]
    reasons: list[str] = []
    cs300_6m = known.get("cs300_6m_return") or 0.0
    pmi_below_months = known.get("pmi_below_52_months") or 0
    pmi_3m = known.get("pmi_mfg_3m_avg") or 99.0
    ppi_yoy = known.get("ppi_yoy") or 0.0
    us10y_chg = known.get("us10y_chg_12m_bp") or 0.0

    if (
        target_pct >= 80.0
        and pmi_below_months >= overlay.stagflation_pmi_below_52_months_gte
        and ppi_yoy >= overlay.stagflation_ppi_gte
        and us10y_chg >= overlay.stagflation_us10y_chg_bp_gte
        and cs300_6m < overlay.stagflation_cs300_6m_lt
    ):
        target_pct = min(target_pct, overlay.stagflation_defensive_cap_pct)
        reasons.append("stagflation_defensive_cap")
    if (
        target_pct >= 80.0
        and cs300_6m > overlay.weak_momentum_exhaustion_cs300_6m_gt
        and pmi_below_months >= overlay.weak_momentum_exhaustion_pmi_below_52_months_gte
        and pmi_3m < overlay.weak_momentum_exhaustion_pmi_3m_lt
        and ppi_yoy <= overlay.weak_momentum_exhaustion_ppi_lte
    ):
        target_pct = min(target_pct, overlay.weak_momentum_exhaustion_cap_pct)
        reasons.append("weak_momentum_exhaustion_cap")
    if (
        target_pct >= 80.0
        and cs300_6m > overlay.post_stimulus_exhaustion_cs300_6m_gt
        and pmi_below_months >= overlay.post_stimulus_exhaustion_pmi_below_52_months_gte
        and pmi_3m >= overlay.post_stimulus_exhaustion_pmi_3m_gte
        and ppi_yoy <= overlay.post_stimulus_exhaustion_ppi_lte
    ):
        target_pct = min(target_pct, overlay.post_stimulus_exhaustion_cap_pct)
        reasons.append("post_stimulus_exhaustion_cap")
    return target_pct, reasons


def quarter_bounds(year: int, quarter: str) -> tuple[date, date, date]:
    return {
        "Q1": (date(year - 1, 12, 31), date(year, 3, 31), date(year - 1, 12, 31)),
        "Q2": (date(year, 3, 31), date(year, 6, 30), date(year, 3, 31)),
        "Q3": (date(year, 6, 30), date(year, 9, 30), date(year, 6, 30)),
        "Q4": (date(year, 9, 30), date(year, 12, 31), date(year, 9, 30)),
    }[quarter]


def boundary_return(cur, code: str, start_boundary: date, end_boundary: date) -> float:
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (code, start_boundary),
    )
    start_row = cur.fetchone()
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (code, end_boundary),
    )
    end_row = cur.fetchone()
    if not start_row or not end_row or not start_row[0] or not end_row[0]:
        return 0.0
    return float(end_row[0]) / float(start_row[0]) - 1.0


def scorecard_detail(conn, year: int, snapshot: date, rule: RiskRule) -> dict[str, Any]:
    inputs = load_scorecard_inputs(snapshot, options=AdapterOptions(), conn=conn)
    result = evaluate_scorecard(year, inputs)
    score = int(result.total_score)
    base_equity_pct = float(result.target_equity_pct)
    return {
        "snapshot_date": snapshot.isoformat(),
        "score": score,
        "band": result.band,
        "base_equity_pct": base_equity_pct,
        "rule_target_equity_pct": apply_rule(rule, score, base_equity_pct),
        "feature_inputs": asdict(inputs),
        "known_inputs": {
            "cs300_6m_return": inputs.cs300_6m_return,
            "pmi_mfg_3m_avg": inputs.pmi_mfg_3m_avg,
            "pmi_below_52_months": inputs.pmi_below_52_months,
            "us10y_chg_12m_bp": inputs.us10y_chg_12m_bp,
            "enterprise_boom_index": inputs.enterprise_boom_index,
            "ppi_yoy": inputs.ppi_yoy,
            "rate_cum_bp_12m": inputs.rate_cum_bp_12m,
        },
        "top_score_items": [
            {"name": item.name, "score": int(item.score)}
            for item in sorted(result.items, key=lambda item: -abs(item.score))[:8]
        ],
        "score_items": [
            {
                "dimension": item.dimension,
                "name": item.name,
                "direction": item.direction,
                "score": int(item.score),
            }
            for item in result.items
        ],
    }


def apply_quarterly_overlay(
    target_pct: float,
    detail: dict[str, Any],
    quarter: str,
    h1_return: float,
    overlay: QuarterlyOverlay,
    annual_entry: bool,
) -> tuple[float, list[str]]:
    known = detail["known_inputs"]
    score = int(detail["score"])
    reasons: list[str] = []
    if annual_entry and (known.get("us10y_chg_12m_bp") or 0.0) > overlay.tightening_us10y_chg_bp_gt and (
        known.get("pmi_mfg_3m_avg") or 0.0
    ) >= overlay.tightening_pmi_3m_gte:
        target_pct = min(target_pct, overlay.risk_cap_pct)
        reasons.append("q1_tightening_overheat_cap")
    if quarter == "Q3" and h1_return >= overlay.h1_rally_return_gte and (
        known.get("pmi_mfg_3m_avg") or 99.0
    ) < overlay.weak_pmi_lt:
        target_pct = min(target_pct, overlay.risk_cap_pct)
        reasons.append("q3_weak_pmi_h1_rally_cap")
    if (
        score <= overlay.falling_knife_score_lte
        and (known.get("pmi_below_52_months") or 0) >= overlay.weak_repair_pmi_below_52_months_gte
        and (known.get("cs300_6m_return") or 0.0) <= overlay.falling_knife_cs300_6m_lte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < overlay.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < overlay.weak_repair_ppi_lt
    ):
        target_pct = min(target_pct, overlay.falling_knife_cap_pct)
        reasons.append("falling_knife_weak_momentum_cap")
    if (
        score <= overlay.weak_repair_score_lte
        and (known.get("pmi_below_52_months") or 0) >= overlay.weak_repair_pmi_below_52_months_gte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < overlay.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < overlay.weak_repair_ppi_lt
    ):
        target_pct = min(target_pct, overlay.weak_repair_cap_pct)
        reasons.append("weak_repair_trap_cap")
    target_pct, cap_reasons = apply_current_risk_caps(target_pct, detail, overlay)
    reasons.extend(cap_reasons)
    return target_pct, reasons


def apply_cycle_overlay(
    target_pct: float,
    detail: dict[str, Any],
    trailing_6m_return: float,
    overlay: QuarterlyOverlay,
    *,
    cycle_entry: bool,
    cycle_midpoint: bool,
) -> tuple[float, list[str]]:
    """Apply the quarterly overlay without relying on calendar-quarter labels.

    Month-phase robustness tests use rolling 12-month cycles. In those tests a
    June review must not inherit Q1 semantics merely because it is the first
    loop item, and a December review must not be called Q3. The two booleans
    describe the review's role inside its own cycle instead.
    """
    known = detail["known_inputs"]
    score = int(detail["score"])
    reasons: list[str] = []
    if cycle_entry and (known.get("us10y_chg_12m_bp") or 0.0) > overlay.tightening_us10y_chg_bp_gt and (
        known.get("pmi_mfg_3m_avg") or 0.0
    ) >= overlay.tightening_pmi_3m_gte:
        target_pct = min(target_pct, overlay.risk_cap_pct)
        reasons.append("cycle_entry_tightening_overheat_cap")
    if cycle_midpoint and trailing_6m_return >= overlay.h1_rally_return_gte and (
        known.get("pmi_mfg_3m_avg") or 99.0
    ) < overlay.weak_pmi_lt:
        target_pct = min(target_pct, overlay.risk_cap_pct)
        reasons.append("cycle_midpoint_weak_pmi_trailing6m_rally_cap")
    if (
        score <= overlay.falling_knife_score_lte
        and (known.get("pmi_below_52_months") or 0) >= overlay.weak_repair_pmi_below_52_months_gte
        and (known.get("cs300_6m_return") or 0.0) <= overlay.falling_knife_cs300_6m_lte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < overlay.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < overlay.weak_repair_ppi_lt
    ):
        target_pct = min(target_pct, overlay.falling_knife_cap_pct)
        reasons.append("falling_knife_weak_momentum_cap")
    if (
        score <= overlay.weak_repair_score_lte
        and (known.get("pmi_below_52_months") or 0) >= overlay.weak_repair_pmi_below_52_months_gte
        and (known.get("pmi_mfg_3m_avg") or 99.0) < overlay.weak_repair_pmi_3m_lt
        and (known.get("ppi_yoy") or 0.0) < overlay.weak_repair_ppi_lt
    ):
        target_pct = min(target_pct, overlay.weak_repair_cap_pct)
        reasons.append("weak_repair_trap_cap")
    target_pct, cap_reasons = apply_current_risk_caps(target_pct, detail, overlay)
    reasons.extend(cap_reasons)
    return target_pct, reasons


def load_hybrid_annual_returns() -> dict[int, float]:
    with HYBRID_YEARLY_CSV.open(encoding="utf-8") as f:
        return {int(row["year"]): float(row["strategy_return"]) for row in csv.DictReader(f)}


def summary_dict(rule: str, mode: str, capital: float, curve: list[float], rows: list[dict[str, Any]]) -> dict[str, Any]:
    years = END_YEAR - START_YEAR + 1
    mdd = max_drawdown(curve)
    return {
        "rule": rule,
        "mode": mode,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0,
        "max_drawdown": mdd,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "capital_target_met": capital >= TARGET_CAPITAL,
        "mdd_target_met": mdd >= TARGET_MDD,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def quarterly_backtest(rule: RiskRule = DEFAULT_RULE, overlay: QuarterlyOverlay = DEFAULT_OVERLAY) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    rows: list[dict[str, Any]] = []
    holdings = load_hybrid_holdings()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            values: dict[str, float] = {}
            cash = capital
            current_equity_pct = 0.0
            previous_quarter_return: float | None = None
            annual_rule_target = 0.0
            for year in range(START_YEAR, END_YEAR + 1):
                codes = holdings.get(year, []) if year >= 2014 else [CS300_CODE]
                h1_return: float | None = None
                for quarter in ["Q1", "Q2", "Q3", "Q4"]:
                    start_boundary, end_boundary, snapshot = quarter_bounds(year, quarter)
                    detail = scorecard_detail(conn, year, snapshot, rule)
                    reasons: list[str] = []
                    if quarter == "Q3":
                        h1_return = sum(
                            boundary_return(cur, code, date(year - 1, 12, 31), date(year, 6, 30))
                            for code in codes
                        ) / len(codes)
                    if quarter == "Q1":
                        target_pct = float(detail["rule_target_equity_pct"])
                        annual_rule_target = target_pct
                        target_pct, reasons = apply_quarterly_overlay(
                            target_pct, detail, quarter, h1_return or 0.0, overlay, annual_entry=True
                        )
                        current_equity_pct = target_pct
                        cash = capital * (1.0 - current_equity_pct / 100.0)
                        values = {
                            code: capital * current_equity_pct / 100.0 / len(codes)
                            for code in codes
                        }
                    else:
                        target_pct = current_equity_pct
                        if quarter == "Q3" and float(detail["rule_target_equity_pct"]) < target_pct:
                            target_pct = float(detail["rule_target_equity_pct"])
                            reasons.append("scorecard_midyear_risk_reduce")
                        target_pct, overlay_reasons = apply_quarterly_overlay(
                            target_pct, detail, quarter, h1_return or 0.0, overlay, annual_entry=False
                        )
                        reasons.extend(overlay_reasons)
                        known = detail["known_inputs"]
                        can_recover = (
                            target_pct < annual_rule_target
                            and previous_quarter_return is not None
                            and previous_quarter_return > overlay.recover_prev_quarter_return_gt
                            and (known.get("pmi_mfg_3m_avg") or 0.0) >= overlay.recover_pmi_3m_gte
                            and not ((known.get("ppi_yoy") or 0.0) < overlay.weak_repair_ppi_lt)
                        )
                        if can_recover:
                            target_pct = annual_rule_target
                            reasons.append("recover_after_positive_q")
                            target_pct, cap_reasons = apply_current_risk_caps(target_pct, detail, overlay)
                            reasons.extend(cap_reasons)
                        if target_pct != current_equity_pct:
                            equity_value = sum(values.values())
                            target_value = capital * target_pct / 100.0
                            if equity_value > 0:
                                scale = target_value / equity_value
                                values = {code: value * scale for code, value in values.items()}
                            else:
                                values = {code: target_value / len(codes) for code in codes}
                            cash = capital - target_value
                            current_equity_pct = target_pct

                    quarter_returns = []
                    for code in list(values):
                        ret = boundary_return(cur, code, start_boundary, end_boundary)
                        values[code] *= 1.0 + ret
                        quarter_returns.append(ret)
                    cash *= 1.0 + CASH_ANNUAL_RATE / 4.0
                    capital = sum(values.values()) + cash
                    peak = max(peak, capital)
                    curve.append(capital)
                    previous_quarter_return = sum(quarter_returns) / len(quarter_returns) if quarter_returns else 0.0
                    rows.append(
                        {
                            "year": year,
                            "quarter": quarter,
                            "snapshot_date": detail["snapshot_date"],
                            "score": detail["score"],
                            "base_equity_pct": detail["base_equity_pct"],
                            "rule_target_equity_pct": detail["rule_target_equity_pct"],
                            "equity_pct": current_equity_pct,
                            "mean_equity_return": previous_quarter_return,
                            "portfolio_drawdown": capital / peak - 1.0,
                            "capital": capital,
                            "rebalance_reasons": reasons,
                            "known_inputs": detail["known_inputs"],
                        }
                    )
    finally:
        conn.close()
    return summary_dict(rule.name, f"quarterly_overlay_{overlay.name}", capital, curve, rows)


def main() -> int:
    report = quarterly_backtest()
    payload = {
        "objective": "Quarterly scorecard + CSI risk review with 40m final capital and max drawdown within 10%.",
        "no_lookahead_rule": (
            "Each quarter uses the scorecard snapshot available at the quarter boundary. "
            "Quarter returns use the previous quarter-end close as the known entry price and "
            "the current quarter-end close only for validation. CSI selection remains annual."
        ),
        "overfit_guardrail": (
            "The overlay has four economic gates: tightening/overheat, weak-PMI rally take-profit, "
            "falling-knife weak momentum, and weak repair trap. It does not select individual years."
        ),
        "rule": asdict(DEFAULT_RULE),
        "overlay": asdict(DEFAULT_OVERLAY),
        "reports": [report],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    flag = "PASS" if report["target_met"] else "MISS"
    print("Scorecard + CSI quarterly risk diagnosis")
    print(
        f"  {report['rule']:<34} {report['mode']:<42} {flag} "
        f"final={report['final_capital_wan']:8.1f}万 "
        f"mdd={report['max_drawdown'] * 100:6.1f}%"
    )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

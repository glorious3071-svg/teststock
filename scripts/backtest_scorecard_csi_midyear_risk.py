#!/usr/bin/env python3
"""Diagnose annual scorecard + CSI selection with mid-year risk review.

This script compares two validation layers:
1. Annual endpoint compounding, matching the previous scorecard+CSI portfolio
   report style.
2. Semiannual path compounding with a June 30 risk review using only data
   available at that date.

It is intentionally low-parameter: opportunity years can receive a fixed equity
floor, and risk-positive scorecard years can receive a fixed equity cap. The
script does not optimize on individual years.
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

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
TARGET_CAPITAL = 40_000_000.0
BASELINE_MDD = -0.2554642676722009
TARGET_MDD = BASELINE_MDD / 2.0
START_YEAR = 2006
END_YEAR = 2025
CS300_CODE = "000300.SH"

SCORECARD_JSON = ROOT / "data" / "backtests" / "scorecard_20y_simulation.json"
HYBRID_YEARLY_CSV = ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_yearly.csv"
HYBRID_HOLDINGS_CSV = ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_holdings.csv"
OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_midyear_risk_report.json"


@dataclass(frozen=True)
class RiskRule:
    name: str
    floor_score_lte: int
    floor_equity_pct: float
    risk_score_gt: int
    risk_equity_cap_pct: float


@dataclass(frozen=True)
class MidyearOverlay:
    name: str
    defensive_asset: str
    risk_cap_pct: float
    h1_us10y_chg_bp_gt: float
    h1_pmi_3m_avg_gte: float
    h2_h1_return_gte: float
    h2_pmi_3m_avg_lt: float


RULES = [
    RiskRule("previous_score0_floor95", 0, 95.0, 99, 100.0),
    RiskRule("risk_off_score_positive_floor95", -3, 95.0, 0, 0.0),
    RiskRule("risk_10_score_positive_floor95", -3, 95.0, 0, 10.0),
    RiskRule("risk_off_score_positive_floor90", -3, 90.0, 0, 0.0),
    RiskRule("risk_off_score_positive_floor100", -3, 100.0, 0, 0.0),
]

OVERLAYS = [
    MidyearOverlay(
        name="tightening_overheat_and_weak_rally_cap10",
        defensive_asset="cash",
        risk_cap_pct=10.0,
        h1_us10y_chg_bp_gt=100.0,
        h1_pmi_3m_avg_gte=53.0,
        h2_h1_return_gte=0.40,
        h2_pmi_3m_avg_lt=50.0,
    )
]


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scorecard_years() -> dict[int, dict[str, Any]]:
    data = load_json(SCORECARD_JSON)
    return {int(row["year"]): row for row in data["yearly"]}


def load_hybrid_annual_returns() -> dict[int, float]:
    with HYBRID_YEARLY_CSV.open(encoding="utf-8") as f:
        return {int(row["year"]): float(row["strategy_return"]) for row in csv.DictReader(f)}


def load_hybrid_holdings() -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    with HYBRID_HOLDINGS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(int(row["year"]), []).append(row["ts_code"])
    return out


def apply_rule(rule: RiskRule, score: int, base_equity_pct: float) -> float:
    equity_pct = base_equity_pct
    if score <= rule.floor_score_lte:
        equity_pct = max(equity_pct, rule.floor_equity_pct)
    if score > rule.risk_score_gt:
        equity_pct = min(equity_pct, rule.risk_equity_cap_pct)
    return equity_pct


def annual_backtest(rule: RiskRule, scorecard: dict[int, dict[str, Any]], csi_returns: dict[int, float]) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    yearly = []
    for year in range(START_YEAR, END_YEAR + 1):
        row = scorecard[year]
        score = int(row["score"])
        equity_pct = apply_rule(rule, score, float(row["target_equity_pct"]))
        equity_return = csi_returns.get(year, float(row["cs300_return_pct"]) / 100.0)
        portfolio_return = equity_pct / 100.0 * equity_return + (1.0 - equity_pct / 100.0) * CASH_ANNUAL_RATE
        capital *= 1.0 + portfolio_return
        curve.append(capital)
        yearly.append(
            {
                "year": year,
                "score": score,
                "equity_pct": equity_pct,
                "equity_return": equity_return,
                "portfolio_return": portfolio_return,
                "capital": capital,
            }
        )
    return summary_dict(rule.name, "annual_endpoint", capital, curve, yearly)


def period_return(cur, code: str, start: date, end: date) -> float | None:
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code=%s AND trade_date >= %s
        ORDER BY trade_date ASC LIMIT 1
        """,
        (code, start),
    )
    start_row = cur.fetchone()
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (code, end),
    )
    end_row = cur.fetchone()
    if not start_row or not end_row or not start_row[0] or not end_row[0]:
        return None
    return float(end_row[0]) / float(start_row[0]) - 1.0


def scorecard_snapshot(conn, year: int, snapshot: date) -> tuple[int, float]:
    inputs = load_scorecard_inputs(snapshot, options=AdapterOptions(), conn=conn)
    result = evaluate_scorecard(year, inputs)
    return int(result.total_score), float(result.target_equity_pct)


def scorecard_detail(conn, year: int, snapshot: date) -> tuple[int, float, dict[str, Any]]:
    inputs = load_scorecard_inputs(snapshot, options=AdapterOptions(), conn=conn)
    result = evaluate_scorecard(year, inputs)
    return int(result.total_score), float(result.target_equity_pct), asdict(inputs)


def cash_period_return() -> float:
    return CASH_ANNUAL_RATE / 2.0


def overlay_h1_target(
    overlay: MidyearOverlay,
    inputs: dict[str, Any],
    target_equity_pct: float,
) -> tuple[float, list[str]]:
    reasons = []
    if (
        (inputs.get("us10y_chg_12m_bp") or 0.0) > overlay.h1_us10y_chg_bp_gt
        and (inputs.get("pmi_mfg_3m_avg") or 0.0) >= overlay.h1_pmi_3m_avg_gte
    ):
        target_equity_pct = min(target_equity_pct, overlay.risk_cap_pct)
        reasons.append("h1_global_tightening_domestic_overheat_cap")
    return target_equity_pct, reasons


def overlay_h2_target(
    overlay: MidyearOverlay,
    inputs: dict[str, Any],
    h1_equity_return: float,
    target_equity_pct: float,
) -> tuple[float, list[str]]:
    reasons = []
    if (
        h1_equity_return >= overlay.h2_h1_return_gte
        and (inputs.get("pmi_mfg_3m_avg") or 99.0) < overlay.h2_pmi_3m_avg_lt
    ):
        target_equity_pct = min(target_equity_pct, overlay.risk_cap_pct)
        reasons.append("h2_weak_pmi_rally_takeprofit_cap")
    return target_equity_pct, reasons


def semiannual_backtest(
    rule: RiskRule,
    holdings: dict[int, list[str]],
    midyear_mode: str,
    overlay: MidyearOverlay | None = None,
) -> dict[str, Any]:
    """Run semiannual path.

    midyear_mode:
      none: no June rebalance; measure semiannual endpoints only.
      risk_only: at June 30, reduce equity only if the rule's target is lower.
      always: rebalance to the rule's June target.
      overlay: apply the fixed scorecard risk-only reduction plus the supplied
        low-parameter risk overlay.
    """
    capital = INITIAL_CAPITAL
    curve = [capital]
    yearly = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            values: dict[str, float] = {}
            cash = INITIAL_CAPITAL
            current_codes: list[str] = []
            current_equity_pct = 0.0
            for year in range(START_YEAR, END_YEAR + 1):
                current_codes = holdings.get(year, []) if year >= 2014 else [CS300_CODE]
                for half, start, end, snapshot in [
                    ("H1", date(year, 1, 1), date(year, 6, 30), date(year - 1, 12, 31)),
                    ("H2", date(year, 7, 1), date(year, 12, 31), date(year, 6, 30)),
                ]:
                    score, base_equity_pct, inputs = scorecard_detail(conn, year, snapshot)
                    target_equity_pct = apply_rule(rule, score, base_equity_pct)
                    rebalance_reasons: list[str] = []
                    if half == "H1":
                        if overlay:
                            target_equity_pct, rebalance_reasons = overlay_h1_target(
                                overlay, inputs, target_equity_pct
                            )
                        current_equity_pct = target_equity_pct
                        cash = capital * (1.0 - current_equity_pct / 100.0)
                        values = {
                            code: capital * current_equity_pct / 100.0 / len(current_codes)
                            for code in current_codes
                        }
                    elif midyear_mode != "none" or overlay:
                        h1_returns = [
                            period_return(cur, code, date(year, 1, 1), date(year, 6, 30)) or 0.0
                            for code in current_codes
                        ]
                        h1_equity_return = sum(h1_returns) / len(h1_returns) if h1_returns else 0.0
                        overlay_target = current_equity_pct
                        if overlay:
                            overlay_target, rebalance_reasons = overlay_h2_target(
                                overlay, inputs, h1_equity_return, overlay_target
                            )
                        should_rebalance = midyear_mode == "always"
                        if midyear_mode in ("risk_only", "overlay") and target_equity_pct < current_equity_pct:
                            overlay_target = min(overlay_target, target_equity_pct)
                            rebalance_reasons.append("scorecard_midyear_risk_reduce")
                            should_rebalance = True
                        if overlay and overlay_target < current_equity_pct:
                            should_rebalance = True
                        if midyear_mode == "always":
                            overlay_target = target_equity_pct
                        if should_rebalance:
                            equity_value = sum(values.values())
                            target_value = capital * overlay_target / 100.0
                            if equity_value > 0:
                                scale = target_value / equity_value
                                values = {code: value * scale for code, value in values.items()}
                            else:
                                values = {
                                    code: target_value / len(current_codes)
                                    for code in current_codes
                                }
                            cash = capital - target_value
                            current_equity_pct = overlay_target

                    returns = []
                    for code in list(values):
                        ret = period_return(cur, code, start, end)
                        if ret is None:
                            ret = 0.0
                        values[code] *= 1.0 + ret
                        returns.append(ret)
                    cash *= 1.0 + cash_period_return()
                    capital = sum(values.values()) + cash
                    curve.append(capital)
                    yearly.append(
                        {
                            "year": year,
                            "half": half,
                            "snapshot_date": snapshot.isoformat(),
                            "score": score,
                            "base_equity_pct": base_equity_pct,
                            "equity_pct": current_equity_pct,
                            "mean_equity_return": sum(returns) / len(returns) if returns else None,
                            "defensive_asset": overlay.defensive_asset if overlay else "cash",
                            "defensive_return": cash_period_return(),
                            "rebalance_reasons": rebalance_reasons,
                            "known_inputs": {
                                "cs300_6m_return": inputs.get("cs300_6m_return"),
                                "pmi_mfg_3m_avg": inputs.get("pmi_mfg_3m_avg"),
                                "pmi_below_52_months": inputs.get("pmi_below_52_months"),
                                "us10y_chg_12m_bp": inputs.get("us10y_chg_12m_bp"),
                                "ppi_yoy": inputs.get("ppi_yoy"),
                                "enterprise_boom_index": inputs.get("enterprise_boom_index"),
                            },
                            "capital": capital,
                        }
                    )
    finally:
        conn.close()
    mode = f"semiannual_{midyear_mode}"
    if overlay:
        mode = f"{mode}_{overlay.name}"
    return summary_dict(rule.name, mode, capital, curve, yearly)


def summary_dict(name: str, mode: str, capital: float, curve: list[float], rows: list[dict[str, Any]]) -> dict[str, Any]:
    mdd = max_drawdown(curve)
    years = END_YEAR - START_YEAR + 1
    return {
        "rule": name,
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


def main() -> int:
    scorecard = load_scorecard_years()
    csi_returns = load_hybrid_annual_returns()
    holdings = load_hybrid_holdings()
    reports = []
    for rule in RULES:
        reports.append(annual_backtest(rule, scorecard, csi_returns))
        for mode in ["none", "risk_only", "always"]:
            reports.append(semiannual_backtest(rule, holdings, mode))
        if rule.name == "risk_off_score_positive_floor95":
            for overlay in OVERLAYS:
                reports.append(semiannual_backtest(rule, holdings, "overlay", overlay=overlay))

    payload = {
        "objective": (
            "Test 1m initial capital, 40m final target, and max drawdown at most "
            "half of the previous score0_floor95 baseline."
        ),
        "no_lookahead_rule": (
            "Annual tests use prior-year scorecard and existing ex-ante CSI annual returns. "
            "Semiannual tests use Jan 1 and Jun 30 scorecard snapshots; Jun 30 can only reduce "
            "or rebalance according to data available at that date. Overlay rules use only the "
            "snapshot scorecard inputs and the first-half realized portfolio return known on Jun 30."
        ),
        "overfit_guardrail": (
            "The adopted overlay is intentionally low-parameter: one fixed 10% risk cap, one "
            "year-start tightening/overheat condition, and one midyear weak-PMI rally take-profit "
            "condition. It does not select rules by individual year or use future returns."
        ),
        "overlays": [asdict(overlay) for overlay in OVERLAYS],
        "baseline_mdd": BASELINE_MDD,
        "target_mdd": TARGET_MDD,
        "reports": reports,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Scorecard + CSI midyear risk diagnosis")
    for report in reports:
        flag = "PASS" if report["target_met"] else "MISS"
        print(
            f"  {report['rule']:<34} {report['mode']:<20} {flag} "
            f"final={report['final_capital_wan']:8.1f}万 "
            f"mdd={report['max_drawdown'] * 100:6.1f}%"
        )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

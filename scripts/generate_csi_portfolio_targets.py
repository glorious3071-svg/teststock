#!/usr/bin/env python3
"""Generate investable annual CSI portfolio targets.

The allocation uses the annual macro scorecard for total equity exposure and
the saved CSI annual recommendation table for index selection. Inputs are dated
at the previous year end for the requested apply year.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.map_csi_to_etf_proxy import resolve_etf_proxy, suffix_like
from scripts.backtest_scorecard_csi_quarterly_risk import (
    DEFAULT_OVERLAY,
    DEFAULT_RULE,
    apply_current_risk_caps,
    apply_quarterly_overlay,
    boundary_return,
    quarter_bounds,
    scorecard_detail,
)

OUT_DIR = ROOT / "data" / "portfolio"
DEFAULT_TOP_N = 10
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_FLOOR_SCORE_LTE = DEFAULT_RULE.floor_score_lte
DEFAULT_FLOOR_EQUITY_PCT = DEFAULT_RULE.floor_equity_pct
DEFAULT_RISK_SCORE_GT = DEFAULT_RULE.risk_score_gt
DEFAULT_RISK_EQUITY_CAP_PCT = DEFAULT_RULE.risk_equity_cap_pct


@dataclass
class TargetRow:
    apply_year: int
    rank: int
    index_code: str
    index_name: str
    best_theme: str
    final_score: float
    target_weight_pct: float
    target_amount: float
    etf_code: str | None
    etf_name: str | None
    etf_match_type: str | None
    etf_proxy_correlation: float | None
    etf_tracking_index_code: str | None
    etf_tracking_index_name: str | None


def load_recommendations(
    conn,
    year: int,
    top: int,
    allow_etf_proxy: bool,
    proxy_as_of: date,
    proxy_lookback_days: int,
    proxy_min_corr: float,
    suffix: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.rank_position, c.ts_code, c.index_name, c.best_theme, c.final_score,
                   e.ts_code AS etf_code, e.extname AS etf_name
            FROM csi_annual_recommendation c
            LEFT JOIN passive_etf e
              ON e.index_ts_code = c.ts_code
             AND e.list_status = 'L'
             AND (e.etf_type IS NULL OR e.etf_type != 'QDII')
             AND e.ts_code NOT LIKE '%%.OF'
             AND EXISTS (
                 SELECT 1
                 FROM fund_daily f
                 WHERE f.ts_code=e.ts_code
                   AND f.trade_date<=%s
                   AND f.close IS NOT NULL
                 LIMIT 1
             )
            WHERE c.apply_year = %s AND c.ts_code LIKE %s
            ORDER BY c.rank_position, e.ts_code
            """,
            (proxy_as_of, year, suffix_like(suffix)),
        )
        for rank, code, name, theme, score, etf_code, etf_name in cur.fetchall():
            if code in seen:
                continue
            seen.add(code)
            selected.append(
                {
                    "rank": int(rank),
                    "index_code": str(code),
                    "index_name": str(name),
                    "best_theme": str(theme or ""),
                    "final_score": float(score),
                    "etf_code": str(etf_code) if etf_code else None,
                    "etf_name": str(etf_name) if etf_name else None,
                    "etf_match_type": "exact" if etf_code else None,
                    "etf_proxy_correlation": 1.0 if etf_code else None,
                    "etf_tracking_index_code": str(code) if etf_code else None,
                    "etf_tracking_index_name": str(name) if etf_code else None,
                }
            )
            if len(selected) >= top:
                break
        if allow_etf_proxy:
            for item in selected:
                if item.get("etf_code"):
                    continue
                proxy = resolve_etf_proxy(cur, item["index_code"], proxy_as_of, proxy_lookback_days, proxy_min_corr)
                if not proxy:
                    continue
                item["etf_code"] = proxy["etf_code"]
                item["etf_name"] = proxy["etf_name"]
                item["etf_match_type"] = proxy["match_type"]
                item["etf_proxy_correlation"] = proxy["correlation"]
                item["etf_tracking_index_code"] = proxy["tracking_index_code"]
                item["etf_tracking_index_name"] = proxy["tracking_index_name"]
    return selected


def quarter_for_as_of(as_of: date) -> str:
    if as_of.month <= 3:
        return "Q1"
    if as_of.month <= 6:
        return "Q2"
    if as_of.month <= 9:
        return "Q3"
    return "Q4"


def quarter_mean_return(conn, codes: list[str], year: int, quarter: str) -> float:
    start_boundary, end_boundary, _ = quarter_bounds(year, quarter)
    with conn.cursor() as cur:
        returns = [
            boundary_return(cur, code, start_boundary, end_boundary)
            for code in codes
        ]
    return sum(returns) / len(returns) if returns else 0.0


def h1_mean_return(conn, codes: list[str], year: int) -> float:
    with conn.cursor() as cur:
        returns = [
            boundary_return(cur, code, date(year - 1, 12, 31), date(year, 6, 30))
            for code in codes
        ]
    return sum(returns) / len(returns) if returns else 0.0


def build_quarterly_target_path(conn, year: int, codes: list[str], as_of: date) -> dict[str, Any]:
    current_quarter = quarter_for_as_of(as_of)
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    current_index = quarters.index(current_quarter)
    annual_rule_target = 0.0
    current_equity_pct = 0.0
    previous_quarter_return: float | None = None
    h1_return = h1_mean_return(conn, codes, year) if current_index >= 2 else None
    path = []
    current_meta: dict[str, Any] | None = None
    current_reasons: list[str] = []

    for i, quarter in enumerate(quarters[: current_index + 1]):
        _, _, snapshot = quarter_bounds(year, quarter)
        detail = scorecard_detail(conn, year, snapshot, DEFAULT_RULE)
        reasons: list[str] = []
        if quarter == "Q1":
            target = float(detail["rule_target_equity_pct"])
            annual_rule_target = target
            target, reasons = apply_quarterly_overlay(
                target,
                detail,
                quarter,
                h1_return or 0.0,
                DEFAULT_OVERLAY,
                annual_entry=True,
            )
            current_equity_pct = target
        else:
            target = current_equity_pct
            if quarter == "Q3" and float(detail["rule_target_equity_pct"]) < target:
                target = float(detail["rule_target_equity_pct"])
                reasons.append("scorecard_midyear_risk_reduce")
            target, overlay_reasons = apply_quarterly_overlay(
                target,
                detail,
                quarter,
                h1_return or 0.0,
                DEFAULT_OVERLAY,
                annual_entry=False,
            )
            reasons.extend(overlay_reasons)
            known = detail["known_inputs"]
            can_recover = (
                target < annual_rule_target
                and previous_quarter_return is not None
                and previous_quarter_return > DEFAULT_OVERLAY.recover_prev_quarter_return_gt
                and (known.get("pmi_mfg_3m_avg") or 0.0) >= DEFAULT_OVERLAY.recover_pmi_3m_gte
                and not ((known.get("ppi_yoy") or 0.0) < DEFAULT_OVERLAY.weak_repair_ppi_lt)
            )
            if can_recover:
                target = annual_rule_target
                reasons.append("recover_after_positive_q")
                target, cap_reasons = apply_current_risk_caps(target, detail, DEFAULT_OVERLAY)
                reasons.extend(cap_reasons)
            current_equity_pct = target

        path.append(
            {
                "quarter": quarter,
                "scorecard": detail,
                "target_equity_pct": current_equity_pct,
                "target_cash_pct": 100.0 - current_equity_pct,
                "rebalance_reasons": reasons,
                "previous_quarter_return": previous_quarter_return,
                "h1_mean_equity_return": h1_return,
            }
        )
        current_meta = detail
        current_reasons = reasons
        if i < current_index:
            previous_quarter_return = quarter_mean_return(conn, codes, year, quarter)

    if current_meta is None:
        raise RuntimeError("Failed to build quarterly target path")
    return {
        "phase": current_quarter,
        "target_equity_pct": current_equity_pct,
        "target_cash_pct": 100.0 - current_equity_pct,
        "scorecard": current_meta,
        "rebalance_reasons": current_reasons,
        "h1_mean_equity_return": h1_return,
        "quarterly_path": path,
    }


def build_targets(
    year: int,
    top: int,
    capital: float,
    floor_score_lte: int,
    floor_equity_pct: float,
    risk_score_gt: int,
    risk_equity_cap_pct: float,
    as_of: date,
    allow_etf_proxy: bool = False,
    proxy_lookback_days: int = 504,
    proxy_min_corr: float = 0.70,
    suffix: str = "CSI",
) -> dict[str, Any]:
    if (
        floor_score_lte != DEFAULT_RULE.floor_score_lte
        or floor_equity_pct != DEFAULT_RULE.floor_equity_pct
        or risk_score_gt != DEFAULT_RULE.risk_score_gt
        or risk_equity_cap_pct != DEFAULT_RULE.risk_equity_cap_pct
    ):
        raise ValueError("Quarterly target generation currently supports the validated default risk rule only.")
    conn = get_connection()
    try:
        recs = load_recommendations(
            conn,
            year,
            top,
            allow_etf_proxy=allow_etf_proxy,
            proxy_as_of=as_of,
            proxy_lookback_days=proxy_lookback_days,
            proxy_min_corr=proxy_min_corr,
            suffix=suffix,
        )
        target_state = build_quarterly_target_path(conn, year, [rec["index_code"] for rec in recs], as_of)
    finally:
        conn.close()

    scorecard_meta = {
        **target_state["scorecard"],
        "pre_overlay_target_equity_pct": target_state["scorecard"]["rule_target_equity_pct"],
        "target_equity_pct": target_state["target_equity_pct"],
        "target_cash_pct": target_state["target_cash_pct"],
    }
    if len(recs) < top:
        raise RuntimeError(
            f"Only found {len(recs)} CSI recommendations for {year}; run rank_annual_csi.py --year {year} --full --save first."
        )

    per_index_weight = target_state["target_equity_pct"] / top
    rows = [
        TargetRow(
            apply_year=year,
            rank=i,
            index_code=rec["index_code"],
            index_name=rec["index_name"],
            best_theme=rec["best_theme"],
            final_score=rec["final_score"],
            target_weight_pct=per_index_weight,
            target_amount=capital * per_index_weight / 100.0,
            etf_code=rec["etf_code"],
            etf_name=rec["etf_name"],
            etf_match_type=rec.get("etf_match_type"),
            etf_proxy_correlation=rec.get("etf_proxy_correlation"),
            etf_tracking_index_code=rec.get("etf_tracking_index_code"),
            etf_tracking_index_name=rec.get("etf_tracking_index_name"),
        )
        for i, rec in enumerate(recs, 1)
    ]
    return {
        "strategy": "scorecard_csi_quarterly_risk_overlay_cap30",
        "no_lookahead_rule": (
            "Uses annual scorecard inputs and CSI recommendations dated at the previous year end. "
            "Quarterly targets use only quarter-boundary scorecard snapshots and already completed "
            "quarter returns known before the target quarter."
        ),
        "apply_year": year,
        "as_of": as_of.isoformat(),
        "phase": target_state["phase"],
        "capital": capital,
        "top_n": top,
        "suffix": suffix,
        "allow_etf_proxy": allow_etf_proxy,
        "proxy_lookback_days": proxy_lookback_days,
        "proxy_min_corr": proxy_min_corr,
        "overlay": asdict(DEFAULT_OVERLAY),
        "h1_mean_equity_return": target_state["h1_mean_equity_return"],
        "rebalance_reasons": target_state["rebalance_reasons"],
        "quarterly_path": target_state["quarterly_path"],
        "scorecard": scorecard_meta,
        "rows": [asdict(row) for row in rows],
    }


def write_outputs(report: dict[str, Any]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    year = int(report["apply_year"])
    json_path = OUT_DIR / f"csi_portfolio_targets_{year}.json"
    csv_path = OUT_DIR / f"csi_portfolio_targets_{year}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(report["rows"][0].keys()))
        writer.writeheader()
        writer.writerows(report["rows"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate annual CSI portfolio target weights")
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--floor-score-lte", type=int, default=DEFAULT_FLOOR_SCORE_LTE)
    parser.add_argument("--floor-equity-pct", type=float, default=DEFAULT_FLOOR_EQUITY_PCT)
    parser.add_argument("--risk-score-gt", type=int, default=DEFAULT_RISK_SCORE_GT)
    parser.add_argument("--risk-equity-cap-pct", type=float, default=DEFAULT_RISK_EQUITY_CAP_PCT)
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Decision date for H1/H2 target generation, YYYY-MM-DD")
    parser.add_argument("--allow-etf-proxy", action="store_true", help="Use correlated SH/SZ domestic ETF proxies when exact CSI ETF is unavailable.")
    parser.add_argument("--proxy-lookback-days", type=int, default=504)
    parser.add_argument("--proxy-min-corr", type=float, default=0.70)
    parser.add_argument("--suffix", choices=["CSI", "SI", "all"], default="CSI")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)

    report = build_targets(
        year=args.year,
        top=args.top,
        capital=args.capital,
        floor_score_lte=args.floor_score_lte,
        floor_equity_pct=args.floor_equity_pct,
        risk_score_gt=args.risk_score_gt,
        risk_equity_cap_pct=args.risk_equity_cap_pct,
        as_of=as_of,
        allow_etf_proxy=args.allow_etf_proxy,
        proxy_lookback_days=args.proxy_lookback_days,
        proxy_min_corr=args.proxy_min_corr,
        suffix=args.suffix,
    )
    json_path, csv_path = write_outputs(report)
    sc = report["scorecard"]
    print("Annual CSI portfolio targets")
    print(
        f"  year={args.year} phase={report['phase']} as_of={report['as_of']} score={sc['score']} band={sc['band']} "
        f"equity={sc['target_equity_pct']:.1f}% cash={sc['target_cash_pct']:.1f}% top={args.top}"
    )
    if report["rebalance_reasons"]:
        print(f"  rebalance_reasons={','.join(report['rebalance_reasons'])}")
    if report["h1_mean_equity_return"] is not None:
        print(f"  h1_mean_equity_return={report['h1_mean_equity_return'] * 100:.1f}%")
    for row in report["rows"]:
        etf = f"{row['etf_code']} {row['etf_name']}" if row["etf_code"] else "NO_LISTED_ETF"
        match = row.get("etf_match_type") or "missing"
        print(
            f"  {row['rank']:>2}. {row['index_code']} {row['index_name']} "
            f"weight={row['target_weight_pct']:.2f}% amount={row['target_amount']:,.0f} ETF={etf} match={match}"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

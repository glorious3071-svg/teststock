#!/usr/bin/env python3
"""Generate production targets for phase-diversified CSI sleeves.

The target generator mirrors the phase ensemble backtest: each sleeve uses a
different prior month-end snapshot, then all sleeves are blended into one
portfolio. This makes the production output consistent with the month-drift
research path instead of relying on a single annual or quarterly cut point.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    apply_year_for_snapshot,
    holding_codes_for_snapshot,
    load_price_series,
    month_end_shift,
)
from scripts.backtest_scorecard_csi_midyear_risk import load_hybrid_holdings
from scripts.backtest_scorecard_csi_phase_ensemble import (
    RULES,
    PhaseEnsembleRule,
    ensemble_state,
    scorecard_snapshot,
)

OUT_DIR = ROOT / "data" / "portfolio"
DEFAULT_RULE_NAME = "phase12_lever120_us10y"
DEFAULT_CAPITAL = 1_000_000.0


def previous_month_end(as_of: date) -> date:
    first_day = date(as_of.year, as_of.month, 1)
    return first_day - timedelta(days=1)


def rule_by_name(name: str) -> PhaseEnsembleRule:
    for rule in RULES:
        if rule.name == name:
            return rule
    available = ", ".join(rule.name for rule in RULES)
    raise ValueError(f"Unknown phase ensemble rule {name!r}. Available rules: {available}")


def load_index_metadata(conn, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join(["%s"] * len(codes))
    metadata: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.ts_code, c.index_name, c.best_theme, c.final_score,
                   c.apply_year, c.rank_position,
                   e.ts_code AS etf_code, e.extname AS etf_name
            FROM csi_annual_recommendation c
            LEFT JOIN passive_etf e ON e.index_ts_code = c.ts_code AND e.list_status = 'L'
            WHERE c.ts_code IN ({placeholders})
            ORDER BY c.ts_code, c.apply_year DESC, c.rank_position, e.ts_code
            """,
            codes,
        )
        for code, name, theme, score, apply_year, rank, etf_code, etf_name in cur.fetchall():
            code = str(code)
            if code in metadata:
                continue
            metadata[code] = {
                "index_name": str(name or code),
                "best_theme": str(theme or ""),
                "final_score": float(score) if score is not None else None,
                "metadata_apply_year": int(apply_year) if apply_year is not None else None,
                "metadata_rank": int(rank) if rank is not None else None,
                "etf_code": str(etf_code) if etf_code else None,
                "etf_name": str(etf_name) if etf_name else None,
            }
    return metadata


def required_apply_years(rule: PhaseEnsembleRule, snapshot: date) -> list[int]:
    years = {
        apply_year_for_snapshot(month_end_shift(snapshot, -offset))
        for offset in rule.sleeve_offsets
        if apply_year_for_snapshot(month_end_shift(snapshot, -offset)) >= 2014
    }
    return sorted(years)


def load_saved_recommendation_codes(conn, year: int, top: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code
            FROM csi_annual_recommendation
            WHERE apply_year = %s AND ts_code LIKE '%%.CSI'
            ORDER BY rank_position
            LIMIT %s
            """,
            (year, top),
        )
        return [str(row[0]) for row in cur.fetchall()]


def load_production_holdings(
    conn,
    rule: PhaseEnsembleRule,
    snapshot: date,
    top_per_sleeve: int,
) -> dict[int, list[str]]:
    holdings = load_hybrid_holdings()
    fallback_top = top_per_sleeve if top_per_sleeve > 0 else 10
    for year in required_apply_years(rule, snapshot):
        if holdings.get(year):
            continue
        codes = load_saved_recommendation_codes(conn, year, fallback_top)
        if not codes:
            raise RuntimeError(
                f"Missing CSI holdings for apply_year={year}. "
                f"Run scripts/rank_annual_csi.py --year {year} --top 30 --suffix CSI --full --save first."
            )
        holdings[year] = codes
    return holdings


def build_sleeve_rows(
    conn,
    holdings: dict[int, list[str]],
    rule: PhaseEnsembleRule,
    snapshot: date,
    top_per_sleeve: int,
) -> list[dict[str, Any]]:
    rows = []
    for offset in rule.sleeve_offsets:
        sleeve_snapshot = month_end_shift(snapshot, -offset)
        apply_year = apply_year_for_snapshot(sleeve_snapshot)
        if apply_year >= 2014 and not holdings.get(apply_year):
            raise RuntimeError(f"Missing CSI holdings for apply_year={apply_year}")
        codes = holding_codes_for_snapshot(holdings, sleeve_snapshot)
        if top_per_sleeve > 0:
            codes = codes[:top_per_sleeve]
        detail = scorecard_snapshot(conn, sleeve_snapshot)
        rows.append(
            {
                "offset_months": offset,
                "snapshot": sleeve_snapshot.isoformat(),
                "apply_year": apply_year,
                "score": int(detail["score"]),
                "band": detail.get("band"),
                "base_target_equity_pct": float(detail["rule_target_equity_pct"]),
                "codes": codes,
            }
        )
    return rows


def aggregate_rows(
    sleeve_rows: list[dict[str, Any]],
    target_equity_pct: float,
    capital: float,
    metadata: dict[str, dict[str, Any]],
    defensive_asset: str,
) -> list[dict[str, Any]]:
    weights: dict[str, float] = defaultdict(float)
    source_sleeves: dict[str, list[str]] = defaultdict(list)
    sleeve_count = len(sleeve_rows)
    sleeve_equity_pct = target_equity_pct / sleeve_count if sleeve_count else 0.0
    for sleeve in sleeve_rows:
        codes = sleeve["codes"]
        if not codes:
            continue
        per_code_weight = sleeve_equity_pct / len(codes)
        sleeve_label = f"{sleeve['snapshot']}@{sleeve['offset_months']}"
        for code in codes:
            weights[code] += per_code_weight
            source_sleeves[code].append(sleeve_label)

    rows = []
    for rank, (code, weight) in enumerate(sorted(weights.items(), key=lambda item: (-item[1], item[0])), 1):
        meta = metadata.get(code, {})
        rows.append(
            {
                "rank": rank,
                "asset_type": "csi_index",
                "index_code": code,
                "index_name": meta.get("index_name", code),
                "best_theme": meta.get("best_theme", ""),
                "final_score": meta.get("final_score"),
                "target_weight_pct": weight,
                "target_amount": capital * weight / 100.0,
                "etf_code": meta.get("etf_code"),
                "etf_name": meta.get("etf_name"),
                "source_sleeves": ",".join(source_sleeves[code]),
            }
        )

    defensive_pct = max(0.0, 100.0 - target_equity_pct)
    financing_pct = max(0.0, target_equity_pct - 100.0)
    if defensive_pct > 1e-9:
        rows.append(
            {
                "rank": len(rows) + 1,
                "asset_type": "defensive",
                "index_code": f"DEFENSIVE_{defensive_asset.upper()}",
                "index_name": defensive_asset,
                "best_theme": "",
                "final_score": None,
                "target_weight_pct": defensive_pct,
                "target_amount": capital * defensive_pct / 100.0,
                "etf_code": None,
                "etf_name": None,
                "source_sleeves": "portfolio_residual",
            }
        )
    if financing_pct > 1e-9:
        rows.append(
            {
                "rank": len(rows) + 1,
                "asset_type": "financing",
                "index_code": "FINANCING",
                "index_name": "portfolio leverage financing",
                "best_theme": "",
                "final_score": None,
                "target_weight_pct": -financing_pct,
                "target_amount": -capital * financing_pct / 100.0,
                "etf_code": None,
                "etf_name": None,
                "source_sleeves": "portfolio_leverage",
            }
        )
    return rows


def build_targets(
    rule_name: str,
    as_of: date,
    snapshot: date,
    capital: float,
    top_per_sleeve: int,
    portfolio_drawdown_pct: float,
) -> dict[str, Any]:
    rule = rule_by_name(rule_name)
    conn = get_connection()
    try:
        series = load_price_series(conn)
        holdings = load_production_holdings(conn, rule, snapshot, top_per_sleeve)
        portfolio_drawdown = portfolio_drawdown_pct / 100.0
        target_equity_pct, _equity_return, _sleeves, reasons = ensemble_state(
            conn,
            series,
            holdings,
            rule,
            snapshot,
            snapshot,
            snapshot,
            portfolio_drawdown,
        )
        sleeve_rows = build_sleeve_rows(conn, holdings, rule, snapshot, top_per_sleeve)
        codes = sorted({code for sleeve in sleeve_rows for code in sleeve["codes"]})
        metadata = load_index_metadata(conn, codes)
        target_rows = aggregate_rows(sleeve_rows, target_equity_pct, capital, metadata, rule.defensive_asset)
        current_scorecard = scorecard_snapshot(conn, snapshot)
    finally:
        conn.close()

    total_weight = sum(float(row["target_weight_pct"]) for row in target_rows)
    return {
        "strategy": "scorecard_csi_phase_ensemble_targets",
        "no_lookahead_rule": (
            "Uses the previous month-end scorecard snapshot and staggered older month-end CSI sleeves. "
            "Historical sleeves use the locked hybrid backtest holdings; missing future apply years use "
            "saved annual CSI recommendations generated before target construction. Current-month "
            "incomplete market data is not used for CSI selection."
        ),
        "rule_name": rule.name,
        "rule": asdict(rule),
        "as_of": as_of.isoformat(),
        "snapshot": snapshot.isoformat(),
        "capital": capital,
        "portfolio_drawdown_pct": portfolio_drawdown_pct,
        "top_per_sleeve": top_per_sleeve if top_per_sleeve > 0 else None,
        "target_equity_pct": target_equity_pct,
        "target_defensive_pct": max(0.0, 100.0 - target_equity_pct),
        "target_financing_pct": max(0.0, target_equity_pct - 100.0),
        "net_weight_pct": total_weight,
        "defensive_asset": rule.defensive_asset,
        "rebalance_reasons": reasons,
        "scorecard": current_scorecard,
        "sleeves": sleeve_rows,
        "rows": target_rows,
    }


def write_outputs(report: dict[str, Any]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(report["as_of"]).replace("-", "")
    json_path = OUT_DIR / f"csi_phase_ensemble_targets_{stamp}.json"
    csv_path = OUT_DIR / f"csi_phase_ensemble_targets_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(report["rows"][0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report["rows"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate phase-ensemble CSI target weights")
    parser.add_argument("--rule", default=DEFAULT_RULE_NAME)
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Decision date, YYYY-MM-DD")
    parser.add_argument("--snapshot", help="Scorecard/selection snapshot date, YYYY-MM-DD. Default: previous month end.")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--top-per-sleeve", type=int, default=0, help="Limit CSI names per sleeve; 0 keeps all saved holdings.")
    parser.add_argument("--portfolio-drawdown-pct", type=float, default=0.0, help="Current portfolio drawdown as a negative percent, e.g. -8.")
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
    )
    json_path, csv_path = write_outputs(report)
    scorecard = report["scorecard"]

    print("Phase-ensemble CSI portfolio targets")
    print(
        f"  rule={report['rule_name']} as_of={report['as_of']} snapshot={report['snapshot']} "
        f"score={scorecard['score']} band={scorecard['band']} "
        f"equity={report['target_equity_pct']:.1f}% defensive={report['target_defensive_pct']:.1f}% "
        f"financing={report['target_financing_pct']:.1f}% sleeves={len(report['sleeves'])}"
    )
    if report["rebalance_reasons"]:
        print(f"  rebalance_reasons={','.join(report['rebalance_reasons'])}")
    print(f"  net_weight={report['net_weight_pct']:.2f}% capital={report['capital']:,.0f}")
    for row in report["rows"][:20]:
        etf = f"{row['etf_code']} {row['etf_name']}" if row["etf_code"] else "NO_LISTED_ETF"
        print(
            f"  {row['rank']:>2}. {row['asset_type']} {row['index_code']} {row['index_name']} "
            f"weight={row['target_weight_pct']:.2f}% amount={row['target_amount']:,.0f} ETF={etf}"
        )
    if len(report["rows"]) > 20:
        print(f"  ... {len(report['rows']) - 20} more rows")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

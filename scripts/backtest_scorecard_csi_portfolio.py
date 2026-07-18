#!/usr/bin/env python3
"""Combine annual macro allocation with CSI selection backtests.

This script does not train or tune a model. It compounds the existing annual
scorecard allocation over 2006-2025 and, where available, replaces the equity
leg with ex-ante CSI selection backtest returns. Years without CSI selection
coverage fall back to CSI300.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

INITIAL_CAPITAL = 1_000_000.0
CASH_ANNUAL_RATE = 0.02
TARGET_CAPITAL = 40_000_000.0
DEFAULT_START_YEAR = 2006
DEFAULT_END_YEAR = 2025

SCORECARD_JSON = ROOT / "data" / "backtests" / "scorecard_20y_simulation.json"
OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_portfolio_backtest.json"
OUT_CSV = OUT_DIR / "scorecard_csi_portfolio_yearly.csv"


SOURCE_DEFS = {
    "cs300_only": {
        "label": "Scorecard allocation + CSI300 equity leg",
        "risk_note": "20-year allocation baseline; no CSI selection overlay.",
    },
    "walk_forward_optimized": {
        "path": ROOT / "data" / "ml" / "csi_selection_gap_report.json",
        "json_path": ("walk_forward_optimized", "yearly"),
        "label": "Scorecard allocation + walk-forward optimized CSI overlay",
        "risk_note": "Strictest CSI overlay, but only has 2021-2025 CSI coverage and misses the 20m target.",
    },
    "guarded_temporal_split": {
        "csv_path": ROOT / "data" / "ml" / "news_vector_guarded_strategy_yearly_returns.csv",
        "label": "Scorecard allocation + guarded CSI overlay selected on early years",
        "risk_note": (
            "Passes the 20m target with weights selected on 2020-2022 and then applied to "
            "2023-2025; still needs longer direct CSI history for full validation."
        ),
    },
    "momentum12_top10_opportunity95": {
        "csv_path": ROOT / "data" / "ml" / "csi_momentum12_top10_yearly_returns.csv",
        "label": "Scorecard allocation + CSI 12M momentum Top10 + opportunity floor",
        "position_floor": {"score_lte": -3, "equity_pct": 95.0},
        "risk_note": (
            "Passes the 20m target with direct CSI price history expanded to 2014-2025. "
            "Selection uses only prior-year 12M momentum; annual position is unchanged "
            "except score <= -3 opportunity years use at least 95% equity."
        ),
    },
    "regime_momentum_hybrid": {
        "csv_path": ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_yearly.csv",
        "label": "Scorecard allocation + CSI regime/momentum hybrid + opportunity floor",
        "position_floor": {"score_lte": -3, "equity_pct": 95.0},
        "risk_note": (
            "Passes the 30m target with direct CSI price history expanded to 2014-2025. "
            "Normal years use prior-year 12M momentum Top10; predefined special macro "
            "regimes use the fixed regime/research selector. The year switch is based "
            "only on prior-year macro state, not realized returns."
        ),
    },
    "regime_momentum_hybrid_score0_floor95": {
        "csv_path": ROOT / "data" / "ml" / "csi_regime_momentum_hybrid_yearly.csv",
        "label": "Scorecard allocation + CSI regime/momentum hybrid + non-risk-year 95% floor",
        "position_floor": {"score_lte": 0, "equity_pct": 95.0},
        "risk_note": (
            "Meets the 40m target with a single low-parameter allocation change: years whose "
            "annual scorecard is not risk-biased (score <= 0) use at least 95% equity. CSI "
            "selection remains the same ex-ante regime/momentum hybrid; 2006-2013 still fall "
            "back to CSI300 because direct CSI selector history is unavailable."
        ),
    },
    "research_enhanced": {
        "path": ROOT / "data" / "ml" / "research_enhanced_csi_strategy_report.json",
        "json_path": ("enhanced_yearly",),
        "label": "Scorecard allocation + fixed research-enhanced CSI overlay",
        "risk_note": "Passes the 20m target with a simpler fixed formula, but CSI validation is still only 2021-2025.",
    },
    "regime_aware": {
        "path": ROOT / "data" / "ml" / "csi_selection_gap_report.json",
        "json_path": ("regime_aware_selection", "yearly"),
        "label": "Scorecard allocation + regime-aware CSI overlay",
        "risk_note": "Passes the 20m target, but regime selection was validated on only five CSI years.",
    },
    "regime_research": {
        "path": ROOT / "data" / "ml" / "regime_research_csi_strategy_report.json",
        "json_path": ("yearly",),
        "label": "Scorecard allocation + regime and fine research CSI overlay",
        "risk_note": "Highest result, but highest overfit risk because CSI evidence is only 2021-2025.",
    },
}


@dataclass
class YearResult:
    source: str
    year: int
    equity_pct: float
    equity_leg: str
    equity_leg_return: float
    cash_return: float
    portfolio_return: float
    year_end_capital: float
    cumulative_return: float


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required report: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def nested_get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in keys:
        cur = cur[key]
    return cur


def load_scorecard_years(path: Path) -> dict[int, dict[str, Any]]:
    data = load_json(path)
    return {int(row["year"]): row for row in data["yearly"]}


def load_csi_returns(source: str) -> dict[int, float]:
    if source == "cs300_only":
        return {}
    spec = SOURCE_DEFS[source]
    if "csv_path" in spec:
        with spec["csv_path"].open(encoding="utf-8") as f:
            return {
                int(row["year"]): float(row["strategy_return"])
                for row in csv.DictReader(f)
            }
    rows = nested_get(load_json(spec["path"]), spec["json_path"])
    return {int(row["year"]): float(row["strategy_return"]) for row in rows}


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def annualized_return(final_capital: float, years: int) -> float:
    return (final_capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0


def compound_source(
    source: str,
    scorecard: dict[int, dict[str, Any]],
    *,
    start_year: int,
    end_year: int,
) -> tuple[list[YearResult], dict[str, Any]]:
    csi_returns = load_csi_returns(source)
    capital = INITIAL_CAPITAL
    curve = [capital]
    rows: list[YearResult] = []
    csi_years_used = []

    for year in range(start_year, end_year + 1):
        if year not in scorecard:
            raise KeyError(f"Scorecard result missing year {year}")
        score_row = scorecard[year]
        equity_pct = float(score_row["target_equity_pct"])
        floor_rule = SOURCE_DEFS[source].get("position_floor")
        if floor_rule and float(score_row["score"]) <= float(floor_rule["score_lte"]):
            equity_pct = max(equity_pct, float(floor_rule["equity_pct"]))
        equity_weight = equity_pct / 100.0
        if year in csi_returns:
            equity_leg = "csi_selection"
            equity_return = csi_returns[year]
            csi_years_used.append(year)
        else:
            equity_leg = "cs300"
            equity_return = float(score_row["cs300_return_pct"]) / 100.0

        cash_weight = 1.0 - equity_weight
        cash_return = cash_weight * CASH_ANNUAL_RATE
        portfolio_return = equity_weight * equity_return + cash_return
        capital *= 1.0 + portfolio_return
        curve.append(capital)
        rows.append(
            YearResult(
                source=source,
                year=year,
                equity_pct=equity_pct,
                equity_leg=equity_leg,
                equity_leg_return=equity_return,
                cash_return=cash_return,
                portfolio_return=portfolio_return,
                year_end_capital=capital,
                cumulative_return=capital / INITIAL_CAPITAL - 1.0,
            )
        )

    years = len(rows)
    summary = {
        "source": source,
        "label": SOURCE_DEFS[source]["label"],
        "risk_note": SOURCE_DEFS[source]["risk_note"],
        "start_year": start_year,
        "end_year": end_year,
        "years": years,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "multiple": capital / INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_met": capital >= TARGET_CAPITAL,
        "annualized_return": annualized_return(capital, years),
        "max_drawdown": max_drawdown(curve),
        "position_floor": SOURCE_DEFS[source].get("position_floor"),
        "csi_years_used": csi_years_used,
        "csi_coverage_years": len(csi_years_used),
        "fallback_years": [row.year for row in rows if row.equity_leg == "cs300"],
    }
    return rows, summary


def coverage_snapshot() -> dict[str, Any]:
    queries = {
        "index_daily_csi": """
            SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT ts_code), COUNT(*)
            FROM index_daily WHERE ts_code LIKE '%.CSI'
        """,
        "index_daily_cs300": """
            SELECT MIN(trade_date), MAX(trade_date), COUNT(*)
            FROM index_daily WHERE ts_code='000300.SH'
        """,
        "broker_research_industry": """
            SELECT MIN(report_date), MAX(report_date), COUNT(*)
            FROM broker_research_report
            WHERE source='eastmoney_api' AND report_type='industry'
        """,
        "index_constituent": """
            SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT index_code), COUNT(*)
            FROM index_constituent
        """,
    }
    out: dict[str, Any] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for name, query in queries.items():
                cur.execute(query)
                out[name] = [str(value) if value is not None else None for value in cur.fetchone()]
    finally:
        conn.close()
    return out


def write_outputs(yearly: list[YearResult], report: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(yearly[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(row) for row in yearly)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument(
        "--source",
        choices=list(SOURCE_DEFS),
        default=None,
        help="Run one source only. By default all sources are compared.",
    )
    args = parser.parse_args()

    scorecard = load_scorecard_years(SCORECARD_JSON)
    sources = [args.source] if args.source else list(SOURCE_DEFS)
    all_yearly: list[YearResult] = []
    summaries: list[dict[str, Any]] = []
    for source in sources:
        yearly, summary = compound_source(
            source,
            scorecard,
            start_year=args.start_year,
            end_year=args.end_year,
        )
        all_yearly.extend(yearly)
        summaries.append(summary)

    report = {
        "objective": "Start with 1,000,000 and test whether compounded capital reaches 40,000,000.",
        "no_lookahead_rule": (
            "Annual scorecard uses prior-year-end inputs. CSI overlay years use existing "
            "ex-ante selection reports; realized returns are used here only for validation."
        ),
        "overfit_guardrail": (
            "CSI overlay evidence is limited by local data coverage. A target_met result is "
            "not treated as fully validated unless the CSI overlay has a longer out-of-sample history."
        ),
        "coverage_snapshot": coverage_snapshot(),
        "summaries": summaries,
        "yearly_csv": str(OUT_CSV),
    }
    write_outputs(all_yearly, report)

    print("Scorecard + CSI portfolio backtest")
    print(f"  period={args.start_year}-{args.end_year} initial={INITIAL_CAPITAL:,.0f} target={TARGET_CAPITAL:,.0f}")
    for summary in summaries:
        flag = "PASS" if summary["target_met"] else "MISS"
        print(
            f"  {summary['source']:<24} {flag} "
            f"final={summary['final_capital_wan']:8.2f}万 "
            f"multiple={summary['multiple']:5.2f} "
            f"ann={pct(summary['annualized_return']):>6} "
            f"mdd={pct(summary['max_drawdown']):>7} "
            f"csi_years={summary['csi_years_used']}"
        )
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

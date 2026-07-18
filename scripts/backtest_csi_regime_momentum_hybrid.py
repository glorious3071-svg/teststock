#!/usr/bin/env python3
"""Backtest a low-parameter CSI regime/momentum hybrid selector.

The selector keeps the broad 12M momentum rule in normal macro regimes and only
switches to the fixed regime/research selector when the prior-year macro state
flags a special environment. Realized returns are used only for validation.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
import scripts.diagnose_csi_selection_gap as gap
import scripts.backtest_regime_research_csi_strategy as regime_research

OUT_DIR = ROOT / "data" / "ml"
OUT_JSON = OUT_DIR / "csi_regime_momentum_hybrid_report.json"
OUT_YEARLY_CSV = OUT_DIR / "csi_regime_momentum_hybrid_yearly.csv"
OUT_HOLDINGS_CSV = OUT_DIR / "csi_regime_momentum_hybrid_holdings.csv"
CONSTITUENT_FEATURES_CSV = OUT_DIR / "csi_selection_constituent_features_2014_2025.csv"

YEARS = list(range(2014, 2026))
MOMENTUM_TOP_K = 10
REGIME_TOP_K = 5
SPECIAL_REGIMES = {
    "stagflation_defensive",
    "policy_recovery",
    "liquidity_growth",
    "weak_disinflation_repair",
}


def configure_years() -> None:
    gap.YEARS = YEARS
    regime_research.YEARS = YEARS
    gap.CONSTITUENT_FEATURES_CSV = CONSTITUENT_FEATURES_CSV


def build_data(conn) -> pd.DataFrame:
    configure_years()
    base = gap.load_base_data()
    constituent_features = gap.load_constituent_features(conn, base)
    data = base.merge(constituent_features, on=["apply_year", "ts_code"], how="left")
    data = gap.add_style_and_regime_scores(gap.add_rank_features(data))
    data = data.merge(regime_research.build_research_features(conn, base), on=["apply_year", "ts_code"], how="left")
    data = data.merge(regime_research.build_fine_research_features(conn, base), on=["apply_year", "ts_code"], how="left")
    return regime_research.add_strategy_scores(data)


def select_momentum(year_df: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    selected = (
        year_df.dropna(subset=["momentum_12m", "target_return"])
        .sort_values("momentum_12m", ascending=False)
        .head(MOMENTUM_TOP_K)
    )
    return "momentum12_top10", selected


def select_hybrid(conn, year_df: pd.DataFrame, year: int) -> tuple[str, dict[str, Any], str, pd.DataFrame]:
    regime_name, macro_values, regime_codes = regime_research.select_for_year(conn, year_df, year)
    if regime_name in SPECIAL_REGIMES:
        selected = year_df.set_index("ts_code").loc[regime_codes[:REGIME_TOP_K]].reset_index()
        return regime_name, macro_values, "special_regime_research", selected
    selection_rule, selected = select_momentum(year_df)
    return regime_name, macro_values, selection_rule, selected


def evaluate(conn, data: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    yearly: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    for year in YEARS:
        year_df = data[data["apply_year"] == year].copy()
        regime_name, macro_values, selection_rule, selected = select_hybrid(conn, year_df, year)
        strategy_return = float(selected["target_return"].mean())
        benchmark_return = float(selected["bench_return"].dropna().iloc[0])
        yearly.append(
            {
                "year": int(year),
                "regime": regime_name,
                "selection_rule": selection_rule,
                "top_k": int(len(selected)),
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": strategy_return - benchmark_return,
                "selected_codes": "|".join(selected["ts_code"].tolist()),
                "selected_names": "|".join(selected["index_name"].tolist()),
                "macro": json.dumps(macro_values, ensure_ascii=False, sort_keys=True),
            }
        )
        for rank, row in enumerate(selected.itertuples(index=False), 1):
            holdings.append(
                {
                    "year": int(year),
                    "rank": rank,
                    "weight": round(1.0 / len(selected), 6),
                    "regime": regime_name,
                    "selection_rule": selection_rule,
                    "ts_code": row.ts_code,
                    "index_name": row.index_name,
                    "best_theme": row.best_theme,
                    "momentum_12m": float(row.momentum_12m) if pd.notna(row.momentum_12m) else None,
                    "target_return": float(row.target_return),
                }
            )
    strategy_curve = 1.0
    benchmark_curve = 1.0
    for row in yearly:
        strategy_curve *= 1.0 + float(row["strategy_return"])
        benchmark_curve *= 1.0 + float(row["benchmark_return"])
    summary = {
        "n_years": len(yearly),
        "mean_strategy_return": statistics.mean(row["strategy_return"] for row in yearly),
        "mean_benchmark_return": statistics.mean(row["benchmark_return"] for row in yearly),
        "mean_excess_return": statistics.mean(row["excess_return"] for row in yearly),
        "worst_strategy_return": min(row["strategy_return"] for row in yearly),
        "strategy_cumulative_return": strategy_curve - 1.0,
        "benchmark_cumulative_return": benchmark_curve - 1.0,
        "special_regime_years": [row["year"] for row in yearly if row["selection_rule"] == "special_regime_research"],
        "momentum_years": [row["year"] for row in yearly if row["selection_rule"] == "momentum12_top10"],
    }
    return yearly, holdings, summary


def write_outputs(yearly: list[dict[str, Any]], holdings: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(yearly[0].keys()))
        writer.writeheader()
        writer.writerows(yearly)
    with OUT_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(holdings[0].keys()))
        writer.writeheader()
        writer.writerows(holdings)
    OUT_JSON.write_text(
        json.dumps(
            {
                "strategy": "csi_regime_momentum_hybrid",
                "no_lookahead_rule": (
                    "Normal years use prior-year 12M CSI momentum Top10. Special regime years "
                    "use prior-year macro state, H2 research metadata, market, and constituent "
                    "features available by the previous year end. Realized returns are validation only."
                ),
                "overfit_guardrail": (
                    "The switch rule has one macro-regime gate: base_current keeps momentum; only "
                    "predefined non-base regimes use the fixed regime/research selector. No realized "
                    "return is used to choose the year-level switch."
                ),
                "year_from": YEARS[0],
                "year_to": YEARS[-1],
                "momentum_top_k": MOMENTUM_TOP_K,
                "regime_top_k": REGIME_TOP_K,
                "special_regimes": sorted(SPECIAL_REGIMES),
                "summary": summary,
                "yearly": yearly,
                "yearly_csv": str(OUT_YEARLY_CSV),
                "holdings_csv": str(OUT_HOLDINGS_CSV),
                "constituent_features_csv": str(CONSTITUENT_FEATURES_CSV),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def main() -> int:
    conn = get_connection()
    try:
        data = build_data(conn)
        yearly, holdings, summary = evaluate(conn, data)
    finally:
        conn.close()
    write_outputs(yearly, holdings, summary)
    print("CSI Regime/Momentum Hybrid")
    print(
        f"  years={summary['n_years']} "
        f"mean={pct(summary['mean_strategy_return'])} "
        f"excess={pct(summary['mean_excess_return'])} "
        f"worst={pct(summary['worst_strategy_return'])}"
    )
    for row in yearly:
        print(
            f"  {row['year']} {row['regime']} {row['selection_rule']}: "
            f"strategy={pct(row['strategy_return'])} bench={pct(row['benchmark_return'])} "
            f"{row['selected_names']}"
        )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

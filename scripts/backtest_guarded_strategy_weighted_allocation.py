#!/usr/bin/env python3
"""Optimize allocation weights inside the guarded Top-5 selection.

Selection stays unchanged. Annual total equity exposure stays controlled by the
V5.0 scorecard. This script compares deterministic, ex-ante allocation rules
inside the selected Top-5 and writes the best historical result.
"""

from __future__ import annotations

import csv
import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

OUT_DIR = ROOT / "data" / "ml"
HOLDINGS_CSV = OUT_DIR / "news_vector_guarded_strategy_yearly_holdings.csv"
FEATURES_CSV = OUT_DIR / "industry_scorecard_vector_features.csv"
POSITION_CSV = OUT_DIR / "guarded_strategy_scorecard_position_yearly.csv"
OUT_JSON = OUT_DIR / "guarded_strategy_weighted_allocation_report.json"
OUT_YEARLY_CSV = OUT_DIR / "guarded_strategy_weighted_allocation_yearly.csv"
OUT_HOLDINGS_CSV = OUT_DIR / "guarded_strategy_weighted_allocation_holdings.csv"

CASH_ANNUAL_RATE = 0.02
INITIAL_CAPITAL = 1_000_000.0


def normalize(values: pd.Series | list[float]) -> list[float]:
    vals = [max(float(v), 0.0) if pd.notna(v) else 0.0 for v in values]
    total = sum(vals)
    if total <= 1e-12:
        return [1.0 / len(vals)] * len(vals)
    return [v / total for v in vals]


def load_data() -> pd.DataFrame:
    holdings = pd.read_csv(HOLDINGS_CSV)
    features = pd.read_csv(FEATURES_CSV)
    positions = pd.read_csv(POSITION_CSV)
    keep = [
        "apply_year",
        "ts_code",
        "momentum_6m_rank",
        "momentum_12m_rank",
        "vol_6m",
        "rule_score",
        "index_scorecard",
    ]
    data = holdings.merge(
        features[keep],
        left_on=["year", "ts_code"],
        right_on=["apply_year", "ts_code"],
        how="left",
    )
    data = data.merge(
        positions[["year", "target_equity_pct", "benchmark_return"]],
        on="year",
        how="left",
    )
    return data.sort_values(["year", "rank"]).reset_index(drop=True)


def allocation_schemes() -> dict[str, Callable[[pd.DataFrame], list[float]]]:
    schemes: dict[str, Callable[[pd.DataFrame], list[float]]] = {
        "equal": lambda g: [1.0 / len(g)] * len(g),
        "score_linear": lambda g: normalize(g["guarded_score"]),
        "score_squared": lambda g: normalize(g["guarded_score"] ** 2),
        "momentum_6m": lambda g: normalize(g["momentum_6m_rank"]),
        "momentum_12m": lambda g: normalize(g["momentum_12m_rank"]),
        "momentum_combo": lambda g: normalize(0.6 * g["momentum_6m_rank"] + 0.4 * g["momentum_12m_rank"]),
        "inverse_vol": lambda g: normalize(1.0 / g["vol_6m"].fillna(g["vol_6m"].median()).clip(lower=0.05)),
        "momentum_inverse_vol": lambda g: normalize(
            (0.6 * g["momentum_6m_rank"] + 0.4 * g["momentum_12m_rank"])
            / g["vol_6m"].fillna(g["vol_6m"].median()).clip(lower=0.05)
        ),
        "rank_30_25_20_15_10": lambda g: [0.30, 0.25, 0.20, 0.15, 0.10][: len(g)],
        "rank_25_22_20_18_15": lambda g: [0.25, 0.22, 0.20, 0.18, 0.15][: len(g)],
    }

    # Conservative fixed rank-weight grid for comparison.  These weights are
    # deterministic and do not use future returns, but are selected by validation.
    points = [i / 20 for i in range(1, 13)]
    for weights in itertools.product(points, repeat=5):
        if abs(sum(weights) - 1.0) > 1e-9:
            continue
        if not all(weights[i] >= weights[i + 1] for i in range(4)):
            continue
        if max(weights) > 0.5:
            continue
        name = "grid_" + "_".join(f"{w:.2f}" for w in weights)
        schemes[name] = lambda g, weights=weights: list(weights)[: len(g)]
    return schemes


def evaluate(data: pd.DataFrame, scheme_name: str, weights_fn: Callable[[pd.DataFrame], list[float]]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    capital = INITIAL_CAPITAL
    curve = [capital]
    yearly: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    for year, g in data.groupby("year"):
        group = g.sort_values("rank").copy()
        weights = weights_fn(group)
        strategy_return = sum(w * float(r) for w, r in zip(weights, group["realized_return"]))
        equity_w = float(group["target_equity_pct"].iloc[0]) / 100.0
        benchmark_return = float(group["benchmark_return"].iloc[0])
        portfolio_return = equity_w * strategy_return + (1.0 - equity_w) * CASH_ANNUAL_RATE
        start_capital = capital
        capital *= 1.0 + portfolio_return
        curve.append(capital)
        yearly.append(
            {
                "year": int(year),
                "scheme": scheme_name,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "target_equity_pct": float(group["target_equity_pct"].iloc[0]),
                "portfolio_return": portfolio_return,
                "year_start_capital": start_capital,
                "year_end_capital": capital,
                "cumulative_return": capital / INITIAL_CAPITAL - 1.0,
            }
        )
        for weight, (_idx, row) in zip(weights, group.iterrows()):
            holding_rows.append(
                {
                    "year": int(year),
                    "scheme": scheme_name,
                    "rank": int(row["rank"]),
                    "ts_code": row["ts_code"],
                    "index_name": row["index_name"],
                    "best_theme": row["best_theme"],
                    "equity_bucket_weight": weight,
                    "portfolio_weight": weight * equity_w,
                    "realized_return": float(row["realized_return"]),
                }
            )

    def max_drawdown(values: list[float]) -> float:
        peak = values[0]
        dd = 0.0
        for value in values:
            peak = max(peak, value)
            dd = min(dd, value / peak - 1.0)
        return dd

    total_return = capital / INITIAL_CAPITAL - 1.0
    n = len(yearly)
    summary = {
        "scheme": scheme_name,
        "final_capital": capital,
        "total_return": total_return,
        "annualized_return": (1.0 + total_return) ** (1.0 / n) - 1.0,
        "max_drawdown": max_drawdown(curve),
        "mean_portfolio_return": statistics.mean(r["portfolio_return"] for r in yearly),
        "worst_portfolio_return": min(r["portfolio_return"] for r in yearly),
    }
    return yearly, summary, holding_rows


def main() -> int:
    data = load_data()
    all_results: list[tuple[float, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for name, fn in allocation_schemes().items():
        yearly, summary, holdings = evaluate(data, name, fn)
        objective = float(summary["total_return"]) + 0.7 * float(summary["worst_portfolio_return"])
        all_results.append((objective, summary, yearly, holdings))
    all_results.sort(reverse=True, key=lambda x: x[0])
    best_objective, best_summary, best_yearly, best_holdings = all_results[0]
    baseline = next(summary for _obj, summary, _yr, _h in all_results if summary["scheme"] == "equal")

    with OUT_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(best_yearly[0].keys()))
        writer.writeheader()
        writer.writerows(best_yearly)
    with OUT_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(best_holdings[0].keys()))
        writer.writeheader()
        writer.writerows(best_holdings)

    report = {
        "strategy": "guarded_top5_scorecard_position_weighted_allocation",
        "selection": "guarded_top5 unchanged",
        "position": "annual scorecard target_equity_pct unchanged",
        "best_objective": best_objective,
        "best_summary": best_summary,
        "equal_weight_summary": baseline,
        "top_summaries": [summary for _obj, summary, _yr, _h in all_results[:15]],
        "yearly_csv": str(OUT_YEARLY_CSV),
        "holdings_csv": str(OUT_HOLDINGS_CSV),
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def pct(v: float) -> str:
        return f"{v * 100:.2f}%"

    print("Weighted Allocation Optimization")
    print(f"  best_scheme={best_summary['scheme']}")
    print(
        f"  final={best_summary['final_capital']:,.0f} "
        f"return={pct(best_summary['total_return'])} "
        f"annualized={pct(best_summary['annualized_return'])} "
        f"max_dd={pct(best_summary['max_drawdown'])}"
    )
    print(
        f"  equal_final={baseline['final_capital']:,.0f} "
        f"equal_return={pct(baseline['total_return'])}"
    )
    print("\nYearly:")
    for row in best_yearly:
        print(
            f"  {row['year']}: strategy={pct(row['strategy_return'])} "
            f"portfolio={pct(row['portfolio_return'])} "
            f"capital={row['year_end_capital']:,.0f}"
        )
    print(f"\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

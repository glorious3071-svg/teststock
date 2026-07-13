#!/usr/bin/env python3
"""Tune and backtest a guarded news-vector CSI strategy.

This is a post-processing layer on top of the walk-forward predictions produced
by scripts/backtest_news_vector_framework.py.  It keeps the semantic vector
signal, but guards it with rule and momentum ranks to reduce theme/style
mis-selection such as the 2024 pure-vector drawdown versus CSI 300.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

OUT_DIR = ROOT / "data" / "ml"
PRED_CSV = OUT_DIR / "news_vector_framework_predictions.csv"
FEATURES_CSV = OUT_DIR / "industry_scorecard_vector_features.csv"
REPORT_JSON = OUT_DIR / "news_vector_guarded_strategy_report.json"
YEARLY_CSV = OUT_DIR / "news_vector_guarded_strategy_yearly_returns.csv"
HOLDINGS_CSV = OUT_DIR / "news_vector_guarded_strategy_yearly_holdings.csv"

COMPONENTS = [
    "vector_ml_score_rank",
    "rule_score_rank",
    "base_ml_score_rank",
    "momentum_12m_rank_rank",
    "momentum_6m_rank_rank",
    "index_scorecard_rank",
]


def rank01(s: pd.Series) -> pd.Series:
    vals = pd.to_numeric(s, errors="coerce")
    if vals.notna().sum() <= 1:
        return pd.Series(0.5, index=s.index)
    return vals.rank(pct=True)


def load_data() -> pd.DataFrame:
    pred = pd.read_csv(PRED_CSV)
    features = pd.read_csv(FEATURES_CSV)
    keep = [
        "apply_year",
        "ts_code",
        "momentum_6m_rank",
        "momentum_12m_rank",
        "index_scorecard",
    ]
    data = pred.merge(features[keep], on=["apply_year", "ts_code"], how="left")
    for col in [
        "vector_ml_score",
        "rule_score",
        "base_ml_score",
        "momentum_12m_rank",
        "momentum_6m_rank",
        "index_scorecard",
    ]:
        data[f"{col}_rank"] = data.groupby("apply_year")[col].transform(rank01)
    return data


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    xr = pd.Series(x).rank().tolist()
    yr = pd.Series(y).rank().tolist()
    mx = statistics.mean(xr)
    my = statistics.mean(yr)
    num = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    den_x = sum((a - mx) ** 2 for a in xr)
    den_y = sum((b - my) ** 2 for b in yr)
    den = (den_x * den_y) ** 0.5
    return None if den < 1e-9 else num / den


def score_with_weights(data: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    score = pd.Series(0.0, index=data.index)
    for component, weight in weights.items():
        score = score + float(weight) * pd.to_numeric(data[component], errors="coerce").fillna(0.5)
    return score


def evaluate(data: pd.DataFrame, score_col: str, top_k: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    yearly: list[dict[str, Any]] = []
    for year, g in data.groupby("apply_year"):
        sub = g.dropna(subset=[score_col, "target_return", "target_excess"]).copy()
        if sub.empty:
            continue
        sub = sub.sort_values(score_col, ascending=False)
        k = min(top_k, len(sub))
        top = sub.head(k)
        bot = sub.tail(k)
        bench = float((sub["target_return"] - sub["target_excess"]).iloc[0])
        strategy_ret = float(top["target_return"].mean())
        excess = strategy_ret - bench
        spread = float(top["target_return"].mean() - bot["target_return"].mean())
        rho = spearman(sub[score_col].tolist(), sub["target_return"].tolist())
        yearly.append(
            {
                "year": int(year),
                "top_k": int(k),
                "strategy_return": strategy_ret,
                "benchmark_return": bench,
                "excess_return": excess,
                "bottom_return": float(bot["target_return"].mean()),
                "top_bottom_spread": spread,
                "spearman": rho,
                "top_codes": top["ts_code"].tolist(),
            }
        )

    def mean_key(key: str) -> float | None:
        vals = [float(row[key]) for row in yearly if row.get(key) is not None]
        return statistics.mean(vals) if vals else None

    strategy_cum = 1.0
    bench_cum = 1.0
    for row in yearly:
        strategy_cum *= 1.0 + float(row["strategy_return"])
        bench_cum *= 1.0 + float(row["benchmark_return"])
    summary = {
        "n_years": len(yearly),
        "mean_strategy_return": mean_key("strategy_return"),
        "mean_benchmark_return": mean_key("benchmark_return"),
        "mean_excess_return": mean_key("excess_return"),
        "mean_top_bottom_spread": mean_key("top_bottom_spread"),
        "mean_spearman": mean_key("spearman"),
        "worst_excess_return": min((float(r["excess_return"]) for r in yearly), default=None),
        "strategy_cumulative_return": strategy_cum - 1.0,
        "benchmark_cumulative_return": bench_cum - 1.0,
        "cumulative_excess_gap": (strategy_cum - 1.0) - (bench_cum - 1.0),
    }
    return yearly, summary


def objective(summary: dict[str, Any], worst_weight: float) -> float:
    return (
        float(summary.get("mean_excess_return") or 0.0)
        + 0.5 * float(summary.get("mean_top_bottom_spread") or 0.0)
        + worst_weight * float(summary.get("worst_excess_return") or 0.0)
    )


def weight_grid(points: list[float], min_vector: float) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for vals in itertools.product(points, repeat=len(COMPONENTS)):
        if abs(sum(vals) - 1.0) > 1e-9:
            continue
        weights = dict(zip(COMPONENTS, vals))
        if weights["vector_ml_score_rank"] < min_vector:
            continue
        out.append(weights)
    return out


def write_outputs(
    data: pd.DataFrame,
    yearly: list[dict[str, Any]],
    summary: dict[str, Any],
    weights: dict[str, float],
    top_k: int,
    report: dict[str, Any],
) -> None:
    with YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "year",
            "top_k",
            "strategy_return",
            "benchmark_return",
            "excess_return",
            "bottom_return",
            "top_bottom_spread",
            "spearman",
            "top_codes",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in yearly:
            out = dict(row)
            out["top_codes"] = "|".join(row.get("top_codes") or [])
            writer.writerow(out)

    holding_rows: list[dict[str, Any]] = []
    for year, g in data.groupby("apply_year"):
        top = g.sort_values("guarded_score", ascending=False).head(top_k)
        for rank, (_idx, row) in enumerate(top.iterrows(), 1):
            holding_rows.append(
                {
                    "year": int(year),
                    "rank": rank,
                    "weight": round(1.0 / top_k, 6),
                    "ts_code": row["ts_code"],
                    "index_name": row["index_name"],
                    "best_theme": row["best_theme"],
                    "guarded_score": float(row["guarded_score"]),
                    "realized_return": float(row["target_return"]),
                    "realized_excess": float(row["target_excess"]),
                }
            )
    with HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(holding_rows[0].keys()))
        writer.writeheader()
        writer.writerows(holding_rows)

    payload = {
        "strategy": "guarded_news_vector",
        "top_k": top_k,
        "weights": weights,
        "yearly": yearly,
        "summary": summary,
        "yearly_csv": str(YEARLY_CSV),
        "holdings_csv": str(HOLDINGS_CSV),
        **report,
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--points", default="0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0")
    parser.add_argument("--min-vector", type=float, default=0.2)
    parser.add_argument("--worst-weight", type=float, default=0.7)
    args = parser.parse_args()

    points = [float(x) for x in args.points.split(",") if x.strip()]
    data = load_data()
    best: tuple[float, dict[str, float], list[dict[str, Any]], dict[str, Any]] | None = None
    combos = weight_grid(points, args.min_vector)
    for weights in combos:
        data["guarded_score"] = score_with_weights(data, weights)
        yearly, summary = evaluate(data, "guarded_score", args.top_k)
        score = objective(summary, args.worst_weight)
        if best is None or score > best[0]:
            best = (score, weights, yearly, summary)
    if best is None:
        raise SystemExit("No valid weight combinations")

    score, weights, yearly, summary = best
    data["guarded_score"] = score_with_weights(data, weights)
    report = {
        "objective": score,
        "objective_formula": "mean_excess + 0.5*mean_top_bottom_spread + worst_weight*worst_excess",
        "worst_weight": args.worst_weight,
        "min_vector": args.min_vector,
        "grid_points": points,
        "n_combinations": len(combos),
    }
    write_outputs(data, yearly, summary, weights, args.top_k, report)

    def pct(v: Any) -> str:
        return "N/A" if v is None else f"{float(v) * 100:.2f}%"

    print("Guarded News Vector Strategy")
    print(f"  combinations={len(combos)} objective={score:.4f}")
    print("  weights:")
    for k, v in weights.items():
        if v:
            print(f"    {k}: {v:.2f}")
    print("  summary:")
    for k in [
        "mean_strategy_return",
        "mean_benchmark_return",
        "mean_excess_return",
        "mean_top_bottom_spread",
        "worst_excess_return",
        "strategy_cumulative_return",
        "benchmark_cumulative_return",
    ]:
        print(f"    {k}: {pct(summary.get(k))}")
    print("\nYearly:")
    for row in yearly:
        print(
            f"  {row['year']}: strategy={pct(row['strategy_return'])} "
            f"bench={pct(row['benchmark_return'])} excess={pct(row['excess_return'])} "
            f"spread={pct(row['top_bottom_spread'])}"
        )
    print(f"\nWrote {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

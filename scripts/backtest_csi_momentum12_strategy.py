#!/usr/bin/env python3
"""Backtest a simple CSI 12M momentum Top-K selector.

Inputs are built by scripts/backtest_news_vector_framework.py or
scripts/ml_industry_scorecard.py. The selector only uses prior-year 12M
momentum ranks and realized returns are used only for validation.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "ml"
FEATURES_CSV = OUT_DIR / "industry_scorecard_vector_features.csv"
def output_paths(top_k: int) -> tuple[Path, Path, Path]:
    return (
        OUT_DIR / f"csi_momentum12_top{top_k}_strategy_report.json",
        OUT_DIR / f"csi_momentum12_top{top_k}_yearly_returns.csv",
        OUT_DIR / f"csi_momentum12_top{top_k}_holdings.csv",
    )


def load_data() -> pd.DataFrame:
    data = pd.read_csv(FEATURES_CSV)
    required = {"apply_year", "ts_code", "index_name", "momentum_12m", "target_return", "bench_return"}
    missing = required - set(data.columns)
    if missing:
        raise SystemExit(f"Missing columns in {FEATURES_CSV}: {sorted(missing)}")
    return data


def evaluate(data: pd.DataFrame, year_from: int, year_to: int, top_k: int) -> dict[str, Any]:
    yearly: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    for year in range(year_from, year_to + 1):
        sub = data[data["apply_year"] == year].dropna(subset=["momentum_12m", "target_return"]).copy()
        if len(sub) < top_k:
            continue
        sub = sub.sort_values("momentum_12m", ascending=False)
        top = sub.head(top_k)
        bottom = sub.tail(top_k)
        strategy_return = float(top["target_return"].mean())
        benchmark_return = float(sub["bench_return"].dropna().iloc[0])
        yearly.append(
            {
                "year": year,
                "top_k": top_k,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": strategy_return - benchmark_return,
                "bottom_return": float(bottom["target_return"].mean()),
                "top_bottom_spread": strategy_return - float(bottom["target_return"].mean()),
                "top_codes": "|".join(top["ts_code"].tolist()),
            }
        )
        for rank, row in enumerate(top.itertuples(index=False), 1):
            holdings.append(
                {
                    "year": year,
                    "rank": rank,
                    "ts_code": row.ts_code,
                    "index_name": row.index_name,
                    "best_theme": getattr(row, "best_theme", ""),
                    "momentum_12m": float(row.momentum_12m),
                    "target_return": float(row.target_return),
                }
            )

    def mean_key(key: str) -> float | None:
        vals = [float(row[key]) for row in yearly if row.get(key) is not None]
        return statistics.mean(vals) if vals else None

    strategy_cum = 1.0
    benchmark_cum = 1.0
    for row in yearly:
        strategy_cum *= 1.0 + float(row["strategy_return"])
        benchmark_cum *= 1.0 + float(row["benchmark_return"])

    return {
        "yearly": yearly,
        "holdings": holdings,
        "summary": {
            "n_years": len(yearly),
            "mean_strategy_return": mean_key("strategy_return"),
            "mean_benchmark_return": mean_key("benchmark_return"),
            "mean_excess_return": mean_key("excess_return"),
            "mean_top_bottom_spread": mean_key("top_bottom_spread"),
            "worst_excess_return": min((float(r["excess_return"]) for r in yearly), default=None),
            "strategy_cumulative_return": strategy_cum - 1.0,
            "benchmark_cumulative_return": benchmark_cum - 1.0,
        },
    }


def write_outputs(report: dict[str, Any], out_json: Path, out_yearly_csv: Path, out_holdings_csv: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with out_yearly_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(report["yearly"][0].keys()))
        writer.writeheader()
        writer.writerows(report["yearly"])
    with out_holdings_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(report["holdings"][0].keys()))
        writer.writeheader()
        writer.writerows(report["holdings"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="year_from", type=int, default=2014)
    parser.add_argument("--to", dest="year_to", type=int, default=2025)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    data = load_data()
    result = evaluate(data, args.year_from, args.year_to, args.top_k)
    out_json, out_yearly_csv, out_holdings_csv = output_paths(args.top_k)
    report = {
        "strategy": f"csi_momentum12_top{args.top_k}",
        "no_lookahead_rule": "Uses prior-year 12M CSI index momentum; realized returns are only validation labels.",
        "year_from": args.year_from,
        "year_to": args.year_to,
        "top_k": args.top_k,
        "features_csv": str(FEATURES_CSV),
        "yearly_csv": str(out_yearly_csv),
        "holdings_csv": str(out_holdings_csv),
        **result,
    }
    write_outputs(report, out_json, out_yearly_csv, out_holdings_csv)

    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value) * 100:.2f}%"

    print("CSI 12M Momentum Top-K Strategy")
    print(
        f"  years={report['summary']['n_years']} "
        f"mean={pct(report['summary']['mean_strategy_return'])} "
        f"excess={pct(report['summary']['mean_excess_return'])} "
        f"spread={pct(report['summary']['mean_top_bottom_spread'])}"
    )
    for row in report["yearly"]:
        print(
            f"  {row['year']}: strategy={pct(row['strategy_return'])} "
            f"bench={pct(row['benchmark_return'])} excess={pct(row['excess_return'])}"
        )
    print(f"Wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

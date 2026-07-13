#!/usr/bin/env python3
"""Compare CSI ranking modes: baseline vs +news vs full on forward 12M returns."""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from csi.enhanced import apply_enhancements
from csi.index_scorecard import compute_index_scorecard
from csi.ranking import price_window, rank_indices, valuation_window, year_as_of
from db.connection import get_connection
from scripts.rank_annual_csi import load_news, load_prices, load_signals, load_theme_map, load_valuations
from scripts.validate_csi_rank import forward_return, spearman


def rank_mode(conn, apply_year: int, mode: str) -> list[dict]:
    as_of = year_as_of(apply_year)
    cutoff = date(apply_year - 1, 12, 31)
    signals = load_signals(conn, apply_year, as_of)
    news = {} if mode == "baseline" else load_news(conn, apply_year)
    has_news = mode != "baseline" and len(news) > 0
    theme_map = load_theme_map(conn, "CSI")
    prices = load_prices(conn, "CSI", apply_year)
    vals = load_valuations(conn, "CSI", apply_year)
    rows = rank_indices(
        signals=signals, news=news, theme_map=theme_map,
        price_data=prices, val_data=vals, as_of=cutoff,
        suffix="CSI", has_news=has_news,
    )
    if mode == "full" and rows:
        price_closes = {ts: [c for _, c in ser] for ts, ser in prices.items()}
        for row in rows:
            sc = compute_index_scorecard(conn, row["ts_code"], prices.get(row["ts_code"], []), cutoff)
            row["final_score"] = row.get("final_score", 0) + 0.10 * sc
        rows = apply_enhancements(conn, rows, apply_year=apply_year, price_closes=price_closes, top_n=30)
    return rows


def eval_rows(conn, apply_year: int, rows: list[dict]) -> dict | None:
    start = date(apply_year, 1, 5)
    end = date(apply_year, 12, 31)
    pairs: list[tuple[float, float]] = []
    for row in rows:
        ret = forward_return(conn, row["ts_code"], start, end)
        if ret is not None:
            pairs.append((row["final_score"], ret))
    if len(pairs) < 10:
        return None
    scores, rets = zip(*pairs)
    rho = spearman(list(scores), list(rets))
    sorted_pairs = sorted(pairs, key=lambda p: -p[0])
    k = min(10, len(sorted_pairs) // 4)
    top_avg = statistics.mean([r for _, r in sorted_pairs[:k]])
    bot_avg = statistics.mean([r for _, r in sorted_pairs[-k:]])
    bench = forward_return(conn, "000300.SH", start, end)
    return {
        "n": len(pairs),
        "rho": rho,
        "top_k": k,
        "top_avg": top_avg,
        "bot_avg": bot_avg,
        "spread": top_avg - bot_avg,
        "bench": bench,
        "excess_top": top_avg - bench if bench is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="y0", type=int, default=2015)
    parser.add_argument("--to", dest="y1", type=int, default=2025)
    args = parser.parse_args()

    conn = get_connection()
    modes = ["baseline", "news", "full"]
    summary: dict[str, list[float]] = defaultdict(list)

    print(f"{'Year':<6} {'Mode':<10} {'ρ':>7} {'Top':>8} {'Spread':>8} {'Excess':>8}  news?")
    print("-" * 72)

    for year in range(args.y0, args.y1 + 1):
        has_news_data = bool(load_news(conn, year))
        for mode in modes:
            if mode != "baseline" and not has_news_data:
                continue
            rows = rank_mode(conn, year, mode)
            m = eval_rows(conn, year, rows)
            if not m:
                print(f"{year:<6} {mode:<10}  (insufficient forward data)")
                continue
            rho_s = f"{m['rho']:.3f}" if m["rho"] is not None else "N/A"
            print(
                f"{year:<6} {mode:<10} {rho_s:>7} "
                f"{m['top_avg']*100:>7.1f}% {m['spread']*100:>7.1f}% "
                f"{(m['excess_top'] or 0)*100:>7.1f}%  {'Y' if has_news_data else 'N'}"
            )
            if m["rho"] is not None:
                summary[f"{mode}_rho"].append(m["rho"])
            if m["spread"] is not None:
                summary[f"{mode}_spread"].append(m["spread"])
            if m["excess_top"] is not None:
                summary[f"{mode}_excess"].append(m["excess_top"])

    print("\n=== Mean across years ===")
    for key in ("baseline_rho", "news_rho", "full_rho", "baseline_spread", "news_spread", "full_spread",
                "baseline_excess", "news_excess", "full_excess"):
        vals = summary.get(key, [])
        if vals:
            print(f"  {key}: {statistics.mean(vals):.3f}  (n={len(vals)})")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

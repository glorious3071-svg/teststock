#!/usr/bin/env python3
"""Ablation: compare flat vs event-salience vs daily rollup theme rankings.

Usage:
  python scripts/backtest_news_salience.py
  python scripts/backtest_news_salience.py --year 2026 --live
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csi.ranking import news_window
from db.connection import get_connection
from scripts.aggregate_theme_news_signals import aggregate, ensure_tables


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    n = len(x)

    def rank(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        for ri, idx in enumerate(order):
            r[idx] = ri + 1
        return r

    rx, ry = rank(x), rank(y)
    mx = statistics.mean(rx)
    my = statistics.mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = (
        sum((rx[i] - mx) ** 2 for i in range(n)) * sum((ry[i] - my) ** 2 for i in range(n))
    ) ** 0.5
    if den < 1e-9:
        return None
    return num / den


def top_themes(results: list[dict], k: int = 5) -> list[str]:
    return [r["theme"] for r in sorted(results, key=lambda x: -x["net_score"])[:k]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    ensure_tables(conn)

    methods = ["flat", "events", "daily"]
    rankings: dict[str, list[dict]] = {}
    for m in methods:
        try:
            rankings[m] = aggregate(
                conn, args.year, dry_run=True, live=args.live, method=m,
            )
        except Exception as exc:
            print(f"  {m}: ERROR {exc}")
            rankings[m] = []

    print(f"=== Salience ablation apply_year={args.year} live={args.live} ===\n")
    w_start, w_end = news_window(args.year)
    if args.live:
        w_start = date(args.year, 1, 1)
        w_end = date.today()
    print(f"Window: {w_start} .. {w_end}\n")

    for m in methods:
        res = rankings[m]
        print(f"--- {m} ({len(res)} themes) ---")
        for r in sorted(res, key=lambda x: -x["net_score"])[:8]:
            print(
                f"  {r['theme']:22s} net={r['net_score']:+8.2f} "
                f"ev={r.get('event_count', 0):4d} mn={r.get('mention_count', 0):5d}"
            )
        print()

    flat_scores = {r["theme"]: r["net_score"] for r in rankings.get("flat", [])}
    for m in ("events", "daily"):
        other = {r["theme"]: r["net_score"] for r in rankings.get(m, [])}
        common = sorted(set(flat_scores) & set(other))
        if len(common) >= 3:
            xs = [flat_scores[t] for t in common]
            ys = [other[t] for t in common]
            rho = spearman(xs, ys)
            print(f"Spearman(flat, {m}) over {len(common)} themes: {rho:.3f}" if rho else f"Spearman flat vs {m}: N/A")

    flat_top = top_themes(rankings.get("flat", []))
    daily_top = top_themes(rankings.get("daily", []))
    overlap = len(set(flat_top) & set(daily_top))
    print(f"\nTop-5 overlap (flat vs daily): {overlap}/5")
    print(f"  flat:  {flat_top}")
    print(f"  daily: {daily_top}")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM news_event WHERE mention_count > 1")
        multi = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM news_event WHERE unique_sources > 1")
        cross = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM news_event")
        total_ev = cur.fetchone()[0]
    print(f"\nEvent stats: total={total_ev} multi_mention={multi} cross_source={cross}")
    if total_ev and cross:
        print("  cross-source events receive salience boost (by design)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

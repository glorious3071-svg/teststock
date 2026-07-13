#!/usr/bin/env python3
"""Validate CSI ranking: forward 12M return vs score (historical years).

Usage:
  python scripts/validate_csi_rank.py --from 2015 --to 2024
  python scripts/validate_csi_rank.py --year 2026   # sanity check only
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from db.connection import get_connection


def forward_return(conn, ts_code: str, start: date, end: date) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, close FROM index_daily
            WHERE ts_code=%s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
            """,
            (ts_code, start, end),
        )
        rows = [(d, float(c)) for d, c in cur.fetchall() if c]
    if len(rows) < 2:
        return None
    s, e = rows[0][1], rows[-1][1]
    if s <= 0:
        return None
    return (e - s) / s


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
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
    den = (sum((rx[i] - mx) ** 2 for i in range(n)) * sum((ry[i] - my) ** 2 for i in range(n))) ** 0.5
    if den < 1e-9:
        return None
    return num / den


def validate_year(conn, year: int, *, regenerate: bool = False) -> dict | None:
    if regenerate:
        subprocess.run(
            [sys.executable, "scripts/rank_annual_csi.py", "--year", str(year), "--save"],
            cwd=ROOT, check=False,
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, final_score FROM csi_annual_recommendation
            WHERE apply_year=%s AND ts_code LIKE '%%.CSI'
            ORDER BY rank_position
            """,
            (year,),
        )
        ranked = cur.fetchall()

    if not ranked:
        return None

    start = date(year, 1, 5)
    end = date(year, 12, 31)
    pairs: list[tuple[float, float]] = []
    for ts, score in ranked:
        ret = forward_return(conn, ts, start, end)
        if ret is not None:
            pairs.append((float(score), ret))

    if len(pairs) < 10:
        return {"year": year, "n": len(pairs), "rho": None, "top10": None, "bot10": None}

    scores, rets = zip(*pairs)
    rho = spearman(list(scores), list(rets))

    sorted_by_score = sorted(pairs, key=lambda p: -p[0])
    k = min(10, len(sorted_by_score) // 4)
    top_avg = statistics.mean([r for _, r in sorted_by_score[:k]])
    bot_avg = statistics.mean([r for _, r in sorted_by_score[-k:]])
    bench = forward_return(conn, "000300.SH", start, end)

    return {
        "year": year,
        "n": len(pairs),
        "rho": rho,
        "top_k": k,
        "top10": top_avg,
        "bot10": bot_avg,
        "spread": top_avg - bot_avg,
        "bench": bench,
        "excess_top": top_avg - bench if bench is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--from", dest="year_from", type=int, default=2015)
    parser.add_argument("--to", dest="year_to", type=int, default=2024)
    parser.add_argument("--regenerate", action="store_true")
    args = parser.parse_args()

    conn = get_connection()

    if args.year:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM csi_annual_recommendation WHERE apply_year=%s",
                (args.year,),
            )
            n = cur.fetchone()[0]
        print(f"=== {args.year} recommendation sanity ===")
        print(f"  csi_annual_recommendation rows: {n}")
        if n:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rank_position, ts_code, index_name, final_score, best_theme
                    FROM csi_annual_recommendation WHERE apply_year=%s
                    ORDER BY rank_position LIMIT 10
                    """,
                    (args.year,),
                )
                for row in cur.fetchall():
                    print(f"  {row[0]:2d}. {row[1]:14s} {row[2]:20s} score={row[3]:.3f} theme={row[4]}")
        conn.close()
        return 0

    print(f"{'Year':<6} {'N':>4} {'ρ_12m':>8} {'Top':>8} {'Bot':>8} {'Spread':>8} {'Excess':>8}")
    print("-" * 60)
    rhos = []
    for y in range(args.year_from, args.year_to + 1):
        r = validate_year(conn, y, regenerate=args.regenerate)
        if not r:
            print(f"{y:<6}  (no data)")
            continue
        rho_s = f"{r['rho']:.3f}" if r["rho"] is not None else "  N/A"
        top_s = f"{r['top10']*100:>7.1f}%" if r["top10"] is not None else "    N/A"
        bot_s = f"{r['bot10']*100:>7.1f}%" if r["bot10"] is not None else "    N/A"
        spr_s = f"{r['spread']*100:>7.1f}%" if r.get("spread") is not None else "    N/A"
        exc_s = f"{(r['excess_top'] or 0)*100:>7.1f}%" if r.get("excess_top") is not None else "    N/A"
        print(
            f"{r['year']:<6} {r['n']:>4} {rho_s:>8} "
            f"{top_s} {bot_s} {spr_s} {exc_s}"
        )
        if r["rho"] is not None:
            rhos.append(r["rho"])

    if rhos:
        print(f"\nMean Spearman ρ = {statistics.mean(rhos):.3f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Map CSI annual recommendation Top-N to passive ETF candidates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.rank_position, c.ts_code, c.index_name, c.final_score, c.best_theme,
                   e.ts_code AS etf_code, e.extname AS etf_name
            FROM csi_annual_recommendation c
            LEFT JOIN passive_etf e ON e.index_ts_code = c.ts_code AND e.list_status = 'L'
            WHERE c.apply_year = %s AND c.ts_code LIKE '%%.CSI'
            ORDER BY c.rank_position, e.ts_code
            LIMIT %s
            """,
            (args.year, args.top * 3),
        )
        rows = cur.fetchall()

    seen_index: set[str] = set()
    print(f"=== CSI → ETF mapping {args.year} Top-{args.top} ===")
    shown = 0
    for rank, ts, name, score, theme, etf, etf_name in rows:
        if ts in seen_index:
            continue
        seen_index.add(ts)
        shown += 1
        if shown > args.top:
            break
        etf_s = f"{etf} {etf_name}" if etf else "(无在售ETF)"
        print(f"{rank:2d}. {ts:14s} {name:18s} score={score:.3f}  ETF: {etf_s}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

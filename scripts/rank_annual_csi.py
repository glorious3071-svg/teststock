#!/usr/bin/env python3
"""Annual CSI index ranking with policy + news + momentum + valuation.

Usage:
  python scripts/rank_annual_csi.py --year 2026
  python scripts/rank_annual_csi.py --year 2026 --top 30 --save
  python scripts/rank_annual_csi.py --year 2026 --suffix CSI --min-signal 中
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from csi.enhanced import apply_enhancements
from csi.index_scorecard import compute_index_scorecard
from csi.ranking import (
    price_window,
    rank_indices,
    save_recommendations,
    valuation_window,
    year_as_of,
)
from db.connection import ensure_schema, get_connection

SCHEMA_PATH = ROOT / "sql" / "theme_news_signals_schema.sql"


def ensure_tables(conn) -> None:
    if SCHEMA_PATH.exists():
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                cur.execute(stmt)
        conn.commit()


def load_signals(conn, apply_year: int, as_of: date) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT theme, signal_strength, policy_basis
            FROM annual_sector_signals
            WHERE apply_year = %s AND as_of_date = %s
            """,
            (apply_year, as_of),
        )
        rows = cur.fetchall()
        if not rows:
            cur.execute(
                """
                SELECT theme, signal_strength, policy_basis
                FROM annual_sector_signals
                WHERE apply_year = %s
                ORDER BY as_of_date DESC
                """,
                (apply_year,),
            )
            seen = set()
            rows = []
            for theme, strength, basis in cur.fetchall():
                if theme in seen:
                    continue
                seen.add(theme)
                rows.append((theme, strength, basis))
    return {
        t: {"signal_strength": s, "policy_basis": b}
        for t, s, b in rows
    }


def load_news(conn, apply_year: int) -> dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT theme, net_score FROM theme_news_signals WHERE apply_year=%s",
            (apply_year,),
        )
        return {t: float(v) for t, v in cur.fetchall()}


def load_theme_map(conn, suffix: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, index_name, theme, relevance
            FROM theme_index_map WHERE ts_code LIKE %s
            """,
            (f"%.{suffix}",),
        )
        return [
            {"ts_code": r[0], "index_name": r[1], "theme": r[2], "relevance": r[3]}
            for r in cur.fetchall()
        ]


def load_prices(conn, suffix: str, apply_year: int) -> dict[str, list[tuple[date, float]]]:
    start, end = price_window(apply_year)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, trade_date, close FROM index_daily
            WHERE ts_code LIKE %s AND trade_date BETWEEN %s AND %s
            ORDER BY ts_code, trade_date
            """,
            (f"%.{suffix}", start, end),
        )
        result: dict[str, list] = defaultdict(list)
        for ts, td, cl in cur.fetchall():
            if cl is not None:
                result[ts].append((td, float(cl)))
    return result


def load_valuations(conn, suffix: str, apply_year: int) -> dict[str, list[float]]:
    start, end = valuation_window(apply_year)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, pb FROM index_dailybasic
            WHERE ts_code LIKE %s AND trade_date BETWEEN %s AND %s
              AND pb IS NOT NULL AND pb > 0
            ORDER BY ts_code, trade_date
            """,
            (f"%.{suffix}", start, end),
        )
        result: dict[str, list] = defaultdict(list)
        for ts, pb in cur.fetchall():
            result[ts].append(float(pb))
    return result


def print_ranking(rows: list[dict], top: int, apply_year: int) -> None:
    print(f"\n{'='*95}")
    print(f"  {apply_year} 年度 CSI 指数推荐 Top-{top}")
    print(f"{'='*95}")
    print(f"{'#':<4} {'代码':<14} {'名称':<22} {'政策':>5} {'新闻':>6} {'动量':>8} {'PB分位':>7} {'综合':>6}  题材")
    print("-" * 95)
    for i, row in enumerate(rows[:top], 1):
        mom = f"{row['momentum']*100:+.1f}%" if row.get("momentum") is not None else "  N/A"
        pbp = f"{row['pb_pct']*100:.0f}%" if row.get("pb_pct") is not None else "  N/A"
        ns = f"{row.get('news_score', 0):+.2f}"
        themes = " / ".join(row.get("all_themes", [])[:2])
        print(
            f"{i:<4} {row['ts_code']:<14} {row['index_name']:<22} "
            f"{row.get('policy_score', 0):>5.1f} {ns:>6} {mom:>8} {pbp:>7} "
            f"{row['final_score']:>6.3f}  {themes}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--suffix", default="CSI", choices=["CSI", "SI", "all"])
    parser.add_argument("--min-signal", default="弱")
    parser.add_argument("--save", action="store_true", help="Write to csi_annual_recommendation")
    parser.add_argument("--full", action="store_true", help="Apply duration, heat, scorecard (no corr dedup by default)")
    parser.add_argument("--full-dedupe", action="store_true", help="Also apply correlation dedup on Top-N")
    parser.add_argument("--no-news", action="store_true", help="Ablation: ignore news signals")
    args = parser.parse_args()

    apply_year = args.year
    as_of = year_as_of(apply_year)
    cutoff = date(apply_year - 1, 12, 31)

    conn = get_connection()
    ensure_tables(conn)

    signals = load_signals(conn, apply_year, as_of)
    news = {} if args.no_news else load_news(conn, apply_year)
    has_news = len(news) > 0 and not args.no_news

    print(f"apply_year={apply_year} as_of={as_of} signals={len(signals)} news_themes={len(news)}")

    suffixes = ["CSI", "SI"] if args.suffix == "all" else [args.suffix]
    all_rows: list[dict] = []

    for suffix in suffixes:
        theme_map = load_theme_map(conn, suffix)
        prices = load_prices(conn, suffix, apply_year)
        vals = load_valuations(conn, suffix, apply_year)
        rows = rank_indices(
            signals=signals,
            news=news,
            theme_map=theme_map,
            price_data=prices,
            val_data=vals,
            as_of=cutoff,
            suffix=suffix,
            min_signal=args.min_signal,
            has_news=has_news,
        )
        if args.full:
            price_closes = {ts: [c for _, c in ser] for ts, ser in prices.items()}
            for row in rows:
                sc = compute_index_scorecard(conn, row["ts_code"], prices.get(row["ts_code"], []), cutoff)
                from csi.enhanced import SCORECARD_BLEND_WEIGHT
                row["index_scorecard"] = round(sc, 4)
                row["final_score"] = row.get("final_score", 0) + SCORECARD_BLEND_WEIGHT * sc
            top_n = args.top if args.full_dedupe else None
            rows = apply_enhancements(
                conn, rows, apply_year=apply_year, price_closes=price_closes,
                top_n=top_n,
            )
        all_rows.extend(rows)
        print(f"  .{suffix}: {len(rows)} indices scored")

    all_rows.sort(key=lambda r: -r["final_score"])

    if args.save:
        n = save_recommendations(conn, apply_year, as_of, all_rows)
        print(f"\nSaved {n} rows → csi_annual_recommendation")

    print_ranking(all_rows, args.top, apply_year)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

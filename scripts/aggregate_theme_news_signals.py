#!/usr/bin/env python3
"""Aggregate theme signals for CSI ranking (annual rollup from daily or events).

Prefers theme_news_daily rollup (salience_v1). Falls back to event-level or flat extraction.

Usage:
  python scripts/aggregate_theme_news_signals.py --year 2026
  python scripts/aggregate_theme_news_signals.py --year 2026 --method flat
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csi.ranking import news_window, year_as_of
from db.connection import get_connection
from collectors.enrichment import CANONICAL_THEMES
from news.processing.daily import rollup_window
from news.processing.salience import event_salience_weight, sentiment_sign
from news.processing.schema import ensure_processing_schema

SCHEMA_PATH = ROOT / "sql" / "theme_news_signals_schema.sql"


def ensure_tables(conn) -> None:
    ensure_processing_schema(conn)
    if SCHEMA_PATH.exists():
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                cur.execute(stmt)
        conn.commit()


def _aggregate_flat(conn, w_start: date, w_end: date) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.themes, e.sentiment, e.magnitude, e.confidence
            FROM news_extraction e
            JOIN news_article a ON a.id = e.article_id
            WHERE DATE(COALESCE(a.pub_time, a.created_at)) >= %s
              AND DATE(COALESCE(a.pub_time, a.created_at)) <= %s
            """,
            (w_start, w_end),
        )
        rows = cur.fetchall()

    theme_stats = {t: {"bull": 0.0, "bear": 0.0, "count": 0, "mag_sum": 0.0, "conf_sum": 0.0} for t in CANONICAL_THEMES}
    for themes_json, sentiment, magnitude, confidence in rows:
        try:
            themes = json.loads(themes_json) if isinstance(themes_json, str) else (themes_json or [])
        except json.JSONDecodeError:
            themes = []
        if not isinstance(themes, list):
            continue
        mag = float(magnitude or 1)
        conf = float(confidence or 0.8)
        sign = sentiment_sign(sentiment)
        for theme in themes:
            if theme not in theme_stats:
                continue
            st = theme_stats[theme]
            st["count"] += 1
            st["mag_sum"] += mag
            st["conf_sum"] += conf
            weighted = sign * mag * conf
            if sign > 0:
                st["bull"] += weighted
            elif sign < 0:
                st["bear"] += abs(weighted)

    out: dict[str, dict] = {}
    for theme, st in theme_stats.items():
        if st["count"] == 0:
            continue
        n = st["count"]
        out[theme] = {
            "net_score": round(st["bull"] - st["bear"], 4),
            "bull_score": round(st["bull"], 4),
            "bear_score": round(st["bear"], 4),
            "article_count": n,
            "event_count": 0,
            "mention_count": n,
            "source_diversity": 0,
            "avg_magnitude": round(st["mag_sum"] / n, 2),
            "avg_confidence": round(st["conf_sum"] / n, 2),
        }
    return out


def _aggregate_events(conn, w_start: date, w_end: date) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ev.mention_count, ev.unique_sources, ev.duration_days,
                   e.sentiment, e.magnitude, e.confidence, e.themes
            FROM news_event ev
            JOIN news_extraction e ON e.article_id = ev.canonical_article_id
            WHERE DATE(ev.last_seen) >= %s AND DATE(ev.first_seen) <= %s
            """,
            (w_start, w_end),
        )
        rows = cur.fetchall()

    theme_stats = {
        t: {"bull": 0.0, "bear": 0.0, "events": 0, "mentions": 0, "sources": set(), "mag_sum": 0.0, "conf_sum": 0.0}
        for t in CANONICAL_THEMES
    }
    for mentions, sources, duration, sentiment, magnitude, confidence, themes_json in rows:
        try:
            themes = json.loads(themes_json) if isinstance(themes_json, str) else (themes_json or [])
        except json.JSONDecodeError:
            themes = []
        if not isinstance(themes, list):
            continue
        mag = float(magnitude or 1)
        conf = float(confidence or 0.8)
        sign = sentiment_sign(sentiment)
        weight = event_salience_weight(
            sign=1.0,
            magnitude=mag,
            confidence=conf,
            mention_count=int(mentions or 1),
            unique_sources=int(sources or 1),
            duration_days=int(duration or 1),
        )
        for theme in themes:
            if theme not in theme_stats:
                continue
            st = theme_stats[theme]
            st["events"] += 1
            st["mentions"] += int(mentions or 1)
            st["mag_sum"] += mag
            st["conf_sum"] += conf
            signed = weight * sign
            if signed > 0:
                st["bull"] += signed
            elif signed < 0:
                st["bear"] += abs(signed)

    out: dict[str, dict] = {}
    for theme, st in theme_stats.items():
        if st["events"] == 0:
            continue
        n = st["events"]
        out[theme] = {
            "net_score": round(st["bull"] - st["bear"], 4),
            "bull_score": round(st["bull"], 4),
            "bear_score": round(st["bear"], 4),
            "article_count": st["mentions"],
            "event_count": n,
            "mention_count": st["mentions"],
            "source_diversity": 0,
            "avg_magnitude": round(st["mag_sum"] / n, 2),
            "avg_confidence": round(st["conf_sum"] / n, 2),
        }
    return out


def aggregate(
    conn,
    apply_year: int,
    *,
    dry_run: bool = False,
    live: bool = False,
    method: str = "auto",
) -> list[dict]:
    as_of = year_as_of(apply_year)
    w_start, w_end = news_window(apply_year)
    if live:
        w_start = date(apply_year, 1, 1)
        w_end = date.today()
        as_of = w_end

    def _signal_strength(theme_map: dict[str, dict]) -> tuple[int, int, float]:
        """Higher is better: (theme_count, total_mentions, total_abs_net)."""
        if not theme_map:
            return 0, 0, 0.0
        mentions = sum(int(v.get("mention_count") or v.get("article_count") or 0) for v in theme_map.values())
        return len(theme_map), mentions, sum(abs(v.get("net_score", 0)) for v in theme_map.values())

    used_method = method
    if method == "auto":
        candidates: list[tuple[str, dict[str, dict]]] = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(event_count), 0), COALESCE(SUM(mention_count), 0)
                FROM theme_news_daily WHERE signal_date BETWEEN %s AND %s
                """,
                (w_start, w_end),
            )
            daily_rows, daily_events, daily_mentions = cur.fetchone()
        if daily_rows and (daily_events >= 30 or daily_mentions >= 50):
            candidates.append(("daily", rollup_window(conn, w_start, w_end)))
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM news_event")
            ev_n = cur.fetchone()[0]
        if ev_n > 0:
            candidates.append(("events", _aggregate_events(conn, w_start, w_end)))
        candidates.append(("flat", _aggregate_flat(conn, w_start, w_end)))
        used_method, theme_map = max(candidates, key=lambda c: _signal_strength(c[1]))

    if method != "auto":
        if used_method == "daily":
            theme_map = rollup_window(conn, w_start, w_end)
            for t, v in theme_map.items():
                v["article_count"] = v.get("mention_count", 0)
        elif used_method == "events":
            theme_map = _aggregate_events(conn, w_start, w_end)
        else:
            theme_map = _aggregate_flat(conn, w_start, w_end)
    elif used_method == "daily":
        for t, v in theme_map.items():
            v["article_count"] = v.get("mention_count", 0)

    results = [{"theme": t, **v} for t, v in theme_map.items()]

    if dry_run:
        for r in results:
            r["method"] = used_method
        return results

    with conn.cursor() as cur:
        for r in results:
            cur.execute(
                """
                INSERT INTO theme_news_signals
                    (apply_year, as_of_date, window_start, window_end, theme,
                     net_score, bull_score, bear_score, article_count,
                     event_count, mention_count, source_diversity,
                     avg_magnitude, avg_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    net_score=VALUES(net_score), bull_score=VALUES(bull_score),
                    bear_score=VALUES(bear_score), article_count=VALUES(article_count),
                    event_count=VALUES(event_count), mention_count=VALUES(mention_count),
                    source_diversity=VALUES(source_diversity),
                    avg_magnitude=VALUES(avg_magnitude), avg_confidence=VALUES(avg_confidence),
                    window_start=VALUES(window_start), window_end=VALUES(window_end),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    apply_year, as_of, w_start, w_end, r["theme"],
                    r["net_score"], r["bull_score"], r["bear_score"],
                    r.get("article_count", 0),
                    r.get("event_count", 0),
                    r.get("mention_count", 0),
                    r.get("source_diversity", 0),
                    r.get("avg_magnitude"),
                    r.get("avg_confidence"),
                ),
            )
    conn.commit()
    for r in results:
        r["method"] = used_method
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True, help="apply_year e.g. 2026")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="Use Y-01-01..today window")
    parser.add_argument("--method", choices=["auto", "daily", "events", "flat"], default="auto")
    args = parser.parse_args()

    conn = get_connection()
    ensure_tables(conn)
    results = aggregate(conn, args.year, dry_run=args.dry_run, live=args.live, method=args.method)
    conn.close()

    method = results[0]["method"] if results else args.method
    print(f"apply_year={args.year} method={method} themes_with_news={len(results)}")
    for r in sorted(results, key=lambda x: -x["net_score"])[:10]:
        print(
            f"  {r['theme']:20s} net={r['net_score']:+.2f} "
            f"events={r.get('event_count', 0)} mentions={r.get('mention_count', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

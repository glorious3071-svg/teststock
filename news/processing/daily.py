"""Daily theme signal aggregation from event extractions (L3)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pymysql

from collectors.enrichment import CANONICAL_THEMES
from news.processing.salience import event_salience_weight, sentiment_sign

MODEL_VERSION = "salience_v1"


def aggregate_daily(
    conn: pymysql.connections.Connection,
    signal_date: date,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Build theme_news_daily from events with extractions active on signal_date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ev.id, ev.mention_count, ev.unique_sources, ev.duration_days,
                   e.sentiment, e.magnitude, e.confidence, e.themes, ev.sources_json,
                   a.category,
                   COALESCE((
                       SELECT SUM(GREATEST(mc.mention_count - 1, 0))
                       FROM news_event_member mem
                       JOIN news_mention_counter mc ON mc.canonical_article_id = mem.article_id
                       WHERE mem.event_id = ev.id
                   ), 0) AS extra_mentions
            FROM news_event ev
            JOIN news_extraction e ON e.article_id = ev.canonical_article_id
            JOIN news_article a ON a.id = ev.canonical_article_id
            WHERE DATE(COALESCE(a.pub_time, a.created_at)) = %s
            """,
            (signal_date,),
        )
        rows = cur.fetchall()

    theme_stats: dict[str, dict] = {
        t: {
            "bull": 0.0,
            "bear": 0.0,
            "events": 0,
            "mentions": 0,
            "sources": set(),
            "mag_sum": 0.0,
            "conf_sum": 0.0,
        }
        for t in CANONICAL_THEMES
    }

    for _eid, mentions, sources, duration, sentiment, magnitude, confidence, themes_json, sources_json, category, extra_mentions in rows:
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
            category=category,
            extra_mentions=int(extra_mentions or 0),
        )
        src_list = json.loads(sources_json) if isinstance(sources_json, str) else (sources_json or [])
        if not isinstance(src_list, list):
            src_list = []

        for theme in themes:
            if theme not in theme_stats:
                continue
            st = theme_stats[theme]
            st["events"] += 1
            st["mentions"] += int(mentions or 1)
            st["sources"].update(src_list)
            st["mag_sum"] += mag
            st["conf_sum"] += conf
            signed = weight * sign
            if signed > 0:
                st["bull"] += signed
            elif signed < 0:
                st["bear"] += abs(signed)

    results = []
    for theme, st in theme_stats.items():
        if st["events"] == 0:
            continue
        n = st["events"]
        results.append({
            "theme": theme,
            "net_score": round(st["bull"] - st["bear"], 4),
            "bull_score": round(st["bull"], 4),
            "bear_score": round(st["bear"], 4),
            "event_count": n,
            "mention_count": st["mentions"],
            "source_diversity": len(st["sources"]),
            "avg_magnitude": round(st["mag_sum"] / n, 2),
            "avg_confidence": round(st["conf_sum"] / n, 2),
        })

    if dry_run:
        return results

    with conn.cursor() as cur:
        for r in results:
            cur.execute(
                """
                INSERT INTO theme_news_daily
                    (signal_date, theme, net_score, bull_score, bear_score,
                     event_count, mention_count, source_diversity,
                     avg_magnitude, avg_confidence, model_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    net_score=VALUES(net_score), bull_score=VALUES(bull_score),
                    bear_score=VALUES(bear_score), event_count=VALUES(event_count),
                    mention_count=VALUES(mention_count), source_diversity=VALUES(source_diversity),
                    avg_magnitude=VALUES(avg_magnitude), avg_confidence=VALUES(avg_confidence),
                    model_version=VALUES(model_version), updated_at=CURRENT_TIMESTAMP
                """,
                (
                    signal_date, r["theme"], r["net_score"], r["bull_score"], r["bear_score"],
                    r["event_count"], r["mention_count"], r["source_diversity"],
                    r["avg_magnitude"], r["avg_confidence"], MODEL_VERSION,
                ),
            )
    conn.commit()
    return results


def rollup_window(
    conn: pymysql.connections.Connection,
    window_start: date,
    window_end: date,
) -> dict[str, dict]:
    """Sum theme_news_daily over a date window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT theme,
                   SUM(net_score), SUM(bull_score), SUM(bear_score),
                   SUM(event_count), SUM(mention_count), MAX(source_diversity),
                   AVG(avg_magnitude), AVG(avg_confidence)
            FROM theme_news_daily
            WHERE signal_date >= %s AND signal_date <= %s
            GROUP BY theme
            """,
            (window_start, window_end),
        )
        out: dict[str, dict] = {}
        for row in cur.fetchall():
            out[row[0]] = {
                "net_score": float(row[1] or 0),
                "bull_score": float(row[2] or 0),
                "bear_score": float(row[3] or 0),
                "event_count": int(row[4] or 0),
                "mention_count": int(row[5] or 0),
                "source_diversity": int(row[6] or 0),
                "avg_magnitude": float(row[7] or 0),
                "avg_confidence": float(row[8] or 0),
            }
        return out


def rollup_weekly(
    conn: pymysql.connections.Connection,
    week_end: date,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Aggregate theme_news_daily for ISO week ending week_end (Monday-based)."""
    week_start = week_end - timedelta(days=6)
    theme_map = rollup_window(conn, week_start, week_end)
    if not theme_map:
        return []
    results = [{"theme": t, **v} for t, v in theme_map.items()]
    if dry_run:
        return results
    with conn.cursor() as cur:
        for r in results:
            cur.execute(
                """
                INSERT INTO theme_news_weekly
                    (week_start, theme, net_score, bull_score, bear_score,
                     event_count, mention_count, source_diversity, model_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    net_score=VALUES(net_score), bull_score=VALUES(bull_score),
                    bear_score=VALUES(bear_score), event_count=VALUES(event_count),
                    mention_count=VALUES(mention_count), source_diversity=VALUES(source_diversity),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    week_start, r["theme"], r["net_score"], r["bull_score"], r["bear_score"],
                    r.get("event_count", 0), r.get("mention_count", 0),
                    r.get("source_diversity", 0), MODEL_VERSION,
                ),
            )
    conn.commit()
    return results

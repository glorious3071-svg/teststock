"""Event clustering (L1): 72h sliding window, title similarity."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pymysql

from news.processing.fingerprint import event_fingerprint, title_similarity

SIMILARITY_THRESHOLD = 0.72
CLUSTER_LOOKBACK_HOURS = 72


@dataclass
class ArticleRow:
    id: int
    source: str
    category: str
    pub_time: datetime | None
    title: str
    body_len: int


@dataclass
class ClusterStats:
    created: int = 0
    updated: int = 0
    scanned: int = 0


def _article_time(a: ArticleRow) -> datetime:
    return a.pub_time or datetime.min


def _duration_days(first: datetime, last: datetime) -> int:
    return max(1, (last.date() - first.date()).days + 1)


def fetch_unclustered(
    conn: pymysql.connections.Connection,
    *,
    window_start: datetime,
    window_end: datetime,
    limit: int | None = None,
) -> list[ArticleRow]:
    sql = """
        SELECT a.id, a.source, a.category, a.pub_time, a.title,
               COALESCE(LENGTH(a.body_text), 0) AS body_len
        FROM news_article a
        LEFT JOIN news_event_member m ON m.article_id = a.id
        WHERE m.article_id IS NULL
          AND COALESCE(a.pub_time, a.created_at) >= %s
          AND COALESCE(a.pub_time, a.created_at) <= %s
        ORDER BY COALESCE(a.pub_time, a.created_at), a.id
    """
    params: list = [window_start, window_end]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [
            ArticleRow(id=r[0], source=r[1], category=r[2], pub_time=r[3], title=r[4], body_len=r[5])
            for r in cur.fetchall()
        ]


def fetch_open_events(
    conn: pymysql.connections.Connection,
    *,
    since: datetime,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_fingerprint, canonical_article_id, title_norm,
                   category, first_seen, last_seen, mention_count, unique_sources, sources_json
            FROM news_event
            WHERE status = 'open' AND last_seen >= %s
            ORDER BY last_seen DESC
            """,
            (since,),
        )
        rows = []
        for r in cur.fetchall():
            sources = json.loads(r[9]) if r[9] else []
            rows.append({
                "id": r[0],
                "fingerprint": r[1],
                "canonical_id": r[2],
                "title_norm": r[3],
                "category": r[4],
                "first_seen": r[5],
                "last_seen": r[6],
                "mention_count": r[7],
                "unique_sources": r[8],
                "sources": sources if isinstance(sources, list) else [],
            })
        return rows


def _pick_canonical(existing_id: int, existing_body_len: int, article: ArticleRow) -> tuple[int, int]:
    if article.body_len > existing_body_len:
        return article.id, article.body_len
    return existing_id, existing_body_len


def _load_canonical_body_len(conn: pymysql.connections.Connection, article_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(LENGTH(body_text), 0) FROM news_article WHERE id=%s", (article_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _attach_to_event(
    conn: pymysql.connections.Connection,
    event: dict,
    article: ArticleRow,
    *,
    is_new_canonical: bool,
) -> None:
    sources = list(event["sources"])
    if article.source not in sources:
        sources.append(article.source)
    mention = event["mention_count"] + 1
    unique_src = len(sources)
    last_seen = max(event["last_seen"], _article_time(article))
    first_seen = min(event["first_seen"], _article_time(article))
    duration = _duration_days(first_seen, last_seen)
    canonical_id = article.id if is_new_canonical else event["canonical_id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE news_event
            SET canonical_article_id=%s, last_seen=%s, mention_count=%s,
                unique_sources=%s, sources_json=%s, duration_days=%s, updated_at=CURRENT_TIMESTAMP
            WHERE id=%s
            """,
            (
                canonical_id,
                last_seen,
                mention,
                unique_src,
                json.dumps(sources, ensure_ascii=False),
                duration,
                event["id"],
            ),
        )
        if is_new_canonical:
            cur.execute(
                "UPDATE news_event_member SET is_canonical=0 WHERE event_id=%s",
                (event["id"],),
            )
        cur.execute(
            """
            INSERT INTO news_event_member (event_id, article_id, source, is_canonical, joined_at)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)
            """,
            (event["id"], article.id, article.source, 1 if is_new_canonical else 0, _article_time(article)),
        )
    conn.commit()
    event["mention_count"] = mention
    event["unique_sources"] = unique_src
    event["sources"] = sources
    event["last_seen"] = last_seen
    event["first_seen"] = first_seen
    event["canonical_id"] = canonical_id


def _create_event(conn: pymysql.connections.Connection, article: ArticleRow) -> dict:
    fp = event_fingerprint(article.title)
    title_norm = article.title[:490]
    t = _article_time(article)
    sources = [article.source]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_event
                (event_fingerprint, canonical_article_id, title_norm, category,
                 first_seen, last_seen, mention_count, unique_sources, sources_json, duration_days)
            VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s, 1)
            """,
            (fp, article.id, title_norm, article.category, t, t, json.dumps(sources, ensure_ascii=False)),
        )
        event_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO news_event_member (event_id, article_id, source, is_canonical, joined_at)
            VALUES (%s, %s, %s, 1, %s)
            """,
            (event_id, article.id, article.source, t),
        )
    conn.commit()
    return {
        "id": event_id,
        "fingerprint": fp,
        "canonical_id": article.id,
        "title_norm": title_norm,
        "category": article.category,
        "first_seen": t,
        "last_seen": t,
        "mention_count": 1,
        "unique_sources": 1,
        "sources": sources,
    }


def find_matching_event(events: list[dict], article: ArticleRow) -> dict | None:
    fp = event_fingerprint(article.title)
    best: dict | None = None
    best_sim = 0.0
    for ev in events:
        if ev["fingerprint"] == fp:
            return ev
        sim = title_similarity(ev["title_norm"], article.title)
        if sim >= SIMILARITY_THRESHOLD and sim > best_sim:
            best_sim = sim
            best = ev
    return best


def cluster_articles(
    conn: pymysql.connections.Connection,
    *,
    process_date: date | None = None,
    lookback_hours: int = CLUSTER_LOOKBACK_HOURS,
    limit: int | None = None,
) -> ClusterStats:
    """Assign unclustered articles in window to news_event clusters."""
    stats = ClusterStats()
    end = datetime.combine(process_date or date.today(), datetime.max.time().replace(microsecond=0))
    start = end - timedelta(hours=lookback_hours)

    articles = fetch_unclustered(conn, window_start=start, window_end=end, limit=limit)
    stats.scanned = len(articles)
    if not articles:
        return stats

    open_since = end - timedelta(hours=lookback_hours)
    open_events = fetch_open_events(conn, since=open_since)

    for article in articles:
        match = find_matching_event(open_events, article)
        if match:
            body_len = _load_canonical_body_len(conn, match["canonical_id"])
            new_canonical, new_len = _pick_canonical(match["canonical_id"], body_len, article)
            _attach_to_event(conn, match, article, is_new_canonical=new_canonical != match["canonical_id"])
            stats.updated += 1
            if new_canonical != match["canonical_id"]:
                match["canonical_id"] = new_canonical
        else:
            ev = _create_event(conn, article)
            open_events.insert(0, ev)
            stats.created += 1

    return stats


def cluster_stream_all(
    conn: pymysql.connections.Connection,
    *,
    commit_every: int = 500,
) -> ClusterStats:
    """Single-pass chronological clustering for backfill (faster than day-by-day)."""
    stats = ClusterStats()
    articles = fetch_unclustered(
        conn,
        window_start=datetime(2000, 1, 1),
        window_end=datetime.now(),
    )
    stats.scanned = len(articles)
    if not articles:
        return stats

    open_events: list[dict] = []
    for i, article in enumerate(articles, 1):
        # Prune open events older than 72h from current article
        t = _article_time(article)
        open_events = [ev for ev in open_events if (t - ev["last_seen"]).total_seconds() <= CLUSTER_LOOKBACK_HOURS * 3600]

        match = find_matching_event(open_events, article)
        if match:
            body_len = _load_canonical_body_len(conn, match["canonical_id"])
            new_canonical, _ = _pick_canonical(match["canonical_id"], body_len, article)
            _attach_to_event(conn, match, article, is_new_canonical=new_canonical != match["canonical_id"])
            stats.updated += 1
            if new_canonical != match["canonical_id"]:
                match["canonical_id"] = new_canonical
        else:
            ev = _create_event(conn, article)
            open_events.insert(0, ev)
            stats.created += 1

        if i % commit_every == 0:
            conn.commit()
    conn.commit()
    return stats


def cluster_all_remaining(
    conn: pymysql.connections.Connection,
    *,
    batch_days: int = 7,
) -> ClusterStats:
    """Backfill: iterate calendar days until no unclustered articles remain."""
    total = ClusterStats()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MIN(DATE(COALESCE(pub_time, created_at))),
                   MAX(DATE(COALESCE(pub_time, created_at)))
            FROM news_article a
            LEFT JOIN news_event_member m ON m.article_id = a.id
            WHERE m.article_id IS NULL
            """
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return total
    d0, d1 = row[0], row[1] or date.today()
    d = d0
    while d <= d1:
        st = cluster_articles(conn, process_date=d)
        total.scanned += st.scanned
        total.created += st.created
        total.updated += st.updated
        d += timedelta(days=1)
    # Final pass on today for stragglers
    st2 = cluster_articles(conn, process_date=date.today())
    total.scanned += st2.scanned
    total.created += st2.created
    total.updated += st2.updated
    return total


def close_stale_events(
    conn: pymysql.connections.Connection,
    *,
    before: datetime,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE news_event SET status='closed' WHERE status='open' AND last_seen < %s",
            (before,),
        )
        n = cur.rowcount
    conn.commit()
    return n

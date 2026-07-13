"""Index scorecard four-dimension scoring (simplified v2 spec)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pymysql


def _percentile_below(history: list[float], current: float) -> float:
    if not history:
        return 50.0
    below = sum(1 for v in history if v < current)
    return 100.0 * below / len(history)


def load_index_basics(
    conn: pymysql.connections.Connection,
    ts_code: str,
    as_of: date,
    years: int = 10,
) -> dict[str, Any]:
    start = date(as_of.year - years, 1, 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, pe_ttm, pb, total_mv
            FROM index_dailybasic
            WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
              AND trade_date <= %s
            ORDER BY trade_date
            """,
            (ts_code, start, as_of, as_of),
        )
        rows = cur.fetchall()
    if not rows:
        return {}
    pe_hist = [float(r[1]) for r in rows if r[1] and float(r[1]) > 0]
    pb_hist = [float(r[2]) for r in rows if r[2] and float(r[2]) > 0]
    cur_pe = pe_hist[-1] if pe_hist else None
    cur_pb = pb_hist[-1] if pb_hist else None
    return {
        "pe_hist": pe_hist,
        "pb_hist": pb_hist,
        "cur_pe": cur_pe,
        "cur_pb": cur_pb,
    }


def score_valuation(basics: dict[str, Any]) -> float:
    if not basics:
        return 0.0
    score = 0.0
    pe = basics.get("cur_pe")
    pe_hist = basics.get("pe_hist", [])
    if pe and pe_hist:
        pctl = _percentile_below(pe_hist, pe)
        if pctl < 15:
            score -= 2
        elif pctl < 30:
            score -= 1
        elif pctl > 85:
            score += 2
        elif pctl > 70:
            score += 1
    pb = basics.get("cur_pb")
    pb_hist = basics.get("pb_hist", [])
    if pb and pb_hist:
        pctl = _percentile_below(pb_hist, pb)
        if pctl < 15:
            score -= 1
        elif pctl > 85:
            score += 1
    return max(-4, min(4, score))


def score_momentum(prices: list[tuple[date, float]], as_of: date) -> float:
    if len(prices) < 60:
        return 0.0
    series = [(d, c) for d, c in prices if d <= as_of]
    if len(series) < 60:
        return 0.0
    end = series[-1][1]
    def ret(days: int) -> float | None:
        if len(series) <= days:
            return None
        start = series[-days - 1][1]
        if start <= 0:
            return None
        return end / start - 1
    r12 = ret(min(252, len(series) - 1))
    score = 0.0
    if r12 is not None:
        if r12 < -0.30:
            score -= 1
        elif r12 > 0.50:
            score += 2
        elif r12 > 0.20:
            score += 1
    return max(-3, min(3, score))


def compute_index_scorecard(
    conn: pymysql.connections.Connection,
    ts_code: str,
    prices: list[tuple[date, float]],
    as_of: date,
) -> float:
    """Return normalized scorecard contribution in [-1, 1] for ranking blend."""
    basics = load_index_basics(conn, ts_code, as_of)
    val = score_valuation(basics)
    mom = score_momentum(prices, as_of)
    raw = val + mom  # [-7, 7] approx
    return raw / 7.0

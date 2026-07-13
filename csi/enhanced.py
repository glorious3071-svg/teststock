"""Enhanced CSI ranking: duration_mult, heat_penalty, correlation dedup."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pymysql

THEME_DURATION_MULT_PER_YEAR = 0.15
THEME_DURATION_MULT_CAP = 2.0
MOMENTUM_HEAT_THRESHOLD = 0.50
MOMENTUM_HEAT_WEIGHT = 0.10
SCORECARD_BLEND_WEIGHT = 0.12
CORR_THRESHOLD = 0.85
MOMENTUM_DAYS = 125


def get_theme_duration(conn: pymysql.connections.Connection, theme: str, apply_year: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT apply_year) FROM annual_sector_signals
            WHERE theme=%s AND apply_year <= %s AND signal_strength IN ('强','中')
            """,
            (theme, apply_year),
        )
        row = cur.fetchone()
    return int(row[0] or 1)


def heat_penalty(price_closes: list[float]) -> float:
    lookback = MOMENTUM_DAYS * 2
    if len(price_closes) < lookback:
        return 0.0
    start, end = price_closes[-lookback], price_closes[-1]
    if start <= 0:
        return 0.0
    cum_2y = end / start - 1
    if cum_2y <= MOMENTUM_HEAT_THRESHOLD:
        return 0.0
    excess = (cum_2y - MOMENTUM_HEAT_THRESHOLD) / MOMENTUM_HEAT_THRESHOLD
    return -min(excess, 1.0)


def duration_multiplier(years: int) -> float:
    return min(1.0 + THEME_DURATION_MULT_PER_YEAR * max(years - 1, 0), THEME_DURATION_MULT_CAP)


def dedupe_by_correlation(
    rows: list[dict],
    price_closes: dict[str, list[float]],
    top_n: int,
) -> list[dict]:
    """Keep low-correlation subset from ranked rows."""
    selected: list[dict] = []
    selected_rets: list[np.ndarray] = []

    def log_returns(ts: str) -> np.ndarray | None:
        ps = price_closes.get(ts, [])
        if len(ps) < 20:
            return None
        arr = np.array(ps, dtype=float)
        return np.diff(np.log(arr))

    for row in rows:
        if len(selected) >= top_n:
            break
        ts = row["ts_code"]
        rets = log_returns(ts)
        if rets is None:
            continue
        too_corr = False
        for existing in selected_rets:
            n = min(len(rets), len(existing))
            if n < 20:
                continue
            rho = float(np.corrcoef(rets[-n:], existing[-n:])[0, 1])
            if rho > CORR_THRESHOLD:
                too_corr = True
                break
        if not too_corr:
            selected.append(row)
            selected_rets.append(rets)
    if not selected:
        return rows[:top_n]
    return selected


def apply_enhancements(
    conn: pymysql.connections.Connection,
    rows: list[dict],
    *,
    apply_year: int,
    price_closes: dict[str, list[float]],
    top_n: int | None = None,
) -> list[dict]:
    """Apply heat penalty and correlation dedup to ranked rows."""
    theme_dur_cache: dict[str, int] = {}

    for row in rows:
        theme = row.get("best_theme") or ""
        if theme and theme not in theme_dur_cache:
            theme_dur_cache[theme] = get_theme_duration(conn, theme, apply_year)
        dur = theme_dur_cache.get(theme, 1)
        dur_mult = duration_multiplier(dur)
        if row.get("raw_policy"):
            row["raw_policy"] = row["raw_policy"] * dur_mult
            row["policy_score"] = round(row.get("policy_score", 0) * dur_mult, 2)
        ts = row["ts_code"]
        heat = heat_penalty(price_closes.get(ts, []))
        row["heat_penalty"] = round(heat, 4)
        row["final_score"] = row.get("final_score", 0) + MOMENTUM_HEAT_WEIGHT * heat

    rows.sort(key=lambda r: -r["final_score"])
    if top_n:
        return dedupe_by_correlation(rows, price_closes, top_n)
    return rows

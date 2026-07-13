"""CSI annual index ranking core logic."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

STRENGTH_SCORE = {"强": 3, "中": 2, "弱": 1}
RELEVANCE_SCORE = {"强": 3, "中": 2, "弱": 1}
MOMENTUM_WINDOW_DAYS = 125
VALUATION_HISTORY_YEARS = 5

DEFAULT_WEIGHTS_WITH_NEWS = {"policy": 0.35, "news": 0.10, "momentum": 0.30, "valuation": 0.20}
DEFAULT_WEIGHTS_NO_NEWS = {"policy": 0.50, "news": 0.0, "momentum": 0.30, "valuation": 0.20}


def year_as_of(apply_year: int) -> date:
    return date(apply_year, 1, 1)


def news_window(apply_year: int) -> tuple[date, date]:
    """H2 of prior year — policy/industry news most relevant for Jan allocation."""
    return date(apply_year - 1, 7, 1), date(apply_year - 1, 12, 31)


def price_window(apply_year: int) -> tuple[date, date]:
    end = date(apply_year - 1, 12, 31)
    start = end - timedelta(days=MOMENTUM_WINDOW_DAYS + 60)
    return start, end


def valuation_window(apply_year: int) -> tuple[date, date]:
    end = date(apply_year - 1, 12, 31)
    start = date(apply_year - 1 - VALUATION_HISTORY_YEARS, 1, 1)
    return start, end


def compute_momentum(price_series: list[tuple[date, float]], as_of: date) -> float | None:
    if len(price_series) < 20:
        return None
    end_prices = [(d, c) for d, c in price_series if d <= as_of]
    if not end_prices:
        end_prices = price_series
    end_date, end_close = end_prices[-1]
    lookback = [(d, c) for d, c in price_series if d <= end_date]
    if len(lookback) < MOMENTUM_WINDOW_DAYS:
        start_close = lookback[0][1]
    else:
        start_close = lookback[-MOMENTUM_WINDOW_DAYS][1]
    if start_close <= 0:
        return None
    return (end_close - start_close) / start_close


def compute_pb_percentile(pb_history: list[float], current_pb: float) -> float:
    if not pb_history:
        return float("nan")
    below = sum(1 for v in pb_history if v < current_pb)
    return below / len(pb_history)


def percentile_map(values: list[float]) -> dict[float, float]:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    return {v: i / max(n - 1, 1) for i, v in enumerate(s)}


def zscore_map(values: list[float], clip: float = 3.0) -> dict[float, float]:
    if not values:
        return {}
    import statistics

    mu = statistics.mean(values)
    std = statistics.pstdev(values)
    if std < 1e-9:
        return {v: 0.0 for v in values}
    return {v: max(-clip, min(clip, (v - mu) / std)) for v in values}


def rank_indices(
    *,
    signals: dict[str, dict],
    news: dict[str, float],
    theme_map: list[dict],
    price_data: dict[str, list[tuple[date, float]]],
    val_data: dict[str, list[float]],
    as_of: date,
    suffix: str = "CSI",
    min_signal: str = "弱",
    weights: dict[str, float] | None = None,
    has_news: bool = False,
) -> list[dict[str, Any]]:
    """Score and rank indices for one suffix (.CSI or .SI)."""
    min_strength = STRENGTH_SCORE.get(min_signal, 1)
    active = {
        t: s for t, s in signals.items()
        if STRENGTH_SCORE.get(s.get("signal_strength", "弱"), 0) >= min_strength
    }
    if not active:
        return []

    w = weights or (DEFAULT_WEIGHTS_WITH_NEWS if has_news else DEFAULT_WEIGHTS_NO_NEWS)

    relevant = [m for m in theme_map if m["theme"] in active or m["theme"] in news]
    code_best: dict[str, dict] = {}

    for m in relevant:
        theme = m["theme"]
        sig = active.get(theme)
        if sig:
            pscore = STRENGTH_SCORE[sig["signal_strength"]] * RELEVANCE_SCORE.get(m["relevance"], 1)
        else:
            pscore = 0
        nscore = news.get(theme, 0.0) * RELEVANCE_SCORE.get(m["relevance"], 1) / 3.0
        key = m["ts_code"]
        if key not in code_best or pscore > code_best[key].get("raw_policy", 0):
            code_best[key] = {
                "ts_code": m["ts_code"],
                "index_name": m["index_name"],
                "suffix": suffix,
                "best_theme": theme,
                "signal_strength": sig["signal_strength"] if sig else None,
                "relevance": m["relevance"],
                "raw_policy": pscore,
                "raw_news": nscore,
                "all_themes": [],
            }
        code_best[key]["all_themes"].append(
            f"{theme}[{m['relevance']}]"
            + (f"({sig['signal_strength']})" if sig else "")
        )

    rows = list(code_best.values())
    for row in rows:
        row["momentum"] = compute_momentum(price_data.get(row["ts_code"], []), as_of)
        history = val_data.get(row["ts_code"], [])
        if history:
            row["current_pb"] = history[-1]
            row["pb_pct"] = compute_pb_percentile(history, history[-1])
        else:
            row["current_pb"] = None
            row["pb_pct"] = None

    mom_vals = [r["momentum"] for r in rows if r["momentum"] is not None]
    mom_map = percentile_map(mom_vals)

    news_vals = [r["raw_news"] for r in rows if r["raw_news"]]
    news_z = zscore_map(news_vals) if has_news and news_vals else {}

    for row in rows:
        p_norm = row["raw_policy"] / 9.0
        mom_norm = mom_map.get(row["momentum"], 0.5) if row["momentum"] is not None else 0.5
        val_score = 1.0 - row["pb_pct"] if row.get("pb_pct") is not None else 0.5
        n_norm = news_z.get(row["raw_news"], 0.0) if has_news else 0.0
        # map z-score roughly to 0-1
        n_norm_01 = (n_norm + 3) / 6 if has_news else 0.0

        row["policy_score"] = round(p_norm * 9, 2)
        row["news_score"] = round(row["raw_news"], 4)
        row["final_score"] = (
            w["policy"] * p_norm
            + w["news"] * n_norm_01
            + w["momentum"] * mom_norm
            + w["valuation"] * val_score
        )

    rows.sort(key=lambda r: -r["final_score"])
    return rows


def save_recommendations(conn, apply_year: int, as_of: date, rows: list[dict]) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM csi_annual_recommendation WHERE apply_year=%s", (apply_year,))
        sql = """
            INSERT INTO csi_annual_recommendation
                (apply_year, as_of_date, rank_position, ts_code, index_name,
                 final_score, policy_score, news_score, momentum, pb_percentile,
                 best_theme, all_themes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        params = []
        for i, row in enumerate(rows, 1):
            params.append((
                apply_year, as_of, i, row["ts_code"], row["index_name"],
                round(row["final_score"], 6),
                row.get("policy_score"), row.get("news_score"),
                row.get("momentum"), row.get("pb_pct"),
                row.get("best_theme"),
                json.dumps(row.get("all_themes", []), ensure_ascii=False),
            ))
        if params:
            cur.executemany(sql, params)
    conn.commit()
    return len(params)

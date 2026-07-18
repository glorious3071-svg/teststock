#!/usr/bin/env python3
"""Map CSI recommendations to domestic passive ETFs with SH/SZ proxy fallback."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import statistics
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

EARLY_BROAD_PROXY_CODES = [
    "510050.SH",  # 上证50
    "510180.SH",  # 上证180
    "159901.SZ",  # 深证100
    "159902.SZ",  # 中小100
    "510880.SH",  # 上证红利
]


def parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 20 or len(xs) != len(ys):
        return None
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx <= 0 or sy <= 0:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / sx / sy


def load_index_series(cur, code: str, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    cur.execute(
        """
        SELECT trade_date, close
        FROM index_daily
        WHERE ts_code=%s AND trade_date BETWEEN %s AND %s AND close IS NOT NULL
        ORDER BY trade_date
        """,
        (code, start, end),
    )
    return {day: float(close) for day, close in cur.fetchall()}


def returns_by_day(series: dict[dt.date, float]) -> dict[dt.date, float]:
    days = sorted(series)
    out = {}
    for prev, day in zip(days[:-1], days[1:]):
        if series[prev] > 0:
            out[day] = series[day] / series[prev] - 1.0
    return out


def latest_price_before(cur, etf_code: str, as_of: dt.date) -> float | None:
    cur.execute(
        """
        SELECT close
        FROM fund_daily
        WHERE ts_code=%s AND trade_date<=%s AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (etf_code, as_of),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def exact_etf_candidates(cur, index_code: str, as_of: dt.date) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
        FROM passive_etf e
        WHERE e.index_ts_code=%s
          AND e.list_status='L'
          AND (e.etf_type IS NULL OR e.etf_type!='QDII')
          AND e.ts_code NOT LIKE '%%.OF'
          AND (e.list_date IS NULL OR e.list_date<=%s)
        ORDER BY e.list_date, e.ts_code
        """,
        (index_code, as_of),
    )
    rows = []
    for code, name, idx_code, idx_name, list_date in cur.fetchall():
        if latest_price_before(cur, code, as_of) is None:
            continue
        rows.append(
            {
                "etf_code": str(code),
                "etf_name": str(name or code),
                "tracking_index_code": str(idx_code or ""),
                "tracking_index_name": str(idx_name or ""),
                "list_date": list_date.isoformat() if list_date else None,
                "match_type": "exact",
                "correlation": 1.0,
            }
        )
    return rows


def correlation_proxy_candidates(
    cur,
    index_code: str,
    as_of: dt.date,
    lookback_days: int,
    min_corr: float,
) -> list[dict[str, Any]]:
    start = as_of - dt.timedelta(days=lookback_days)
    target_returns = returns_by_day(load_index_series(cur, index_code, start, as_of))
    if len(target_returns) < 20:
        return []
    cur.execute(
        """
        SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
        FROM passive_etf e
        WHERE e.list_status='L'
          AND (e.etf_type IS NULL OR e.etf_type!='QDII')
          AND e.ts_code NOT LIKE '%%.OF'
          AND e.index_ts_code IS NOT NULL
          AND (e.list_date IS NULL OR e.list_date<=%s)
        ORDER BY e.list_date, e.ts_code
        """,
        (as_of,),
    )
    scored = []
    for code, name, idx_code, idx_name, list_date in cur.fetchall():
        code = str(code)
        idx_code = str(idx_code or "")
        if not idx_code or latest_price_before(cur, code, as_of) is None:
            continue
        proxy_returns = returns_by_day(load_index_series(cur, idx_code, start, as_of))
        common = sorted(set(target_returns) & set(proxy_returns))
        value = corr([target_returns[day] for day in common], [proxy_returns[day] for day in common])
        if value is None or value < min_corr:
            continue
        scored.append(
            {
                "etf_code": code,
                "etf_name": str(name or code),
                "tracking_index_code": idx_code,
                "tracking_index_name": str(idx_name or ""),
                "list_date": list_date.isoformat() if list_date else None,
                "match_type": "correlation_proxy",
                "correlation": value,
            }
        )
    scored.sort(key=lambda row: (-row["correlation"], row["list_date"] or "", row["etf_code"]))
    return scored


def broad_proxy_candidates(cur, as_of: dt.date) -> list[dict[str, Any]]:
    rows = []
    placeholders = ",".join(["%s"] * len(EARLY_BROAD_PROXY_CODES))
    cur.execute(
        f"""
        SELECT ts_code, extname, index_ts_code, index_name, list_date
        FROM passive_etf
        WHERE ts_code IN ({placeholders})
        ORDER BY FIELD(ts_code, {placeholders})
        """,
        [*EARLY_BROAD_PROXY_CODES, *EARLY_BROAD_PROXY_CODES],
    )
    for code, name, idx_code, idx_name, list_date in cur.fetchall():
        if list_date and list_date > as_of:
            continue
        if latest_price_before(cur, code, as_of) is None:
            continue
        rows.append(
            {
                "etf_code": str(code),
                "etf_name": str(name or code),
                "tracking_index_code": str(idx_code or ""),
                "tracking_index_name": str(idx_name or ""),
                "list_date": list_date.isoformat() if list_date else None,
                "match_type": "early_broad_proxy",
                "correlation": None,
            }
        )
    return rows


def resolve_etf_proxy(
    cur,
    index_code: str,
    as_of: dt.date,
    lookback_days: int,
    min_corr: float,
) -> dict[str, Any] | None:
    exact = exact_etf_candidates(cur, index_code, as_of)
    if exact:
        return exact[0]
    proxies = correlation_proxy_candidates(cur, index_code, as_of, lookback_days, min_corr)
    if proxies:
        return proxies[0]
    broad = broad_proxy_candidates(cur, as_of)
    return broad[0] if broad else None


def suffix_like(suffix: str) -> str:
    if suffix == "all":
        return "%"
    return f"%.{suffix}"


def load_recommendations(cur, year: int, top: int, suffix: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT rank_position, ts_code, index_name, final_score, best_theme
        FROM csi_annual_recommendation
        WHERE apply_year=%s AND ts_code LIKE %s
        ORDER BY rank_position
        LIMIT %s
        """,
        (year, suffix_like(suffix), top),
    )
    return [
        {
            "rank": int(rank),
            "index_code": str(code),
            "index_name": str(name or code),
            "final_score": float(score) if score is not None else None,
            "best_theme": str(theme or ""),
        }
        for rank, code, name, score, theme in cur.fetchall()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Map CSI recommendations to ETF candidates with proxy fallback.")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--suffix", choices=["CSI", "SI", "all"], default="CSI")
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument("--lookback-days", type=int, default=504)
    parser.add_argument("--min-corr", type=float, default=0.70)
    parser.add_argument("--output")
    args = parser.parse_args()

    as_of = args.as_of or dt.date(args.year - 1, 12, 31)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            recs = load_recommendations(cur, args.year, args.top, args.suffix)
            rows = []
            for rec in recs:
                proxy = resolve_etf_proxy(cur, rec["index_code"], as_of, args.lookback_days, args.min_corr)
                rows.append({**rec, **(proxy or {})})
    finally:
        conn.close()

    fields = [
        "rank",
        "index_code",
        "index_name",
        "final_score",
        "best_theme",
        "etf_code",
        "etf_name",
        "tracking_index_code",
        "tracking_index_name",
        "match_type",
        "correlation",
        "list_date",
    ]
    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {out}")

    print(f"=== CSI/SI ETF proxy mapping year={args.year} suffix={args.suffix} as_of={as_of} top={args.top} ===")
    for row in rows:
        etf = row.get("etf_code") or "(none)"
        match = row.get("match_type") or "missing"
        corr_s = "" if row.get("correlation") is None else f" corr={row['correlation']:.3f}"
        print(f"{row['rank']:2d}. {row['index_code']:12s} -> {etf:10s} {match}{corr_s}")
    missing = sum(1 for row in rows if not row.get("etf_code"))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

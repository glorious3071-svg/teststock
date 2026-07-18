#!/usr/bin/env python3
"""Import selected FRED macro and credit-risk series into external_asset_daily.

FRED series are cached as `FRED:<series_id>` symbols with the observation value
stored in close/adj_close.  This keeps macro experiments on the same local table
as the external ETF/index proxies and avoids live network calls during backtests.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.import_external_asset_daily import ensure_schema

SOURCE = "fred_csv"
DEFAULT_SERIES = [
    "BAMLH0A0HYM2",  # ICE BofA US High Yield OAS
    "BAMLC0A0CM",   # ICE BofA US Corporate OAS
    "NFCI",         # Chicago Fed National Financial Conditions Index
    "ANFCI",        # Adjusted NFCI
    "DFF",          # Effective Federal Funds Rate
    "DGS10",        # 10Y Treasury constant maturity
    "DGS2",         # 2Y Treasury constant maturity
    "DTWEXBGS",     # Broad nominal dollar index
    "BAA10Y",       # Moody's Baa corporate yield relative to 10Y Treasury
    "AAA10Y",       # Moody's Aaa corporate yield relative to 10Y Treasury
    "STLFSI4",      # St. Louis Fed Financial Stress Index
]


def parse_date(raw: str) -> dt.date:
    return dt.date.fromisoformat(raw)


def parse_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if raw in {"", "."}:
        return None
    return float(raw)


def fetch_series(series_id: str, start: dt.date, end: dt.date, timeout: float) -> list[dict[str, Any]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    resp = requests.get(url, params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()}, timeout=timeout)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    if not reader.fieldnames or "observation_date" not in reader.fieldnames or series_id not in reader.fieldnames:
        raise RuntimeError(f"unexpected FRED CSV columns for {series_id}: {reader.fieldnames}")
    rows: list[dict[str, Any]] = []
    symbol = f"FRED:{series_id}"
    for row in reader:
        value = parse_float(row.get(series_id))
        if value is None:
            continue
        day = parse_date(row["observation_date"])
        rows.append(
            {
                "symbol": symbol,
                "trade_date": day,
                "open": None,
                "high": None,
                "low": None,
                "close": value,
                "adj_close": value,
                "volume": None,
                "source": SOURCE,
            }
        )
    return rows


def upsert_rows(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO external_asset_daily
          (symbol, trade_date, open, high, low, close, adj_close, volume, source)
        VALUES
          (%(symbol)s, %(trade_date)s, %(open)s, %(high)s, %(low)s, %(close)s, %(adj_close)s, %(volume)s, %(source)s)
        ON DUPLICATE KEY UPDATE
          open=VALUES(open),
          high=VALUES(high),
          low=VALUES(low),
          close=VALUES(close),
          adj_close=VALUES(adj_close),
          volume=VALUES(volume),
          source=VALUES(source)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def coverage(conn, series_ids: list[str]) -> list[tuple[str, int, dt.date | None, dt.date | None]]:
    symbols = [f"FRED:{series_id}" for series_id in series_ids]
    placeholders = ",".join(["%s"] * len(symbols))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM external_asset_daily
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
            ORDER BY symbol
            """,
            symbols,
        )
        return list(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description="Import selected FRED macro series into external_asset_daily.")
    parser.add_argument("--series", nargs="+", default=DEFAULT_SERIES)
    parser.add_argument("--start", default="2004-01-01")
    parser.add_argument("--end", default=dt.date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    conn = get_connection()
    try:
        ensure_schema(conn)
        total = 0
        for series_id in args.series:
            rows = fetch_series(series_id, start, end, args.timeout)
            print(f"{series_id}: fetched={len(rows)} range={rows[0]['trade_date'] if rows else None}..{rows[-1]['trade_date'] if rows else None}")
            if not args.dry_run:
                total += upsert_rows(conn, rows)
            time.sleep(args.sleep)
        if args.dry_run:
            print("dry_run=True; no rows written")
        else:
            print(f"upserted={total}")
            for row in coverage(conn, args.series):
                print(f"coverage symbol={row[0]} rows={row[1]} min={row[2]} max={row[3]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Import CBOE option-strategy and volatility indices into external_asset_daily."""

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

DEFAULT_SYMBOLS = ["PPUT", "PUT", "BXM", "BXMD", "CLLZ", "VXTH", "VPD", "VVIX", "VIX3M"]
SOURCE = "cboe_index_csv"


def parse_date(raw: str) -> dt.date:
    return dt.datetime.strptime(raw.strip(), "%m/%d/%Y").date()


def fetch_symbol(symbol: str, timeout: float) -> list[dict[str, Any]]:
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.startswith("<?xml"):
        raise RuntimeError(f"CBOE returned non-CSV response for {symbol}")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    fields = [field.upper() for field in (reader.fieldnames or [])]
    if "DATE" not in fields:
        raise RuntimeError(f"CBOE CSV missing DATE column for {symbol}: {reader.fieldnames}")
    value_field = symbol if symbol in (reader.fieldnames or []) else None
    for row in reader:
        raw_date = row.get("DATE") or row.get("Date") or row.get("date")
        if not raw_date:
            continue
        close = parse_float(row.get("CLOSE")) if "CLOSE" in fields else None
        if close is None and value_field:
            close = parse_float(row.get(value_field))
        if close is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "trade_date": parse_date(raw_date),
                "open": parse_float(row.get("OPEN")) if "OPEN" in fields else None,
                "high": parse_float(row.get("HIGH")) if "HIGH" in fields else None,
                "low": parse_float(row.get("LOW")) if "LOW" in fields else None,
                "close": close,
                "adj_close": close,
                "volume": None,
                "source": SOURCE,
            }
        )
    return rows


def parse_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    return float(raw)


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


def coverage(conn, symbols: list[str]) -> list[tuple[str, int, dt.date | None, dt.date | None]]:
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
    parser = argparse.ArgumentParser(description="Import CBOE option-strategy and volatility index daily prices.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    try:
        ensure_schema(conn)
        total = 0
        for symbol in args.symbols:
            rows = fetch_symbol(symbol, args.timeout)
            print(f"{symbol}: fetched={len(rows)} range={rows[0]['trade_date'] if rows else None}..{rows[-1]['trade_date'] if rows else None}")
            if not args.dry_run:
                total += upsert_rows(conn, rows)
            time.sleep(args.sleep)
        if args.dry_run:
            print("dry_run=True; no rows written")
        else:
            print(f"upserted={total}")
            for row in coverage(conn, args.symbols):
                print(f"coverage symbol={row[0]} rows={row[1]} min={row[2]} max={row[3]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

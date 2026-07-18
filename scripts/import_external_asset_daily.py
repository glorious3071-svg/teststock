#!/usr/bin/env python3
"""Import external ETF/index daily prices from Yahoo chart API.

The cached table is intended for portfolio hedge, defensive-asset, and
cross-asset allocation experiments. It avoids making backtests depend on a live
network call.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

SCHEMA_FILE = ROOT / "sql" / "external_asset_daily_schema.sql"
DEFAULT_SYMBOLS = [
    "SPY",
    "QQQ",
    "TLT",
    "IEF",
    "SHY",
    "GLD",
    "UUP",
    "DBC",
    "^VIX",
    "EFA",
    "EEM",
    "IWM",
    "XLK",
    "XLE",
    "XLU",
    "HYG",
    "LQD",
    "AGG",
    "TIP",
    "SH",
    "PSQ",
    "RWM",
    "GC=F",
    "SI=F",
    "CL=F",
    "NG=F",
    "ZB=F",
    "ZN=F",
    "ZF=F",
    "6E=F",
    "6J=F",
    "DX-Y.NYB",
]
SOURCE = "yahoo_chart"


def parse_date(raw: str) -> dt.date:
    return dt.date.fromisoformat(raw)


def unix_seconds(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())


def ensure_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for statement in [part.strip() for part in sql.split(";") if part.strip()]:
            cur.execute(statement)
    conn.commit()


def fetch_symbol(symbol: str, start: dt.date, end: dt.date, timeout: float) -> list[dict[str, Any]]:
    # Yahoo period2 is exclusive enough in practice; add one day to include the requested end date.
    period1 = unix_seconds(start)
    period2 = unix_seconds(end + dt.timedelta(days=1))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error for {symbol}: {error}")
    results = chart.get("result") or []
    if not results:
        return []
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows: list[dict[str, Any]] = []
    for idx, ts in enumerate(timestamps):
        trade_date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date()
        close = value_at(quote.get("close"), idx)
        adj_close = value_at(adj, idx)
        if close is None and adj_close is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": value_at(quote.get("open"), idx),
                "high": value_at(quote.get("high"), idx),
                "low": value_at(quote.get("low"), idx),
                "close": close,
                "adj_close": adj_close,
                "volume": int(value_at(quote.get("volume"), idx) or 0) if value_at(quote.get("volume"), idx) is not None else None,
                "source": SOURCE,
            }
        )
    return rows


def value_at(values: list[Any] | None, idx: int) -> float | None:
    if not values or idx >= len(values):
        return None
    value = values[idx]
    if value is None:
        return None
    return float(value)


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
    if not symbols:
        return []
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
    parser = argparse.ArgumentParser(description="Import external ETF/index daily prices from Yahoo chart API.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
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
        for symbol in args.symbols:
            rows = fetch_symbol(symbol, start, end, args.timeout)
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

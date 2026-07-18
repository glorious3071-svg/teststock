#!/usr/bin/env python3
"""Sync point-in-time ChinaBond government and AAA yield curves.

ChinaBond publishes the end-of-day curve at about 17:30 Beijing time.  Values
are stored in ``external_asset_daily`` as percentage yields so the backtest can
use the prior trading day's published curve before the next A-share session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.import_external_asset_daily import ensure_schema, upsert_rows


URL = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
SOURCE = "chinabond_yield"
CURVES = {
    "中债国债收益率曲线": "CN_GOV",
    "中债商业银行普通债收益率曲线(AAA)": "CN_BANK_AAA",
    "中债中短期票据收益率曲线(AAA)": "CN_MTN_AAA",
}
TENORS = {
    "1年": "1Y",
    "3年": "3Y",
    "5年": "5Y",
    "10年": "10Y",
}


def parse_date(raw: str) -> dt.date:
    return dt.date.fromisoformat(raw)


def yearly_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + dt.timedelta(days=364))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def parse_curve_table(html: str) -> list[dict[str, Any]]:
    tables = pd.read_html(StringIO(html.replace("&nbsp", "")), header=0)
    table = next(
        (
            item
            for item in tables
            if {"曲线名称", "日期", "1年", "10年"}.issubset(item.columns)
        ),
        None,
    )
    if table is None:
        raise ValueError("ChinaBond response did not contain a yield-curve table")
    rows: list[dict[str, Any]] = []
    for record in table.to_dict("records"):
        prefix = CURVES.get(str(record.get("曲线名称", "")).strip())
        day = pd.to_datetime(record.get("日期"), errors="coerce")
        if prefix is None or pd.isna(day):
            continue
        for source_tenor, target_tenor in TENORS.items():
            value = pd.to_numeric(record.get(source_tenor), errors="coerce")
            if pd.isna(value):
                continue
            rows.append(
                {
                    "symbol": f"{prefix}_{target_tenor}",
                    "trade_date": day.date(),
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": float(value),
                    "adj_close": None,
                    "volume": None,
                    "source": SOURCE,
                }
            )
    return rows


def fetch_chunk(
    start: dt.date,
    end: dt.date,
    timeout: float,
    retries: int,
) -> list[dict[str, Any]]:
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "gjqx": "0",
        "qxId": "ycqx",
        "locale": "cn_ZH",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                URL,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://yield.chinabond.com.cn/",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            return parse_curve_table(response.text)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ChinaBond fetch failed for {start}..{end}: {last_error}")


def coverage(conn) -> list[tuple[str, int, dt.date | None, dt.date | None]]:
    symbols = [
        f"{prefix}_{tenor}"
        for prefix in CURVES.values()
        for tenor in TENORS.values()
    ]
    placeholders = ",".join(["%s"] * len(symbols))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM external_asset_daily
            WHERE symbol IN ({placeholders}) AND source=%s
            GROUP BY symbol
            ORDER BY symbol
            """,
            (*symbols, SOURCE),
        )
        return list(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2006-03-01")
    parser.add_argument("--end", default=dt.date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    conn = get_connection()
    try:
        ensure_schema(conn)
        fetched = 0
        upserted = 0
        for chunk_start, chunk_end in yearly_chunks(start, end):
            rows = fetch_chunk(chunk_start, chunk_end, args.timeout, args.retries)
            fetched += len(rows)
            if not args.dry_run:
                upserted += upsert_rows(conn, rows)
            print(
                f"chunk={chunk_start}..{chunk_end} rows={len(rows)} "
                f"symbols={len(set(row['symbol'] for row in rows))}"
            )
            time.sleep(args.sleep)
        print(f"fetched={fetched} upserted={upserted} dry_run={args.dry_run}")
        if not args.dry_run:
            for symbol, count, minimum, maximum in coverage(conn):
                print(
                    f"coverage symbol={symbol} rows={count} "
                    f"min={minimum} max={maximum}"
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

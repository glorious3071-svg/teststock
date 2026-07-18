#!/usr/bin/env python3
"""Sync conservative point-in-time monthly ETF share/size snapshots.

The Tushare ``etf_share_size`` endpoint reports exchange ETF shares in 10,000
units and size in CNY 10,000.  The exchange updates the prior trading day's
data the following morning, so each row is assigned ``trade_date + 1`` as its
earliest availability date.  Feature builders must require
``available_date <= decision_date`` and ``list_date <= decision_date``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from calendar import monthrange
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from tushare_client import create_client


SCHEMA_FILE = ROOT / "sql/etf_share_size_snapshot_schema.sql"
SOURCE = "tushare_etf_share_size"
FIELDS = "trade_date,ts_code,etf_name,total_share,total_size,nav,close,exchange"
OVERSEAS_PATTERN = (
    "港股|沪港深|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|"
    "东南亚|沙特"
)


def period_ends(
    start: dt.date, end: dt.date, frequency: str = "monthly"
) -> list[dt.date]:
    output = []
    for year in range(start.year, end.year + 1):
        months = range(1, 13) if frequency == "monthly" else (3, 6, 9, 12)
        for month in months:
            day = dt.date(year, month, monthrange(year, month)[1])
            if start <= day <= end:
                output.append(day)
    return output


def quarter_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    return period_ends(start, end, "quarterly")


def ensure_schema(conn) -> None:
    statement = SCHEMA_FILE.read_text(encoding="utf-8").strip().rstrip(";")
    with conn.cursor() as cur:
        cur.execute(statement)
    conn.commit()


def eligible_codes(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ts_code
            FROM passive_etf
            WHERE list_date IS NOT NULL
              AND (etf_type IS NULL OR etf_type!='QDII')
              AND (is_enhanced IS NULL OR is_enhanced=0)
              AND (ts_code LIKE '%%.SH' OR ts_code LIKE '%%.SZ')
              AND COALESCE(cname, '') NOT REGEXP %s
              AND COALESCE(index_name, '') NOT REGEXP %s
            """,
            (OVERSEAS_PATTERN, OVERSEAS_PATTERN),
        )
        return {str(row[0]) for row in cur.fetchall() if row[0]}


def last_market_dates(
    conn, start: dt.date, end: dt.date, frequency: str = "monthly"
) -> list[dt.date]:
    dates = []
    with conn.cursor() as cur:
        for period_end in period_ends(start, end, frequency):
            cur.execute(
                "SELECT MAX(trade_date) FROM fund_daily WHERE trade_date <= %s",
                (period_end,),
            )
            row = cur.fetchone()
            if row and row[0] and row[0] >= start and row[0] not in dates:
                dates.append(row[0])
    return dates


def parse_date(raw: Any) -> dt.date:
    return dt.datetime.strptime(str(raw), "%Y%m%d").date()


def finite_positive(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_response_rows(
    payload: dict[str, Any],
    allowed_codes: set[str],
) -> list[tuple[Any, ...]]:
    data = payload.get("data") or {}
    fields = list(data.get("fields") or [])
    output = []
    for values in data.get("items") or []:
        record = dict(zip(fields, values))
        code = str(record.get("ts_code") or "")
        if code not in allowed_codes:
            continue
        try:
            trade_date = parse_date(record["trade_date"])
        except (KeyError, TypeError, ValueError):
            continue
        total_share = finite_positive(record.get("total_share"))
        if total_share is None:
            continue
        output.append(
            (
                code,
                trade_date,
                trade_date + dt.timedelta(days=1),
                total_share,
                finite_positive(record.get("total_size")),
                finite_positive(record.get("nav")),
                finite_positive(record.get("close")),
                str(record.get("exchange") or "") or None,
                SOURCE,
            )
        )
    return output


def fetch_trade_date(client, trade_date: dt.date, timeout: int, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            payload = client.query_http(
                "etf_share_size",
                {"trade_date": trade_date.strftime("%Y%m%d")},
                fields=FIELDS,
                timeout=timeout,
            )
            if int(payload.get("code", 0)) != 0:
                raise RuntimeError(str(payload.get("msg") or payload))
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2.0 * attempt)
    raise RuntimeError(f"etf_share_size failed for {trade_date}: {last_error}")


def upsert_rows(conn, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO etf_share_size_snapshot
              (ts_code, trade_date, available_date, total_share_wan,
               total_size_wan, nav, close_price, exchange, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              available_date=VALUES(available_date),
              total_share_wan=VALUES(total_share_wan),
              total_size_wan=VALUES(total_size_wan),
              nav=VALUES(nav), close_price=VALUES(close_price),
              exchange=VALUES(exchange), source=VALUES(source)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2009-01-01")
    parser.add_argument("--end", default=dt.date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--frequency", choices=("monthly", "quarterly"), default="monthly"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    conn = get_connection()
    client = create_client()
    try:
        ensure_schema(conn)
        allowed = eligible_codes(conn)
        dates = last_market_dates(conn, start, end, args.frequency)
        fetched = upserted = 0
        for trade_date in dates:
            payload = fetch_trade_date(client, trade_date, args.timeout, args.retries)
            rows = parse_response_rows(payload, allowed)
            fetched += len(rows)
            if not args.dry_run:
                upserted += upsert_rows(conn, rows)
            print(f"trade_date={trade_date} eligible_rows={len(rows)}", flush=True)
            time.sleep(args.sleep)
        print(
            f"eligible_codes={len(allowed)} dates={len(dates)} fetched={fetched} "
            f"upserted={upserted} dry_run={args.dry_run}",
            flush=True,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

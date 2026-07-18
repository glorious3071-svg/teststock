#!/usr/bin/env python3
"""Sync point-in-time quarterly share snapshots for domestic passive ETFs.

Tushare ``fund_nav`` exposes reported net assets, unit NAV, NAV date, and the
announcement date.  Derived fund units (net assets / unit NAV) are usable only
on or after the announcement date.  The source is research data only; the
eligible investment universe remains domestic, non-QDII, non-enhanced passive
index ETFs listed in Shanghai or Shenzhen.
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


SCHEMA_FILE = ROOT / "sql/fund_nav_share_snapshot_schema.sql"
SOURCE = "tushare_fund_nav"
FIELDS = "ts_code,ann_date,nav_date,unit_nav,net_asset"
OVERSEAS_PATTERN = (
    "港股|沪港深|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|"
    "东南亚|沙特"
)


def parse_date(raw: str) -> dt.date:
    return dt.datetime.strptime(str(raw), "%Y%m%d").date()


def quarter_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    output = []
    for year in range(start.year, end.year + 1):
        for month in (3, 6, 9, 12):
            day = dt.date(year, month, monthrange(year, month)[1])
            if start <= day <= end:
                output.append(day)
    return output


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


def parse_response_rows(
    payload: dict[str, Any],
    allowed_codes: set[str],
) -> list[tuple[str, dt.date, dt.date, float, float, float, str]]:
    data = payload.get("data") or {}
    fields = list(data.get("fields") or [])
    output = []
    for values in data.get("items") or []:
        record = dict(zip(fields, values))
        code = str(record.get("ts_code") or "")
        if code not in allowed_codes:
            continue
        try:
            ann_date = parse_date(record["ann_date"])
            nav_date = parse_date(record["nav_date"])
            unit_nav = float(record["unit_nav"])
            net_asset = float(record["net_asset"])
        except (KeyError, TypeError, ValueError):
            continue
        if unit_nav <= 0 or net_asset <= 0 or ann_date < nav_date:
            continue
        output.append(
            (
                code,
                nav_date,
                ann_date,
                unit_nav,
                net_asset,
                net_asset / unit_nav,
                SOURCE,
            )
        )
    return output


def fetch_nav_date(client, nav_date: dt.date, timeout: int, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            payload = client.query_http(
                "fund_nav",
                {"nav_date": nav_date.strftime("%Y%m%d"), "market": "E"},
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
    raise RuntimeError(f"fund_nav failed for {nav_date}: {last_error}")


def upsert_rows(conn, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fund_nav_share_snapshot
              (ts_code, nav_date, ann_date, unit_nav, net_asset,
               fund_share_units, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              unit_nav=VALUES(unit_nav),
              net_asset=VALUES(net_asset),
              fund_share_units=VALUES(fund_share_units),
              source=VALUES(source)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def coverage(conn) -> tuple[int, int, dt.date | None, dt.date | None, dt.date | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT ts_code), MIN(nav_date),
                   MAX(nav_date), MAX(ann_date)
            FROM fund_nav_share_snapshot
            WHERE source=%s
            """,
            (SOURCE,),
        )
        return cur.fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2004-12-31")
    parser.add_argument("--end", default=dt.date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    client = create_client()
    conn = get_connection()
    try:
        ensure_schema(conn)
        allowed = eligible_codes(conn)
        fetched = 0
        upserted = 0
        for nav_date in quarter_ends(start, end):
            payload = fetch_nav_date(client, nav_date, args.timeout, args.retries)
            rows = parse_response_rows(payload, allowed)
            fetched += len(rows)
            if not args.dry_run:
                upserted += upsert_rows(conn, rows)
            print(f"nav_date={nav_date} eligible_rows={len(rows)}")
            time.sleep(args.sleep)
        print(
            f"eligible_codes={len(allowed)} fetched={fetched} "
            f"upserted={upserted} dry_run={args.dry_run}"
        )
        if not args.dry_run:
            count, codes, minimum, maximum, latest_announcement = coverage(conn)
            print(
                f"coverage rows={count} codes={codes} min_nav={minimum} "
                f"max_nav={maximum} max_ann={latest_announcement}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

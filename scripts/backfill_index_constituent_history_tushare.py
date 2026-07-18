#!/usr/bin/env python3
"""Backfill point-in-time quarterly index weights from the Tushare proxy.

The existing AkShare importer is useful for current constituents but is not a
historical backfill.  This script deliberately requests the exact last local
trading day of each calendar quarter and stores only rows returned for that
date.  It never carries today's constituents backwards.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from tushare_client import create_client


DEFAULT_INDEX_MAP = {
    "000016.SH": "000016.SH",  # SSE 50
    "000010.SH": "000010.SH",  # SSE 180
    "399330.SZ": "399330.SZ",  # SZSE 100
    "399005.SZ": "399005.SZ",  # SME 100
    # Tushare's historical weight endpoint uses the SZSE alias for CSI 300.
    "000300.SH": "399300.SZ",
}


def quarter_end_trade_dates(conn, index_code: str, start: date, end: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date)
            FROM index_daily
            WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
            GROUP BY YEAR(trade_date), QUARTER(trade_date)
            ORDER BY MAX(trade_date)
            """,
            (index_code, start, end),
        )
        return [row[0] for row in cur.fetchall()]


def existing_dates(conn, index_code: str) -> set[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM index_constituent WHERE index_code=%s",
            (index_code,),
        )
        return {row[0] for row in cur.fetchall()}


def normalize_constituent(code: str) -> str | None:
    code = str(code).strip()
    if len(code) == 9 and code[6] == ".":
        return code
    if len(code) != 6 or not code.isdigit():
        return None
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def upsert_rows(conn, index_code: str, trade_date: date, frame) -> int:
    rows = []
    for record in frame.to_dict("records"):
        con_code = normalize_constituent(record.get("con_code", ""))
        if con_code is None:
            continue
        rows.append(
            (
                index_code,
                trade_date,
                con_code,
                float(record["weight"]) if record.get("weight") is not None else None,
                None,
            )
        )
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO index_constituent
                (index_code, trade_date, con_code, weight, con_name)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE weight=VALUES(weight)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def parse_mapping(values: list[str] | None) -> dict[str, str]:
    if not values:
        return dict(DEFAULT_INDEX_MAP)
    output = {}
    for value in values:
        local, separator, api = value.partition("=")
        output[local] = api if separator else local
    return output


def fetch_weight_snapshot(client, api_code: str, snapshot: date, retries: int):
    last_error: Exception | None = None
    for attempt in range(max(retries, 1)):
        try:
            return client.query(
                "index_weight",
                {
                    "index_code": api_code,
                    "trade_date": snapshot.strftime("%Y%m%d"),
                },
            )
        except Exception as exc:  # network/client failures are isolated per date
            last_error = exc
            if attempt + 1 < max(retries, 1):
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(
        f"index_weight failed for {api_code} {snapshot} after {retries} attempts: "
        f"{last_error}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        action="append",
        help="LOCAL or LOCAL=API; repeatable. Defaults to early broad proxy indices.",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2005, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--sleep", type=float, default=0.20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mapping = parse_mapping(args.index)
    client = create_client()
    conn = get_connection()
    fetched_dates = inserted_rows = empty_dates = skipped_dates = invalid_dates = 0
    failed_dates = 0
    try:
        for local_code, api_code in mapping.items():
            targets = quarter_end_trade_dates(conn, local_code, args.start, args.end)
            known = existing_dates(conn, local_code)
            for snapshot in targets:
                if snapshot in known and not args.refresh:
                    skipped_dates += 1
                    continue
                try:
                    frame = fetch_weight_snapshot(
                        client, api_code, snapshot, args.retries
                    )
                except RuntimeError as exc:
                    failed_dates += 1
                    print(f"FAIL {local_code} <- {api_code} {snapshot}: {exc}")
                    continue
                fetched_dates += 1
                if frame.empty:
                    empty_dates += 1
                    continue
                row_count = len(frame)
                weight_sum = float(frame["weight"].fillna(0.0).sum())
                if row_count < 10 or not 95.0 <= weight_sum <= 105.0:
                    invalid_dates += 1
                    print(
                        f"INVALID {local_code} <- {api_code} {snapshot}: "
                        f"rows={row_count} weight_sum={weight_sum:.4f}; skipped"
                    )
                    continue
                if not args.dry_run:
                    inserted_rows += upsert_rows(conn, local_code, snapshot, frame)
                print(
                    f"{local_code} <- {api_code} {snapshot}: rows={row_count} "
                    f"weight_sum={weight_sum:.4f}"
                )
                if args.sleep > 0:
                    time.sleep(args.sleep)
    finally:
        conn.close()
    print(
        "Done: "
        f"fetched_dates={fetched_dates} inserted_rows={inserted_rows} "
        f"empty_dates={empty_dates} invalid_dates={invalid_dates} "
        f"failed_dates={failed_dates} skipped_dates={skipped_dates} "
        f"dry_run={args.dry_run}"
    )
    return 0 if failed_dates == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

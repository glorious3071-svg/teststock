#!/usr/bin/env python3
"""Backfill tracked-index daily prices before the regular 2019 import window.

The existing passive-index sync starts at 2019-01-01. Several CSI theme indices
have earlier price history, which is useful for walk-forward validation. This
script only writes index_daily prices for indices already present in
theme_index_map and does not alter recommendations or scorecard outputs.
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql

warnings.filterwarnings(
    "ignore",
    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
    category=FutureWarning,
)

from scripts.import_passive_index_daily import mysql_config, parse_date, upsert_price
from tushare_client import create_client

DEFAULT_START_DATE = "20100101"
DEFAULT_END_DATE = "20181231"
CHUNK_YEARS = 4
REQUEST_SLEEP = 0.35


def ymd(value: str) -> str:
    text = value.replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise argparse.ArgumentTypeError("date must be YYYYMMDD or YYYY-MM-DD")
    return text


def load_csi_codes(conn: pymysql.Connection) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, MIN(index_name) AS index_name
            FROM theme_index_map
            WHERE ts_code LIKE '%.CSI'
            GROUP BY ts_code
            ORDER BY ts_code
            """
        )
        return [(str(code), str(name or "")) for code, name in cur.fetchall()]


def load_requested_codes(
    conn: pymysql.Connection, requested: list[str]
) -> list[tuple[str, str]]:
    placeholders = ",".join(["%s"] * len(requested))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT index_ts_code, MIN(index_name)
            FROM passive_etf
            WHERE index_ts_code IN ({placeholders})
            GROUP BY index_ts_code
            """,
            requested,
        )
        names = {str(code): str(name or "") for code, name in cur.fetchall()}
    return [(code, names.get(code, "")) for code in requested]


def fetch_chunk(client, ts_code: str, start: str, end: str) -> pd.DataFrame:
    raw = client.query_http(
        "index_daily",
        {"ts_code": ts_code, "start_date": start, "end_date": end},
        timeout=120,
    )
    items = (raw.get("data") or {}).get("items") or []
    fields = (raw.get("data") or {}).get("fields") or []
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def fetch_range(client, ts_code: str, start_date: str, end_date: str, sleep: float) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + CHUNK_YEARS - 1
        chunk_end = min(f"{chunk_end_year}1231", end_date)
        df = fetch_chunk(client, ts_code, chunk_start, chunk_end)
        if not df.empty:
            frames.append(df)
        if sleep > 0:
            time.sleep(sleep)
        if chunk_end >= end_date:
            break
        chunk_start = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if "trade_date" not in merged.columns:
        return pd.DataFrame()
    merged["trade_date"] = merged["trade_date"].map(parse_date)
    return merged.dropna(subset=["trade_date"]).drop_duplicates(subset=["ts_code", "trade_date"])


def existing_before_start(conn: pymysql.Connection, ts_code: str, start: str) -> int:
    cutoff = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM index_daily WHERE ts_code=%s AND trade_date < %s",
            (ts_code, cutoff),
        )
        return int(cur.fetchone()[0] or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", type=ymd, default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", type=ymd, default=DEFAULT_END_DATE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--index",
        action="append",
        help="Tracked index code to backfill; repeatable. Defaults to CSI theme indices.",
    )
    parser.add_argument("--sleep", type=float, default=REQUEST_SLEEP)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-existing-before-start",
        action="store_true",
        help="Skip a code if it already has any row before start-date.",
    )
    args = parser.parse_args()

    if args.start_date > args.end_date:
        raise SystemExit("start-date must be <= end-date")

    client = create_client()
    conn = pymysql.connect(**mysql_config())
    total_rows = 0
    empty = 0
    failed = 0
    skipped = 0
    try:
        codes = (
            load_requested_codes(conn, args.index)
            if args.index
            else load_csi_codes(conn)
        )
        if args.limit:
            codes = codes[: args.limit]
        print(
            f"CSI index history backfill: {len(codes)} codes "
            f"{args.start_date}-{args.end_date}"
        )
        for i, (ts_code, name) in enumerate(codes, 1):
            if args.skip_existing_before_start and existing_before_start(conn, ts_code, args.start_date) > 0:
                skipped += 1
                print(f"[{i}/{len(codes)}] {ts_code} {name}: skip existing pre-start")
                continue
            try:
                df = fetch_range(client, ts_code, args.start_date, args.end_date, args.sleep)
                if df.empty:
                    empty += 1
                    print(f"[{i}/{len(codes)}] {ts_code} {name}: empty")
                    continue
                n = len(df) if args.dry_run else upsert_price(conn, df)
                total_rows += n
                print(
                    f"[{i}/{len(codes)}] {ts_code} {name}: "
                    f"{n} rows {df['trade_date'].min()}~{df['trade_date'].max()}"
                )
            except Exception as exc:
                failed += 1
                print(f"[{i}/{len(codes)}] {ts_code} {name}: FAIL {exc}")
    finally:
        conn.close()

    print(
        f"Done: rows={total_rows} empty={empty} skipped={skipped} "
        f"failed={failed} dry_run={args.dry_run}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backfill opt_daily snapshots needed by the real option-package MTM diagnostic."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
import requests

from db.connection import mysql_config
from scripts.import_cn_etf_option_snapshot import apply_schema, fetch_daily, upsert_daily
from scripts.search_scorecard_csi_cn_option_package_real_history import HistoricalCnPackagePricer, load_package_shape
from scripts.search_scorecard_csi_cn_option_package_real_tipp import setup_cases
from tushare_client import create_client


def dates_between(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> list[dt.date]:
    left = bisect_right(rows, (start, float("inf")))
    right = bisect_right(rows, (end, float("inf")))
    return [day for day, _value in rows[left:right]]


def existing_pair_dates(conn, put_code: str, call_code: str, dates: list[dt.date]) -> set[dt.date]:
    if not dates:
        return set()
    out: set[dt.date] = set()
    with conn.cursor() as cur:
        for start in range(0, len(dates), 500):
            chunk = dates[start : start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT trade_date, COUNT(DISTINCT ts_code)
                FROM cn_option_daily
                WHERE ts_code IN (%s, %s)
                  AND trade_date IN ({placeholders})
                  AND close IS NOT NULL
                GROUP BY trade_date
                HAVING COUNT(DISTINCT ts_code)=2
                """,
                [put_code, call_code, *chunk],
            )
            out.update(row[0] for row in cur.fetchall())
    return out


def collect_required_dates(args, conn) -> dict[dt.date, dict[str, Any]]:
    raw_cases, _meta = setup_cases(args)
    package = load_package_shape()
    pricer = HistoricalCnPackagePricer(
        conn,
        package,
        args.underlying_mode,
        args.max_quote_stale_days,
        args.slippage_bps_per_leg,
        args.missing_package_policy,
    )
    required: dict[dt.date, dict[str, Any]] = {}
    selected_periods = 0
    missing_selection = 0
    for raw_case in raw_cases:
        for period in raw_case["periods"]:
            start_exec = period["start_exec"]
            end_exec = period["end_exec"]
            if args.start_date and end_exec < args.start_date:
                continue
            if args.end_date and start_exec > args.end_date:
                continue
            selected = pricer.select_quote_legs_for_period(start_exec, end_exec)
            if selected is None:
                missing_selection += 1
                continue
            underlying, quote_date, _spot, selection = selected
            selected_periods += 1
            long_put, short_call = selection
            trade_dates = dates_between(pricer.fund_series[underlying.fund_code], quote_date, end_exec)
            have_dates = existing_pair_dates(conn, long_put.ts_code, short_call.ts_code, trade_dates)
            for trade_date in trade_dates:
                if trade_date in have_dates:
                    continue
                row = required.setdefault(
                    trade_date,
                    {
                        "trade_date": trade_date,
                        "period_count": 0,
                        "contracts": set(),
                        "opt_codes": set(),
                    },
                )
                row["period_count"] += 1
                row["contracts"].update([long_put.ts_code, short_call.ts_code])
                row["opt_codes"].add(underlying.opt_code)
    print(
        f"selected_periods={selected_periods} missing_selection={missing_selection} "
        f"missing_trade_dates={len(required)}",
        flush=True,
    )
    return required


def summarize_required(required: dict[dt.date, dict[str, Any]]) -> None:
    if not required:
        print("No missing MTM trade dates.", flush=True)
        return
    dates = sorted(required)
    print(f"date_range={dates[0]} ~ {dates[-1]}", flush=True)
    for day in dates[:20]:
        row = required[day]
        print(
            f"{day} periods={row['period_count']} contracts={len(row['contracts'])} "
            f"opt_codes={','.join(sorted(row['opt_codes']))}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill opt_daily snapshots required by option-package MTM diagnostics.")
    parser.add_argument("--underlying-mode", default="switch_50_to_300", choices=["510300_only", "switch_50_to_300"])
    parser.add_argument("--missing-package-policy", default="zero", choices=["zero", "proxy"])
    parser.add_argument("--max-quote-stale-days", type=int, default=10)
    parser.add_argument("--slippage-bps-per-leg", type=float, default=5.0)
    parser.add_argument("--start-date", type=lambda raw: dt.date.fromisoformat(raw))
    parser.add_argument("--end-date", type=lambda raw: dt.date.fromisoformat(raw))
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing trade dates to fetch; 0 means all.")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--continue-on-error", action="store_true", help="Continue fetching later dates when one trade date fails.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        required = collect_required_dates(args, conn)
        summarize_required(required)
        if args.dry_run or not required:
            return 0
        dates = sorted(required)
        if args.limit > 0:
            dates = dates[: args.limit]
        client = create_client()
        total_rows = 0
        fetched_dates = 0
        empty_dates = 0
        failed_dates = 0
        for trade_date in dates:
            try:
                df = fetch_daily(client, trade_date.strftime("%Y%m%d"))
            except (requests.RequestException, TimeoutError, OSError) as exc:
                failed_dates += 1
                print(f"{trade_date} failed={type(exc).__name__}: {exc}", flush=True)
                if not args.continue_on_error:
                    raise
                if args.sleep > 0:
                    time.sleep(args.sleep)
                continue
            if df.empty:
                empty_dates += 1
                print(f"{trade_date} rows=0", flush=True)
            else:
                n = upsert_daily(conn, df)
                total_rows += n
                fetched_dates += 1
                print(f"{trade_date} rows={n}", flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)
        print(
            f"Done: requested_dates={len(dates)} fetched_dates={fetched_dates} "
            f"empty_dates={empty_dates} failed_dates={failed_dates} upserted_rows={total_rows}",
            flush=True,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
